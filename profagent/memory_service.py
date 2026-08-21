from __future__ import annotations

import re
import uuid
import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import API_VERSION, SceneRequest, Style
from .memory_repository import (
    MemoryRepository,
    MemoryRepositoryError,
    create_memory_repository,
)
from .tracing import TraceStore, utc_now


MemoryNamespace = Literal["shared", "stylist"]
MemoryDecision = Literal["confirm", "edit", "reject"]
MemoryType = Literal[
    "constraint",
    "comfort_constraint",
    "preference",
    "profile_stable",
    "feedback",
    "session_emotion",
    "sensitive",
]
MemoryClass = Literal[
    "profile_current",
    "hard_constraint",
    "preference_event",
    "episodic_summary",
]
MemorySourceKind = Literal[
    "user_confirmed",
    "user_edited_confirmed",
    "feedback_confirmed",
    "legacy_migrated",
]


class MemoryError(ValueError):
    pass


class MemoryNotFound(KeyError):
    pass


class MemoryProposeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    styling_session_id: str | None = None
    namespace: MemoryNamespace
    type: MemoryType
    content: str = Field(min_length=1, max_length=1000)
    ttl_days: int | None = Field(default=None, ge=1, le=365)

    @field_validator("content")
    @classmethod
    def strip_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class MemoryConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    decision: MemoryDecision
    edited_content: str | None = Field(default=None, max_length=1000)


class MemoryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    user_id: str
    styling_session_id: str | None
    namespace: MemoryNamespace
    type: MemoryType
    content: str | None
    ttl_days: int | None
    status: Literal["proposed", "committed", "rejected"]
    sensitivity: Literal["non_sensitive", "sensitive"]
    commit_blocked: bool
    created_at: datetime
    updated_at: datetime
    trace_id: str
    record_id: str | None = None


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    user_id: str
    namespace: MemoryNamespace
    type: MemoryType
    content: str
    status: Literal["committed"] = "committed"
    sensitivity: Literal["non_sensitive"] = "non_sensitive"
    source: Literal["user_confirmed"] = "user_confirmed"
    ttl_days: int | None
    source_proposal_id: str
    created_at: datetime
    expires_at: datetime | None
    memory_class: MemoryClass = "preference_event"
    valid_from: datetime = Field(default_factory=utc_now)
    valid_to: datetime | None = None
    supersedes_memory_id: str | None = None
    source_kind: MemorySourceKind = "user_confirmed"
    provenance_version: Literal[
        "memory_provenance_v1", "memory_legacy_v0"
    ] = "memory_provenance_v1"
    consent_version: Literal[
        "explicit_confirm_v1", "legacy_confirm_v0"
    ] = "explicit_confirm_v1"
    confirmation_count: int = Field(default=1, ge=1)
    lifecycle_status: Literal[
        "active", "superseded", "deleted", "expired"
    ] = "active"

    @model_validator(mode="before")
    @classmethod
    def fill_server_lifecycle_defaults(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized.setdefault("valid_from", normalized.get("created_at"))
        normalized.setdefault("valid_to", normalized.get("expires_at"))
        return normalized

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "MemoryRecord":
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        if self.type in {"session_emotion", "sensitive"}:
            raise ValueError("working/sensitive memory cannot be a long-term record")
        historical_redaction = (
            self.lifecycle_status in {"deleted", "expired"}
            and self.content in {"[deleted]", "[expired]"}
        )
        if not historical_redaction:
            if not MemoryService.is_approved_content(self.type, self.content):
                raise ValueError("memory content is outside the controlled closure")
            expected_class = MemoryService._memory_class_for(self.type, self.content)
            if self.memory_class != expected_class:
                raise ValueError("memory_class does not match controlled type/content")
        legacy = self.source_kind == "legacy_migrated"
        if legacy != (
            self.provenance_version == "memory_legacy_v0"
            and self.consent_version == "legacy_confirm_v0"
        ):
            raise ValueError("legacy provenance fields must form an exact triplet")
        if not legacy and (
            self.provenance_version != "memory_provenance_v1"
            or self.consent_version != "explicit_confirm_v1"
        ):
            raise ValueError("confirmed provenance fields must form an exact triplet")
        if (
            self.source_kind == "feedback_confirmed"
            and self.type != "feedback"
        ) or (
            self.type == "feedback"
            and self.source_kind
            not in {
                "feedback_confirmed",
                "user_edited_confirmed",
                "legacy_migrated",
            }
        ):
            raise ValueError("source_kind does not match memory type")
        if self.supersedes_memory_id is None:
            if self.confirmation_count != 1:
                raise ValueError("root memory confirmation_count must be one")
        elif (
            self.supersedes_memory_id == self.memory_id
            or self.confirmation_count < 2
        ):
            raise ValueError("superseding memory requires a predecessor and count")
        if self.ttl_days is None:
            if self.expires_at is not None:
                raise ValueError("expires_at requires ttl_days")
        else:
            if self.expires_at is None or abs(
                (
                    self.expires_at
                    - (self.created_at + timedelta(days=self.ttl_days))
                ).total_seconds()
            ) >= 0.001:
                raise ValueError("ttl_days and expires_at must match")
        if (
            self.lifecycle_status == "active"
            and self.valid_to is not None
            and self.expires_at is not None
            and self.valid_to > self.expires_at
        ):
            raise ValueError("active valid_to cannot exceed expires_at")
        if self.lifecycle_status != "active" and self.valid_to is None:
            raise ValueError("historical memory requires valid_to")
        return self


class MemoryOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: str = API_VERSION
    proposal: MemoryProposal
    record: MemoryRecord | None = None
    trace_id: str


class MemoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: str = API_VERSION
    user_id: str
    namespace: MemoryNamespace | None
    proposals: list[MemoryProposal]
    records: list[MemoryRecord]
    retrieval_index_count: int


class MemoryDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: str = API_VERSION
    deleted_id: str
    deleted_kind: Literal["proposal", "record"]
    user_id: str
    namespace: MemoryNamespace
    truth_version: int = Field(ge=0)
    trace_id: str


class ShoeSimilaritySignature(BaseModel):
    """Private, structured shoe attributes used only by ranking policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    styles: tuple[Style, ...] = Field(min_length=1)
    fit: Literal["slim", "regular", "loose", "straight"]
    material: Literal[
        "cotton", "knit", "denim", "wool", "linen", "leather", "synthetic"
    ]
    formal: int = Field(ge=0, le=4)
    warmth: int = Field(ge=1, le=5)


class MemorySignal(BaseModel):
    """Closed-set, content-free signal exposed to recommendation policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str
    namespace: MemoryNamespace
    type: MemoryType
    applied_signal: str
    # Private policy context. `exclude=True` prevents accidental model dumps;
    # Scene/Trace code must never serialize this grounded target.
    target_item_id: str | None = Field(default=None, exclude=True, repr=False)
    similarity_policy: Literal["demote_structured_similar_shoes_v1"] | None = Field(
        default=None, exclude=True, repr=False
    )
    shoe_signature: ShoeSimilaritySignature | None = Field(
        default=None, exclude=True, repr=False
    )

    @field_validator("applied_signal")
    @classmethod
    def validate_applied_signal(cls, value: str) -> str:
        if value in {"long_walk", "no_high_heels", "no_skirts"}:
            return value
        prefix, separator, color = value.partition(":")
        if separator and prefix in {"prefer_color", "avoid_color"}:
            from .memory_candidates import COLOR_VALUES

            if color in COLOR_VALUES:
                return value
        raise ValueError("memory signal is outside the controlled closure")


@dataclass(frozen=True)
class MemoryTargetContext:
    """Server-derived context that is never part of a public memory model."""

    policy: Literal["exclude_item_when_long_walk"]
    target_item_id: str = field(repr=False)
    similarity_policy: Literal["demote_structured_similar_shoes_v1"]
    shoe_signature: ShoeSimilaritySignature = field(repr=False)


class MemoryService:
    # Long-term memory is a semantic-template API, not a free-text store. Every
    # persistable string is explicit here; unknown wording is represented only
    # by a content-free blocked proposal.
    _APPROVED_CONTENT: dict[str, frozenset[str]] = {
        "constraint": frozenset(
            {
                "不穿高跟鞋",
                "不穿裙装",
                "久走或长时间站立时需要舒适鞋履",
            }
        ),
        "comfort_constraint": frozenset(
            {"久走或长时间站立时需要舒适鞋履"}
        ),
        "preference": frozenset(
            {
                "偏爱直筒裤",
                "偏爱简洁风格",
                "偏爱低调配色",
                "长时间站立时优先选择适合久走的鞋。",
                "长时间站立时优先选择适合久走的鞋",
            }
        ),
        "profile_stable": frozenset(
            {"不穿高跟鞋", "不穿裙装", "偏爱直筒裤"}
        ),
        "feedback": frozenset(
            {"偏好久走与长时间站立时选择舒适鞋履"}
        ),
        "session_emotion": frozenset(),
        "sensitive": frozenset(),
    }
    _SENSITIVE = re.compile(
        r"(?:健康|疾病|病史|医疗|药物|过敏|身体|身高|体重|胸围|腰围|"
        r"身材|残疾|残障|怀孕|孕期|孕妇|年龄|生日|\d{1,3}岁|"
        r"糖尿病|焦虑症|抑郁症|焦虑|抑郁|心理|精神|诊断|哮喘|癌症|肿瘤|"
        r"心脏病|高血压|宗教|穆斯林|基督|佛教|"
        r"性别|性取向|同性恋|住址|家庭地址|门牌|经纬度|GPS|精确位置|"
        r"电话|手机号|手机号码|邮箱|电子邮件|身份证|护照|证件|微信号|"
        r"银行|工资|收入|信用卡|账户|负债|人脸|指纹|虹膜|声纹|生物识别|"
        r"health|medical|disease|medication|body|height|weight|age|birthday|"
        r"anxiety|depression|mental|diagnosis|diabetes|asthma|cancer|tumou?r|"
        r"pregnan(?:t|cy)|disab(?:led|ility)|phone|telephone|mobile|e-?mail|"
        r"passport|identity|id\s*card|national\s*id|religion|sexual|gender|"
        r"address|latitude|longitude|bank|salary|income|credit\s*card|"
        r"biometric|fingerprint|face\s*id|(?<!\d)1[3-9]\d{9}(?!\d)|"
        r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|"
        r"(?<!\d)\d{17}[\dx](?!\d))",
        flags=re.IGNORECASE,
    )

    RRF_K = 60
    RRF_CANDIDATE_LIMIT = 20
    RRF_WEIGHTS = {
        "bm25": 1.0,
        "dense": 1.0,
        "recency": 0.75,
        "importance": 1.25,
    }
    RRF_TOP_K = 5

    def __init__(
        self,
        traces: TraceStore,
        repository: MemoryRepository | None = None,
        *,
        database_url: str | None = None,
        root_dir: Path | None = None,
    ):
        self.traces = traces
        self.repository = repository or create_memory_repository(
            database_url, root_dir or Path.cwd()
        )
        self._proposals: dict[str, MemoryProposal] = {}
        self._records: dict[str, MemoryRecord] = {}
        self._index: dict[tuple[str, str], set[str]] = {}
        self._proposal_context: dict[str, MemoryTargetContext] = {}
        self._record_context: dict[str, MemoryTargetContext] = {}
        self._lock = RLock()
        self._refresh()

    @staticmethod
    def _context_dump(context: MemoryTargetContext) -> dict[str, object]:
        return {
            "policy": context.policy,
            "target_item_id": context.target_item_id,
            "similarity_policy": context.similarity_policy,
            "shoe_signature": context.shoe_signature.model_dump(mode="json"),
        }

    @staticmethod
    def _context_load(payload: dict[str, object]) -> MemoryTargetContext:
        return MemoryTargetContext(
            policy="exclude_item_when_long_walk",
            target_item_id=str(payload["target_item_id"]),
            similarity_policy="demote_structured_similar_shoes_v1",
            shoe_signature=ShoeSimilaritySignature.model_validate(
                payload["shoe_signature"]
            ),
        )

    @staticmethod
    def _semantic_key(record: MemoryRecord, context: MemoryTargetContext | None) -> str:
        from .memory_candidates import (
            canonical_candidate_for_content,
            canonical_semantic_key,
        )

        candidate = canonical_candidate_for_content(record.type, record.content)
        if candidate is not None:
            key = canonical_semantic_key(
                candidate.canonical_kind, candidate.canonical_value
            )
            if context is not None:
                key = f"{key}:{context.target_item_id}"
            return key
        normalized = re.sub(r"\s+", "", record.content).lower()
        controlled = {
            "不穿高跟鞋": "hard:no_high_heels",
            "不穿裙装": "hard:no_skirts",
            "久走或长时间站立时需要舒适鞋履": "hard:long_walk",
            "偏爱直筒裤": "soft:fit:straight",
            "偏爱简洁风格": "soft:style:simple",
            "偏爱低调配色": "soft:color:low_key",
            "长时间站立时优先选择适合久走的鞋。": "soft:comfort:long_walk",
            "长时间站立时优先选择适合久走的鞋": "soft:comfort:long_walk",
            "偏好久走与长时间站立时选择舒适鞋履": "feedback:long_walk",
        }
        key = controlled.get(normalized, f"controlled:{record.type}:{normalized}")
        if context is not None:
            key = f"{key}:{context.target_item_id}"
        return key

    @classmethod
    def _memory_class_for(
        cls, memory_type: MemoryType, content: str
    ) -> MemoryClass:
        from .memory_candidates import canonical_candidate_for_content

        candidate = canonical_candidate_for_content(memory_type, content)
        if candidate is not None:
            return candidate.memory_class
        normalized = re.sub(r"\s+", "", content)
        if memory_type in {"constraint", "comfort_constraint"} or normalized in {
            "不穿高跟鞋",
            "不穿裙装",
            "久走或长时间站立时需要舒适鞋履",
        }:
            return "hard_constraint"
        if memory_type == "profile_stable":
            return "profile_current"
        return "preference_event"

    @staticmethod
    def _source_kind_for(
        memory_type: MemoryType, decision: MemoryDecision
    ) -> MemorySourceKind:
        if decision == "edit":
            return "user_edited_confirmed"
        if memory_type == "feedback":
            return "feedback_confirmed"
        return "user_confirmed"

    def _refresh(self) -> None:
        with self._lock:
            now = utc_now()
            locally_expired = [
                record.memory_id
                for record in self._records.values()
                if record.expires_at is not None and record.expires_at <= now
            ]
            if locally_expired:
                self.repository.expire_ids(locally_expired, now.isoformat())
            proposals, records, proposal_contexts, record_contexts = (
                self.repository.load_state()
            )
            parsed_records = [MemoryRecord.model_validate(raw) for raw in records]
            durably_expired = [
                item.memory_id
                for item in parsed_records
                if item.lifecycle_status == "active"
                and (
                    (item.expires_at is not None and item.expires_at <= now)
                    or (item.valid_to is not None and item.valid_to <= now)
                )
            ]
            if durably_expired:
                self.repository.expire_ids(durably_expired, now.isoformat())
                proposals, records, proposal_contexts, record_contexts = (
                    self.repository.load_state()
                )
                parsed_records = [
                    MemoryRecord.model_validate(raw) for raw in records
                ]
            self._proposals = {
                item.proposal_id: item
                for raw in proposals
                for item in [MemoryProposal.model_validate(raw)]
            }
            self._records = {
                item.memory_id: item
                for item in parsed_records
                if item.lifecycle_status == "active"
                and item.valid_from <= now
                and (item.valid_to is None or item.valid_to > now)
                and (item.expires_at is None or item.expires_at > now)
            }
            self._proposal_context = {
                object_id: self._context_load(raw)
                for object_id, raw in proposal_contexts.items()
            }
            self._record_context = {
                object_id: self._context_load(raw)
                for object_id, raw in record_contexts.items()
            }
            self._index = {}
            for record in self._records.values():
                self._index.setdefault((record.user_id, record.namespace), set()).add(
                    record.memory_id
                )
            self._purge_expired_locked(now)

    @classmethod
    def is_sensitive(cls, content: str) -> bool:
        return bool(cls._SENSITIVE.search(content))

    @classmethod
    def is_approved_content(cls, memory_type: str, content: str) -> bool:
        if content in cls._APPROVED_CONTENT.get(memory_type, frozenset()):
            return True
        from .memory_candidates import canonical_candidate_for_content

        return canonical_candidate_for_content(memory_type, content) is not None

    def _trace(
        self,
        *,
        operation: str,
        user_id: str,
        session_id: str | None,
        object_id: str,
        memory_type: str,
        namespace: str,
        outcome: str,
    ) -> str:
        trace_id = f"trace_{uuid.uuid4().hex[:12]}"
        self.traces.start(
            trace_id=trace_id,
            request_id=f"memory_{operation}_{object_id}",
            styling_session_id=session_id or f"memory_{user_id}",
            query_text=f"memory {operation}",
            user_id=user_id,
        )
        self.traces.update(
            trace_id,
            retrieval={
                "component": "memory_store",
                "operation": operation,
                "object_id": object_id,
                "type": memory_type,
                "namespace": namespace,
                "outcome": outcome,
                "content_logged": False,
                "private_context_logged": False,
                "rules": (
                    "propose_confirm_commit|controlled_semantics_v1|"
                    "sensitive_nonpersistent_v1|namespace_isolation_v1"
                ),
            },
        )
        return trace_id

    def _prepare_proposal(
        self,
        payload: MemoryProposeInput,
        *,
        private_context: MemoryTargetContext | None = None,
    ) -> MemoryOperationResponse:
        proposal_id = f"mprop_{uuid.uuid4().hex}"
        approved = self.is_approved_content(payload.type, payload.content)
        forced_nonpersistent = payload.type in {"sensitive", "session_emotion"}
        sensitive = self.is_sensitive(payload.content) or forced_nonpersistent
        blocked = sensitive or not approved
        if private_context is not None and (
            payload.type != "feedback"
            or private_context.policy != "exclude_item_when_long_walk"
            or private_context.similarity_policy
            != "demote_structured_similar_shoes_v1"
            or not re.fullmatch(r"g\d{3}", private_context.target_item_id)
        ):
            raise MemoryError("private memory context failed server validation")
        # Sensitive content is deliberately not persisted, even in proposal
        # state.  The proposal ID/type remains available for an explicit reject.
        stored_content = None if blocked else payload.content
        trace_id = self._trace(
            operation="propose",
            user_id=payload.user_id,
            session_id=payload.styling_session_id,
            object_id=proposal_id,
            memory_type=payload.type,
            namespace=payload.namespace,
            outcome=(
                "blocked_sensitive"
                if sensitive
                else ("blocked_unapproved" if not approved else "proposed")
            ),
        )
        now = utc_now()
        proposal = MemoryProposal(
            proposal_id=proposal_id,
            user_id=payload.user_id,
            styling_session_id=payload.styling_session_id,
            namespace=payload.namespace,
            type=payload.type,
            content=stored_content,
            ttl_days=payload.ttl_days,
            status="proposed",
            sensitivity="sensitive" if blocked else "non_sensitive",
            commit_blocked=blocked,
            created_at=now,
            updated_at=now,
            trace_id=trace_id,
        )
        return MemoryOperationResponse(proposal=proposal, trace_id=trace_id)

    def prepare_candidate_proposal(
        self, payload: MemoryProposeInput
    ) -> MemoryOperationResponse:
        """Build a canonical proposal for an enclosing repository transaction."""

        return self._prepare_proposal(payload)

    def refresh_state(self) -> None:
        self._refresh()

    def propose(
        self,
        payload: MemoryProposeInput,
        *,
        private_context: MemoryTargetContext | None = None,
    ) -> MemoryOperationResponse:
        operation = self._prepare_proposal(
            payload, private_context=private_context
        )
        proposal = operation.proposal
        with self._lock:
            self._proposals[proposal.proposal_id] = proposal
            if private_context is not None and not proposal.commit_blocked:
                self._proposal_context[proposal.proposal_id] = private_context
            self.repository.put_proposal(
                proposal.model_dump(mode="json"),
                (
                    self._context_dump(private_context)
                    if private_context is not None and not proposal.commit_blocked
                    else None
                ),
            )
        return operation

    def confirm(
        self,
        proposal_id: str,
        payload: MemoryConfirmInput,
        *,
        candidate_authorized: bool = False,
        candidate_decision: dict[str, Any] | None = None,
    ) -> MemoryOperationResponse:
        self._refresh()
        with self._lock:
            current = self._proposals.get(proposal_id)
            if current is None or current.user_id != payload.user_id:
                raise MemoryNotFound(proposal_id)
            if (
                self.repository.candidate_proposal_managed(proposal_id)
                and not candidate_authorized
            ):
                raise MemoryNotFound(proposal_id)
            proposal = current.model_copy(deep=True)

            if proposal.status == "committed" and proposal.record_id:
                record = self._records.get(proposal.record_id)
                if payload.decision == "confirm" and record is not None:
                    trace_id = self._trace(
                        operation="confirm",
                        user_id=proposal.user_id,
                        session_id=proposal.styling_session_id,
                        object_id=proposal.proposal_id,
                        memory_type=proposal.type,
                        namespace=proposal.namespace,
                        outcome="already_committed",
                    )
                    return MemoryOperationResponse(
                        proposal=proposal.model_copy(update={"trace_id": trace_id}),
                        record=record,
                        trace_id=trace_id,
                    )
                raise MemoryError("proposal is already committed")
            if proposal.status == "rejected":
                if payload.decision == "reject":
                    trace_id = self._trace(
                        operation="confirm",
                        user_id=proposal.user_id,
                        session_id=proposal.styling_session_id,
                        object_id=proposal.proposal_id,
                        memory_type=proposal.type,
                        namespace=proposal.namespace,
                        outcome="already_rejected",
                    )
                    return MemoryOperationResponse(
                        proposal=proposal.model_copy(update={"trace_id": trace_id}),
                        trace_id=trace_id,
                    )
                raise MemoryError("proposal is already rejected")

            if payload.decision == "reject" or proposal.commit_blocked:
                proposal.status = "rejected"
                proposal.content = None
                self._proposal_context.pop(proposal_id, None)
                outcome = "rejected_sensitive" if proposal.commit_blocked else "rejected"
                record = None
            else:
                if payload.decision == "edit":
                    content = (payload.edited_content or "").strip()
                    if not content:
                        raise MemoryError("edited_content is required for edit")
                else:
                    if payload.edited_content is not None:
                        raise MemoryError("edited_content is only valid for edit")
                    content = proposal.content or ""
                if not content:
                    raise MemoryError("proposal has no committable content")
                if self.is_sensitive(content) or not self.is_approved_content(
                    proposal.type, content
                ):
                    proposal.status = "rejected"
                    proposal.sensitivity = "sensitive"
                    proposal.commit_blocked = True
                    proposal.content = None
                    self._proposal_context.pop(proposal_id, None)
                    outcome = "rejected_sensitive"
                    record = None
                else:
                    memory_id = f"mem_{uuid.uuid4().hex}"
                    now = utc_now()
                    record = MemoryRecord(
                        memory_id=memory_id,
                        user_id=proposal.user_id,
                        namespace=proposal.namespace,
                        type=proposal.type,
                        content=content,
                        ttl_days=proposal.ttl_days,
                        source_proposal_id=proposal.proposal_id,
                        created_at=now,
                        expires_at=(
                            now + timedelta(days=proposal.ttl_days)
                            if proposal.ttl_days
                            else None
                        ),
                        memory_class=self._memory_class_for(proposal.type, content),
                        valid_from=now,
                        valid_to=(
                            now + timedelta(days=proposal.ttl_days)
                            if proposal.ttl_days
                            else None
                        ),
                        source_kind=self._source_kind_for(
                            proposal.type, payload.decision
                        ),
                        provenance_version="memory_provenance_v1",
                        consent_version="explicit_confirm_v1",
                        confirmation_count=1,
                    )
                    proposal.status = "committed"
                    # The committed record is the only content-bearing object.
                    # Retaining a second copy in proposal state would make
                    # deletion/TTL guarantees easy to bypass.
                    proposal.content = None
                    proposal.record_id = memory_id
                    self._records[memory_id] = record
                    self._index.setdefault(
                        (record.user_id, record.namespace), set()
                    ).add(memory_id)
                    context = self._proposal_context.pop(proposal_id, None)
                    # Editing breaks the server-authored feedback meaning. The
                    # edited record may still exist as ordinary memory, but it
                    # cannot retain the hidden target policy.
                    if context is not None and payload.decision == "confirm":
                        self._record_context[memory_id] = context
                    outcome = "committed"
            proposal.updated_at = utc_now()
            trace_id = self._trace(
                operation="confirm",
                user_id=proposal.user_id,
                session_id=proposal.styling_session_id,
                object_id=proposal.proposal_id,
                memory_type=proposal.type,
                namespace=proposal.namespace,
                outcome=outcome,
            )
            proposal.trace_id = trace_id
            self._proposals[proposal_id] = proposal
            candidate_atomic = None
            if candidate_decision is not None:
                decision_status = (
                    "committed"
                    if candidate_decision["decision"] == "remember"
                    else (
                        "session_only"
                        if candidate_decision["decision"] == "session_only"
                        else "rejected"
                    )
                )
                candidate_atomic = {
                    **candidate_decision,
                    "status": candidate_decision["candidate_status"],
                    "response": {
                        "candidate_id": candidate_decision["candidate_id"],
                        "decision": candidate_decision["decision"],
                        "status": decision_status,
                        "record_id": record.memory_id if record is not None else None,
                        "trace_id": trace_id,
                    },
                }
            try:
                if record is None:
                    authoritative_proposal = self.repository.put_rejected_proposal(
                        proposal.model_dump(mode="json"),
                        candidate_decision=candidate_atomic,
                    )
                    proposal = MemoryProposal.model_validate(
                        authoritative_proposal
                    ).model_copy(update={"trace_id": trace_id})
                else:
                    record_context = self._record_context.get(record.memory_id)
                    authoritative_proposal, authoritative_record, _accepted = self.repository.commit(
                        proposal.model_dump(mode="json"),
                        record.model_dump(mode="json"),
                        semantic_key=self._semantic_key(record, record_context),
                        proposal_context=None,
                        record_context=(
                            self._context_dump(record_context)
                            if record_context is not None
                            else None
                        ),
                        candidate_decision=candidate_atomic,
                    )
                    proposal = MemoryProposal.model_validate(authoritative_proposal)
                    record = MemoryRecord.model_validate(authoritative_record)
                    # The durable proposal/record identity comes from the CAS
                    # winner. The response trace remains local to this process so
                    # it is always queryable from this instance's TraceStore.
                    proposal = proposal.model_copy(update={"trace_id": trace_id})
            except Exception as exc:
                self.traces.discard(trace_id)
                self._refresh()
                raise MemoryError("memory proposal decision conflict") from exc
        self._refresh()
        return MemoryOperationResponse(proposal=proposal, record=record, trace_id=trace_id)

    def _purge_expired_locked(self, now: datetime) -> None:
        expired = [
            item
            for item in self._records.values()
            if item.expires_at is not None and item.expires_at <= now
        ]
        for item in expired:
            self._records.pop(item.memory_id, None)
            self._index.get((item.user_id, item.namespace), set()).discard(
                item.memory_id
            )
            # Remove the linked proposal too, so neither content nor a stale
            # committed reference survives expiry.
            self._proposals.pop(item.source_proposal_id, None)
            self._proposal_context.pop(item.source_proposal_id, None)
            self._record_context.pop(item.memory_id, None)
        if expired:
            self.repository.expire_ids(
                [item.memory_id for item in expired], now.isoformat()
            )

    @staticmethod
    def _derive_signal(
        record: MemoryRecord,
        context: MemoryTargetContext | None,
    ) -> str | None:
        """Fail closed: only exact controlled type/content pairs become signals."""
        from .memory_candidates import canonical_candidate_for_content

        candidate = canonical_candidate_for_content(record.type, record.content)
        if candidate is not None:
            if candidate.canonical_kind == "color_preference":
                return f"prefer_color:{candidate.canonical_value}"
            if candidate.canonical_kind == "color_avoidance":
                return f"avoid_color:{candidate.canonical_value}"
        text = re.sub(r"\s+", "", record.content).lower()
        if record.type == "feedback":
            if (
                text == "偏好久走与长时间站立时选择舒适鞋履"
                and context is not None
                and context.policy == "exclude_item_when_long_walk"
            ):
                return "long_walk"
            return None
        if record.type not in {
            "constraint",
            "comfort_constraint",
            "profile_stable",
        }:
            return None
        if any(marker in text for marker in ("不是", "以前", "现在", "取消", "不再")):
            return None
        controlled: dict[
            str, Literal["long_walk", "no_high_heels", "no_skirts"]
        ] = {
            "久走或长时间站立时需要舒适鞋履": "long_walk",
            "不穿高跟鞋": "no_high_heels",
            "不穿裙装": "no_skirts",
        }
        return controlled.get(text)

    @staticmethod
    def _is_hard_signal(applied_signal: str | None) -> bool:
        return applied_signal in {
            "long_walk",
            "no_high_heels",
            "no_skirts",
        } or bool(applied_signal and applied_signal.startswith("avoid_color:"))

    def active_signals(self, user_id: str) -> tuple[MemorySignal, ...]:
        """Return only committed, non-sensitive, user-confirmed safe signals."""
        self._refresh()
        active_rows = self.repository.active_records(
            user_id, ("shared", "stylist"), utc_now().isoformat()
        )
        records = [
            (
                MemoryRecord.model_validate(raw),
                self._context_load(context) if context is not None else None,
            )
            for raw, context in active_rows
        ]
        signals: list[MemorySignal] = []
        seen: set[tuple[str, str, str | None]] = set()
        for record, context in sorted(records, key=lambda item: item[0].created_at):
            applied = self._derive_signal(record, context)
            if applied is None:
                continue
            target_item_id = context.target_item_id if context is not None else None
            key = (record.namespace, applied, target_item_id)
            if key in seen:
                continue
            seen.add(key)
            signals.append(
                MemorySignal(
                    memory_id=record.memory_id,
                    namespace=record.namespace,
                    type=record.type,
                    applied_signal=applied,
                    target_item_id=target_item_id,
                    similarity_policy=(
                        context.similarity_policy if context is not None else None
                    ),
                    shoe_signature=(
                        context.shoe_signature.model_copy(deep=True)
                        if context is not None
                        else None
                    ),
                )
            )
        return tuple(signals)

    @staticmethod
    def _tokens(value: str) -> list[str]:
        normalized = re.sub(r"\s+", "", value.lower())
        latin = re.findall(r"[a-z0-9_]+", normalized)
        cjk = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
        return latin + list(cjk) + [cjk[index : index + 2] for index in range(max(0, len(cjk) - 1))]

    @classmethod
    def _dense_vector(cls, value: str) -> tuple[float, ...]:
        buckets = [0.0] * 64
        for token in cls._tokens(value):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            buckets[digest[0] % len(buckets)] += 1.0 if digest[1] % 2 else -1.0
        norm = math.sqrt(sum(item * item for item in buckets)) or 1.0
        return tuple(item / norm for item in buckets)

    @staticmethod
    def _soft_term(record: MemoryRecord) -> tuple[str, ...]:
        from .memory_candidates import canonical_candidate_for_content

        candidate = canonical_candidate_for_content(record.type, record.content)
        if candidate is not None:
            return (candidate.soft_term,) if candidate.soft_term is not None else ()
        controlled = {
            "偏爱直筒裤": ("fit:straight",),
            "偏爱简洁风格": ("style:simple",),
            "偏爱低调配色": ("style:classic", "style:simple"),
            "长时间站立时优先选择适合久走的鞋。": ("comfort:long_walk",),
            "长时间站立时优先选择适合久走的鞋": ("comfort:long_walk",),
        }
        return controlled.get(record.content, ())

    @staticmethod
    def _applicability_tags(record: MemoryRecord) -> frozenset[str]:
        from .memory_candidates import canonical_candidate_for_content

        candidate = canonical_candidate_for_content(record.type, record.content)
        if candidate is not None:
            return frozenset(candidate.applicability_tags)
        controlled = {
            "偏爱直筒裤": frozenset({"goal:comfortable"}),
            "偏爱简洁风格": frozenset(
                {"goal:low_key", "goal:reliable", "occasion:interview", "occasion:meeting"}
            ),
            "偏爱低调配色": frozenset({"goal:low_key", "goal:reliable"}),
            "长时间站立时优先选择适合久走的鞋。": frozenset(
                {"comfort:long_walk"}
            ),
            "长时间站立时优先选择适合久走的鞋": frozenset(
                {"comfort:long_walk"}
            ),
        }
        return controlled.get(record.content, frozenset())

    @staticmethod
    def _scene_tags(scene: SceneRequest | None) -> frozenset[str]:
        if scene is None:
            return frozenset()
        tags = {f"occasion:{scene.occasion}"}
        tags.update(f"goal:{goal}" for goal in scene.goals)
        tags.update(
            f"comfort:{note}" for note in scene.constraints.comfort_notes
        )
        return frozenset(tags)

    @classmethod
    def _context_match(
        cls, record: MemoryRecord, scene: SceneRequest | None, query: str
    ) -> float:
        applicability = cls._applicability_tags(record)
        if not applicability:
            return 0.0
        if scene is not None:
            return 1.0 if applicability & cls._scene_tags(scene) else 0.0
        # Compatibility path for direct repository-level callers. Production
        # recommendation always supplies the authoritative Scene. This fallback
        # accepts only exact controlled lexical evidence and never model output.
        controlled_markers = {
            "偏爱直筒裤": ("直筒",),
            "偏爱简洁风格": ("简洁", "简单"),
            "偏爱低调配色": ("低调",),
            "长时间站立时优先选择适合久走的鞋。": ("久走", "长时间站立"),
            "长时间站立时优先选择适合久走的鞋": ("久走", "长时间站立"),
        }
        return (
            1.0
            if any(marker in query for marker in controlled_markers.get(record.content, ()))
            else 0.0
        )

    @classmethod
    def _specificity(cls, record: MemoryRecord) -> float:
        tags = cls._applicability_tags(record)
        if any(tag.startswith("comfort:") for tag in tags):
            return 1.0
        if any(tag.startswith("occasion:") for tag in tags):
            return 0.75
        if tags:
            return 0.5
        return 0.25 if record.memory_class == "profile_current" else 0.0

    @staticmethod
    def _confirmation_strength(record: MemoryRecord) -> float:
        return min(1.0, 0.70 + 0.10 * (record.confirmation_count - 1))

    def retrieve_soft(
        self,
        user_id: str,
        query: str,
        namespaces: tuple[MemoryNamespace, ...] = ("shared", "stylist"),
        *,
        scene: SceneRequest | None = None,
    ) -> tuple[tuple[str, ...], dict[str, object]]:
        """Retrieve controlled soft preferences after SQL ACL/status filtering.

        The returned trace is deliberately ID/rank/latency-only: neither memory
        content nor hashed dense vectors are serializable into Trace.
        """
        started = datetime.now().timestamp()
        self._refresh()
        active_rows = self.repository.active_records(
            user_id, tuple(namespaces), utc_now().isoformat()
        )
        candidates: list[MemoryRecord] = []
        for raw, context_raw in active_rows:
            record = MemoryRecord.model_validate(raw)
            context = (
                self._context_load(context_raw) if context_raw is not None else None
            )
            # Hard memory is consumed only by active_signals(), never by RRF.
            applied_signal = self._derive_signal(record, context)
            if record.memory_class == "hard_constraint" or self._is_hard_signal(
                applied_signal
            ):
                continue
            if self._soft_term(record):
                candidates.append(record)
        candidates = sorted(candidates, key=lambda item: item.memory_id)

        query_tokens = self._tokens(query)
        query_counts = {token: query_tokens.count(token) for token in set(query_tokens)}
        docs = {item.memory_id: self._tokens(item.content) for item in candidates}
        average_length = (
            sum(len(tokens) for tokens in docs.values()) / len(docs) if docs else 1.0
        )
        bm25: dict[str, float] = {}
        for item in candidates:
            tokens = docs[item.memory_id]
            score = 0.0
            for token, query_frequency in query_counts.items():
                document_frequency = sum(token in doc for doc in docs.values())
                inverse_frequency = math.log(
                    1.0 + (len(docs) - document_frequency + 0.5) / (document_frequency + 0.5)
                ) if docs else 0.0
                term_frequency = tokens.count(token)
                denominator = term_frequency + 1.2 * (
                    0.25 + 0.75 * len(tokens) / max(average_length, 1.0)
                )
                if denominator:
                    score += query_frequency * inverse_frequency * (
                        term_frequency * 2.2 / denominator
                    )
            bm25[item.memory_id] = score

        query_vector = self._dense_vector(query)
        dense = {
            item.memory_id: sum(
                left * right
                for left, right in zip(
                    query_vector, self._dense_vector(item.content)
                )
            )
            for item in candidates
        }
        now = utc_now()
        recency = {
            item.memory_id: 1.0
            / (1.0 + max(0.0, (now - item.created_at).total_seconds()) / 2_592_000.0)
            for item in candidates
        }
        importance_values = {
            "preference": 1.0,
            "feedback": 0.9,
            "profile_stable": 0.85,
            "comfort_constraint": 0.8,
            "constraint": 0.8,
        }
        importance = {
            item.memory_id: importance_values.get(item.type, 0.5)
            for item in candidates
        }
        branches = {
            "bm25": bm25,
            "dense": dense,
            "recency": recency,
            "importance": importance,
        }
        ranked: dict[str, list[str]] = {
            branch: [
                memory_id
                for memory_id, _score in sorted(
                    scores.items(), key=lambda pair: (-pair[1], pair[0])
                )[: self.RRF_CANDIDATE_LIMIT]
            ]
            for branch, scores in branches.items()
        }
        fused = {
            memory_id: 0.0
            for ids in ranked.values()
            for memory_id in ids
        }
        for branch, ids in ranked.items():
            weight = self.RRF_WEIGHTS[branch]
            for rank, memory_id in enumerate(ids, 1):
                fused[memory_id] += weight / (self.RRF_K + rank)
        # Exact S15 structured rerank. Every feature is derived here from SQL
        # truth and the authoritative parsed Scene; no caller/provider score is
        # accepted.
        rrf_peak = max(fused.values(), default=0.0)
        bm25_peak = max(bm25.values(), default=0.0)
        by_id = {item.memory_id: item for item in candidates}
        features: dict[str, dict[str, float]] = {}
        reranked: dict[str, float] = {}
        for memory_id, rrf_score in fused.items():
            record = by_id[memory_id]
            rrf_norm = rrf_score / rrf_peak if rrf_peak > 0 else 0.0
            lexical_norm = (
                max(0.0, bm25.get(memory_id, 0.0)) / bm25_peak
                if bm25_peak > 0
                else 0.0
            )
            context_match = self._context_match(record, scene, query)
            specificity = self._specificity(record)
            confirmation_strength = self._confirmation_strength(record)
            final = (
                0.60 * rrf_norm
                + 0.15 * context_match
                + 0.10 * specificity
                + 0.10 * confirmation_strength
                + 0.05 * lexical_norm
            )
            features[memory_id] = {
                "rrf_norm": round(rrf_norm, 6),
                "context_match": round(context_match, 6),
                "specificity": round(specificity, 6),
                "confirmation_strength": round(confirmation_strength, 6),
                "lexical_norm": round(lexical_norm, 6),
                "final": round(final, 6),
            }
            reranked[memory_id] = final
        final_ids = [
            memory_id
            for memory_id, _score in sorted(
                reranked.items(),
                key=lambda pair: (
                    -pair[1],
                    -bm25.get(pair[0], 0.0),
                    -importance.get(pair[0], 0.0),
                    pair[0],
                ),
            )[: self.RRF_TOP_K]
        ]
        terms: list[str] = []
        for memory_id in final_ids:
            for term in self._soft_term(by_id[memory_id]):
                if term not in terms:
                    terms.append(term)
        trace: dict[str, object] = {
            "component": "memory_soft_retrieval",
            "version": "weighted_rrf_k60_structured_rerank_v1",
            "prefilter": "user_confirmed_non_sensitive_active_acl_v1",
            "namespaces": list(namespaces),
            "candidate_limit": self.RRF_CANDIDATE_LIMIT,
            "eligible_count": len(candidates),
            "fused_candidate_count": len(fused),
            "k": self.RRF_K,
            "weights": dict(self.RRF_WEIGHTS),
            "dense_backend": "deterministic_hashed_surrogate_v1",
            "rerank": "structured_rerank_v1",
            "rerank_formula": {
                "rrf_norm": 0.60,
                "context_match": 0.15,
                "specificity": 0.10,
                "confirmation_strength": 0.10,
                "lexical_norm": 0.05,
            },
            "feature_source": "server_sql_truth_and_authoritative_scene_v1",
            "context_source": (
                "authoritative_scene_v1"
                if scene is not None
                else "controlled_query_compat_v1"
            ),
            "controlled_scores": features,
            "top_k": self.RRF_TOP_K,
            "branches": ranked,
            "selected_memory_ids": final_ids,
            "hard_memory_in_rrf": False,
            "content_logged": False,
            "vectors_logged": False,
            "latency_ms": round((datetime.now().timestamp() - started) * 1000, 2),
        }
        return tuple(terms), trace

    def rebuild_soft_index(
        self,
        user_id: str,
        namespaces: tuple[MemoryNamespace, ...] = ("shared", "stylist"),
    ) -> tuple[str, ...]:
        """Deterministically rebuild the disposable ID-only projection.

        SQL active truth is the sole source. Hard memories are deliberately
        excluded; the projection is never trusted to restore eligibility.
        """
        now = utc_now()
        eligible = self._eligible_soft_ids(user_id, namespaces, now)
        rebuild = getattr(self.repository, "rebuild_soft_index", None)
        if rebuild is None:
            return eligible
        return rebuild(
            user_id,
            tuple(namespaces),
            eligible,
            index_version="memory_soft_projection_v1",
            rebuilt_at=now.isoformat(),
        )

    def _eligible_soft_ids(
        self,
        user_id: str,
        namespaces: tuple[MemoryNamespace, ...],
        now: datetime,
    ) -> tuple[str, ...]:
        rows = self.repository.active_records(
            user_id, tuple(namespaces), now.isoformat()
        )
        eligible: list[str] = []
        for raw, context_raw in rows:
            record = MemoryRecord.model_validate(raw)
            context = (
                self._context_load(context_raw) if context_raw is not None else None
            )
            applied_signal = self._derive_signal(record, context)
            if record.memory_class == "hard_constraint" or self._is_hard_signal(
                applied_signal
            ):
                continue
            if self._soft_term(record):
                eligible.append(record.memory_id)
        return tuple(sorted(eligible))

    def consume_soft_index_outbox(
        self,
        user_id: str,
        namespaces: tuple[MemoryNamespace, ...] = ("shared", "stylist"),
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Idempotently apply lifecycle outbox facts to the derived projection."""
        self._refresh()
        now = utc_now()
        eligible = self._eligible_soft_ids(user_id, namespaces, now)
        consume = getattr(self.repository, "consume_soft_index_outbox", None)
        if consume is None:
            return eligible, ()
        return consume(
            user_id,
            tuple(namespaces),
            eligible,
            consumer_name="memory_soft_projection_v1",
            index_version="memory_soft_projection_v1",
            consumed_at=now.isoformat(),
        )

    def close(self) -> None:
        self.repository.close()

    def list(
        self, user_id: str, namespace: MemoryNamespace | None = None
    ) -> MemoryListResponse:
        self._refresh()
        with self._lock:
            self._purge_expired_locked(utc_now())
            proposals = [
                item.model_copy(deep=True)
                for item in self._proposals.values()
                if item.user_id == user_id
                and item.status == "proposed"
                and (namespace is None or item.namespace == namespace)
                and not self.repository.candidate_proposal_managed(
                    item.proposal_id
                )
            ]
            records = [
                item.model_copy(deep=True)
                for item in self._records.values()
                if item.user_id == user_id
                and (namespace is None or item.namespace == namespace)
            ]
        return MemoryListResponse(
            user_id=user_id,
            namespace=namespace,
            proposals=sorted(proposals, key=lambda item: item.created_at),
            records=sorted(records, key=lambda item: item.created_at),
            retrieval_index_count=len(records),
        )

    def delete(self, user_id: str, memory_id: str) -> MemoryDeleteResponse:
        self._refresh()
        with self._lock:
            proposal = self._proposals.get(memory_id)
            if proposal is not None and proposal.user_id == user_id:
                if proposal.record_id:
                    deleted_kind: Literal["proposal", "record"] = "record"
                    authoritative_deleted_id = proposal.record_id
                else:
                    deleted_kind = "proposal"
                    authoritative_deleted_id = proposal.proposal_id
                memory_type = proposal.type
                namespace = proposal.namespace
                session_id = proposal.styling_session_id
            else:
                record = self._records.get(memory_id)
                if record is None or record.user_id != user_id:
                    raise MemoryNotFound(memory_id)
                deleted_kind = "record"
                authoritative_deleted_id = record.memory_id
                memory_type = record.type
                namespace = record.namespace
                session_id = None
            try:
                tombstoned = self.repository.tombstone(
                    user_id=user_id,
                    object_id=memory_id,
                    deleted_at=utc_now().isoformat(),
                )
            except Exception as exc:
                self._refresh()
                raise MemoryError("memory deletion transaction failed") from exc
            if tombstoned is None:
                raise MemoryNotFound(memory_id)
            if (
                tombstoned.deleted_kind != deleted_kind
                or tombstoned.user_id != user_id
                or tombstoned.namespace != namespace
            ):
                self._refresh()
                raise MemoryError("memory deletion receipt mismatch")
        self._refresh()
        trace_id = self._trace(
            operation="delete",
            user_id=user_id,
            session_id=session_id,
            object_id=authoritative_deleted_id,
            memory_type=memory_type,
            namespace=namespace,
            outcome="deleted",
        )
        return MemoryDeleteResponse(
            deleted_id=authoritative_deleted_id,
            deleted_kind=deleted_kind,
            user_id=user_id,
            namespace=namespace,
            truth_version=tombstoned.truth_version,
            trace_id=trace_id,
        )
