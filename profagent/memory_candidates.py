from __future__ import annotations

import re
from dataclasses import dataclass
from threading import RLock
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .memory_service import MemoryClass, MemoryService, MemoryType


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
    ) -> None:
        self.memory = memory
        self.provider = provider
        self.prefilter = MemoryCandidatePrefilter()

    async def extract(self, text: str) -> MemoryCandidateExtractionResult:
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


def canonical_candidate_for_content(
    memory_type: str, content: str
) -> CanonicalMemoryCandidate | None:
    candidate = _CANDIDATE_BY_TYPE_CONTENT.get((memory_type, content))
    return candidate.model_copy(deep=True) if candidate is not None else None
