from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal, get_args

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from .asset_service import AssetRecord
from .config import Settings
from .models import Audience, Slot
from .providers import CPA_REPORTED_MODELS, CPA_TRANSPORT_MODEL


VISION_TRANSPORT_MODEL = CPA_TRANSPORT_MODEL
VISION_REPORTED_MODELS = CPA_REPORTED_MODELS
VISIBLE_SLOTS = {"top", "bottom", "shoes", "overall"}
MIN_REGION_CONFIDENCE = 0.70
CATALOG_VISION_MAX_IMAGE_BYTES = 25 * 1024 * 1024
VISION_MAX_RESPONSE_BYTES = 256 * 1024
CATALOG_ASSESSMENT_SCHEMA = "catalog_asset_assessment_v1"
CATALOG_AUDIENCES = ("womenswear", "unisex_womenswear_compatible")
CATALOG_QUALITY_SIGNALS = (
    "logo_or_watermark",
    "low_resolution",
    "multiple_items",
    "severe_occlusion",
    "text_overlay",
)
CATALOG_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
CATALOG_SLOTS = frozenset(get_args(Slot))

CatalogQualityIssueCode = Literal[
    "logo_or_watermark",
    "text_overlay",
    "multiple_items",
    "severe_occlusion",
    "low_resolution",
]
CatalogConfidenceBand = Literal["high", "medium", "low"]
CatalogFailureReason = Literal[
    "input_rejected",
    "timeout",
    "provider_unavailable",
    "http_error",
    "response_schema_invalid",
    "model_mismatch",
    "slot_mismatch",
    "audience_rejected",
    "identifiable_person",
    "low_confidence",
    "invalid_region",
    "quality_rejected",
]

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


