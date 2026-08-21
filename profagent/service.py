from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Any

from pydantic import ValidationError

from .catalog import CatalogService, CatalogUnavailable
from .config import Settings
from .memory_service import MemoryService
from .models import (
    InitialRecommendation,
    RecommendationInput,
    RecommendedOutfit,
    SceneRequest,
    UICapabilities,
)
from .repository import FixtureRepository
from .retrieval import (
    HardFilter,
    HybridRetriever,
    OutfitAssembler,
    RecommendationValidator,
)
from .scene import SceneStateStore
from .tracing import TraceStore


class RecommendationNotFound(KeyError):
    pass


class RecommendationPayloadMismatch(ValueError):
    pass


class RecommendationService:
    def __init__(
        self,
        settings: Settings,
        repository: FixtureRepository,
        state: SceneStateStore,
        traces: TraceStore,
        hard_filter: HardFilter,
        retriever: HybridRetriever,
        assembler: OutfitAssembler,
        validator: RecommendationValidator,
        catalog: CatalogService,
        memory: MemoryService | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.state = state
        self.traces = traces
        self.hard_filter = hard_filter
        self.retriever = retriever
        self.assembler = assembler
        self.validator = validator
        self.catalog = catalog
        self.memory = memory
        self._saved: dict[str, InitialRecommendation] = {}
        self._saved_lock = RLock()

    def _save_recommendation(self, result: InitialRecommendation) -> None:
        with self._saved_lock:
            self._saved[result.request_id] = result.model_copy(deep=True)

    def get_saved_recommendation(
        self, user_id: str, styling_session_id: str, request_id: str
    ) -> tuple[SceneRequest, InitialRecommendation]:
        scene = self.state.by_request(request_id)
        with self._saved_lock:
            result = self._saved.get(request_id)
        if (
            scene is None
            or result is None
            or scene.user_id != user_id
            or scene.styling_session_id != styling_session_id
            or result.styling_session_id != styling_session_id
        ):
            raise RecommendationNotFound(request_id)
        return scene, result.model_copy(deep=True)

    def get_saved_outfit(
        self,
        user_id: str,
        styling_session_id: str,
        request_id: str,
        outfit_id: str,
    ) -> tuple[SceneRequest, RecommendedOutfit]:
        scene, result = self.get_saved_recommendation(
            user_id, styling_session_id, request_id
        )
        outfit = next(
            (item for item in result.outfits if item.outfit_id == outfit_id), None
        )
        if outfit is None:
            raise RecommendationNotFound(outfit_id)
        return scene, outfit.model_copy(deep=True)

    def apply_requested_outfit_count(
        self,
        scene: SceneRequest,
        recommendation: InitialRecommendation,
        requested_outfit_count: int | None,
    ) -> InitialRecommendation:
        """Apply the server-parsed current-turn count without rerunning tools."""
        if requested_outfit_count not in {None, 1, 2, 3}:
            raise ValueError("requested_outfit_count must be between 1 and 3")
        if requested_outfit_count is None:
            return recommendation
        result = recommendation.model_copy(deep=True)
        result.requested_outfit_count = requested_outfit_count
        result.outfits = result.outfits[:requested_outfit_count]
        if len(result.outfits) < requested_outfit_count:
            result.gap_explanation = (
                f"你明确需要 {requested_outfit_count} 套；当前硬约束下只能形成 "
                f"{len(result.outfits)} 套有明显差异的完整方向。"
                "没有复制方案，也没有放宽禁忌、可用状态或天气要求。"
            )
        existing = self.traces.get(scene.trace_id)
        validator = dict(existing.validator) if existing is not None else {}
        validator.update(
            {
                "requested_outfit_count": requested_outfit_count,
                "returned_outfit_count": len(result.outfits),
                "requested_count_satisfied": (
                    len(result.outfits) == requested_outfit_count
                ),
            }
        )
        self.traces.update(scene.trace_id, validator=validator)
        self._save_recommendation(result)
        return result.model_copy(deep=True)

    def reorder_validated_outfits(
        self,
        *,
        recommendation: InitialRecommendation,
        ordered_outfit_ids: tuple[str, ...],
        request_id: str,
        trace_id: str,
    ) -> InitialRecommendation:
        """Reorder only an existing validated outfit set; never rerun tools."""

        by_id = {outfit.outfit_id: outfit for outfit in recommendation.outfits}
        if (
            len(by_id) != len(recommendation.outfits)
            or len(ordered_outfit_ids) != len(recommendation.outfits)
            or set(ordered_outfit_ids) != set(by_id)
            or any(
                not (
                    outfit.validation.all_ids_grounded
                    and outfit.validation.hard_constraints_passed
                    and outfit.validation.required_slots_complete
                )
                for outfit in recommendation.outfits
            )
        ):
            raise RecommendationPayloadMismatch(
                "preference reorder must preserve the validated outfit set"
            )
        result = recommendation.model_copy(deep=True)
        result.request_id = request_id
        result.trace_id = trace_id
        result.outfits = [
            by_id[outfit_id].model_copy(deep=True)
            for outfit_id in ordered_outfit_ids
        ]
        for index, outfit in enumerate(result.outfits):
            outfit.is_primary = index == 0
        self._save_recommendation(result)
        return result.model_copy(deep=True)

    @staticmethod
    def _server_urgency(horizon: str) -> str:
        return {
            "now": "high",
            "today": "high",
            "unknown": "high",
            "soon": "medium",
            "planned": "low",
        }[horizon]

    def _enforce_gate(self, scene: SceneRequest) -> SceneRequest:
        safe = scene.model_copy(deep=True)
        safe.urgency = self._server_urgency(safe.event_horizon)  # type: ignore[assignment]
        safe.shopping_allowed = (
            safe.urgency != "high"
            and safe.intent in {"buy", "fill_gap", "browse"}
            and self.settings.catalog_enabled
            and (
                safe.urgency == "low"
                or (
                    safe.event_time is not None
                    and (safe.event_time - datetime.now().astimezone()).days >= 4
                )
            )
        )
        safe.ui_capabilities = UICapabilities(shopping_cta=safe.shopping_allowed)
        return safe

    def resolve_payload(self, payload: dict[str, Any]) -> SceneRequest:
        supplied: SceneRequest | None = None
        request_id: str | None = None
        if "scene" in payload:
            parsed = RecommendationInput.model_validate(payload)
            if parsed.scene is None:
                raise ValueError("scene is required")
            supplied = parsed.scene
            request_id = supplied.request_id
            if parsed.request_id and parsed.request_id != request_id:
                raise RecommendationPayloadMismatch("request_id does not match scene")
        elif payload.get("request_id") and len(payload) == 1:
            request_id = str(payload["request_id"])
        else:
            try:
                supplied = SceneRequest.model_validate(payload)
                request_id = supplied.request_id
            except ValidationError:
                parsed = RecommendationInput.model_validate(payload)
                request_id = parsed.request_id
        if not request_id:
            raise ValueError("request_id is required")
        authoritative = self.state.by_request(request_id)
        if authoritative is None:
            # A client-supplied complete SceneRequest never establishes authority.
            raise RecommendationNotFound(request_id)
        if supplied is not None:
            identity_fields = (
                "user_id",
                "styling_session_id",
                "trace_id",
                "team_id",
                "member_id",
                "persona_id",
            )
            if any(
                getattr(supplied, field) != getattr(authoritative, field)
                for field in identity_fields
            ):
                raise RecommendationPayloadMismatch(
                    "client scene identity does not match server scene"
                )
        # All policy, temporal, intent and constraint fields come from the
        # parse-time server record. A full scene body remains wire-compatible,
        # but cannot loosen the authoritative safety decision.
        return self._enforce_gate(authoritative)

    @staticmethod
    def _support_mode(scene: SceneRequest) -> str | None:
        text = scene.query_text
        if any(term in text for term in ("胸闷", "治疗", "治好", "医疗")):
            return "medical"
        if any(term in text for term in ("太胖", "身材缺点", "颜值", "很丑")):
            return "body"
        if scene.intent == "vent" and not any(
            term in text for term in ("帮我搭", "给我一套", "推荐", "帮我选")
        ):
            return "support_only"
        return None

    @staticmethod
    def _assistant_message(scene: SceneRequest, support_mode: str | None) -> str:
        if support_mode == "medical":
            return (
                "我不能用衣服治疗胸闷。若症状突然、严重或持续，请尽快寻求专业医疗帮助；"
                "穿搭只能围绕舒适和活动需求提供一般建议，不能替代诊疗。"
            )
        if support_mode == "body":
            return (
                "我不会评价你的身材或所谓缺点。我们可以只围绕这次场合、舒适度和你想表达的感觉来选衣服。"
            )
        if support_mode == "support_only":
            timing = "这次"
            for term in (
                "明天",
                "后天",
                "下周",
                "周末",
                "这周",
                "今天",
                "今晚",
                "现在",
            ):
                if term in scene.query_text:
                    timing = term
                    break
            return f"听起来你现在有些不安。你愿意的话，我可以先帮你把{timing}的穿搭选择缩小到一两个方向。"
        if scene.urgency == "high":
            if any(term in scene.query_text for term in ("紧张", "有点慌", "焦虑")):
                return "听起来你现在有些紧张。我们先把决定变简单：以下都只用你现在可穿的衣橱单品，先试首选方向。"
            return "先把决定变简单：以下都只用你现在可穿的衣橱单品，先试首选方向。"
        return "我先用现有衣橱给出完整方向；商品只会在你明确提出且门控允许时作为可选项。"

    def _ensure_trace(self, scene: SceneRequest) -> None:
        existing_trace = self.traces.get(scene.trace_id)
        if existing_trace is None:
            existing_trace = self.traces.start(
                trace_id=scene.trace_id,
                request_id=scene.request_id,
                styling_session_id=scene.styling_session_id,
                query_text=scene.query_text,
                user_id=scene.user_id,
            )
        # TraceStore.update replaces a complete section. Preserve only the
        # parse-time audit fields explicitly allowed here; an unrestricted
        # merge could reintroduce a raw query or private memory target ID.
        preserved_scene: dict[str, Any] = {}
        raw_memory_signals = existing_trace.scene.get("memory_signals")
        if isinstance(raw_memory_signals, list):
            allowed_signal_keys = (
                "memory_id",
                "namespace",
                "type",
                "applied_signal",
                "targeted_policy_applied",
                "similarity_policy",
                "private_signature_logged",
            )
            preserved_scene["memory_signals"] = [
                {
                    key: signal[key]
                    for key in allowed_signal_keys
                    if key in signal
                }
                for signal in raw_memory_signals
                if isinstance(signal, dict)
            ]
        for key in ("server_recomputed", "carried_from_session"):
            value = existing_trace.scene.get(key)
            if isinstance(value, bool):
                preserved_scene[key] = value
        constraint_summary = scene.constraints.model_dump()
        excluded_count = len(constraint_summary.get("excluded_items", []))
        # Excluded IDs may include a private, confirmed feedback target. Trace
        # keeps the enforcement count while never serializing target IDs.
        constraint_summary["excluded_items"] = []
        constraint_summary["excluded_item_count"] = excluded_count
        self.traces.update(
            scene.trace_id,
            scene={
                **preserved_scene,
                "event_horizon": scene.event_horizon,
                "urgency": scene.urgency,
                "shopping_allowed": scene.shopping_allowed,
                "intent": scene.intent,
                "occasion": scene.occasion,
                "goals": scene.goals,
                "constraint_summary": constraint_summary,
                "server_recomputed_at_recommend": True,
            },
        )

    def recommend(
        self,
        scene: SceneRequest,
        *,
        requested_outfit_count: int | None = None,
    ) -> InitialRecommendation:
        if requested_outfit_count not in {None, 1, 2, 3}:
            raise ValueError("requested_outfit_count must be between 1 and 3")
        scene = self._enforce_gate(scene)
        self._ensure_trace(scene)
        user = self.repository.get_user(scene.user_id)
        if user is None:
            raise RecommendationNotFound(scene.user_id)
        support_mode = self._support_mode(scene)
        if support_mode:
            self.traces.update(
                scene.trace_id,
                filters=[],
                retrieval={"skipped": True, "reason": support_mode},
                validator={
                    "all_ids_grounded": True,
                    "hard_constraints_passed": True,
                    "accepted_outfit_count": 0,
                },
                catalog={
                    "attempted": False,
                    "call_count": 0,
                    "blocked_reason": "SUPPORT_OR_SAFETY_RESPONSE",
                },
            )
            result = InitialRecommendation(
                request_id=scene.request_id,
                styling_session_id=scene.styling_session_id,
                assistant_message=self._assistant_message(scene, support_mode),
                outfits=[],
                shopping_suggestions=[],
                requested_outfit_count=requested_outfit_count,
                gap_explanation="先确认是否需要进入穿搭决策；未在情绪、身体或医疗请求上强推推荐。",
                ui_capabilities=scene.ui_capabilities,
                trace_id=scene.trace_id,
            )
            self._save_recommendation(result)
            return result

        all_garments = self.repository.list_garments(scene.user_id)
        filtered = self.hard_filter.apply(all_garments, scene)
        memory_signals = (
            self.memory.active_signals(scene.user_id) if self.memory is not None else ()
        )
        soft_memory_terms: tuple[str, ...] = ()
        memory_soft_trace: dict[str, object] | None = None
        if self.memory is not None:
            soft_memory_terms, memory_soft_trace = self.memory.retrieve_soft(
                scene.user_id, scene.query_text, scene=scene
            )
        ranked, retrieval_trace, fallback_events = self.retriever.retrieve(
            scene,
            filtered,
            user.favorite_colors,
            memory_signals,
            soft_memory_terms,
        )
        if memory_soft_trace is not None:
            retrieval_trace["memory_soft"] = memory_soft_trace
        for event in fallback_events:
            self.traces.append_fallback(scene.trace_id, event)
        outfits, gap = self.assembler.assemble(
            ranked, scene, scene.request_id.rsplit("_", 1)[-1]
        )
        valid_outfits, validator_trace = self.validator.validate(
            scene, outfits, filtered.eligible_ids
        )
        if requested_outfit_count is not None:
            valid_outfits = valid_outfits[:requested_outfit_count]
            validator_trace = {
                **validator_trace,
                "requested_outfit_count": requested_outfit_count,
                "returned_outfit_count": len(valid_outfits),
                "requested_count_satisfied": (
                    len(valid_outfits) == requested_outfit_count
                ),
            }
            if len(valid_outfits) < requested_outfit_count:
                gap = (
                    f"你明确需要 {requested_outfit_count} 套；当前硬约束下只能形成 "
                    f"{len(valid_outfits)} 套有明显差异的完整方向。"
                    "没有复制方案，也没有放宽禁忌、可用状态或天气要求。"
                )
        if outfits and not valid_outfits:
            self.traces.append_fallback(
                scene.trace_id,
                {
                    "component": "response_validator",
                    "fallback": "safe_empty_recommendation",
                    "reason": "all assembled outfits rejected",
                },
            )
            gap = "候选在最终白名单或硬约束复检中未通过，已安全停止输出。"

        shopping_suggestions = []
        if scene.shopping_allowed and scene.constraints.gap_slots:
            try:
                catalog_result = self.catalog.search(scene)
                shopping_suggestions = self.catalog.suggestions(catalog_result, scene)
                catalog_trace = {
                    "attempted": catalog_result.attempted,
                    "call_count": catalog_result.call_count,
                    "blocked_reason": catalog_result.blocked_reason,
                    "returned_count": len(catalog_result.items),
                    "filtered": list(catalog_result.filtered),
                }
            except CatalogUnavailable as exc:
                catalog_trace = {
                    "attempted": True,
                    "call_count": 0,
                    "blocked_reason": "PROVIDER_UNAVAILABLE",
                    "returned_count": 0,
                }
                self.traces.append_fallback(
                    scene.trace_id,
                    {
                        "component": "catalog",
                        "fallback": "wardrobe_only",
                        "reason": str(exc),
                    },
                )
        else:
            catalog_trace = {
                "attempted": False,
                "call_count": 0,
                "blocked_reason": (
                    "ORCHESTRATOR_HIGH_URGENCY"
                    if scene.urgency == "high"
                    else (
                        "ORCHESTRATOR_NO_CONFIRMED_GAP"
                        if scene.shopping_allowed
                        else "ORCHESTRATOR_SHOPPING_NOT_ALLOWED"
                    )
                ),
                "returned_count": 0,
            }

        trace_filters = [
            (
                {
                    "item_id": "[REDACTED_EXCLUDED_ITEM]",
                    "reason": row.get("reason"),
                    "item_id_logged": False,
                }
                if row.get("reason") == "EXCLUDED_ITEM"
                else row
            )
            for row in filtered.filtered
        ]
        self.traces.update(
            scene.trace_id,
            filters=trace_filters,
            retrieval=retrieval_trace,
            validator=validator_trace,
            catalog=catalog_trace,
        )
        result = InitialRecommendation(
            request_id=scene.request_id,
            styling_session_id=scene.styling_session_id,
            assistant_message=self._assistant_message(scene, None),
            outfits=valid_outfits,
            shopping_suggestions=shopping_suggestions,
            requested_outfit_count=requested_outfit_count,
            gap_explanation=gap,
            ui_capabilities=scene.ui_capabilities,
            trace_id=scene.trace_id,
        )
        self._save_recommendation(result)
        return result
