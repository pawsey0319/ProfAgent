from __future__ import annotations

import uuid
from datetime import datetime
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .memory_service import (
    MemoryProposal,
    MemoryProposeInput,
    MemoryService,
    MemoryTargetContext,
    ShoeSimilaritySignature,
)
from .models import API_VERSION
from .service import RecommendationService
from .tracing import TraceStore, utc_now


FeedbackDecision = Literal["like", "dislike"]
FeedbackReason = Literal["long_walk_shoes"]
MemoryScope = Literal["session", "propose"]


class FeedbackError(ValueError):
    pass


class RecommendationFeedbackInput(BaseModel):
    """Frozen AC-07 input: no free text and no client-supplied item IDs."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    styling_session_id: str = Field(min_length=1, max_length=160)
    request_id: str = Field(min_length=1, max_length=160)
    outfit_id: str = Field(min_length=1, max_length=160)
    decision: FeedbackDecision
    reason_code: FeedbackReason | None = None
    memory_scope: MemoryScope

    @model_validator(mode="after")
    def decision_contract(self) -> "RecommendationFeedbackInput":
        if self.decision == "dislike" and self.reason_code is None:
            raise ValueError("dislike requires a whitelisted reason_code")
        if self.decision == "like":
            if self.reason_code is not None:
                raise ValueError("like does not accept a reason_code")
            if self.memory_scope != "session":
                raise ValueError("like feedback is session-scoped and creates no memory")
        return self


class RecommendationFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feedback_id: str
    user_id: str
    styling_session_id: str
    request_id: str
    outfit_id: str
    decision: FeedbackDecision
    reason_code: FeedbackReason | None
    memory_scope: MemoryScope
    created_at: datetime


class RecommendationFeedbackResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_version: str = API_VERSION
    feedback: RecommendationFeedback
    memory_proposal: MemoryProposal | None = None
    record: None = None
    trace_id: str


class FeedbackService:
    _PROPOSAL_CONTENT = {
        "long_walk_shoes": "偏好久走与长时间站立时选择舒适鞋履",
    }

    def __init__(
        self,
        recommendations: RecommendationService,
        memory: MemoryService,
        traces: TraceStore,
    ):
        self.recommendations = recommendations
        self.memory = memory
        self.traces = traces
        self._records: dict[str, RecommendationFeedback] = {}
        self._lock = RLock()

    def submit(
        self, payload: RecommendationFeedbackInput
    ) -> RecommendationFeedbackResponse:
        # This server snapshot is authoritative for all identities and confirms
        # that the outfit really belonged to the saved recommendation.
        _scene, outfit = self.recommendations.get_saved_outfit(
            payload.user_id,
            payload.styling_session_id,
            payload.request_id,
            payload.outfit_id,
        )
        feedback_id = f"feedback_{uuid.uuid4().hex}"
        trace_id = f"trace_{uuid.uuid4().hex[:12]}"
        memory_proposal: MemoryProposal | None = None
        grounded_target_stored = False
        if (
            payload.decision == "dislike"
            and payload.memory_scope == "propose"
            and payload.reason_code is not None
        ):
            grounded_shoes = [
                item
                for item_id in outfit.items
                if (
                    (item := self.recommendations.repository.get_garment(item_id))
                    is not None
                    and item.user_id == payload.user_id
                    and item.slot == "shoes"
                )
            ]
            if len(grounded_shoes) != 1:
                raise FeedbackError(
                    "saved outfit does not have one grounded shoe target"
                )
            target_shoe = grounded_shoes[0]
            operation = self.memory.propose(
                MemoryProposeInput(
                    user_id=payload.user_id,
                    styling_session_id=payload.styling_session_id,
                    namespace="stylist",
                    type="feedback",
                    content=self._PROPOSAL_CONTENT[payload.reason_code],
                ),
                private_context=MemoryTargetContext(
                    policy="exclude_item_when_long_walk",
                    target_item_id=target_shoe.garment_id,
                    similarity_policy="demote_structured_similar_shoes_v1",
                    shoe_signature=ShoeSimilaritySignature(
                        styles=tuple(sorted(target_shoe.styles)),
                        fit=target_shoe.fit,
                        material=target_shoe.material,
                        formal=target_shoe.formal,
                        warmth=target_shoe.warmth,
                    ),
                ),
            )
            memory_proposal = operation.proposal
            grounded_target_stored = True

        feedback = RecommendationFeedback(
            feedback_id=feedback_id,
            user_id=payload.user_id,
            styling_session_id=payload.styling_session_id,
            request_id=payload.request_id,
            outfit_id=payload.outfit_id,
            decision=payload.decision,
            reason_code=payload.reason_code,
            memory_scope=payload.memory_scope,
            created_at=utc_now(),
        )
        with self._lock:
            self._records[feedback_id] = feedback

        self.traces.start(
            trace_id=trace_id,
            request_id=f"recommend_feedback_{feedback_id}",
            styling_session_id=payload.styling_session_id,
            query_text="recommendation feedback",
            user_id=payload.user_id,
        )
        self.traces.update(
            trace_id,
            retrieval={
                "component": "recommendation_feedback",
                "feedback_id": feedback_id,
                "request_id": payload.request_id,
                "outfit_id": payload.outfit_id,
                "decision": payload.decision,
                "reason_code": payload.reason_code,
                "memory_scope": payload.memory_scope,
                "memory_proposal_id": (
                    memory_proposal.proposal_id if memory_proposal else None
                ),
                "content_logged": False,
                "item_ids_logged": False,
                "private_target_stored": grounded_target_stored,
                "private_target_logged": False,
                "rules": "saved_outfit_authority|closed_reason_codes|propose_confirm_commit",
            },
        )
        return RecommendationFeedbackResponse(
            feedback=feedback,
            memory_proposal=memory_proposal,
            trace_id=trace_id,
        )