class CatalogAssetAssessment(BaseModel):
    """Provider evidence for catalog image safety, never asset authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: Slot
    audience: Audience
    contains_identifiable_person: StrictBool
    object_region: tuple[float, float, float, float]
    confidence_band: CatalogConfidenceBand
    quality_issues: tuple[CatalogQualityIssueCode, ...] = ()

    @field_validator("object_region", mode="before")
    @classmethod
    def finite_normalized_ordered_region(
        cls, value: Any
    ) -> tuple[float, float, float, float]:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError("invalid_region")
        if any(
            isinstance(coordinate, bool)
            or not isinstance(coordinate, (int, float))
            for coordinate in value
        ):
            raise ValueError("invalid_region")
        region = tuple(float(coordinate) for coordinate in value)
        left, top, right, bottom = region
        if (
            not all(math.isfinite(coordinate) for coordinate in region)
            or not all(0.0 <= coordinate <= 1.0 for coordinate in region)
            or left >= right
            or top >= bottom
        ):
            raise ValueError("invalid_region")
        return region

    @field_validator("quality_issues", mode="before")
    @classmethod
    def unique_quality_issues(cls, value: Any) -> Any:
        if not isinstance(value, (list, tuple)):
            raise ValueError("quality_issues_must_be_an_array")
        if any(not isinstance(issue, str) for issue in value):
            raise ValueError("quality_issue_must_be_a_code")
        if len(set(value)) != len(value):
            raise ValueError("quality_issues_must_be_unique")
        return value


@dataclass(frozen=True)
class VisionInspection:
    assessment: VisionAssessment
    provider_trace: dict[str, Any]


class VisionUnavailable(RuntimeError):
    def __init__(self, reason: str, provider_trace: dict[str, Any]):
        super().__init__(reason)
        self.reason = reason
        self.provider_trace = provider_trace


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _load_json_without_duplicate_keys(value: bytes | str) -> Any:
    try:
        return json.loads(value, object_pairs_hook=_closed_json_object)
    except (OverflowError, RecursionError):
        raise ValueError("json_nesting_invalid") from None


class _VisionResponseInvalid(Exception):
    pass


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
            "requested_model": "grok4.6",
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
            "requested_model": "grok4.6",
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

    def _catalog_trace(
        self,
        *,
        status: Literal["ok", "quarantined"],
        reason_code: CatalogFailureReason | None,
        latency_ms: int | float,
        resolved_model: str | None = None,
        model_verified: bool = False,
        assessment_count: int = 0,
        quality_issue_count: int = 0,
    ) -> dict[str, Any]:
        return {
            "component": "vision",
            "operation": "catalog_asset_assessment",
            "status": status,
            "reason_code": reason_code,
            "requested_model": "grok4.6",
            "transport_model": VISION_TRANSPORT_MODEL,
            "resolved_model": resolved_model if model_verified else None,
            "model_verified": model_verified,
            "schema": CATALOG_ASSESSMENT_SCHEMA,
            "image_logged": False,
            "latency_ms": latency_ms,
            "interaction_budget_seconds": (
                self.settings.effective_vision_interaction_budget_seconds
            ),
            "assessment_count": assessment_count,
            "quality_issue_count": quality_issue_count,
        }

    def _catalog_failure(
        self,
        reason_code: CatalogFailureReason,
        *,
        latency_ms: int | float = 0,
        resolved_model: str | None = None,
        model_verified: bool = False,
        quality_issue_count: int = 0,
    ) -> VisionUnavailable:
        trace = self._catalog_trace(
            status="quarantined",
            reason_code=reason_code,
            latency_ms=latency_ms,
            resolved_model=resolved_model,
            model_verified=model_verified,
            quality_issue_count=quality_issue_count,
        )
        self._last_health = {
            **self._last_health,
            "available": False,
            "status": "quarantined",
            "resolved_model": trace["resolved_model"],
            "model_verified": trace["model_verified"],
        }
        return VisionUnavailable("catalog_asset_quarantined", trace)

    def _request_failure(
        self,
        *,
        operation: str,
        reason_code: CatalogFailureReason,
        latency_ms: int | float,
        schema: str,
    ) -> VisionUnavailable:
        if operation == "catalog_asset_assessment":
            return self._catalog_failure(reason_code, latency_ms=latency_ms)
        legacy = {
            "timeout": ("vision interaction budget exceeded", "timeout"),
            "provider_unavailable": ("vision provider request failed", "unavailable"),
            "http_error": (
                "vision provider returned a non-success status",
                "http_error",
            ),
            "response_schema_invalid": (
                "vision provider response failed strict schema validation",
                "malformed",
            ),
            "model_mismatch": ("vision provider model mismatch", "model_mismatch"),
        }
        reason, status = legacy[reason_code]
        return self._fail(
            reason,
            status,
            operation=operation,
            schema=schema,
            latency_ms=latency_ms,
            interaction_budget_seconds=(
                self.settings.effective_vision_interaction_budget_seconds
            ),
        )

    @staticmethod
    def _provider_prompt(
        *, operation: str, allowed_values: dict[str, tuple[str, ...]]
    ) -> tuple[str, str]:
        if operation == "catalog_asset_assessment":
            expected_slots = ",".join(allowed_values["slot"])
            audiences = ",".join(allowed_values["audience"])
            quality_signals = ",".join(allowed_values["quality_issues"])
            return (
                "You are a restricted catalog image assessor. Follow the exact "
                "closed JSON contract.",
                "Return one JSON object only with exactly these fields: slot, "
                "audience, contains_identifiable_person, object_region, "
                "confidence_band, quality_issues. "
                f"slot must be one of [{expected_slots}]. "
                f"audience must be one of [{audiences}]. "
                "contains_identifiable_person must be true or false. "
                "object_region must be [left,top,right,bottom] using finite "
                "numbers from 0 to 1 with left < right and top < bottom. "
                "confidence_band must be high, medium, or low. "
                "quality_issues must be a unique JSON array containing only "
                f"values from [{quality_signals}]. Do not add fields or prose.",
            )
        return (
            "You are a restricted garment-visibility inspector. Follow the exact "
            "JSON schema and safety boundary.",
            "Return one JSON object only: {\"regions\":[...]}. Regions must be "
            "exactly top,bottom,shoes,overall. Each region has only slot, visible, "
            "observation, confidence, issue_codes. observation is a controlled "
            "code, never prose: top_visible,top_not_visible,bottom_visible,"
            "bottom_not_visible,shoes_visible,shoes_not_visible,overall_visible,"
            "overall_not_visible. It must equal <slot>_visible when visible=true "
            "and <slot>_not_visible when visible=false. An invisible region must "
            "use issue_codes=[\"none\"]. Do not output any other text or fields. "
            "issue_codes may only be none,bottom_cuff_messy,"
            "layer_alignment_issue,shoe_coordination_issue,detail_competition.",
        )

    async def _request_json(
        self,
        *,
        operation: Literal[
            "garment_visibility_inspection", "catalog_asset_assessment"
        ],
        image_bytes: bytes | tuple[bytes, ...],
        mime_type: str | tuple[str, ...],
        allowed_values: dict[str, tuple[str, ...]],
        schema: str,
        max_tokens: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Use one verified timeout/model/closed-JSON path for all Vision calls."""
        images = (image_bytes,) if isinstance(image_bytes, bytes) else image_bytes
        mime_types = (mime_type,) if isinstance(mime_type, str) else mime_type
        system_prompt, user_prompt = self._provider_prompt(
            operation=operation, allowed_values=allowed_values
        )
        content: list[dict[str, Any]] = [
            {"type": "text", "text": user_prompt}
        ]
        for raw, media_type in zip(images, mime_types, strict=True):
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": (
                            f"data:{media_type};base64,"
                            f"{base64.b64encode(raw).decode('ascii')}"
                        )
                    },
                }
            )
        request_body = {
            "model": VISION_TRANSPORT_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        headers = {"Content-Type": "application/json"}
        headers["Accept-Encoding"] = "identity"
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
                async def request_provider() -> tuple[int, bytes]:
                    request = client.build_request(
                        "POST",
                        f"{self.settings.cpa_base_url}/chat/completions",
                        headers=headers,
                        json=request_body,
                    )
                    response = await client.send(request, stream=True)
                    try:
                        if response.status_code < 200 or response.status_code >= 300:
                            return response.status_code, b""
                        content_encoding = response.headers.get(
                            "content-encoding", "identity"
                        ).strip().lower()
                        if content_encoding != "identity":
                            raise _VisionResponseInvalid(
                                "unsupported_content_encoding"
                            )
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_raw():
                            total += len(chunk)
                            if total > VISION_MAX_RESPONSE_BYTES:
                                raise _VisionResponseInvalid(
                                    "response_too_large"
                                )
                            chunks.append(chunk)
                        return response.status_code, b"".join(chunks)
                    finally:
                        with suppress(Exception):
                            await response.aclose()

                provider_task = asyncio.create_task(
                    request_provider()
                )
                try:
                    status_code, response_body = await asyncio.wait_for(
                        asyncio.shield(provider_task), timeout=interaction_budget
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    provider_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await provider_task
                    latency_ms = round((time.perf_counter() - started) * 1000)
                    raise self._request_failure(
                        operation=operation,
                        reason_code="timeout",
                        latency_ms=latency_ms,
                        schema=schema,
                    ) from None
                except asyncio.CancelledError:
                    if provider_task.done() and provider_task.cancelled():
                        latency_ms = round((time.perf_counter() - started) * 1000)
                        raise self._request_failure(
                            operation=operation,
                            reason_code="provider_unavailable",
                            latency_ms=latency_ms,
                            schema=schema,
                        ) from None
                    provider_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await provider_task
                    raise
        except VisionUnavailable:
            raise
        except _VisionResponseInvalid:
            latency_ms = round((time.perf_counter() - started) * 1000)
            raise self._request_failure(
                operation=operation,
                reason_code="response_schema_invalid",
                latency_ms=latency_ms,
                schema=schema,
            ) from None
        except httpx.TimeoutException:
            latency_ms = round((time.perf_counter() - started) * 1000)
            raise self._request_failure(
                operation=operation,
                reason_code="timeout",
                latency_ms=latency_ms,
                schema=schema,
            ) from None
        except httpx.HTTPError:
            latency_ms = round((time.perf_counter() - started) * 1000)
            raise self._request_failure(
                operation=operation,
                reason_code="provider_unavailable",
                latency_ms=latency_ms,
                schema=schema,
            ) from None
        except Exception:
            latency_ms = round((time.perf_counter() - started) * 1000)
            raise self._request_failure(
                operation=operation,
                reason_code="provider_unavailable",
                latency_ms=latency_ms,
                schema=schema,
            ) from None

        latency_ms = round((time.perf_counter() - started) * 1000)
        if status_code < 200 or status_code >= 300:
            raise self._request_failure(
                operation=operation,
                reason_code="http_error",
                latency_ms=latency_ms,
                schema=schema,
            ) from None
        try:
            envelope = _load_json_without_duplicate_keys(response_body)
            if not isinstance(envelope, dict) or set(envelope) != {
                "model",
                "choices",
            }:
                raise ValueError("invalid_response_envelope")
            choices = envelope["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("invalid_response_choices")
            choice = choices[0]
            if not isinstance(choice, dict) or set(choice) != {"message"}:
                raise ValueError("invalid_response_choice")
            message = choice["message"]
            if not isinstance(message, dict) or set(message) != {"content"}:
                raise ValueError("invalid_response_message")
            message_content = message["content"]
            if not isinstance(message_content, str):
                raise ValueError("invalid_response_content")
        except (TypeError, UnicodeDecodeError, ValueError):
            raise self._request_failure(
                operation=operation,
                reason_code="response_schema_invalid",
                latency_ms=latency_ms,
                schema=schema,
            ) from None

        resolved_model = envelope["model"]
        if (
            not isinstance(resolved_model, str)
            or resolved_model not in VISION_REPORTED_MODELS
        ):
            raise self._request_failure(
                operation=operation,
                reason_code="model_mismatch",
                latency_ms=latency_ms,
                schema=schema,
            ) from None
        try:
            raw = _load_json_without_duplicate_keys(message_content)
            if not isinstance(raw, dict):
                raise ValueError("response_content_must_be_an_object")
        except (TypeError, ValueError):
            raise self._request_failure(
                operation=operation,
                reason_code="response_schema_invalid",
                latency_ms=latency_ms,
                schema=schema,
            ) from None

        trace = {
            "component": "vision",
            "operation": operation,
            "status": "ok",
            "requested_model": "grok4.6",
            "transport_model": VISION_TRANSPORT_MODEL,
            "resolved_model": resolved_model,
            "model_verified": True,
            "schema": schema,
            "image_logged": False,
            "latency_ms": latency_ms,
            "interaction_budget_seconds": interaction_budget,
        }
        self._last_health = {
            **self._last_health,
            "available": True,
            "status": "ok",
            "resolved_model": resolved_model,
            "model_verified": True,
        }
        return raw, trace

    @staticmethod
    def _valid_catalog_image_input(
        image_bytes: Any, mime_type: Any, allowed_slot: Any
    ) -> bool:
        if (
            not isinstance(image_bytes, bytes)
            or not image_bytes
            or len(image_bytes) > CATALOG_VISION_MAX_IMAGE_BYTES
            or not isinstance(mime_type, str)
            or mime_type not in CATALOG_MIME_TYPES
            or not isinstance(allowed_slot, str)
            or allowed_slot not in CATALOG_SLOTS
        ):
            return False
        return True

    async def inspect(
        self, assets: list[tuple[AssetRecord, bytes]]
    ) -> VisionInspection:
        if self.settings.vision_force_failure:
            raise self._fail("vision failure injection", "forced_failure")
        if not self.settings.cpa_text_enabled:
            raise self._fail("CPA vision is disabled", "disabled")
        if not assets or any(record.visual_quality != "usable" for record, _ in assets):
            raise self._fail("no technically usable owned asset", "insufficient_asset")
        raw, trace = await self._request_json(
            operation="garment_visibility_inspection",
            image_bytes=tuple(raw for _, raw in assets),
            mime_type=tuple(record.media_type for record, _ in assets),
            allowed_values={
                "slot": ("top", "bottom", "shoes", "overall"),
                "observation": tuple(get_args(ObservationCode)),
                "issue_codes": (
                    "none",
                    "bottom_cuff_messy",
                    "layer_alignment_issue",
                    "shoe_coordination_issue",
                    "detail_competition",
                ),
            },
            schema="garment_visibility_codes_v2",
            max_tokens=800,
        )
        try:
            assessment = VisionAssessment.model_validate(raw)
        except ValidationError:
            raise self._fail(
                "vision provider response failed strict schema validation",
                "malformed",
                operation="garment_visibility_inspection",
                schema="garment_visibility_codes_v2",
                latency_ms=trace["latency_ms"],
                interaction_budget_seconds=trace["interaction_budget_seconds"],
            ) from None
        trace = {
            **trace,
            "region_count": len(assessment.regions),
            "complete_visibility": assessment.complete_and_confident,
            "issue_codes": sorted(assessment.issue_codes),
        }
        return VisionInspection(assessment=assessment, provider_trace=trace)

    async def inspect_catalog_asset(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        allowed_slot: Slot,
    ) -> tuple[CatalogAssetAssessment, dict[str, Any]]:
        """Assess one image while keeping identity and provenance server-owned."""
        if not self._valid_catalog_image_input(
            image_bytes, mime_type, allowed_slot
        ):
            raise self._catalog_failure("input_rejected") from None
        if self.settings.vision_force_failure or not self.settings.cpa_text_enabled:
            raise self._catalog_failure("provider_unavailable") from None

        raw, request_trace = await self._request_json(
            operation="catalog_asset_assessment",
            image_bytes=image_bytes,
            mime_type=mime_type,
            allowed_values={
                "slot": (allowed_slot,),
                "audience": CATALOG_AUDIENCES,
                "quality_issues": CATALOG_QUALITY_SIGNALS,
            },
            schema=CATALOG_ASSESSMENT_SCHEMA,
            max_tokens=300,
        )
        if set(raw) != set(CatalogAssetAssessment.model_fields):
            raise self._catalog_failure(
                "response_schema_invalid",
                latency_ms=request_trace["latency_ms"],
            ) from None
        try:
            assessment = CatalogAssetAssessment.model_validate(raw)
        except ValidationError as error:
            locations = {
                detail["loc"][0]
                for detail in error.errors()
                if detail.get("loc")
            }
            has_extra = any(
                detail.get("type") == "extra_forbidden"
                for detail in error.errors()
            )
            if has_extra:
                reason: CatalogFailureReason = "response_schema_invalid"
            elif "audience" in locations and "audience" in raw:
                reason = "audience_rejected"
            elif "object_region" in locations:
                reason = "invalid_region"
            else:
                reason = "response_schema_invalid"
            raise self._catalog_failure(
                reason,
                latency_ms=request_trace["latency_ms"],
                resolved_model=request_trace["resolved_model"],
                model_verified=(reason not in {"response_schema_invalid"}),
            ) from None

        failure_reason: CatalogFailureReason | None = None
        if assessment.slot != allowed_slot:
            failure_reason = "slot_mismatch"
        elif assessment.audience not in CATALOG_AUDIENCES:
            failure_reason = "audience_rejected"
        elif assessment.contains_identifiable_person:
            failure_reason = "identifiable_person"
        elif assessment.confidence_band == "low":
            failure_reason = "low_confidence"
        elif assessment.quality_issues:
            failure_reason = "quality_rejected"
        if failure_reason is not None:
            raise self._catalog_failure(
                failure_reason,
                latency_ms=request_trace["latency_ms"],
                resolved_model=request_trace["resolved_model"],
                model_verified=True,
                quality_issue_count=len(assessment.quality_issues),
            ) from None

        trace = self._catalog_trace(
            status="ok",
            reason_code=None,
            latency_ms=request_trace["latency_ms"],
            resolved_model=request_trace["resolved_model"],
            model_verified=True,
            assessment_count=1,
            quality_issue_count=0,
        )
        return assessment, trace
