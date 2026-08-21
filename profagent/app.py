from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .asset_service import (
    MAX_ASSET_BYTES,
    AssetError,
    AssetDeleteResponse,
    AssetNotFound,
    AssetService,
    AssetUploadResponse,
)
from .catalog import CatalogService
from .config import Settings
from .dialogue import DialogueConflict, DialogueNotFound, DialogueService
from .feedback_service import (
    FeedbackError,
    FeedbackService,
    RecommendationFeedbackInput,
    RecommendationFeedbackResponse,
)
from .image_provider import GrokImageProvider
from .look_models import (
    AdjustLookInput,
    AdjustLookResponse,
    FinalLook,
    FinalizeLookInput,
    LookChainResponse,
    LookCreateInput,
    LookVersion,
    Scorecard,
    ScorecardInput,
)
from .look_service import LookConflict, LookError, LookNotFound, LookService
from .models import (
    API_VERSION,
    DialogueTurnInput,
    DialogueTurnResponse,
    HealthResponse,
    InitialRecommendation,
    RecommendationInput,
    SceneParseInput,
    SceneRequest,
    TeamHomeResponse,
    TeamMember,
    WardrobePatch,
    WardrobePatchResponse,
    WardrobeResponse,
)
from .providers import DenseAdapter, GrokLLMProvider, ProviderUnavailable
from .preview import (
    Preview2DInput,
    Preview2DResponse,
    Preview2DService,
    PreviewConflict,
    PreviewDeleteResponse,
    PreviewError,
    PreviewNotFound,
    RecommendationPreview2DBatchResponse,
    RecommendationPreview2DInput,
    RecommendationPreview2DService,
)
from .memory_service import (
    MemoryConfirmInput,
    MemoryDeleteResponse,
    MemoryError,
    MemoryListResponse,
    MemoryNotFound,
    MemoryOperationResponse,
    MemoryProposeInput,
    MemoryService,
)
from .memory_candidates import (
    MemoryCandidateConflict,
    MemoryCandidateDecisionError,
    MemoryCandidateDecisionInput,
    MemoryCandidateDecisionResponse,
    MemoryCandidateError,
    MemoryCandidateExtractInput,
    MemoryCandidateExtractResponse,
    MemoryCandidateNotFound,
    MemoryCandidateService,
)
from .repository import FixtureRepository
from .retrieval import HardFilter, HybridRetriever, OutfitAssembler, RecommendationValidator
from .scene import SceneParser, SceneStateConflict, SceneStateStore
from .service import RecommendationNotFound, RecommendationService
from .tracing import TraceStore
from .vision import VisionAdapter
from .wardrobe_assets import (
    WardrobeCatalogAssetNotFound,
    WardrobeCatalogAssetService,
    WardrobeCatalogAssetsResponse,
)


class AppServices:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.repository = FixtureRepository(settings.root_dir)
        self.traces = TraceStore()
        self.state = SceneStateStore()
        self.assets = AssetService(settings.root_dir, self.traces)
        self.vision = VisionAdapter(settings)
        self.memory = MemoryService(
            self.traces,
            database_url=settings.database_url,
            root_dir=settings.root_dir,
        )
        self.llm = GrokLLMProvider(settings)
        self.memory_candidates = MemoryCandidateService(
            memory=self.memory,
            provider=self.llm,
            session_is_active=lambda user_id, session_id: (
                (scene := self.state.by_session(session_id)) is not None
                and scene.user_id == user_id
            ),
            ttl_seconds=settings.effective_dialogue_ttl_seconds,
        )
        self.image_provider = GrokImageProvider(settings)
        self.dense = DenseAdapter(settings)
        self.hard_filter = HardFilter()
        self.retriever = HybridRetriever(self.dense)
        self.assembler = OutfitAssembler(self.repository, self.hard_filter)
        self.validator = RecommendationValidator(self.repository, self.hard_filter)
        self.catalog = CatalogService(
            self.repository, force_failure=settings.catalog_force_failure
        )
        self.scene_parser = SceneParser(
            settings,
            self.repository,
            self.llm,
            self.traces,
            self.state,
            self.memory,
        )
        self.recommendations = RecommendationService(
            settings,
            self.repository,
            self.state,
            self.traces,
            self.hard_filter,
            self.retriever,
            self.assembler,
            self.validator,
            self.catalog,
            self.memory,
        )
        self.feedback = FeedbackService(
            self.recommendations, self.memory, self.traces
        )
        self.dialogue = DialogueService(
            self.scene_parser,
            self.recommendations,
            self.llm,
            self.traces,
            self.memory_candidates,
        )
        self.looks = LookService(
            settings,
            self.repository,
            self.state,
            self.recommendations,
            self.assets,
            self.vision,
            self.hard_filter,
            self.traces,
        )
        self.previews = Preview2DService(
            self.looks,
            self.assets,
            self.image_provider,
            self.traces,
        )
        self.recommendation_previews = RecommendationPreview2DService(
            self.recommendations,
            self.assets,
            self.image_provider,
            self.traces,
        )
        self.wardrobe_assets = WardrobeCatalogAssetService(
            settings, self.repository
        )


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    recoverable: bool = True,
    fallback: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "api_version": API_VERSION,
            "error": {
                "code": code,
                "message": message,
                "details": jsonable_encoder(details or {}),
                "trace_id": None,
                "recoverable": recoverable,
                "fallback": fallback,
            },
        },
    )


