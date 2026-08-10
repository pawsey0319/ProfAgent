from __future__ import annotations

import asyncio
import base64
import json
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .asset_service import AssetRecord
from .config import Settings
from .providers import CPA_REPORTED_MODELS, CPA_TRANSPORT_MODEL


VISION_TRANSPORT_MODEL = CPA_TRANSPORT_MODEL
VISION_REPORTED_MODELS = CPA_REPORTED_MODELS
VISIBLE_SLOTS = {"top", "bottom", "shoes", "overall"}
MIN_REGION_CONFIDENCE = 0.70

ObservationCode = Literal[
    "top_visible",
    "top_not_visible",
    "bottom_visible",
    "bottom_not_visible",
    "shoes_visible",
    "shoes_not_visible",
    "overall_visible",
    "overall_not_visible",
]

_SERVER_EVIDENCE_TEXT: dict[str, str] = {
    "top_visible": "已确认上装衣物区域清晰可见。",
    "top_not_visible": "未确认上装衣物区域清晰可见。",
    "bottom_visible": "已确认下装衣物区域清晰可见。",
    "bottom_not_visible": "未确认下装衣物区域清晰可见。",
    "shoes_visible": "已确认鞋履衣物区域清晰可见。",
    "shoes_not_visible": "未确认鞋履衣物区域清晰可见。",
    "overall_visible": "已确认整套衣物区域可用于搭配核对。",
    "overall_not_visible": "未确认整套衣物区域可用于搭配核对。",
}


class VisionRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: Literal["top", "bottom", "shoes", "overall"]
    visible: bool
    observation: ObservationCode
    confidence: float = Field(ge=0, le=1)
    issue_codes: tuple[
        Literal[
            "none",
            "bottom_cuff_messy",
            "layer_alignment_issue",
            "shoe_coordination_issue",
            "detail_competition",
        ],
        ...,
    ] = Field(default_factory=lambda: ("none",), max_length=3)

    @field_validator("issue_codes")
    @classmethod
    def valid_issue_combination(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("issue_codes must be unique")
        if "none" in value and len(value) != 1:
            raise ValueError("none cannot be combined with another issue")
        return value

    @model_validator(mode="after")
    def observation_and_issues_match_region(self) -> "VisionRegion":
        expected_observation = (
            f"{self.slot}_visible"
            if self.visible
            else f"{self.slot}_not_visible"
        )
        if self.observation != expected_observation:
            raise ValueError("observation code does not match slot/visible")
        if not self.visible and self.issue_codes != ("none",):
            raise ValueError("invisible regions cannot carry issue codes")
        allowed = {
            "top": {"none", "layer_alignment_issue", "detail_competition"},
            "bottom": {"none", "bottom_cuff_messy", "detail_competition"},
            "shoes": {"none", "shoe_coordination_issue"},
            "overall": {
                "none",
                "layer_alignment_issue",
                "shoe_coordination_issue",
                "detail_competition",
            },
        }[self.slot]
        if not set(self.issue_codes).issubset(allowed):
            raise ValueError("issue code is not valid for this garment region")
        return self

    @property
    def evidence_text(self) -> str:
        """Server-owned text; Provider free text is never propagated."""
        return _SERVER_EVIDENCE_TEXT[self.observation]


class VisionAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    regions: tuple[VisionRegion, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def exact_region_set(self) -> "VisionAssessment":
        slots = [region.slot for region in self.regions]
        if len(set(slots)) != len(slots) or set(slots) != VISIBLE_SLOTS:
            raise ValueError("exactly top/bottom/shoes/overall are required")
        return self

    @property
    def complete_and_confident(self) -> bool:
        return all(
            region.visible and region.confidence >= MIN_REGION_CONFIDENCE
            for region in self.regions
        )

    @property
    def issue_codes(self) -> frozenset[str]:
        return frozenset(
            code
            for region in self.regions
            for code in region.issue_codes
            if code != "none"
        )


@dataclass(frozen=True)
class VisionInspection:
    assessment: VisionAssessment
    provider_trace: dict[str, Any]


class VisionUnavailable(RuntimeError):
    def __init__(self, reason: str, provider_trace: dict[str, Any]):
        super().__init__(reason)
        self.reason = reason
        self.provider_trace = provider_trace


class VisionAdapter:
    """Strict, injectable CPA adapter for garment-region visibility only."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.transport = transport
        self._last_health: dict[str, Any] = {
            "enabled": settings.cpa_text_enabled,
            "available": False,
            "status": "unverified" if settings.cpa_text_enabled else "disabled",
            "requested_model": "grok4.5",
            "transport_model": VISION_TRANSPORT_MODEL,
            "resolved_model": None,
            "model_verified": False,
            "exact_model_required": True,
        }

    def set_transport(self, transport: httpx.AsyncBaseTransport | None) -> None:
        """Deterministic test injection; never changes the requested model."""
        self.transport = transport

    def health(self) -> dict[str, Any]:
        return dict(self._last_health)

    @staticmethod
    def _base_trace(status: str) -> dict[str, Any]:
        return {
            "component": "vision",
            "status": status,
            "requested_model": "grok4.5",
            "transport_model": VISION_TRANSPORT_MODEL,
            "resolved_model": None,
            "model_verified": False,
            "schema": "garment_visibility_codes_v2",
            "image_logged": False,
        }

    def _fail(self, reason: str, status: str, **details: Any) -> VisionUnavailable:
        trace = {**self._base_trace(status), **details}
        self._last_health = {
            **self._last_health,
            "available": False,
            "status": status,
            "resolved_model": trace.get("resolved_model"),
            "model_verified": False,
        }
        return VisionUnavailable(reason, trace)

    async def inspect(
        self, assets: list[tuple[AssetRecord, bytes]]
    ) -> VisionInspection:
        if self.settings.vision_force_failure:
            raise self._fail("vision failure injection", "forced_failure")
        if not self.settings.cpa_text_enabled:
            raise self._fail("CPA vision is disabled", "disabled")
        if not assets or any(record.visual_quality != "usable" for record, _ in assets):
            raise self._fail("no technically usable owned asset", "insufficient_asset")

        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Return one JSON object only: {\"regions\":[...]}. "
                    "Regions must be exactly top,bottom,shoes,overall. Each region has only "
                    "slot, visible, observation, confidence, issue_codes. observation is a "
                    "controlled code, never prose: top_visible,top_not_visible,"
                    "bottom_visible,bottom_not_visible,shoes_visible,shoes_not_visible,"
                    "overall_visible,overall_not_visible. It must equal <slot>_visible when "
                    "visible=true and <slot>_not_visible when visible=false. An invisible "
                    "region must use issue_codes=[\"none\"]. Do not output any other text "
                    "or fields. issue_codes may only be "
                    "none,bottom_cuff_messy,layer_alignment_issue,"
                    "shoe_coordination_issue,detail_competition."
                ),
            }
        ]
        for record, raw in assets:
            encoded = base64.b64encode(raw).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{record.media_type};base64,{encoded}",
                    },
                }
            )
        request_body = {
            "model": VISION_TRANSPORT_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a restricted garment-visibility inspector. "
                        "Follow the exact JSON schema and safety boundary."
                    ),
                },
                {"role": "user", "content": content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 800,
        }
        headers = {"Content-Type": "application/json"}
        if self.settings.cpa_api_key:
            headers["Authorization"] = f"Bearer {self.settings.cpa_api_key}"
        started = time.perf_counter()
        interaction_budget = (
            self.settings.effective_vision_interaction_budget_seconds
        )
        try:
            async with httpx.AsyncClient(
                timeout=min(self.settings.cpa_timeout_seconds, interaction_budget),
                trust_env=False,
                transport=self.transport,
            ) as client:
                provider_task = asyncio.create_task(
                    client.post(
                        f"{self.settings.cpa_base_url}/chat/completions",
                        headers=headers,
                        json=request_body,
                    )
                )
                try:
                    response = await asyncio.wait_for(
                        asyncio.shield(provider_task), timeout=interaction_budget
                    )
                except (TimeoutError, asyncio.TimeoutError) as exc:
                    provider_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await provider_task
                    raise self._fail(
                        "vision interaction budget exceeded",
                        "timeout",
                        latency_ms=round((time.perf_counter() - started) * 1000),
                        interaction_budget_seconds=interaction_budget,
                    ) from exc
                except asyncio.CancelledError as exc:
                    # A Provider child task may cancel itself. Convert that
                    # controlled failure to a qualitative Scorecard. If the
                    # caller cancelled this request, cancel the child and
                    # preserve cancellation semantics instead.
                    if provider_task.done() and provider_task.cancelled():
                        raise self._fail(
                            "vision provider task cancelled",
                            "cancelled",
                            latency_ms=round(
                                (time.perf_counter() - started) * 1000
                            ),
                            interaction_budget_seconds=interaction_budget,
                        ) from exc
                    provider_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await provider_task
                    raise
        except VisionUnavailable:
            raise
        except httpx.HTTPError as exc:
            raise self._fail(
                "vision provider request failed",
                "unavailable",
                latency_ms=round((time.perf_counter() - started) * 1000),
                error_type=type(exc).__name__,
                interaction_budget_seconds=interaction_budget,
            ) from exc
        latency_ms = round((time.perf_counter() - started) * 1000)
        if response.status_code < 200 or response.status_code >= 300:
            raise self._fail(
                "vision provider returned a non-success status",
                "http_error",
                latency_ms=latency_ms,
                http_status=response.status_code,
            )
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise self._fail(
                "vision provider response is not JSON",
                "malformed",
                latency_ms=latency_ms,
            ) from exc
        resolved_model = payload.get("model") if isinstance(payload, dict) else None
        if resolved_model not in VISION_REPORTED_MODELS:
            raise self._fail(
                "vision provider model mismatch",
                "model_mismatch",
                latency_ms=latency_ms,
                resolved_model=resolved_model if isinstance(resolved_model, str) else None,
            )
        try:
            message_content = payload["choices"][0]["message"]["content"]
            if not isinstance(message_content, str):
                raise TypeError("content must be a string")
            assessment = VisionAssessment.model_validate(json.loads(message_content))
        except VisionUnavailable:
            raise
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise self._fail(
                "vision provider response failed strict schema validation",
                "malformed",
                latency_ms=latency_ms,
                resolved_model=resolved_model,
            ) from exc
        trace = {
            **self._base_trace("ok"),
            "latency_ms": latency_ms,
            "interaction_budget_seconds": interaction_budget,
            "resolved_model": resolved_model,
            "model_verified": True,
            "region_count": len(assessment.regions),
            "complete_visibility": assessment.complete_and_confident,
            "issue_codes": sorted(assessment.issue_codes),
        }
        self._last_health = {
            **self._last_health,
            "available": True,
            "status": "ok",
            "resolved_model": resolved_model,
            "model_verified": True,
        }
        return VisionInspection(assessment=assessment, provider_trace=trace)
