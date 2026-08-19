from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from threading import RLock
from typing import Literal
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .asset_service import AssetError, AssetNotFound, AssetService
from .look_service import LookError, LookNotFound, LookService
from .image_provider import GrokImageProvider
from .models import API_VERSION
from .providers import ProviderUnavailable
from .service import RecommendationNotFound, RecommendationService
from .tracing import TraceStore


class PreviewError(ValueError):
    pass


class PreviewNotFound(KeyError):
    pass


class PreviewConflict(PreviewError):
    pass


class Preview2DInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    styling_session_id: str = Field(min_length=1, max_length=160)
    look_version_id: str = Field(min_length=1, max_length=160)
    render_mode: Literal["static_2d"]
    consent: Literal[True]
    identity_asset_id: str | None = Field(default=None, min_length=1, max_length=160)
    request_id: str | None = Field(default=None, min_length=1, max_length=160)


class PreviewProviderStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "fallback"]
    attempted: bool
    requested_model: Literal["grok-imagine-image-quality"] = "grok-imagine-image-quality"
    transport_model: Literal["grok-imagine-image-quality"] = "grok-imagine-image-quality"
    request_model_pinned: bool
    cpa_trace_verified: bool
    model_reported: bool
    resolved_model: Literal["grok-imagine-image-quality"] | None = None
    model_verified: bool
    verification_basis: Literal[
        "reported_model_exact", "exact_request_with_cpa_trace"
    ] | None = None
    degraded: bool
    reason_code: str | None = None

    @model_validator(mode="after")
    def verify_evidence_contract(self) -> "PreviewProviderStatus":
        if self.status == "ok":
            if (
                not self.attempted
                or not self.request_model_pinned
                or not self.cpa_trace_verified
                or self.degraded
                or self.reason_code is not None
            ):
                raise ValueError("successful image provider evidence is inconsistent")
            if self.verification_basis == "reported_model_exact":
                if (
                    not self.model_reported
                    or not self.model_verified
                    or self.resolved_model != self.transport_model
                ):
                    raise ValueError("reported image model evidence is inconsistent")
            elif self.verification_basis == "exact_request_with_cpa_trace":
                if (
                    not self.cpa_trace_verified
                    or self.model_reported
                    or self.model_verified
                    or self.resolved_model is not None
                ):
                    raise ValueError("unreported image model evidence is inconsistent")
            else:
                raise ValueError("successful image provider requires evidence basis")
        elif (
            not self.degraded
            or self.reason_code is None
            or self.model_verified
            or self.resolved_model is not None
            or self.verification_basis is not None
        ):
            raise ValueError("fallback image provider evidence is inconsistent")
        return self


class PreviewFidelity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: Literal["not_assessed"] = "not_assessed"
    garment: Literal["style_color_reference_only"] = "style_color_reference_only"
    fit: Literal["unknown"] = "unknown"
    material: Literal["unknown"] = "unknown"
    drape: Literal["unknown"] = "unknown"


class PreviewAILabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated: Literal[True] = True
    display_label: Literal["AI生成的2D视觉参考"] = "AI生成的2D视觉参考"
    metadata_status: Literal["http_header_and_response"] = "http_header_and_response"
    disclaimer: Literal[
        "仅供风格与配色参考，不代表精确尺码、面料或垂坠"
    ] = "仅供风格与配色参考，不代表精确尺码、面料或垂坠"


class Preview2DResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: Literal["r1_demo_v1"] = API_VERSION
    preview_id: str
    request_id: str
    user_id: str
    styling_session_id: str
    look_version_id: str
    render_mode: Literal["static_2d"] = "static_2d"
    owned_garment_ids: tuple[str, ...]
    external_item_ids: tuple[str, ...] = ()
    scene_id: str
    identity_asset_id: str | None
    asset_id: str | None
    image_url: str | None
    status: Literal["succeeded", "degraded", "failed"]
    provider: PreviewProviderStatus
    fidelity: PreviewFidelity
    ai_label: PreviewAILabel = Field(default_factory=PreviewAILabel)
    fallback: dict[str, str] | None = None
    trace_id: str


class PreviewDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: Literal["r1_demo_v1"] = API_VERSION
    preview_id: str
    deleted: Literal[True] = True
    image_deleted: Literal[True] = True
    trace_id: str


class RecommendationPreview2DInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    styling_session_id: str = Field(min_length=1, max_length=160)
    request_id: str = Field(min_length=1, max_length=160)
    outfit_ids: tuple[str, ...] = Field(min_length=1, max_length=3)
    preview_request_id: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def unique_outfits(self) -> "RecommendationPreview2DInput":
        if len(set(self.outfit_ids)) != len(self.outfit_ids):
            raise ValueError("outfit_ids must be unique")
        return self


class RecommendationPreview2DItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outfit_id: str
    preview_id: str
    status: Literal["succeeded", "degraded", "failed"]
    image_url: str | None
    owned_garment_ids: tuple[str, ...]
    provider: PreviewProviderStatus
    fidelity: PreviewFidelity
    ai_label: PreviewAILabel = Field(default_factory=PreviewAILabel)
    fallback: dict[str, str] | None = None
    trace_id: str


class RecommendationPreview2DBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: Literal["r1_demo_v1"] = API_VERSION
    preview_request_id: str
    request_id: str
    styling_session_id: str
    previews: tuple[RecommendationPreview2DItem, ...]