def _safe_validation_issues(
    exc: RequestValidationError | ValidationError,
) -> list[dict[str, Any]]:
    """Expose validation metadata without reflecting rejected request values."""

    return [
        {key: issue[key] for key in ("type", "loc", "msg") if key in issue}
        for issue in exc.errors()
    ]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    services = AppServices(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            services.assets.close()
            services.memory.close()

    app = FastAPI(
        title="ProfAgent R1 Stylist Demo",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.services = services
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            422,
            "REQUEST_VALIDATION_ERROR",
            "请求字段不符合 API 合同。",
            # Pydantic includes rejected field values by default. Those values
            # can contain precisely the private text that a validation boundary
            # refused, so the public error contract allowlists metadata and
            # never reflects request input or validator context.
            {"issues": _safe_validation_issues(exc)},
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        _request: Request, exc: HTTPException
    ) -> JSONResponse:
        return _error_response(
            exc.status_code,
            "RESOURCE_NOT_FOUND" if exc.status_code == 404 else "REQUEST_REJECTED",
            str(exc.detail),
            recoverable=exc.status_code < 500,
        )

    @app.exception_handler(ValidationError)
    async def model_validation_handler(
        _request: Request, exc: ValidationError
    ) -> JSONResponse:
        return _error_response(
            422,
            "SCENE_VALIDATION_ERROR",
            "场景字段不符合 API 合同。",
            {"issues": _safe_validation_issues(exc)},
        )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        llm_health = await services.llm.health()
        image_health = services.image_provider.health()
        vision_health = services.vision.health()
        vision_degraded = bool(
            vision_health.get("enabled")
            and vision_health.get("status") not in {"ok", "unverified"}
        )
        degraded = (
            (settings.cpa_text_enabled and not llm_health["available"])
            or (settings.dense_enabled and settings.dense_force_failure)
            or (settings.catalog_enabled and settings.catalog_force_failure)
            or settings.vision_force_failure
            or vision_degraded
            or (
                settings.cpa_image_enabled and not image_health.get("available")
            )
        )
        return HealthResponse(
            status="degraded" if degraded else "ok",
            ready=True,
            data={"version": settings.data_version, "counts": services.repository.counts},
            providers={
                "llm": llm_health,
                "dense": {
                    "enabled": settings.dense_enabled,
                    "available": not settings.dense_force_failure,
                    "version": services.dense.version,
                },
                "catalog": {
                    "enabled": settings.catalog_enabled,
                    "available": not settings.catalog_force_failure,
                    "source": "fixtures_v1.0_mock",
                },
                "vision": vision_health,
                "image": image_health,
                "static_2d": {
                    **image_health,
                    "output": "single_frame_png_jpeg_webp",
                },
            },
            capabilities={
                "rule_recommendation": True,
                "shopping": settings.catalog_enabled,
                "static_2d": settings.cpa_image_enabled,
                "video": False,
                "three_d": False,
            },
        )

    @app.post("/scene/parse", response_model=SceneRequest)
    async def parse_scene(payload: SceneParseInput) -> SceneRequest:
        try:
            return await services.scene_parser.parse(payload)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown user_id")
        except SceneStateConflict as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/dialogue/turn", response_model=DialogueTurnResponse)
    async def dialogue_turn(payload: DialogueTurnInput) -> DialogueTurnResponse:
        try:
            return await services.dialogue.turn(payload)
        except DialogueNotFound:
            raise HTTPException(status_code=404, detail="dialogue session not found")
        except DialogueConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown user_id")
        except SceneStateConflict as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/recommend", response_model=InitialRecommendation)
    async def recommend(payload: dict[str, Any] = Body(...)) -> InitialRecommendation:
        try:
            # The service supports a compact {request_id} form. Validate that
            # shortcut before its lookup path can stringify an arbitrary object.
            if set(payload) == {"request_id"}:
                RecommendationInput.model_validate(payload)
            scene = services.recommendations.resolve_payload(payload)
            return services.recommendations.recommend(scene)
        except ValidationError:
            # Pydantic ValidationError is also a ValueError. Re-raise it so the
            # safe validation handler strips rejected input/ctx instead of the
            # HTTPException handler reflecting str(exc).
            raise
        except RecommendationNotFound:
            raise HTTPException(status_code=404, detail="recommendation request not found")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post(
        "/recommend/feedback", response_model=RecommendationFeedbackResponse
    )
    async def recommend_feedback(
        payload: RecommendationFeedbackInput,
    ) -> RecommendationFeedbackResponse:
        try:
            return services.feedback.submit(payload)
        except RecommendationNotFound:
            # Missing and cross-owner IDs deliberately share one response.
            raise HTTPException(
                status_code=404, detail="saved recommendation outfit not found"
            )
        except FeedbackError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.get("/team/home", response_model=TeamHomeResponse)
    async def team_home(user_id: str = Query(...)) -> TeamHomeResponse:
        if services.repository.get_user(user_id) is None:
            raise HTTPException(status_code=404, detail="unknown user_id")
        return TeamHomeResponse(
            team={"team_id": "personal_team", "name": "我的私人团队"},
            members=[
                TeamMember(
                    member_id="stylist",
                    persona_id="stylist",
                    name="Stylist",
                    status="available",
                    boundary="只处理穿搭、衣橱与造型共创；不诊断、不评价人的身体或价值。",
                    capabilities=[
                        "scene_understanding",
                        "grounded_wardrobe_recommendation",
                        "styling_session",
                    ],
                )
            ],
            active_sessions=services.state.active_sessions(user_id),
            unavailable_capabilities=[
                "第二专业成员（未上线）",
                "2D 购前试演（未上线）",
                "真人专家（未上线）",
                "真实购物与支付（未上线）",
                "3D/360°/视频（未上线）",
            ],
        )

    @app.get("/wardrobe", response_model=WardrobeResponse)
    async def wardrobe(
        user_id: str = Query(...),
        slot: str | None = None,
        status: str | None = None,
        season: str | None = None,
        color: str | None = None,
        occasion: str | None = None,
    ) -> WardrobeResponse:
        if services.repository.get_user(user_id) is None:
            raise HTTPException(status_code=404, detail="unknown user_id")
        items = services.repository.list_garments(
            user_id,
            slot=slot,
            status=status,
            season=season,
            color=color,
            occasion=occasion,
        )
        all_items = services.repository.list_garments(user_id)
        return WardrobeResponse(
            user_id=user_id,
            items=items,
            count=len(items),
            available_filters={
                "slot": sorted({item.slot for item in all_items}),
                "status": sorted({item.status for item in all_items}),
                "season": sorted({season for item in all_items for season in item.seasons}),
                "color": sorted({item.color for item in all_items}),
                "occasion": sorted(
                    {occasion for item in all_items for occasion in item.occasions}
                ),
            },
        )

    @app.patch("/wardrobe/{garment_id}", response_model=WardrobePatchResponse)
    async def patch_wardrobe(
        garment_id: str,
        payload: WardrobePatch,
        user_id: str = Query(...),
    ) -> WardrobePatchResponse:
        updated = services.repository.update_garment(garment_id, user_id, payload)
        if updated is None:
            raise HTTPException(status_code=404, detail="garment not found for user")
        return WardrobePatchResponse(item=updated)

    @app.get(
        "/wardrobe/catalog-assets", response_model=WardrobeCatalogAssetsResponse
    )
    async def wardrobe_catalog_assets(
        user_id: str = Query(..., min_length=1, max_length=128),
    ) -> WardrobeCatalogAssetsResponse:
        try:
            return services.wardrobe_assets.list_for_user(user_id)
        except WardrobeCatalogAssetNotFound:
            raise HTTPException(status_code=404, detail="wardrobe catalog assets not found")

    @app.get("/wardrobe/{garment_id}/catalog-image")
    async def wardrobe_catalog_image(
        garment_id: str,
        user_id: str = Query(..., min_length=1, max_length=128),
        asset_version: str = Query(..., min_length=1, max_length=80),
        content_sha256: str = Query(
            ..., min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
        ),
    ) -> Response:
        try:
            body, media_type = services.wardrobe_assets.read_owned(
                garment_id, user_id, asset_version, content_sha256
            )
        except WardrobeCatalogAssetNotFound:
            # Missing and cross-owner lookups intentionally share one response.
            raise HTTPException(status_code=404, detail="wardrobe catalog image not found")
        return Response(
            content=body,
            media_type=media_type,
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
                "X-AI-Generated": "true",
                "X-AI-Requested-Model": "grok-imagine-image-quality",
                "X-AI-Model-Reported": "false",
                "X-AI-Verification-Basis": "batch_exact_request_contract",
            },
        )

    @app.post("/assets", response_model=AssetUploadResponse)
    async def upload_asset(
        user_id: str = Form(...),
        styling_session_id: str = Form(...),
        angle: str = Form(...),
        consent: Literal["true"] = Form(...),
        purpose: Literal["styling_assessment"] = Form(...),
        file: UploadFile = File(...),
    ) -> AssetUploadResponse:
        if services.repository.get_user(user_id) is None:
            raise HTTPException(status_code=404, detail="unknown user_id")
        session = services.state.by_session(styling_session_id)
        if session is None or session.user_id != user_id:
            raise HTTPException(status_code=404, detail="styling session not found for user")
        content = await file.read(MAX_ASSET_BYTES + 1)
        await file.close()
        try:
            return services.assets.upload(
                user_id=user_id,
                styling_session_id=styling_session_id,
                angle=angle,
                media_type=file.content_type,
                content=content,
                consent_obtained=True,
                purpose=purpose,
            )
        except AssetError as exc:
            status = 413 if "5MB" in str(exc) else 422
            raise HTTPException(status_code=status, detail=str(exc))

    @app.delete("/assets/{asset_id}", response_model=AssetDeleteResponse)
    async def delete_asset(
        asset_id: str,
        user_id: str = Query(...),
        styling_session_id: str = Query(...),
    ) -> AssetDeleteResponse:
        session = services.state.by_session(styling_session_id)
        if session is None or session.user_id != user_id:
            raise HTTPException(status_code=404, detail="asset not found")
        try:
            return services.assets.delete(asset_id, user_id, styling_session_id)
        except AssetNotFound:
            raise HTTPException(status_code=404, detail="asset not found")

    @app.post("/look", response_model=LookVersion)
    async def create_look(payload: LookCreateInput) -> LookVersion:
        try:
            return services.looks.create(payload)
        except LookNotFound:
            raise HTTPException(status_code=404, detail="Look source not found")
        except LookConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except LookError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.get("/look", response_model=LookChainResponse)
    async def get_look(
        user_id: str = Query(...),
        styling_session_id: str = Query(...),
        look_version_id: str | None = Query(default=None),
    ) -> LookChainResponse:
        try:
            return services.looks.chain(user_id, styling_session_id, look_version_id)
        except LookNotFound:
            raise HTTPException(status_code=404, detail="Look or styling session not found")

    @app.post("/scorecard", response_model=Scorecard)
    async def scorecard(payload: ScorecardInput) -> Scorecard:
        try:
            return await services.looks.score(payload)
        except LookNotFound:
            raise HTTPException(status_code=404, detail="Look not found")
        except LookConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except LookError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/adjust", response_model=AdjustLookResponse)
    async def adjust_look(payload: AdjustLookInput) -> AdjustLookResponse:
        try:
            return services.looks.adjust(payload)
        except LookNotFound:
            raise HTTPException(status_code=404, detail="Look not found")
        except LookConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except LookError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/finalize", response_model=FinalLook)
    async def finalize_look(payload: FinalizeLookInput) -> FinalLook:
        try:
            return services.looks.finalize(payload)
        except LookNotFound:
            raise HTTPException(status_code=404, detail="Look not found")
        except LookConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except LookError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/preview/static-2d", response_model=Preview2DResponse)
    async def preview_static_2d(payload: Preview2DInput) -> Preview2DResponse:
        try:
            return await services.previews.generate(payload)
        except PreviewNotFound:
            raise HTTPException(status_code=404, detail="Look or identity asset not found")
        except PreviewConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except PreviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post(
        "/recommend/previews/static-2d",
        response_model=RecommendationPreview2DBatchResponse,
    )
    async def recommendation_previews_static_2d(
        payload: RecommendationPreview2DInput,
    ) -> RecommendationPreview2DBatchResponse:
        try:
            return await services.recommendation_previews.generate(payload)
        except PreviewNotFound:
            # Unknown and cross-owner recommendation/outfit identifiers share
            # one response and never reach the image provider.
            raise HTTPException(
                status_code=404, detail="saved recommendation outfit not found"
            )
        except PreviewConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except PreviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.get("/preview/static-2d/{preview_id}/image")
    async def preview_static_2d_image(
        preview_id: str,
        user_id: str = Query(...),
        styling_session_id: str = Query(...),
    ) -> Response:
        try:
            body, media_type = services.previews.image(
                preview_id, user_id, styling_session_id
            )
        except PreviewNotFound:
            raise HTTPException(status_code=404, detail="preview image not found")
        return Response(
            content=body,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "X-AI-Generated": "true",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/recommend/previews/static-2d/{preview_id}/image")
    async def recommendation_preview_static_2d_image(
        preview_id: str,
        user_id: str = Query(...),
        styling_session_id: str = Query(...),
    ) -> Response:
        try:
            body, media_type = services.recommendation_previews.image(
                preview_id, user_id, styling_session_id
            )
        except PreviewNotFound:
            raise HTTPException(status_code=404, detail="preview image not found")
        return Response(
            content=body,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "X-AI-Generated": "true",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.delete(
        "/preview/static-2d/{preview_id}", response_model=PreviewDeleteResponse
    )
    async def delete_preview_static_2d(
        preview_id: str,
        user_id: str = Query(...),
        styling_session_id: str = Query(...),
    ) -> PreviewDeleteResponse:
        try:
            return services.previews.delete(
                preview_id, user_id, styling_session_id
            )
        except PreviewNotFound:
            raise HTTPException(status_code=404, detail="preview not found")

    @app.delete(
        "/recommend/previews/static-2d/{preview_id}",
        response_model=PreviewDeleteResponse,
    )
    async def delete_recommendation_preview_static_2d(
        preview_id: str,
        user_id: str = Query(...),
        styling_session_id: str = Query(...),
    ) -> PreviewDeleteResponse:
        try:
            return services.recommendation_previews.delete(
                preview_id, user_id, styling_session_id
            )
        except PreviewNotFound:
            raise HTTPException(status_code=404, detail="preview not found")

    @app.post(
        "/memory/candidates/extract",
        response_model=MemoryCandidateExtractResponse,
    )
    async def extract_memory_candidates(
        payload: MemoryCandidateExtractInput,
    ) -> MemoryCandidateExtractResponse:
        if services.repository.get_user(payload.user_id) is None:
            raise HTTPException(status_code=404, detail="unknown user_id")
        if payload.styling_session_id:
            session = services.state.by_session(payload.styling_session_id)
            if session is None or session.user_id != payload.user_id:
                raise HTTPException(
                    status_code=404, detail="styling session not found for user"
                )
        try:
            result = await services.memory_candidates.extract(payload)
            if not isinstance(result, MemoryCandidateExtractResponse):
                raise MemoryCandidateError(
                    "memory candidate response contract mismatch"
                )
            return result
        except MemoryCandidateConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ProviderUnavailable:
            raise HTTPException(
                status_code=503,
                detail="memory candidate provider unavailable",
            )
        except MemoryCandidateError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post(
        "/memory/candidates/{candidate_id}/decide",
        response_model=MemoryCandidateDecisionResponse,
    )
    async def decide_memory_candidate(
        candidate_id: str,
        payload: MemoryCandidateDecisionInput,
    ) -> MemoryCandidateDecisionResponse:
        if services.repository.get_user(payload.user_id) is None:
            raise HTTPException(status_code=404, detail="memory candidate not found")
        try:
            return services.memory_candidates.decide(candidate_id, payload)
        except MemoryCandidateNotFound:
            raise HTTPException(status_code=404, detail="memory candidate not found")
        except MemoryCandidateConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except (MemoryCandidateDecisionError, MemoryCandidateError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/memory/propose", response_model=MemoryOperationResponse)
    async def propose_memory(payload: MemoryProposeInput) -> MemoryOperationResponse:
        if services.repository.get_user(payload.user_id) is None:
            raise HTTPException(status_code=404, detail="unknown user_id")
        if payload.styling_session_id:
            session = services.state.by_session(payload.styling_session_id)
            if session is None or session.user_id != payload.user_id:
                raise HTTPException(
                    status_code=404, detail="styling session not found for user"
                )
        return services.memory.propose(payload)

    @app.post(
        "/memory/{proposal_id}/confirm", response_model=MemoryOperationResponse
    )
    async def confirm_memory(
        proposal_id: str, payload: MemoryConfirmInput
    ) -> MemoryOperationResponse:
        if services.repository.get_user(payload.user_id) is None:
            raise HTTPException(status_code=404, detail="unknown user_id")
        try:
            return services.memory.confirm(proposal_id, payload)
        except MemoryNotFound:
            raise HTTPException(status_code=404, detail="memory proposal not found")
        except MemoryError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.get("/memory", response_model=MemoryListResponse)
    async def list_memory(
        user_id: str = Query(...),
        namespace: Literal["shared", "stylist"] | None = Query(default=None),
    ) -> MemoryListResponse:
        if services.repository.get_user(user_id) is None:
            raise HTTPException(status_code=404, detail="unknown user_id")
        return services.memory.list(user_id, namespace)

    @app.delete("/memory", response_model=MemoryDeleteResponse)
    async def delete_memory(
        user_id: str = Query(...), memory_id: str = Query(...)
    ) -> MemoryDeleteResponse:
        try:
            return services.memory.delete(user_id, memory_id)
        except MemoryNotFound:
            raise HTTPException(status_code=404, detail="memory not found")

    @app.delete("/memory/{memory_id}", response_model=MemoryDeleteResponse)
    async def delete_memory_by_id(
        memory_id: str, user_id: str = Query(...)
    ) -> MemoryDeleteResponse:
        try:
            return services.memory.delete(user_id, memory_id)
        except MemoryNotFound:
            raise HTTPException(status_code=404, detail="memory not found")

    @app.get("/trace/{trace_id}")
    async def trace_by_id(
        trace_id: str, user_id: str = Query(...)
    ) -> dict[str, Any]:
        if not services.traces.belongs_to(trace_id, user_id):
            raise HTTPException(status_code=404, detail="trace not found")
        trace = services.traces.get(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return {"api_version": API_VERSION, "trace": trace.model_dump(mode="json")}

    @app.get("/trace")
    async def traces_by_session(
        user_id: str = Query(...), styling_session_id: str = Query(...)
    ) -> dict[str, Any]:
        session = services.state.by_session(styling_session_id)
        if session is None or session.user_id != user_id:
            raise HTTPException(status_code=404, detail="styling session not found")
        traces = services.traces.list_for_session(styling_session_id)
        return {
            "api_version": API_VERSION,
            "traces": [trace.model_dump(mode="json") for trace in traces],
            "count": len(traces),
        }

    @app.post("/eval/run")
    async def run_frozen_eval() -> dict[str, Any]:
        # Lazy import avoids an app/evaluator import cycle. The evaluator creates
        # an isolated offline service graph, so this endpoint never reaches CPA.
        from .eval import run_evaluation

        return await run_evaluation(settings.root_dir)

    web_dir = Path(settings.root_dir) / "web"
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


app = create_app()
