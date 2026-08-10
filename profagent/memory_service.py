from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import API_VERSION, Style
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
    applied_signal: Literal["long_walk", "no_high_heels", "no_skirts"]
    # Private policy context. `exclude=True` prevents accidental model dumps;
    # Scene/Trace code must never serialize this grounded target.
    target_item_id: str | None = Field(default=None, exclude=True, repr=False)
    similarity_policy: Literal["demote_structured_similar_shoes_v1"] | None = Field(
        default=None, exclude=True, repr=False
    )
    shoe_signature: ShoeSimilaritySignature | None = Field(
        default=None, exclude=True, repr=False
    )


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

    def __init__(self, traces: TraceStore):
        self.traces = traces
        self._proposals: dict[str, MemoryProposal] = {}
        self._records: dict[str, MemoryRecord] = {}
        self._index: dict[tuple[str, str], set[str]] = {}
        self._proposal_context: dict[str, MemoryTargetContext] = {}
        self._record_context: dict[str, MemoryTargetContext] = {}
        self._lock = RLock()

    @classmethod
    def is_sensitive(cls, content: str) -> bool:
        return bool(cls._SENSITIVE.search(content))

    @classmethod
    def is_approved_content(cls, memory_type: str, content: str) -> bool:
        return content in cls._APPROVED_CONTENT.get(memory_type, frozenset())

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

    def propose(
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
        with self._lock:
            self._proposals[proposal_id] = proposal
            if private_context is not None and not blocked:
                self._proposal_context[proposal_id] = private_context
        return MemoryOperationResponse(proposal=proposal, trace_id=trace_id)

    def confirm(
        self, proposal_id: str, payload: MemoryConfirmInput
    ) -> MemoryOperationResponse:
        with self._lock:
            current = self._proposals.get(proposal_id)
            if current is None or current.user_id != payload.user_id:
                raise MemoryNotFound(proposal_id)
            proposal = current.model_copy(deep=True)

            if proposal.status == "committed" and proposal.record_id:
                record = self._records.get(proposal.record_id)
                if payload.decision == "confirm" and record is not None:
                    return MemoryOperationResponse(
                        proposal=proposal, record=record, trace_id=proposal.trace_id
                    )
                raise MemoryError("proposal is already committed")
            if proposal.status == "rejected":
                if payload.decision == "reject":
                    return MemoryOperationResponse(proposal=proposal, trace_id=proposal.trace_id)
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

    @staticmethod
    def _derive_signal(
        record: MemoryRecord,
        context: MemoryTargetContext | None,
    ) -> Literal["long_walk", "no_high_heels", "no_skirts"] | None:
        """Fail closed: only exact controlled type/content pairs become signals."""
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

    def active_signals(self, user_id: str) -> tuple[MemorySignal, ...]:
        """Return only committed, non-sensitive, user-confirmed safe signals."""
        with self._lock:
            self._purge_expired_locked(utc_now())
            records = [
                (
                    item.model_copy(deep=True),
                    self._record_context.get(item.memory_id),
                )
                for item in self._records.values()
                if item.user_id == user_id
                and item.status == "committed"
                and item.sensitivity == "non_sensitive"
                and item.source == "user_confirmed"
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

    def list(
        self, user_id: str, namespace: MemoryNamespace | None = None
    ) -> MemoryListResponse:
        with self._lock:
            self._purge_expired_locked(utc_now())
            proposals = [
                item.model_copy(deep=True)
                for item in self._proposals.values()
                if item.user_id == user_id
                and (namespace is None or item.namespace == namespace)
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
        with self._lock:
            proposal = self._proposals.get(memory_id)
            if proposal is not None and proposal.user_id == user_id:
                del self._proposals[memory_id]
                self._proposal_context.pop(memory_id, None)
                if proposal.record_id:
                    linked = self._records.pop(proposal.record_id, None)
                    self._record_context.pop(proposal.record_id, None)
                    if linked is not None:
                        self._index.get(
                            (linked.user_id, linked.namespace), set()
                        ).discard(linked.memory_id)
                deleted_kind: Literal["proposal", "record"] = "proposal"
                memory_type = proposal.type
                namespace = proposal.namespace
                session_id = proposal.styling_session_id
            else:
                record = self._records.get(memory_id)
                if record is None or record.user_id != user_id:
                    raise MemoryNotFound(memory_id)
                del self._records[memory_id]
                self._record_context.pop(memory_id, None)
                self._index.get((record.user_id, record.namespace), set()).discard(memory_id)
                # The proposal is metadata-only after commit, but remove it as
                # well so a deleted record leaves no stale committed handle.
                self._proposals.pop(record.source_proposal_id, None)
                self._proposal_context.pop(record.source_proposal_id, None)
                deleted_kind = "record"
                memory_type = record.type
                namespace = record.namespace
                session_id = None
        trace_id = self._trace(
            operation="delete",
            user_id=user_id,
            session_id=session_id,
            object_id=memory_id,
            memory_type=memory_type,
            namespace=namespace,
            outcome="deleted",
        )
        return MemoryDeleteResponse(
            deleted_id=memory_id,
            deleted_kind=deleted_kind,
            trace_id=trace_id,
        )