class Preview2DService:
    """Synchronous Demo adapter for owner-bound, static-only 2D previews."""

    def __init__(
        self,
        looks: LookService,
        assets: AssetService,
        provider: GrokImageProvider,
        traces: TraceStore,
    ) -> None:
        self.looks = looks
        self.assets = assets
        self.provider = provider
        self.traces = traces
        self._previews: dict[str, Preview2DResponse] = {}
        self._requests: dict[str, tuple[str, Preview2DResponse]] = {}
        self._deleted_requests: set[str] = set()
        self._lock = RLock()
        self._generation_lock = asyncio.Lock()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _fingerprint(payload: Preview2DInput) -> str:
        canonical = json.dumps(
            payload.model_dump(exclude={"request_id"}),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _prompt(garments, scene) -> str:
        item_lines = [
            (
                f"- {item.slot}: {item.name}; color={item.color}; "
                f"material={item.material}; fit={item.fit}; styles={','.join(item.styles)}"
            )
            for item in garments
        ]
        return (
            "生成一张正方形、单帧、静态2D服装造型参考图。"
            "使用中性无身份模特或干净平铺，不生成或模仿任何真人身份，不美颜或改变身体。"
            "准确保留以下服装的品类、主色和整体廓形；不要在图中印出内部ID、品牌、价格或文字。"
            f"场合={scene.occasion}；目标={','.join(scene.goals)}。\n"
            + "\n".join(item_lines)
            + "\n只输出静态2D图片。禁止3D模型、360度展示、动画、视频、分镜或运动模糊。"
        )

    def _retry(self, request_id: str, payload: Preview2DInput) -> Preview2DResponse | None:
        fingerprint = self._fingerprint(payload)
        with self._lock:
            if request_id in self._deleted_requests:
                raise PreviewConflict("request_id belongs to a deleted preview")
            stored = self._requests.get(request_id)
            if stored is None:
                return None
            stored_fingerprint, response = stored
            if stored_fingerprint != fingerprint:
                raise PreviewConflict("request_id reuse conflicts with the preview request")
            return response.model_copy(deep=True)

    def _fallback_provider_status(self, reason_code: str) -> PreviewProviderStatus:
        evidence = self.provider.health()
        return PreviewProviderStatus(
            status="fallback",
            attempted=bool(evidence.get("attempted")),
            request_model_pinned=bool(evidence.get("request_model_pinned")),
            cpa_trace_verified=bool(evidence.get("cpa_trace_verified")),
            model_reported=bool(evidence.get("model_reported")),
            resolved_model=None,
            model_verified=False,
            verification_basis=None,
            degraded=True,
            reason_code=reason_code,
        )

    async def generate(self, payload: Preview2DInput) -> Preview2DResponse:
        async with self._generation_lock:
            request_id = payload.request_id or self._new_id("previewreq")
            retry = self._retry(request_id, payload)
            if retry is not None:
                return retry
            try:
                version, scene, garments = self.looks.preview_source(
                    payload.user_id,
                    payload.styling_session_id,
                    payload.look_version_id,
                )
            except LookNotFound as exc:
                raise PreviewNotFound("Look not found") from exc
            except LookError as exc:
                # Revalidate the immutable Look against current wardrobe state
                # before any trace/provider work.  Do not expose internal IDs or
                # hard-filter implementation details through the API response.
                raise PreviewError(
                    "当前 Look 包含不可用或不符合硬约束的衣物，无法生成 2D 参考。"
                ) from exc
            if payload.identity_asset_id is not None:
                try:
                    self.assets.get_owned(
                        payload.identity_asset_id,
                        payload.user_id,
                        payload.styling_session_id,
                    )
                except AssetNotFound as exc:
                    raise PreviewNotFound("identity asset not found") from exc

            preview_id = self._new_id("preview")
            trace_id = self._new_id("trace")
            self.traces.start(
                trace_id=trace_id,
                request_id=request_id,
                styling_session_id=payload.styling_session_id,
                query_text="static 2d preview",
                user_id=payload.user_id,
            )
            self.traces.update(
                trace_id,
                validator={
                    "component": "preview_static_2d",
                    "render_mode": "static_2d",
                    "look_version_id": version.look_version_id,
                    "owned_garment_ids": list(version.item_ids),
                    "all_ids_grounded": True,
                    "identity_asset_provided": payload.identity_asset_id is not None,
                    "identity_asset_sent_to_provider": False,
                    "video_provider_calls": 0,
                    "three_d_provider_calls": 0,
                },
            )
            budget = self.provider.settings.effective_preview_2d_budget_seconds
            asset_id: str | None = None
            image_url: str | None = None
            fallback: dict[str, str] | None = None
            started = time.perf_counter()
            try:
                body, media_type, provider_meta = await asyncio.wait_for(
                    self.provider.generate_static_2d(self._prompt(garments, scene)),
                    timeout=budget,
                )
                uploaded = self.assets.upload(
                    user_id=payload.user_id,
                    styling_session_id=payload.styling_session_id,
                    angle="preview_2d",
                    media_type=media_type,
                    content=body,
                    consent_obtained=True,
                    purpose="preview_2d",
                )
                asset_id = uploaded.asset_id
                image_url = (
                    f"/preview/static-2d/{preview_id}/image?"
                    + urlencode(
                        {
                            "user_id": payload.user_id,
                            "styling_session_id": payload.styling_session_id,
                        }
                    )
                )
                provider_status = PreviewProviderStatus(
                    status="ok",
                    attempted=True,
                    requested_model="grok-imagine-image-quality",
                    transport_model="grok-imagine-image-quality",
                    request_model_pinned=bool(
                        provider_meta.get("request_model_pinned")
                    ),
                    cpa_trace_verified=bool(
                        provider_meta.get("cpa_trace_verified")
                    ),
                    model_reported=bool(provider_meta.get("model_reported")),
                    resolved_model=provider_meta.get("resolved_model"),
                    model_verified=bool(provider_meta.get("model_verified")),
                    verification_basis=provider_meta.get("verification_basis"),
                    degraded=False,
                )
                status: Literal["succeeded", "degraded", "failed"] = "succeeded"
                trace_provider = {
                    **provider_meta,
                    "interaction_budget_seconds": budget,
                    "content_logged": False,
                    "base64_logged": False,
                    "asset_id": asset_id,
                }
            except (TimeoutError, asyncio.TimeoutError):
                reason_code = "CPA_IMAGE_BUDGET_EXCEEDED"
                self.provider.mark_timeout()
                provider_status = self._fallback_provider_status(reason_code)
                status = "degraded"
                fallback = {
                    "type": "flat_lay_and_text",
                    "message": "2D生成暂不可用；保留同一Look的平铺组合与文字解释。",
                }
                trace_provider = provider_status.model_dump()
            except (ProviderUnavailable, AssetError) as exc:
                reason_code = (
                    exc.reason_code
                    if isinstance(exc, ProviderUnavailable)
                    else "CPA_PREVIEW_OUTPUT_REJECTED"
                )
                provider_status = self._fallback_provider_status(reason_code)
                status = "degraded"
                fallback = {
                    "type": "flat_lay_and_text",
                    "message": "2D生成暂不可用；保留同一Look的平铺组合与文字解释。",
                }
                trace_provider = provider_status.model_dump()

            self.traces.update(
                trace_id,
                provider={
                    **trace_provider,
                    "component": "cpa_image_static_2d",
                    "latency_ms_total": round(
                        (time.perf_counter() - started) * 1000, 2
                    ),
                    "prompt_logged": False,
                    "image_logged": False,
                },
            )
            if fallback:
                self.traces.append_fallback(
                    trace_id,
                    {
                        "component": "static_2d",
                        "fallback": fallback["type"],
                        "reason_code": provider_status.reason_code,
                    },
                )
            response = Preview2DResponse(
                preview_id=preview_id,
                request_id=request_id,
                user_id=payload.user_id,
                styling_session_id=payload.styling_session_id,
                look_version_id=payload.look_version_id,
                owned_garment_ids=tuple(version.item_ids),
                scene_id=scene.request_id,
                identity_asset_id=payload.identity_asset_id,
                asset_id=asset_id,
                image_url=image_url,
                status=status,
                provider=provider_status,
                # Static flat-lay/mannequin output never makes an identity claim,
                # regardless of whether an owner-validated reference was supplied.
                fidelity=PreviewFidelity(identity="not_assessed"),
                fallback=fallback,
                trace_id=trace_id,
            )
            with self._lock:
                self._previews[preview_id] = response.model_copy(deep=True)
                self._requests[request_id] = (
                    self._fingerprint(payload),
                    response.model_copy(deep=True),
                )
            return response

    def image(
        self, preview_id: str, user_id: str, session_id: str
    ) -> tuple[bytes, str]:
        with self._lock:
            response = self._previews.get(preview_id)
        if (
            response is None
            or response.user_id != user_id
            or response.styling_session_id != session_id
            or response.asset_id is None
        ):
            raise PreviewNotFound(preview_id)
        try:
            record, body = self.assets.read_owned(response.asset_id, user_id, session_id)
        except AssetNotFound as exc:
            raise PreviewNotFound(preview_id) from exc
        return body, record.media_type

    def delete(
        self, preview_id: str, user_id: str, session_id: str
    ) -> PreviewDeleteResponse:
        with self._lock:
            response = self._previews.get(preview_id)
        if (
            response is None
            or response.user_id != user_id
            or response.styling_session_id != session_id
            or response.asset_id is None
        ):
            raise PreviewNotFound(preview_id)
        try:
            deleted = self.assets.delete(response.asset_id, user_id, session_id)
        except AssetNotFound as exc:
            raise PreviewNotFound(preview_id) from exc
        with self._lock:
            self._previews.pop(preview_id, None)
            self._requests.pop(response.request_id, None)
            self._deleted_requests.add(response.request_id)
        return PreviewDeleteResponse(
            preview_id=preview_id,
            trace_id=deleted.trace_id,
        )


class RecommendationPreview2DService:
    """Owner-bound batch previews derived only from saved recommendations."""

    def __init__(
        self,
        recommendations: RecommendationService,
        assets: AssetService,
        provider: GrokImageProvider,
        traces: TraceStore,
    ) -> None:
        self.recommendations = recommendations
        self.assets = assets
        self.provider = provider
        self.traces = traces
        self._previews: dict[str, tuple[str, str, str | None, str, str]] = {}
        self._requests: dict[
            str, tuple[str, RecommendationPreview2DBatchResponse]
        ] = {}
        self._deleted_requests: set[str] = set()
        self._lock = RLock()
        self._generation_lock = asyncio.Lock()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _fingerprint(payload: RecommendationPreview2DInput) -> str:
        canonical = json.dumps(
            payload.model_dump(exclude={"preview_request_id"}),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _prompt(garments, scene) -> str:
        # Names, IDs, user text, memory and identity assets deliberately do not
        # cross this boundary. Only controlled fixture attributes are sent.
        item_lines = [
            (
                f"- category={item.slot}; color={item.color}; "
                f"material={item.material}; fit={item.fit}; "
                f"styles={','.join(item.styles[:3])}"
            )
            for item in garments
        ]
        return (
            "生成一张正方形、单帧、静态2D整套服装造型参考图。"
            "使用中性无身份模特或干净平铺；不得生成、模仿或推断任何真人身份，"
            "不得美颜、改变身体或声称真实上身。"
            "保留所列服装的类别、主色与整体廓形，不显示文字、品牌、价格或内部ID。"
            f"场合={scene.occasion}；受控造型目标={','.join(scene.goals[:3])}。\n"
            + "\n".join(item_lines)
            + "\n只输出单帧静态2D图片；禁止3D、360度、动画、视频、分镜和运动模糊。"
        )

    def _fallback_provider_status(self, reason_code: str) -> PreviewProviderStatus:
        # Per-call conservative evidence: never inherit mutable shared health
        # from a concurrently successful sibling.
        return PreviewProviderStatus(
            status="fallback",
            attempted=True,
            request_model_pinned=True,
            cpa_trace_verified=False,
            model_reported=False,
            resolved_model=None,
            model_verified=False,
            verification_basis=None,
            degraded=True,
            reason_code=reason_code,
        )

    def _retry(
        self, payload: RecommendationPreview2DInput
    ) -> RecommendationPreview2DBatchResponse | None:
        fingerprint = self._fingerprint(payload)
        with self._lock:
            if payload.preview_request_id in self._deleted_requests:
                raise PreviewConflict(
                    "preview_request_id belongs to a deleted preview"
                )
            stored = self._requests.get(payload.preview_request_id)
            if stored is None:
                return None
            stored_fingerprint, response = stored
            if stored_fingerprint != fingerprint:
                raise PreviewConflict(
                    "preview_request_id reuse conflicts with the preview request"
                )
            return response.model_copy(deep=True)

    def _sources(self, payload: RecommendationPreview2DInput):
        try:
            scene, recommendation = self.recommendations.get_saved_recommendation(
                payload.user_id,
                payload.styling_session_id,
                payload.request_id,
            )
        except RecommendationNotFound as exc:
            raise PreviewNotFound("saved recommendation not found") from exc
        by_id = {outfit.outfit_id: outfit for outfit in recommendation.outfits}
        if any(outfit_id not in by_id for outfit_id in payload.outfit_ids):
            raise PreviewNotFound("saved recommendation outfit not found")
        all_garments = self.recommendations.repository.list_garments(payload.user_id)
        filtered = self.recommendations.hard_filter.apply(all_garments, scene)
        sources = []
        for outfit_id in payload.outfit_ids:
            outfit = by_id[outfit_id].model_copy(deep=True)
            validated, _trace = self.recommendations.validator.validate(
                scene, [outfit], filtered.eligible_ids
            )
            if len(validated) != 1:
                raise PreviewError(
                    "推荐方向当前已不可用或不符合硬约束，无法生成 2D 参考。"
                )
            garments = [
                self.recommendations.repository.get_garment(item_id)
                for item_id in validated[0].items
            ]
            if any(garment is None for garment in garments):
                raise PreviewError("推荐方向衣物来源无法验证。")
            sources.append((outfit_id, validated[0], garments))
        return scene, sources

    async def _generate_one(
        self,
        payload: RecommendationPreview2DInput,
        scene,
        outfit_id: str,
        outfit,
        garments,
    ) -> RecommendationPreview2DItem:
        preview_id = self._new_id("recpreview")
        trace_id = self._new_id("trace")
        self.traces.start(
            trace_id=trace_id,
            request_id=payload.preview_request_id,
            styling_session_id=payload.styling_session_id,
            query_text="recommendation static 2d preview",
            user_id=payload.user_id,
        )
        self.traces.update(
            trace_id,
            validator={
                "component": "recommendation_preview_static_2d",
                "render_mode": "static_2d",
                "recommendation_request_id": payload.request_id,
                "outfit_id": outfit_id,
                "owned_garment_ids": list(outfit.items),
                "all_ids_grounded": True,
                "hard_constraints_passed": True,
                "identity_asset_provided": False,
                "identity_asset_sent_to_provider": False,
                "user_text_sent_to_provider": False,
                "memory_content_sent_to_provider": False,
                "video_provider_calls": 0,
                "three_d_provider_calls": 0,
            },
        )
        budget = self.provider.settings.effective_preview_2d_budget_seconds
        asset_id: str | None = None
        image_url: str | None = None
        fallback: dict[str, str] | None = None
        started = time.perf_counter()
        try:
            body, media_type, provider_meta = await asyncio.wait_for(
                self.provider.generate_static_2d(self._prompt(garments, scene)),
                timeout=budget,
            )
            uploaded = self.assets.upload(
                user_id=payload.user_id,
                styling_session_id=payload.styling_session_id,
                angle="recommendation_preview_2d",
                media_type=media_type,
                content=body,
                consent_obtained=True,
                purpose="preview_2d",
            )
            asset_id = uploaded.asset_id
            image_url = (
                f"/recommend/previews/static-2d/{preview_id}/image?"
                + urlencode(
                    {
                        "user_id": payload.user_id,
                        "styling_session_id": payload.styling_session_id,
                    }
                )
            )
            provider_status = PreviewProviderStatus(
                status="ok",
                attempted=True,
                requested_model="grok-imagine-image-quality",
                transport_model="grok-imagine-image-quality",
                request_model_pinned=bool(
                    provider_meta.get("request_model_pinned")
                ),
                cpa_trace_verified=bool(provider_meta.get("cpa_trace_verified")),
                model_reported=bool(provider_meta.get("model_reported")),
                resolved_model=provider_meta.get("resolved_model"),
                model_verified=bool(provider_meta.get("model_verified")),
                verification_basis=provider_meta.get("verification_basis"),
                degraded=False,
            )
            status: Literal["succeeded", "degraded", "failed"] = "succeeded"
            trace_provider = {
                **provider_meta,
                "interaction_budget_seconds": budget,
                "content_logged": False,
                "base64_logged": False,
                "asset_id": asset_id,
            }
        except (TimeoutError, asyncio.TimeoutError):
            reason_code = "CPA_IMAGE_BUDGET_EXCEEDED"
            self.provider.mark_timeout()
            provider_status = self._fallback_provider_status(reason_code)
            status = "degraded"
            fallback = {
                "type": "wardrobe_cards_and_text",
                "message": "2D生成暂不可用；保留同一方向的衣橱卡片与文字解释。",
            }
            trace_provider = provider_status.model_dump()
        except Exception as exc:
            reason_code = (
                exc.reason_code
                if isinstance(exc, ProviderUnavailable)
                else "CPA_PREVIEW_OUTPUT_REJECTED"
                if isinstance(exc, (AssetError, ValueError))
                else "CPA_IMAGE_PROVIDER_UNAVAILABLE"
            )
            provider_status = self._fallback_provider_status(reason_code)
            status = "degraded"
            fallback = {
                "type": "wardrobe_cards_and_text",
                "message": "2D生成暂不可用；保留同一方向的衣橱卡片与文字解释。",
            }
            trace_provider = provider_status.model_dump()

        self.traces.update(
            trace_id,
            provider={
                **trace_provider,
                "component": "cpa_image_recommendation_static_2d",
                "latency_ms_total": round(
                    (time.perf_counter() - started) * 1000, 2
                ),
                "prompt_logged": False,
                "image_logged": False,
                "raw_receipt_logged": False,
            },
        )
        if fallback:
            self.traces.append_fallback(
                trace_id,
                {
                    "component": "recommendation_static_2d",
                    "fallback": fallback["type"],
                    "reason_code": provider_status.reason_code,
                },
            )
        item = RecommendationPreview2DItem(
            outfit_id=outfit_id,
            preview_id=preview_id,
            status=status,
            image_url=image_url,
            owned_garment_ids=tuple(outfit.items),
            provider=provider_status,
            fidelity=PreviewFidelity(identity="not_assessed"),
            fallback=fallback,
            trace_id=trace_id,
        )
        with self._lock:
            self._previews[preview_id] = (
                payload.user_id,
                payload.styling_session_id,
                asset_id,
                payload.preview_request_id,
                trace_id,
            )
        return item

    async def generate(
        self, payload: RecommendationPreview2DInput
    ) -> RecommendationPreview2DBatchResponse:
        async with self._generation_lock:
            retry = self._retry(payload)
            if retry is not None:
                return retry
            # Resolve and revalidate the complete batch before the first image
            # call so a mixed valid/unknown request cannot probe or spend.
            scene, sources = self._sources(payload)
            previews = await asyncio.gather(
                *(
                    self._generate_one(
                        payload, scene, outfit_id, outfit, garments
                    )
                    for outfit_id, outfit, garments in sources
                )
            )
            response = RecommendationPreview2DBatchResponse(
                preview_request_id=payload.preview_request_id,
                request_id=payload.request_id,
                styling_session_id=payload.styling_session_id,
                previews=tuple(previews),
            )
            with self._lock:
                self._requests[payload.preview_request_id] = (
                    self._fingerprint(payload),
                    response.model_copy(deep=True),
                )
            return response

    def image(
        self, preview_id: str, user_id: str, session_id: str
    ) -> tuple[bytes, str]:
        with self._lock:
            record = self._previews.get(preview_id)
        if (
            record is None
            or record[0] != user_id
            or record[1] != session_id
            or record[2] is None
        ):
            raise PreviewNotFound(preview_id)
        try:
            asset, body = self.assets.read_owned(record[2], user_id, session_id)
        except AssetNotFound as exc:
            raise PreviewNotFound(preview_id) from exc
        return body, asset.media_type

    def delete(
        self, preview_id: str, user_id: str, session_id: str
    ) -> PreviewDeleteResponse:
        with self._lock:
            record = self._previews.get(preview_id)
        if (
            record is None
            or record[0] != user_id
            or record[1] != session_id
            or record[2] is None
        ):
            raise PreviewNotFound(preview_id)
        try:
            deleted = self.assets.delete(record[2], user_id, session_id)
        except AssetNotFound as exc:
            raise PreviewNotFound(preview_id) from exc
        with self._lock:
            self._previews.pop(preview_id, None)
            self._requests.pop(record[3], None)
            self._deleted_requests.add(record[3])
        return PreviewDeleteResponse(preview_id=preview_id, trace_id=deleted.trace_id)
