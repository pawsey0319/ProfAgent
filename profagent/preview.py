from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from threading import RLock
from typing import Literal
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field

from .asset_service import AssetError, AssetNotFound, AssetService
from .look_service import LookError, LookNotFound, LookService
from .models import API_VERSION
from .providers import GrokLLMProvider, ProviderUnavailable
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
    requested_model: Literal["grok4.5"] = "grok4.5"
    transport_model: Literal["grok-4.5-high"] = "grok-4.5-high"
    resolved_model: str | None = None
    model_verified: bool
    degraded: bool
    reason_code: str | None = None


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


class Preview2DService:
    """Synchronous Demo adapter for owner-bound, static-only 2D previews."""

    def __init__(
        self,
        looks: LookService,
        assets: AssetService,
        provider: GrokLLMProvider,
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
                    requested_model="grok4.5",
                    transport_model="grok-4.5-high",
                    resolved_model=provider_meta.get("resolved_model"),
                    model_verified=bool(provider_meta.get("model_verified")),
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
                reason_code = "CPA_PREVIEW_BUDGET_EXCEEDED"
                self.provider.mark_interaction_failure(reason_code)
                provider_status = PreviewProviderStatus(
                    status="fallback",
                    attempted=True,
                    requested_model="grok4.5",
                    transport_model="grok-4.5-high",
                    resolved_model=None,
                    model_verified=False,
                    degraded=True,
                    reason_code=reason_code,
                )
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
                provider_status = PreviewProviderStatus(
                    status="fallback",
                    attempted=True,
                    requested_model="grok4.5",
                    transport_model="grok-4.5-high",
                    resolved_model=None,
                    model_verified=False,
                    degraded=True,
                    reason_code=reason_code,
                )
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
                    "component": "cpa_static_2d",
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
