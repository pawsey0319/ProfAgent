from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from threading import RLock
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .memory_service import (
    MemoryClass,
    MemoryConfirmInput,
    MemoryProposeInput,
    MemoryService,
    MemoryType,
)
from .memory_repository import MemoryRepositoryError


CanonicalMemoryKind = Literal[
    "color_preference",
    "color_avoidance",
    "style_preference",
    "garment_preference",
    "garment_avoidance",
    "comfort_preference",
    "occasion_preference",
]


class MemoryCandidateError(ValueError):
    pass


class MemoryCandidateNotFound(KeyError):
    pass


class MemoryCandidateConflict(MemoryCandidateError):
    pass


class MemoryCandidateDecisionError(MemoryCandidateError):
    pass


class CanonicalMemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_kind: CanonicalMemoryKind
    canonical_value: str
    memory_type: MemoryType
    memory_class: MemoryClass
    content: str
    applicability_tags: tuple[str, ...] = ()
    confirmation_copy: str
    soft_term: str | None = None


class MemoryCandidatePrefilterResult(BaseModel):
    """Content-free gate result safe for Trace/UI serialization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    code: Literal["MEMORY_TEXT_READY", "SENSITIVE_MEMORY_DEFAULT_NO_WRITE"]


class ExtractedMemoryCandidate(CanonicalMemoryCandidate):
    """Server-canonical candidate plus the bounded provider confidence label."""

    confidence_band: Literal["high", "medium", "low"]


class MemoryCandidateProviderEvidence(BaseModel):
    """Closed, content-free proof for one verified CPA extraction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempted: Literal[True]
    status: Literal["ok"]
    requested_model: Literal["grok4.6"]
    transport_model: Literal["grok-4.6-high"]
    resolved_model: Literal["grok-4.6-high", "grok-4.6-build"]
    model_verified: Literal[True]
    latency_ms: float = Field(ge=0, allow_inf_nan=False)


