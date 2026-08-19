from __future__ import annotations

import re
import uuid
import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import API_VERSION, Style
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
            self._proposals = {
                item.proposal_id: item
                for raw in proposals
                for item in [MemoryProposal.model_validate(raw)]
            }
            self._records = {
                item.memory_id: item
                for raw in records
                for item in [MemoryRecord.model_validate(raw)]
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
            self.repository.put_proposal(
                proposal.model_dump(mode="json"),
                (
                    self._context_dump(private_context)
                    if private_context is not None and not blocked
                    else None
                ),
            )
        return MemoryOperationResponse(proposal=proposal, trace_id=trace_id)

    def confirm(
        self, proposal_id: str, payload: MemoryConfirmInput
    ) -> MemoryOperationResponse:
        self._refresh()
        with self._lock:
            current = self._proposals.get(proposal_id)
            if current is None or current.user_id != payload.user_id:
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
            try:
                if record is None:
                    authoritative_proposal = self.repository.put_rejected_proposal(
                        proposal.model_dump(mode="json")
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
                    )
                    proposal = MemoryProposal.model_validate(authoritative_proposal)
                    record = MemoryRecord.model_validate(authoritative_record)
                    # The durable proposal/record identity comes from the CAS
                    # winner. The response trace remains local to this process so
                    # it is always queryable from this instance's TraceStore.
                    proposal = proposal.model_copy(update={"trace_id": trace_id})
            except MemoryRepositoryError as exc:
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
        controlled = {
            "偏爱直筒裤": ("fit:straight",),
            "偏爱简洁风格": ("style:simple",),
            "偏爱低调配色": ("style:classic", "style:simple"),
            "长时间站立时优先选择适合久走的鞋。": ("comfort:long_walk",),
            "长时间站立时优先选择适合久走的鞋": ("comfort:long_walk",),
        }
        return controlled.get(record.content, ())

    def retrieve_soft(
        self,
        user_id: str,
        query: str,
        namespaces: tuple[MemoryNamespace, ...] = ("shared", "stylist"),
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
            if self._derive_signal(record, context) is not None:
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
                for left, right in zip(query_vector, self._dense_vector(item.content))
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
        # Deterministic lightweight rerank restores strong lexical relevance
        # after rank-only fusion without introducing another model/provider.
        bm25_peak = max(bm25.values(), default=0.0)
        lexical_bonus = {
            memory_id: (
                0.03 * max(0.0, score) / bm25_peak if bm25_peak > 0 else 0.0
            )
            for memory_id, score in bm25.items()
        }
        reranked = {
            memory_id: score + lexical_bonus.get(memory_id, 0.0)
            for memory_id, score in fused.items()
        }
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
        by_id = {item.memory_id: item for item in candidates}
        terms: list[str] = []
        for memory_id in final_ids:
            for term in self._soft_term(by_id[memory_id]):
                if term not in terms:
                    terms.append(term)
        trace: dict[str, object] = {
            "component": "memory_soft_retrieval",
            "version": "weighted_rrf_k60_v1",
            "prefilter": "user_confirmed_non_sensitive_active_acl_v1",
            "namespaces": list(namespaces),
            "candidate_limit": self.RRF_CANDIDATE_LIMIT,
            "eligible_count": len(candidates),
            "fused_candidate_count": len(fused),
            "k": self.RRF_K,
            "weights": dict(self.RRF_WEIGHTS),
            "dense_backend": "deterministic_hashed_surrogate_v1",
            "rerank": "deterministic_lexical_bonus_v1",
            "top_k": self.RRF_TOP_K,
            "branches": ranked,
            "selected_memory_ids": final_ids,
            "hard_memory_in_rrf": False,
            "content_logged": False,
            "vectors_logged": False,
            "latency_ms": round((datetime.now().timestamp() - started) * 1000, 2),
        }
        return tuple(terms), trace

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
        self._refresh()
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
            tombstoned = self.repository.tombstone(
                user_id=user_id,
                object_id=memory_id,
                deleted_at=utc_now().isoformat(),
            )
            if tombstoned is None:
                raise MemoryNotFound(memory_id)
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