class MemoryCandidateExtractionResult(BaseModel):
    """Transient extraction result; it contains no input or provider prose."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    code: Literal["MEMORY_TEXT_READY", "SENSITIVE_MEMORY_DEFAULT_NO_WRITE"]
    candidates: tuple[ExtractedMemoryCandidate, ...] = ()
    provider_evidence: dict[str, object] | None = None


class MemoryCandidateExtractInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    styling_session_id: str | None = None
    namespace: Literal["shared", "stylist"]
    text: str = Field(min_length=1, max_length=1000)
    request_id: str


class MemoryCandidateDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    styling_session_id: str | None = None
    decision: Literal["remember", "session_only", "reject", "rephrase"]
    idempotency_key: str


class MemoryCandidateCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    confirmation_copy: str
    confidence_band: Literal["high", "medium", "low"]
    conflict_copy: str | None = None
    allowed_actions: tuple[
        Literal["remember", "session_only", "reject", "rephrase"], ...
    ]


class MemoryCandidateExtractResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    status: Literal["ready", "needs_rephrase", "blocked"]
    candidates: tuple[MemoryCandidateCard, ...]
    trace_id: str


class MemoryCandidateDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    decision: Literal["remember", "session_only", "reject", "rephrase"]
    status: Literal["committed", "session_only", "rejected"]
    record_id: str | None
    trace_id: str


class MemoryCandidatePrefilter:
    """Reject sensitive free text before any provider or repository call."""

    _INTERNAL_IDENTIFIER = re.compile(
        r"(?i)(?<![a-z0-9])(?:u|g)\d+(?![a-z0-9])"
    )

    @staticmethod
    def prefilter(text: str) -> MemoryCandidatePrefilterResult:
        if MemoryService.is_sensitive(text) or (
            MemoryCandidatePrefilter._INTERNAL_IDENTIFIER.search(text)
            is not None
        ):
            return MemoryCandidatePrefilterResult(
                allowed=False,
                code="SENSITIVE_MEMORY_DEFAULT_NO_WRITE",
            )
        return MemoryCandidatePrefilterResult(
            allowed=True,
            code="MEMORY_TEXT_READY",
        )


class MemoryCandidateProvider(Protocol):
    async def extract_memory_candidates(
        self,
        text: str,
        *,
        allowed_kinds: tuple[str, ...],
        allowed_values: dict[str, tuple[str, ...]],
    ) -> tuple[tuple[dict[str, object], ...], dict[str, object]]: ...


@dataclass
class _ServerMemoryCandidate:
    candidate_id: str
    user_id: str
    styling_session_id: str | None
    namespace: Literal["shared", "stylist"]
    proposal_id: str
    canonical_kind: CanonicalMemoryKind
    canonical_value: str
    confidence_band: Literal["high", "medium", "low"]
    expires_at: float
    status: Literal[
        "active", "committed", "session_only", "rejected", "rephrase"
    ] = "active"


@dataclass(frozen=True)
class _ExtractReceipt:
    user_id: str
    fingerprint: str
    expires_at: float
    response: MemoryCandidateExtractResponse


@dataclass(frozen=True)
class _DecisionReceipt:
    fingerprint: str
    response: MemoryCandidateDecisionResponse


class MemoryCandidateService:
    """Shared S16 ingress that owns the pre-provider privacy boundary.

    Task 3 extends the safe branch with strict provider schema mapping and Task 4
    exposes it through HTTP. Sensitive input returns before either dependency is
    invoked, so it cannot create proposals, durable rows, outbox facts, or Trace.
    """

    def __init__(
        self,
        *,
        memory: MemoryService,
        provider: MemoryCandidateProvider,
        session_is_active: Callable[[str, str], bool] | None = None,
        ttl_seconds: float = 1800.0,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.memory = memory
        self.provider = provider
        self.prefilter = MemoryCandidatePrefilter()
        self.session_preferences = SessionPreferenceStore(ttl_seconds)
        self._session_is_active = session_is_active or (lambda _user, _session: False)
        self._ttl_seconds = ttl_seconds
        self._candidates: dict[str, _ServerMemoryCandidate] = {}
        self._extract_receipts: dict[str, _ExtractReceipt] = {}
        self._decision_receipts: dict[
            tuple[str, str], _DecisionReceipt
        ] = {}
        self._rejected: set[tuple[str, str, str, str, str]] = set()
        self._lock = RLock()
        self._extract_lock = asyncio.Lock()

    async def _extract_transient(self, text: str) -> MemoryCandidateExtractionResult:
        result = self.prefilter.prefilter(text)
        if not result.allowed:
            return MemoryCandidateExtractionResult(
                allowed=False,
                code=result.code,
            )
        allowed_values: dict[str, list[str]] = {}
        for kind, value in CANONICAL_MEMORY_MAP:
            allowed_values.setdefault(kind, []).append(value)
        provider_candidates, provider_evidence = (
            await self.provider.extract_memory_candidates(
                text,
                allowed_kinds=tuple(allowed_values),
                allowed_values={
                    kind: tuple(values) for kind, values in allowed_values.items()
                },
            )
        )
        try:
            if len(provider_candidates) > 5:
                raise MemoryCandidateError("provider returned too many candidates")
            evidence = MemoryCandidateProviderEvidence.model_validate(
                provider_evidence
            )
            mapped: list[ExtractedMemoryCandidate] = []
            seen: set[tuple[str, str]] = set()
            for provider_candidate in provider_candidates:
                if set(provider_candidate) != {
                    "canonical_kind",
                    "canonical_value",
                    "applicability_tags",
                    "confidence_band",
                }:
                    raise MemoryCandidateError("provider candidate fields rejected")
                if provider_candidate["applicability_tags"] != []:
                    raise MemoryCandidateError("provider applicability rejected")
                kind = provider_candidate["canonical_kind"]
                value = provider_candidate["canonical_value"]
                confidence_band = provider_candidate["confidence_band"]
                if (
                    not isinstance(kind, str)
                    or not isinstance(value, str)
                    or confidence_band not in {"high", "medium", "low"}
                    or (kind, value) in seen
                ):
                    raise MemoryCandidateError("provider candidate value rejected")
                canonical = canonicalize_candidate(
                    kind,
                    value,
                )
                seen.add((kind, value))
                mapped.append(
                    ExtractedMemoryCandidate.model_validate(
                        {
                            **canonical.model_dump(),
                            "confidence_band": confidence_band,
                        }
                    )
                )
        except Exception:
            # Reject the entire untrusted payload without retaining or exposing
            # provider content, even if an earlier member mapped successfully.
            raise MemoryCandidateError("provider candidate batch rejected") from None
        return MemoryCandidateExtractionResult(
            allowed=True,
            code=result.code,
            candidates=tuple(mapped),
            provider_evidence=evidence.model_dump(),
        )

    @staticmethod
    def _fingerprint(*parts: str | None) -> str:
        serialized = "\x1f".join("" if part is None else part for part in parts)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _trace(
        self,
        *,
        user_id: str,
        request_id: str,
        styling_session_id: str | None,
        operation: str,
        outcome: str,
        candidate_count: int,
    ) -> str:
        trace_id = f"trace_memory_candidate_{uuid.uuid4().hex}"
        self.memory.traces.start(
            trace_id=trace_id,
            request_id=request_id,
            styling_session_id=styling_session_id or "memory_candidate_unbound",
            query_text="",
            user_id=user_id,
        )
        self.memory.traces.update(
            trace_id,
            dialogue={
                "memory_candidate": {
                    "operation": operation,
                    "outcome": outcome,
                    "candidate_count": candidate_count,
                    "content_logged": False,
                    "provider_reasoning_logged": False,
                    "authority": "server",
                }
            },
        )
        return trace_id

    def _has_conflict(
        self,
        *,
        user_id: str,
        namespace: Literal["shared", "stylist"],
        candidate: ExtractedMemoryCandidate,
    ) -> bool:
        listed = self.memory.list(user_id, namespace)
        candidate_key = canonical_semantic_key(
            candidate.canonical_kind, candidate.canonical_value
        )
        for record in listed.records:
            existing = canonical_candidate_for_content(
                record.type, record.content
            )
            if existing is not None and canonical_semantic_key(
                existing.canonical_kind, existing.canonical_value
            ) == candidate_key:
                return True
        return False

    def _is_suppressed(
        self,
        payload: MemoryCandidateExtractInput,
        candidate: ExtractedMemoryCandidate,
    ) -> bool:
        if payload.styling_session_id is None:
            return False
        key = (
            payload.user_id,
            payload.styling_session_id,
            payload.namespace,
            candidate.canonical_kind,
            candidate.canonical_value,
        )
        return key in self._rejected or self.memory.repository.candidate_rejected(
            user_id=payload.user_id,
            styling_session_id=payload.styling_session_id,
            namespace=payload.namespace,
            canonical_kind=candidate.canonical_kind,
            canonical_value=candidate.canonical_value,
        )

    @staticmethod
    def _state_dump(state: _ServerMemoryCandidate) -> dict[str, object]:
        return {
            "candidate_id": state.candidate_id,
            "user_id": state.user_id,
            "styling_session_id": state.styling_session_id,
            "namespace": state.namespace,
            "proposal_id": state.proposal_id,
            "canonical_kind": state.canonical_kind,
            "canonical_value": state.canonical_value,
            "confidence_band": state.confidence_band,
            "expires_at": state.expires_at,
            "status": state.status,
        }

    @staticmethod
    def _state_load(payload: dict[str, object]) -> _ServerMemoryCandidate:
        expected = {
            "candidate_id",
            "user_id",
            "styling_session_id",
            "namespace",
            "proposal_id",
            "canonical_kind",
            "canonical_value",
            "confidence_band",
            "expires_at",
            "status",
        }
        if set(payload) != expected:
            raise MemoryCandidateError("stored memory candidate is invalid")
        namespace = payload["namespace"]
        kind = payload["canonical_kind"]
        value = payload["canonical_value"]
        confidence = payload["confidence_band"]
        status = payload["status"]
        if (
            namespace not in {"shared", "stylist"}
            or not isinstance(kind, str)
            or not isinstance(value, str)
            or confidence not in {"high", "medium", "low"}
            or status
            not in {"active", "committed", "session_only", "rejected", "rephrase"}
        ):
            raise MemoryCandidateError("stored memory candidate is invalid")
        canonical = canonicalize_candidate(kind, value)
        return _ServerMemoryCandidate(
            candidate_id=str(payload["candidate_id"]),
            user_id=str(payload["user_id"]),
            styling_session_id=(
                str(payload["styling_session_id"])
                if payload.get("styling_session_id") is not None
                else None
            ),
            namespace=namespace,
            proposal_id=str(payload["proposal_id"]),
            canonical_kind=canonical.canonical_kind,
            canonical_value=canonical.canonical_value,
            confidence_band=confidence,
            expires_at=float(payload["expires_at"]),
            status=status,
        )

    def _extract_receipt(
        self, request_id: str
    ) -> _ExtractReceipt | None:
        cached = self._extract_receipts.get(request_id)
        if cached is not None:
            return cached
        durable = self.memory.repository.candidate_request(request_id)
        if durable is None:
            return None
        receipt = _ExtractReceipt(
            user_id=str(durable["user_id"]),
            fingerprint=str(durable["fingerprint"]),
            expires_at=float(durable["expires_at"]),
            response=MemoryCandidateExtractResponse.model_validate(
                durable["response"]
            ),
        )
        self._extract_receipts[request_id] = receipt
        return receipt

    def _candidate_state(
        self, candidate_id: str
    ) -> _ServerMemoryCandidate | None:
        cached = self._candidates.get(candidate_id)
        durable = self.memory.repository.candidate_state(candidate_id)
        if durable is None:
            return cached
        loaded = self._state_load(durable)
        self._candidates[candidate_id] = loaded
        return loaded

    async def extract(
        self, payload: MemoryCandidateExtractInput | str
    ) -> MemoryCandidateExtractResponse | MemoryCandidateExtractionResult:
        # The string form remains an internal, transient ingress for Task 2/3
        # provider contract tests. Only the typed form can create proposals.
        if isinstance(payload, str):
            return await self._extract_transient(payload)
        async with self._extract_lock:
            return await self._extract_and_create(payload)

    async def _extract_and_create(
        self, payload: MemoryCandidateExtractInput
    ) -> MemoryCandidateExtractResponse:
        fingerprint = self._fingerprint(
            payload.user_id,
            payload.styling_session_id,
            payload.namespace,
            payload.request_id,
            payload.text,
        )
        with self._lock:
            previous = self._extract_receipt(payload.request_id)
            if previous is not None:
                if (
                    previous.user_id != payload.user_id
                    or previous.fingerprint != fingerprint
                ):
                    raise MemoryCandidateConflict("request_id conflicts")
                if previous.expires_at <= time.time():
                    raise MemoryCandidateConflict("memory candidate request expired")
                return previous.response

        transient = await self._extract_transient(payload.text)
        if not transient.allowed:
            trace_id = self._trace(
                user_id=payload.user_id,
                request_id=payload.request_id,
                styling_session_id=payload.styling_session_id,
                operation="extract",
                outcome="blocked",
                candidate_count=0,
            )
            response = MemoryCandidateExtractResponse(
                request_id=payload.request_id,
                status="blocked",
                candidates=(),
                trace_id=trace_id,
            )
            expires_at = time.time() + self._ttl_seconds
            with self._lock:
                self._extract_receipts[payload.request_id] = _ExtractReceipt(
                    user_id=payload.user_id,
                    fingerprint=fingerprint,
                    expires_at=expires_at,
                    response=response,
                )
            return response

        cards: list[MemoryCandidateCard] = []
        states: list[_ServerMemoryCandidate] = []
        now = time.time()
        for candidate in transient.candidates:
            if self._is_suppressed(payload, candidate):
                continue
            proposal = self.memory.propose(
                MemoryProposeInput(
                    user_id=payload.user_id,
                    styling_session_id=payload.styling_session_id,
                    namespace=payload.namespace,
                    type=candidate.memory_type,
                    content=candidate.content,
                )
            ).proposal
            if proposal.commit_blocked:
                raise MemoryCandidateError("server canonical proposal rejected")
            candidate_id = f"mcand_{uuid.uuid4().hex}"
            state = _ServerMemoryCandidate(
                candidate_id=candidate_id,
                user_id=payload.user_id,
                styling_session_id=payload.styling_session_id,
                namespace=payload.namespace,
                proposal_id=proposal.proposal_id,
                canonical_kind=candidate.canonical_kind,
                canonical_value=candidate.canonical_value,
                confidence_band=candidate.confidence_band,
                expires_at=now + self._ttl_seconds,
            )
            actions: tuple[
                Literal["remember", "session_only", "reject", "rephrase"], ...
            ] = ("remember", "session_only", "reject", "rephrase")
            conflict_copy = (
                "已有同类已确认记忆；确认后将由服务端建立新版本。"
                if self._has_conflict(
                    user_id=payload.user_id,
                    namespace=payload.namespace,
                    candidate=candidate,
                )
                else None
            )
            states.append(state)
            cards.append(
                MemoryCandidateCard(
                    candidate_id=candidate_id,
                    confirmation_copy=candidate.confirmation_copy,
                    confidence_band=candidate.confidence_band,
                    conflict_copy=conflict_copy,
                    allowed_actions=actions,
                )
            )
        status: Literal["ready", "needs_rephrase", "blocked"] = (
            "ready" if cards else "needs_rephrase"
        )
        trace_id = self._trace(
            user_id=payload.user_id,
            request_id=payload.request_id,
            styling_session_id=payload.styling_session_id,
            operation="extract",
            outcome=status,
            candidate_count=len(cards),
        )
        response = MemoryCandidateExtractResponse(
            request_id=payload.request_id,
            status=status,
            candidates=tuple(cards),
            trace_id=trace_id,
        )
        expires_at = now + self._ttl_seconds
        try:
            self.memory.repository.put_candidate_batch(
                {
                    "request_id": payload.request_id,
                    "user_id": payload.user_id,
                    "fingerprint": fingerprint,
                    "expires_at": expires_at,
                    "response": response.model_dump(mode="json"),
                },
                [self._state_dump(state) for state in states],
            )
        except MemoryRepositoryError as exc:
            for state in states:
                self.memory.confirm(
                    state.proposal_id,
                    MemoryConfirmInput(user_id=state.user_id, decision="reject"),
                )
            raise MemoryCandidateConflict(
                "memory candidate request conflicts"
            ) from exc
        with self._lock:
            for state in states:
                self._candidates[state.candidate_id] = state
            self._extract_receipts[payload.request_id] = _ExtractReceipt(
                user_id=payload.user_id,
                fingerprint=fingerprint,
                expires_at=expires_at,
                response=response,
            )
        return response

    def decide(
        self,
        candidate_id: str,
        payload: MemoryCandidateDecisionInput,
    ) -> MemoryCandidateDecisionResponse:
        fingerprint = self._fingerprint(
            payload.user_id,
            payload.styling_session_id,
            payload.decision,
            payload.idempotency_key,
        )
        with self._lock:
            state = self._candidate_state(candidate_id)
            if (
                state is None
                or state.user_id != payload.user_id
                or state.styling_session_id != payload.styling_session_id
                or state.expires_at <= time.time()
            ):
                raise MemoryCandidateNotFound(candidate_id)
            receipt = self._decision_receipts.get(
                (candidate_id, payload.idempotency_key)
            )
            if receipt is None:
                durable_receipt = self.memory.repository.candidate_decision(
                    candidate_id, payload.idempotency_key
                )
                if durable_receipt is not None:
                    receipt = _DecisionReceipt(
                        fingerprint=str(durable_receipt["fingerprint"]),
                        response=MemoryCandidateDecisionResponse.model_validate(
                            durable_receipt["response"]
                        ),
                    )
                    self._decision_receipts[
                        (candidate_id, payload.idempotency_key)
                    ] = receipt
            if receipt is not None:
                if receipt.fingerprint != fingerprint:
                    raise MemoryCandidateConflict("idempotency key conflicts")
                return receipt.response
            if state.status != "active":
                raise MemoryCandidateConflict("candidate already decided")
            if payload.decision == "session_only" and (
                payload.styling_session_id is None
                or not self._session_is_active(
                    payload.user_id, payload.styling_session_id
                )
            ):
                raise MemoryCandidateDecisionError(
                    "session_only requires an active styling session"
                )

            if payload.decision == "remember":
                operation = self.memory.confirm(
                    state.proposal_id,
                    MemoryConfirmInput(
                        user_id=payload.user_id,
                        decision="confirm",
                    ),
                    candidate_authorized=True,
                )
                if operation.record is None:
                    raise MemoryCandidateDecisionError("candidate commit failed")
                status: Literal["committed", "session_only", "rejected"] = (
                    "committed"
                )
                record_id = operation.record.memory_id
                state.status = "committed"
            else:
                operation = self.memory.confirm(
                    state.proposal_id,
                    MemoryConfirmInput(
                        user_id=payload.user_id,
                        decision="reject",
                    ),
                    candidate_authorized=True,
                )
                record_id = None
                if payload.decision == "session_only":
                    assert payload.styling_session_id is not None
                    self.session_preferences.put(
                        SessionPreference(
                            user_id=payload.user_id,
                            styling_session_id=payload.styling_session_id,
                            canonical_kind=state.canonical_kind,
                            canonical_value=state.canonical_value,
                            expires_at=time.time() + self._ttl_seconds,
                        )
                    )
                    status = "session_only"
                    state.status = "session_only"
                else:
                    status = "rejected"
                    state.status = payload.decision
                    if (
                        payload.decision == "reject"
                        and payload.styling_session_id is not None
                    ):
                        self._rejected.add(
                            (
                                payload.user_id,
                                payload.styling_session_id,
                                state.namespace,
                                state.canonical_kind,
                                state.canonical_value,
                            )
                        )
            response = MemoryCandidateDecisionResponse(
                candidate_id=candidate_id,
                decision=payload.decision,
                status=status,
                record_id=record_id,
                trace_id=operation.trace_id,
            )
            try:
                authoritative = self.memory.repository.record_candidate_decision(
                    candidate_id,
                    payload.idempotency_key,
                    fingerprint,
                    state.status,
                    response.model_dump(mode="json"),
                )
            except MemoryRepositoryError as exc:
                raise MemoryCandidateConflict(
                    "memory candidate decision conflicts"
                ) from exc
            response = MemoryCandidateDecisionResponse.model_validate(authoritative)
            self._decision_receipts[(candidate_id, payload.idempotency_key)] = (
                _DecisionReceipt(
                    fingerprint=fingerprint,
                    response=response,
                )
            )
            return response


@dataclass(frozen=True)
class SessionPreference:
    user_id: str
    styling_session_id: str
    canonical_kind: CanonicalMemoryKind
    canonical_value: str
    expires_at: float


class SessionPreferenceStore:
    def __init__(self, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = ttl_seconds
        self._items: dict[tuple[str, str, str], SessionPreference] = {}
        self._lock = RLock()

    def put(self, preference: SessionPreference) -> SessionPreference:
        key = (
            preference.user_id,
            preference.styling_session_id,
            preference.canonical_kind,
        )
        with self._lock:
            self._items[key] = preference
        return preference

    def active(
        self, user_id: str, styling_session_id: str, now: float
    ) -> tuple[SessionPreference, ...]:
        with self._lock:
            return tuple(
                item
                for item in self._items.values()
                if item.user_id == user_id
                and item.styling_session_id == styling_session_id
                and item.expires_at > now
            )


COLOR_LABELS: dict[str, str] = {
    "black": "黑色",
    "white": "白色",
    "gray": "灰色",
    "navy": "藏青色",
    "beige": "米色",
    "brown": "棕色",
    "khaki": "卡其色",
    "red": "红色",
    "pink": "粉色",
    "orange": "橙色",
    "yellow": "黄色",
    "green": "绿色",
    "blue": "蓝色",
    "purple": "紫色",
    "multi": "多色",
}
COLOR_VALUES = tuple(COLOR_LABELS)

STYLE_LABELS: dict[str, str] = {
    "simple": "简洁",
    "classic": "经典",
    "smart": "利落",
    "formal": "正式",
    "street": "街头",
    "sporty": "运动",
    "soft": "柔和",
    "vintage": "复古",
    "business": "商务",
    "campus": "学院",
}

GARMENT_PREFERENCES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "straight_trousers": (
        "偏爱直筒裤",
        "fit:straight",
        ("goal:comfortable",),
    ),
}
GARMENT_AVOIDANCES: dict[str, str] = {
    "high_heels": "不穿高跟鞋",
    "skirts": "不穿裙装",
}
COMFORT_PREFERENCES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "long_walk": (
        "长时间站立时优先选择适合久走的鞋",
        "comfort:long_walk",
        ("comfort:long_walk",),
    ),
}
OCCASION_LABELS: dict[str, str] = {
    "daily": "日常",
    "commute": "通勤",
    "interview": "面试",
    "meeting": "会议",
    "date": "约会",
    "party": "聚会",
    "travel": "旅行",
    "outdoor": "户外",
    "home": "居家",
    "sports": "运动",
}


def _candidate(
    *,
    kind: CanonicalMemoryKind,
    value: str,
    memory_type: MemoryType,
    memory_class: MemoryClass,
    content: str,
    applicability_tags: tuple[str, ...] = (),
    soft_term: str | None = None,
) -> CanonicalMemoryCandidate:
    return CanonicalMemoryCandidate(
        canonical_kind=kind,
        canonical_value=value,
        memory_type=memory_type,
        memory_class=memory_class,
        content=content,
        applicability_tags=applicability_tags,
        confirmation_copy=f"要长期记住“{content}”吗？",
        soft_term=soft_term,
    )


CANONICAL_MEMORY_MAP: dict[
    tuple[str, str], CanonicalMemoryCandidate
] = {}

for _value, _label in COLOR_LABELS.items():
    CANONICAL_MEMORY_MAP[("color_preference", _value)] = _candidate(
        kind="color_preference",
        value=_value,
        memory_type="preference",
        memory_class="preference_event",
        content=f"偏爱{_label}",
        soft_term=f"color:{_value}",
    )
    CANONICAL_MEMORY_MAP[("color_avoidance", _value)] = _candidate(
        kind="color_avoidance",
        value=_value,
        memory_type="constraint",
        memory_class="hard_constraint",
        content=f"不穿{_label}",
    )

for _value, _label in STYLE_LABELS.items():
    CANONICAL_MEMORY_MAP[("style_preference", _value)] = _candidate(
        kind="style_preference",
        value=_value,
        memory_type="preference",
        memory_class="preference_event",
        content=f"偏爱{_label}风格",
        applicability_tags=(f"style:{_value}",),
        soft_term=f"style:{_value}",
    )

for _value, (_content, _term, _tags) in GARMENT_PREFERENCES.items():
    CANONICAL_MEMORY_MAP[("garment_preference", _value)] = _candidate(
        kind="garment_preference",
        value=_value,
        memory_type="preference",
        memory_class="preference_event",
        content=_content,
        applicability_tags=_tags,
        soft_term=_term,
    )

for _value, _content in GARMENT_AVOIDANCES.items():
    CANONICAL_MEMORY_MAP[("garment_avoidance", _value)] = _candidate(
        kind="garment_avoidance",
        value=_value,
        memory_type="constraint",
        memory_class="hard_constraint",
        content=_content,
    )

for _value, (_content, _term, _tags) in COMFORT_PREFERENCES.items():
    CANONICAL_MEMORY_MAP[("comfort_preference", _value)] = _candidate(
        kind="comfort_preference",
        value=_value,
        memory_type="preference",
        memory_class="preference_event",
        content=_content,
        applicability_tags=_tags,
        soft_term=_term,
    )

for _value, _label in OCCASION_LABELS.items():
    _content = f"在{_label}场合优先沿用已确认偏好"
    CANONICAL_MEMORY_MAP[("occasion_preference", _value)] = _candidate(
        kind="occasion_preference",
        value=_value,
        memory_type="preference",
        memory_class="preference_event",
        content=_content,
        applicability_tags=(f"occasion:{_value}",),
    )

_CANDIDATE_BY_TYPE_CONTENT: dict[
    tuple[str, str], CanonicalMemoryCandidate
] = {
    (candidate.memory_type, candidate.content): candidate
    for candidate in CANONICAL_MEMORY_MAP.values()
}


def canonicalize_candidate(kind: str, value: str) -> CanonicalMemoryCandidate:
    key = (kind.strip(), value.strip().lower())
    try:
        return CANONICAL_MEMORY_MAP[key].model_copy(deep=True)
    except KeyError as exc:
        raise MemoryCandidateError(
            "candidate is outside the controlled closure"
        ) from exc


def canonical_semantic_key(kind: str, value: str) -> str:
    """Return the server-owned conflict key for one validated candidate.

    Positive and negative preferences for the same color are contradictory and
    therefore share one version head. Other kinds keep their exact canonical
    identity, allowing a user to retain multiple non-conflicting preferences.
    """

    candidate = canonicalize_candidate(kind, value)
    if candidate.canonical_kind in {"color_preference", "color_avoidance"}:
        return f"canonical:color:{candidate.canonical_value}"
    return (
        f"canonical:{candidate.canonical_kind}:"
        f"{candidate.canonical_value}"
    )


def canonical_candidate_for_content(
    memory_type: str, content: str
) -> CanonicalMemoryCandidate | None:
    candidate = _CANDIDATE_BY_TYPE_CONTENT.get((memory_type, content))
    return candidate.model_copy(deep=True) if candidate is not None else None
