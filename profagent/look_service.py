from __future__ import annotations

import uuid
from collections import Counter
from threading import RLock
from typing import Any, Iterable

from .asset_service import AssetError, AssetNotFound, AssetRecord, AssetService
from .config import Settings
from .look_models import (
    AdjustLookInput,
    AdjustLookResponse,
    AdjustmentDecisionRecord,
    AdjustmentProposal,
    DimensionName,
    FinalLook,
    FinalizeLookInput,
    IdChanges,
    LookChainResponse,
    LookComparison,
    LookCreateInput,
    LookVersion,
    RollbackLookInput,
    ScoreComparison,
    ScoreDimension,
    ScoreDimensions,
    Scorecard,
    ScorecardInput,
    ScoreContext,
    ScoreKeepPoint,
    SelectedOutfitLookInput,
    UserRevisionLookInput,
    VisualEvidence,
)
from .models import Garment, SceneRequest
from .repository import FixtureRepository
from .retrieval import HardFilter, _occasion_semihard_pass, _required_complete
from .scene import SceneStateStore
from .service import RecommendationNotFound, RecommendationService
from .tracing import TraceStore, utc_now
from .vision import (
    VISION_REPORTED_MODELS,
    VISION_TRANSPORT_MODEL,
    VisionAdapter,
    VisionAssessment,
    VisionUnavailable,
)


class LookError(ValueError):
    pass


class LookNotFound(KeyError):
    pass


class LookConflict(LookError):
    pass


class LookService:
    """In-memory R1 co-creation state with immutable version snapshots."""

    _WEIGHTS: dict[DimensionName, float] = {
        "occasion_fit": 0.20,
        "expression_match": 0.17,
        "overall_harmony": 0.18,
        "silhouette_layering": 0.15,
        "comfort_practicality": 0.18,
        "detail_finish": 0.12,
    }
    _LABELS: dict[DimensionName, str] = {
        "occasion_fit": "场合适配",
        "expression_match": "目标表达",
        "overall_harmony": "整体协调",
        "silhouette_layering": "廓形层次",
        "comfort_practicality": "舒适实用",
        "detail_finish": "细节完成度",
    }

    def __init__(
        self,
        settings: Settings,
        repository: FixtureRepository,
        state: SceneStateStore,
        recommendations: RecommendationService,
        assets: AssetService,
        vision: VisionAdapter,
        hard_filter: HardFilter,
        traces: TraceStore,
    ):
        self.settings = settings
        self.repository = repository
        self.state = state
        self.recommendations = recommendations
        self.assets = assets
        self.vision = vision
        self.hard_filter = hard_filter
        self.traces = traces
        self._versions: dict[str, list[LookVersion]] = {}
        self._scorecards: dict[str, list[Scorecard]] = {}
        self._proposals: dict[str, tuple[str, str, AdjustmentProposal]] = {}
        self._decided_adjustment_ids: set[str] = set()
        self._rejected_canonical: dict[str, set[str]] = {}
        self._decided_canonical: dict[str, set[str]] = {}
        self._decisions: dict[str, AdjustmentDecisionRecord] = {}
        self._finals: dict[str, FinalLook] = {}
        self._lock = RLock()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    def _trace(
        self,
        operation: str,
        user_id: str,
        session_id: str,
        request_id: str,
        details: dict[str, Any],
        *,
        fallback: dict[str, Any] | None = None,
        provider_details: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> str:
        trace_id = trace_id or self._new_id("trace")
        self.traces.start(
            trace_id=trace_id,
            request_id=f"look_{operation}_{request_id}",
            styling_session_id=session_id,
            query_text=f"look {operation}",
            user_id=user_id,
        )
        self.traces.update(
            trace_id,
            validator={
                "component": "look_service",
                "operation": operation,
                "user_id": user_id,
                "rules": "grounded_owner_bound_immutable_versions_v1",
                **details,
            },
            provider=provider_details
            or {
                "vision": "not_attempted",
                "model_claim": None,
                "image_logged": False,
            },
        )
        if fallback:
            self.traces.append_fallback(trace_id, fallback)
        return trace_id

    def _session_scene(self, user_id: str, session_id: str) -> SceneRequest:
        scene = self.state.by_session(session_id)
        if scene is None or scene.user_id != user_id:
            raise LookNotFound("styling session")
        return scene

    def _stored_version(self, session_id: str, version_id: str) -> LookVersion:
        with self._lock:
            version = next(
                (
                    item
                    for item in self._versions.get(session_id, [])
                    if item.look_version_id == version_id
                ),
                None,
            )
        if version is None:
            raise LookNotFound(version_id)
        return version.model_copy(deep=True)

    def _owned_version(
        self, user_id: str, session_id: str, version_id: str
    ) -> LookVersion:
        self._session_scene(user_id, session_id)
        version = self._stored_version(session_id, version_id)
        if version.user_id != user_id:
            raise LookNotFound(version_id)
        return version

    def preview_source(
        self, user_id: str, session_id: str, version_id: str
    ) -> tuple[LookVersion, SceneRequest, tuple[Garment, ...]]:
        """Return a grounded immutable Look snapshot for static-2D rendering."""

        version = self._owned_version(user_id, session_id, version_id)
        scene = self._scene_for_version(version)
        garments = self._validate_items(user_id, scene, version.item_ids)
        return version, scene, garments

    def _active_id(self, session_id: str) -> str | None:
        final = self._finals.get(session_id)
        if final is not None:
            return final.look_version_id
        versions = self._versions.get(session_id, [])
        return versions[-1].look_version_id if versions else None

    def _view_version(self, version: LookVersion) -> LookVersion:
        final = self._finals.get(version.styling_session_id)
        if final is not None and final.look_version_id == version.look_version_id:
            status = "final"
        elif self._active_id(version.styling_session_id) == version.look_version_id:
            status = "active"
        else:
            status = "superseded"
        return version.model_copy(update={"status": status}, deep=True)

    def _scene_for_version(self, version: LookVersion) -> SceneRequest:
        scene = self.state.by_request(version.request_id)
        if (
            scene is None
            or scene.user_id != version.user_id
            or scene.styling_session_id != version.styling_session_id
        ):
            raise LookNotFound("authoritative scene")
        return scene

    def _validate_items(
        self, user_id: str, scene: SceneRequest, item_ids: Iterable[str]
    ) -> tuple[Garment, ...]:
        ids = tuple(item_ids)
        if not ids or len(ids) != len(set(ids)):
            raise LookError("item_ids must be non-empty and unique")
        garments: list[Garment] = []
        for item_id in ids:
            item = self.repository.get_garment(item_id)
            if item is None or item.user_id != user_id:
                raise LookError("item_id is not in the user's wardrobe whitelist")
            if not self.hard_filter.item_passes(item, scene):
                raise LookError("item_id is unavailable or violates a hard constraint")
            garments.append(item)
        core_slots = [
            item.slot
            for item in garments
            if item.slot in {"outer", "top", "bottom", "dress", "shoes", "bag"}
        ]
        duplicates = [slot for slot, count in Counter(core_slots).items() if count > 1]
        if duplicates:
            raise LookError("multiple items occupy the same core slot")
        slots = set(core_slots)
        if "dress" in slots and ({"top", "bottom"} & slots):
            raise LookError("dress cannot be combined with duplicate top/bottom core slots")
        if not _required_complete(garments, scene.constraints.required_slots):
            raise LookError("required wardrobe slots are incomplete")
        if not _occasion_semihard_pass(garments, scene):
            raise LookError("items fail the occasion/formality recheck")
        return tuple(garments)

    def _validate_assets(
        self, asset_ids: Iterable[str], user_id: str, session_id: str
    ) -> tuple[str, ...]:
        ids = tuple(asset_ids)
        self._asset_records(ids, user_id, session_id)
        return ids

    def _asset_records(
        self, asset_ids: Iterable[str], user_id: str, session_id: str
    ) -> tuple[AssetRecord, ...]:
        ids = tuple(asset_ids)
        try:
            records = self.assets.resolve_many(list(ids), user_id, session_id)
        except (AssetError, AssetNotFound) as exc:
            raise LookError("asset_id is invalid for this user/session") from exc
        return tuple(records)

    def _new_version(
        self,
        *,
        user_id: str,
        session_id: str,
        request_id: str,
        parent: LookVersion | None,
        item_ids: tuple[str, ...],
        asset_ids: tuple[str, ...],
        created_from: str,
        source_outfit_id: str | None,
        rollback_target_version_id: str | None = None,
        accepted_adjustments: tuple[str, ...] | None = None,
        rejected_adjustments: tuple[str, ...] | None = None,
        partially_accepted_adjustments: tuple[str, ...] | None = None,
    ) -> LookVersion:
        version_id = self._new_id("lookv")
        trace_id = self._trace(
            "create_version",
            user_id,
            session_id,
            request_id,
            {
                "look_version_id": version_id,
                "parent_version_id": parent.look_version_id if parent else None,
                "created_from": created_from,
                "item_ids": list(item_ids),
                "asset_ids": list(asset_ids),
                "all_ids_grounded": True,
                "hard_constraints_passed": True,
            },
        )
        inherited_rejections = (
            rejected_adjustments
            if rejected_adjustments is not None
            else (parent.rejected_adjustments if parent else ())
        )
        session_rejections = tuple(
            sorted(self._rejected_canonical.get(session_id, set()))
        )
        rejection_snapshot = tuple(
            dict.fromkeys((*inherited_rejections, *session_rejections))
        )
        version = LookVersion(
            user_id=user_id,
            styling_session_id=session_id,
            request_id=request_id,
            look_version_id=version_id,
            parent_version_id=parent.look_version_id if parent else None,
            version_index=(parent.version_index + 1 if parent else 1),
            status="active",
            item_ids=item_ids,
            asset_ids=asset_ids,
            accepted_adjustments=accepted_adjustments
            if accepted_adjustments is not None
            else (parent.accepted_adjustments if parent else ()),
            rejected_adjustments=rejection_snapshot,
            partially_accepted_adjustments=partially_accepted_adjustments
            if partially_accepted_adjustments is not None
            else (parent.partially_accepted_adjustments if parent else ()),
            created_from=created_from,  # type: ignore[arg-type]
            source_outfit_id=source_outfit_id,
            rollback_target_version_id=rollback_target_version_id,
            trace_id=trace_id,
            created_at=utc_now(),
        )
        return version

    def create(self, payload: LookCreateInput) -> LookVersion:
        self._session_scene(payload.user_id, payload.styling_session_id)
        with self._lock:
            if payload.styling_session_id in self._finals:
                raise LookConflict("the styling session is finalized")

        if isinstance(payload, SelectedOutfitLookInput):
            with self._lock:
                if self._versions.get(payload.styling_session_id):
                    raise LookConflict("the styling session already has a Look chain")
            try:
                scene, outfit = self.recommendations.get_saved_outfit(
                    payload.user_id,
                    payload.styling_session_id,
                    payload.request_id,
                    payload.outfit_id,
                )
            except RecommendationNotFound as exc:
                raise LookNotFound("saved recommendation outfit") from exc
            self._validate_items(payload.user_id, scene, outfit.items)
            asset_ids = self._validate_assets(
                payload.asset_ids, payload.user_id, payload.styling_session_id
            )
            version = self._new_version(
                user_id=payload.user_id,
                session_id=payload.styling_session_id,
                request_id=payload.request_id,
                parent=None,
                item_ids=tuple(outfit.items),
                asset_ids=asset_ids,
                created_from="selected_outfit",
                source_outfit_id=payload.outfit_id,
            )
        elif isinstance(payload, UserRevisionLookInput):
            parent = self._owned_version(
                payload.user_id, payload.styling_session_id, payload.parent_version_id
            )
            if self._active_id(payload.styling_session_id) != parent.look_version_id:
                raise LookConflict("parent_version_id must be the Active Look")
            scene = self._scene_for_version(parent)
            self._validate_items(payload.user_id, scene, payload.item_ids)
            asset_ids = self._validate_assets(
                payload.asset_ids, payload.user_id, payload.styling_session_id
            )
            if tuple(payload.item_ids) == parent.item_ids and asset_ids == parent.asset_ids:
                raise LookError("user_revision must change an item or asset")
            version = self._new_version(
                user_id=payload.user_id,
                session_id=payload.styling_session_id,
                request_id=parent.request_id,
                parent=parent,
                item_ids=tuple(payload.item_ids),
                asset_ids=asset_ids,
                created_from="user_revision",
                source_outfit_id=parent.source_outfit_id,
            )
        elif isinstance(payload, RollbackLookInput):
            parent = self._owned_version(
                payload.user_id, payload.styling_session_id, payload.parent_version_id
            )
            target = self._owned_version(
                payload.user_id,
                payload.styling_session_id,
                payload.rollback_target_version_id,
            )
            if self._active_id(payload.styling_session_id) != parent.look_version_id:
                raise LookConflict("parent_version_id must be the Active Look")
            if target.look_version_id == parent.look_version_id:
                raise LookError("rollback target must differ from the Active Look")
            scene = self._scene_for_version(parent)
            self._validate_items(payload.user_id, scene, target.item_ids)
            self._validate_assets(
                target.asset_ids, payload.user_id, payload.styling_session_id
            )
            version = self._new_version(
                user_id=payload.user_id,
                session_id=payload.styling_session_id,
                request_id=parent.request_id,
                parent=parent,
                item_ids=target.item_ids,
                asset_ids=target.asset_ids,
                created_from="rollback",
                source_outfit_id=target.source_outfit_id,
                rollback_target_version_id=target.look_version_id,
                accepted_adjustments=target.accepted_adjustments,
                rejected_adjustments=target.rejected_adjustments,
                partially_accepted_adjustments=target.partially_accepted_adjustments,
            )
        else:  # pragma: no cover - discriminated union is exhaustive
            raise LookError("unsupported Look operation")

        with self._lock:
            if payload.styling_session_id in self._finals:
                raise LookConflict("the styling session is finalized")
            self._versions.setdefault(payload.styling_session_id, []).append(version)
        return self._view_version(version)

    @staticmethod
    def _id_changes(before: tuple[str, ...], after: tuple[str, ...]) -> IdChanges:
        before_set, after_set = set(before), set(after)
        return IdChanges(
            added=tuple(sorted(after_set - before_set)),
            removed=tuple(sorted(before_set - after_set)),
            retained=tuple(sorted(before_set & after_set)),
        )

    @staticmethod
    def _dimension_scores(card: Scorecard | None) -> dict[str, int | None]:
        if card is None:
            return {}
        return {
            key: value.get("score")
            for key, value in card.dimensions.model_dump().items()
        }

    def compare(self, before: LookVersion, after: LookVersion) -> LookComparison:
        before_card = self.latest_scorecard(before.look_version_id)
        after_card = self.latest_scorecard(after.look_version_id)
        before_scores = self._dimension_scores(before_card)
        after_scores = self._dimension_scores(after_card)
        improved: list[DimensionName] = []
        declined: list[DimensionName] = []
        unchanged: list[DimensionName] = []
        uncertain: list[DimensionName] = []
        for name in self._WEIGHTS:
            left, right = before_scores.get(name), after_scores.get(name)
            if left is None or right is None:
                uncertain.append(name)
            elif right > left:
                improved.append(name)
            elif right < left:
                declined.append(name)
            else:
                unchanged.append(name)
        total_delta = (
            round(after_card.total_score - before_card.total_score, 2)
            if before_card
            and after_card
            and before_card.total_score is not None
            and after_card.total_score is not None
            else None
        )
        return LookComparison(
            styling_session_id=after.styling_session_id,
            from_version_id=before.look_version_id,
            to_version_id=after.look_version_id,
            item_changes=self._id_changes(before.item_ids, after.item_ids),
            asset_changes=self._id_changes(before.asset_ids, after.asset_ids),
            accepted_adjustments=after.accepted_adjustments,
            rejected_adjustments=after.rejected_adjustments,
            partially_accepted_adjustments=after.partially_accepted_adjustments,
            total_score_delta=total_delta,
            improved_dimensions=tuple(improved),
            declined_dimensions=tuple(declined),
            unchanged_dimensions=tuple(unchanged),
            uncertain_dimensions=tuple(uncertain),
        )

    def chain(
        self,
        user_id: str,
        styling_session_id: str,
        look_version_id: str | None = None,
    ) -> LookChainResponse:
        scene = self._session_scene(user_id, styling_session_id)
        with self._lock:
            stored = [
                version.model_copy(deep=True)
                for version in self._versions.get(styling_session_id, [])
            ]
            final = self._finals.get(styling_session_id)
            decisions = tuple(
                sorted(
                    (
                        item.model_copy(deep=True)
                        for item in self._decisions.values()
                        if item.styling_session_id == styling_session_id
                    ),
                    key=lambda item: item.created_at,
                )
            )
        versions = tuple(self._view_version(item) for item in stored)
        if look_version_id:
            selected = next(
                (item for item in versions if item.look_version_id == look_version_id),
                None,
            )
            if selected is None:
                raise LookNotFound(look_version_id)
        else:
            active_id = self._active_id(styling_session_id)
            selected = next(
                (item for item in versions if item.look_version_id == active_id), None
            )
        parent = (
            next(
                (
                    item
                    for item in versions
                    if selected and item.look_version_id == selected.parent_version_id
                ),
                None,
            )
            if selected
            else None
        )
        comparison = self.compare(parent, selected) if parent and selected else None
        return LookChainResponse(
            user_id=scene.user_id,
            styling_session_id=styling_session_id,
            versions=versions,
            active_version_id=self._active_id(styling_session_id),
            final_version_id=final.look_version_id if final else None,
            status="final" if final else ("active" if versions else "empty"),
            look_version=selected,
            parent=parent,
            comparison=comparison,
            adjustment_decisions=decisions,
        )

    def _rule_scores(
        self, garments: tuple[Garment, ...], scene: SceneRequest
    ) -> dict[DimensionName, int]:
        occasion_match = sum(scene.occasion in item.occasions for item in garments)
        desired_styles = {
            "reliable": {"business", "classic", "formal", "smart"},
            "modern": {"smart", "street", "simple"},
            "comfortable": {"soft", "sporty", "simple", "campus"},
            "polished": {"business", "formal", "smart", "classic"},
            "low_key": {"simple", "classic", "campus"},
            "confident": {"business", "formal", "smart"},
            "cool": {"street", "sporty", "campus"},
        }
        target_styles = set().union(
            *(desired_styles.get(goal, set()) for goal in scene.goals)
        )
        styles = {style for item in garments for style in item.styles}
        colors = {item.color for item in garments}
        slots = {item.slot for item in garments}
        comfort = sum(item.fit in {"regular", "loose", "straight"} for item in garments)
        return {
            "occasion_fit": min(96, 68 + round(28 * occasion_match / len(garments))),
            "expression_match": min(94, 72 + 5 * len(styles & target_styles)),
            "overall_harmony": 90 if len(colors) <= 3 else 78,
            "silhouette_layering": 88 if {"top", "bottom", "shoes"}.issubset(slots) or "dress" in slots else 76,
            "comfort_practicality": min(94, 70 + round(24 * comfort / len(garments))),
            "detail_finish": 88 if slots & {"bag", "accessory", "outer"} else 80,
        }

    def _dimension_object(
        self,
        scores: dict[DimensionName, int] | None,
        confidence: float,
        scene: SceneRequest,
    ) -> ScoreDimensions:
        evidence: dict[DimensionName, tuple[str, ...]] = {
            "occasion_fit": (f"仅核对衣物元数据与 {scene.occasion} 场合要求。",),
            "expression_match": ("仅核对搭配风格标签与本次表达目标。",),
            "overall_harmony": ("仅核对衣物之间的颜色与风格组合。",),
            "silhouette_layering": ("仅核对上装、下装、鞋履与外层的组合结构。",),
            "comfort_practicality": ("仅核对材质、松量、天气与活动需求元数据。",),
            "detail_finish": ("仅核对鞋履、包袋、配饰与外层的搭配完整度。",),
        }
        values = {
            name: ScoreDimension(
                label=self._LABELS[name],
                weight=self._WEIGHTS[name],
                score=scores[name] if scores else None,
                evidence=evidence[name],
                confidence=confidence,
            )
            for name in self._WEIGHTS
        }
        return ScoreDimensions(**values)

    def _priority_adjustments(
        self,
        version: LookVersion,
        trace_id: str,
        *,
        scorecard_id: str | None = None,
        verified_regions: frozenset[str] = frozenset(),
        issue_codes: frozenset[str] = frozenset(),
    ) -> tuple[AdjustmentProposal, ...]:
        with self._lock:
            if version.styling_session_id in self._finals:
                raise LookConflict(
                    "the styling session is finalized; no further advice"
                )
        # Technical decoding or image entropy is not outfit-region evidence.
        # Until a trusted Vision result binds visible garment regions to this
        # server-grounded Look, do not invent cuff/layer/detail actions.
        if not verified_regions or scorecard_id is None:
            return ()
        garments = [
            item
            for item_id in version.item_ids
            if (item := self.repository.get_garment(item_id)) is not None
        ]
        slots = {item.slot for item in garments}
        has_trousers = any(
            item.slot == "bottom" and "裤" in item.name for item in garments
        )
        rejected = self._rejected_canonical.get(version.styling_session_id, set())
        decided = self._decided_canonical.get(version.styling_session_id, set())
        candidates = (
            (
                "trouser_cuff_single",
                "将裤脚整理为一次窄卷边",
                "先用零成本细节动作检查下装与鞋履的衔接。",
                ("silhouette_layering", "detail_finish"),
                frozenset({"bottom"}),
                "bottom_cuff_messy",
                has_trousers,
            ),
            (
                "layer_alignment",
                "整理上装与外层的衣摆和开合关系",
                "让现有衣物的层次边界更清楚，不增加购物项。",
                ("overall_harmony", "detail_finish"),
                frozenset({"top", "overall"}),
                "layer_alignment_issue",
                {"top", "outer"}.issubset(slots),
            ),
            (
                "accessory_simplify",
                "保留一个重点配饰，其余先移除",
                "用可逆的小动作减少细节竞争。",
                ("overall_harmony", "detail_finish"),
                frozenset({"overall"}),
                "detail_competition",
                "accessory" in slots,
            ),
            (
                "shoe_finish_cleanup",
                "整理鞋带与鞋面细节后再比较整套",
                "可信衣物区域证据标记了鞋履协调问题，先做可逆整理。",
                ("detail_finish", "overall_harmony"),
                frozenset({"shoes", "overall"}),
                "shoe_coordination_issue",
                "shoes" in slots,
            ),
        )
        result: list[AdjustmentProposal] = []
        for (
            canonical,
            action,
            reason,
            dimensions,
            regions,
            required_issue,
            slot_applicable,
        ) in candidates:
            if (
                not slot_applicable
                or not regions.issubset(verified_regions)
                or required_issue not in issue_codes
            ):
                continue
            if canonical in rejected or canonical in decided:
                continue
            existing = next(
                (
                    stored[2]
                    for stored in self._proposals.values()
                    if stored[0] == version.styling_session_id
                    and stored[1] == version.look_version_id
                    and stored[2].canonical_action == canonical
                    and stored[2].adjustment_id not in self._decided_adjustment_ids
                ),
                None,
            )
            proposal = existing or AdjustmentProposal(
                adjustment_id=self._new_id("adj"),
                canonical_action=canonical,
                canonical_key=canonical,
                action=action,
                reason=reason,
                expected_dimensions=dimensions,  # type: ignore[arg-type]
                cost_level="zero_cost",
                evidence_source="verified_visual",
                evidence_regions=tuple(sorted(regions)),
                scorecard_id=scorecard_id,
                trace_id=trace_id,
            )
            self._proposals[proposal.adjustment_id] = (
                version.styling_session_id,
                version.look_version_id,
                proposal,
            )
            result.append(proposal)
            if len(result) == 2:
                break
        return tuple(result)

    def latest_scorecard(self, look_version_id: str) -> Scorecard | None:
        with self._lock:
            values = self._scorecards.get(look_version_id, [])
            return values[-1].model_copy(deep=True) if values else None

    async def score(self, payload: ScorecardInput) -> Scorecard:
        version = self._owned_version(
            payload.user_id, payload.styling_session_id, payload.look_version_id
        )
        with self._lock:
            if payload.styling_session_id in self._finals:
                raise LookConflict(
                    "the styling session is finalized; no further scorecards or advice"
                )
        scene = self._scene_for_version(version)
        if (
            payload.asset_ids is not None
            and tuple(payload.asset_ids) != version.asset_ids
        ):
            raise LookError(
                "asset_ids must exactly match the immutable LookVersion evidence"
            )
        asset_ids = version.asset_ids
        asset_records = self._asset_records(
            asset_ids, payload.user_id, payload.styling_session_id
        )
        usable_assets = [
            record for record in asset_records if record.visual_quality == "usable"
        ]
        try:
            garments = self._validate_items(payload.user_id, scene, version.item_ids)
            hard_pass = True
        except LookError:
            garments = ()
            hard_pass = False

        assessment: VisionAssessment | None = None
        provider_trace: dict[str, Any] = {
            "component": "vision",
            "status": "not_attempted",
            "requested_model": "grok4.6",
            "transport_model": VISION_TRANSPORT_MODEL,
            "resolved_model": None,
            "model_verified": False,
            "image_logged": False,
        }
        fallback_reason: str | None = None
        if not hard_pass:
            evidence_status = "hard_constraint_failed"
            confidence = 0.15
            fallback_reason = "current wardrobe state failed hard-constraint recheck"
        elif not asset_ids:
            evidence_status = "no_asset"
            confidence = 0.20
            fallback_reason = "no static asset"
        elif not usable_assets:
            evidence_status = "limited_asset"
            confidence = 0.20
            fallback_reason = "all static assets have limited technical quality"
        else:
            owned_payloads = [
                self.assets.read_owned(
                    record.asset_id, payload.user_id, payload.styling_session_id
                )
                for record in usable_assets
            ]
            try:
                inspection = await self.vision.inspect(owned_payloads)
                assessment = inspection.assessment
                provider_trace = inspection.provider_trace
                confidence = min(
                    region.confidence for region in assessment.regions
                )
                if assessment.complete_and_confident:
                    evidence_status = "sufficient"
                else:
                    evidence_status = "vision_degraded"
                    fallback_reason = "partial or low-confidence garment visibility"
            except VisionUnavailable as exc:
                evidence_status = "vision_degraded"
                confidence = 0.20
                fallback_reason = exc.reason
                provider_trace = exc.provider_trace

        numeric = bool(
            hard_pass
            and assessment is not None
            and assessment.complete_and_confident
            and provider_trace.get("model_verified") is True
            and provider_trace.get("resolved_model") in VISION_REPORTED_MODELS
        )
        scores = self._rule_scores(garments, scene) if numeric else None
        dimensions = self._dimension_object(scores, confidence, scene)
        total = (
            round(
                sum(scores[name] * weight for name, weight in self._WEIGHTS.items()),
                1,
            )
            if scores
            else None
        )
        verified_regions = (
            frozenset(
                region.slot
                for region in assessment.regions
                if region.visible and region.confidence >= 0.70
            )
            if assessment and numeric
            else frozenset()
        )
        issue_codes = assessment.issue_codes if assessment and numeric else frozenset()
        visual_evidence = (
            tuple(
                VisualEvidence(
                    region=region.slot,
                    observation=region.evidence_text,
                    confidence=region.confidence,
                )
                for region in assessment.regions
                if region.visible
            )
            if assessment
            else (
                tuple(
                    VisualEvidence(
                        region="静态图技术质量",
                        observation=(
                            f"图像为 {record.width}×{record.height}，"
                            "仅完成安全解码，未获得可信衣物区域结果。"
                        ),
                        confidence=0.15,
                    )
                    for record in asset_records
                )
                if asset_records
                else ()
            )
        )
        missing_slots = (
            sorted(
                region.slot
                for region in assessment.regions
                if not region.visible or region.confidence < 0.70
            )
            if assessment
            else []
        )
        if numeric:
            missing_evidence: tuple[str, ...] = ()
            guidance = (
                "数值由服务端衣物白名单元数据规则计算；视觉 Provider 仅确认"
                "衣物区域可见性与服装观察，不识别人，也不评价人。"
            )
        else:
            missing_evidence = (
                (
                    "需要可信视觉结果确认缺失或低置信衣物区域："
                    + ",".join(missing_slots)
                )
                if missing_slots
                else "需要可信视觉结果确认 top、bottom、shoes、overall 均清晰可见"
            ,)
            guidance = (
                "当前视觉证据未通过精确模型、严格结构、安全与完整可见性全部校验；"
                "因此只给衣物元数据定性结论，不伪造分数，也不评价人。"
            )

        keep_point = ScoreKeepPoint(
            statement=(
                f"当前穿搭的核心衣物槽位完整，并通过了本次 {scene.occasion} "
                "场景的服务端硬约束复核；这一点值得保留。"
                if hard_pass
                else (
                    f"当前反馈仍严格围绕已选衣物与本次 {scene.occasion} 目标；"
                    "这个评价边界值得保留，需先修复硬约束再讨论分数。"
                )
            ),
            evidence_basis="grounded_metadata",
        )
        scorecard_id = self._new_id("score")
        trace_id = self._new_id("trace")
        priority_adjustments = self._priority_adjustments(
            version,
            trace_id,
            scorecard_id=scorecard_id,
            verified_regions=verified_regions,
            issue_codes=issue_codes,
        )
        trace_id = self._trace(
            "scorecard",
            payload.user_id,
            payload.styling_session_id,
            version.request_id,
            {
                "scorecard_id": scorecard_id,
                "look_version_id": version.look_version_id,
                "asset_ids": list(asset_ids),
                "asset_quality": [
                    {
                        "asset_id": record.asset_id,
                        "width": record.width,
                        "height": record.height,
                        "visual_quality": record.visual_quality,
                    }
                    for record in asset_records
                ],
                "numeric_score_available": numeric,
                "hard_constraint_status": "pass" if hard_pass else "fail",
                "verified_regions": sorted(verified_regions),
                "issue_codes": sorted(issue_codes),
                "adjustment_ids": [
                    item.adjustment_id for item in priority_adjustments
                ],
                "target": "current_look_x_current_goal",
                "prohibited_subject_checks": {
                    "appearance": False,
                    "body": False,
                    "age": False,
                    "sexual_attractiveness": False,
                },
            },
            provider_details=provider_trace,
            trace_id=trace_id,
            fallback=(
                {
                    "component": "vision",
                    "fallback": "qualitative_scorecard",
                    "reason": fallback_reason,
                }
                if fallback_reason
                else None
            ),
        )
        parent = (
            self._stored_version(
                payload.styling_session_id, version.parent_version_id
            )
            if version.parent_version_id
            else None
        )
        parent_card = self.latest_scorecard(parent.look_version_id) if parent else None
        comparison = ScoreComparison(
            parent_version_id=parent.look_version_id if parent else None,
            total_delta=(
                round(total - parent_card.total_score, 2)
                if total is not None
                and parent_card is not None
                and parent_card.total_score is not None
                else None
            ),
            uncertain=(
                tuple(self._WEIGHTS)
                if total is None
                or not parent_card
                or parent_card.total_score is None
                else ()
            ),
        )
        card = Scorecard(
            scorecard_id=scorecard_id,
            look_version_id=version.look_version_id,
            context=ScoreContext(
                occasion=scene.occasion,
                goals=tuple(scene.goals),
                styling_session_id=payload.styling_session_id,
            ),
            hard_constraint_status="pass" if hard_pass else "fail",
            dimensions=dimensions,
            numeric_score_available=numeric,
            total_score=total,
            missing_evidence=missing_evidence,
            visual_evidence=visual_evidence,
            visual_confidence=confidence,
            evidence_status=evidence_status,  # type: ignore[arg-type]
            guidance=guidance,
            keep_point=keep_point,
            priority_adjustments=priority_adjustments,
            comparison_to_parent=comparison,
            trace_id=trace_id,
            created_at=utc_now(),
        )
        with self._lock:
            if payload.styling_session_id in self._finals:
                # A Final may have been committed while awaiting Vision. Do not
                # leave newly generated advice addressable after that boundary.
                for proposal in priority_adjustments:
                    self._proposals.pop(proposal.adjustment_id, None)
                raise LookConflict(
                    "the styling session is finalized; no further scorecards or advice"
                )
            self._scorecards.setdefault(version.look_version_id, []).append(card)
        return card.model_copy(deep=True)

    def adjust(self, payload: AdjustLookInput) -> AdjustLookResponse:
        version = self._owned_version(
            payload.user_id, payload.styling_session_id, payload.look_version_id
        )
        with self._lock:
            if payload.styling_session_id in self._finals:
                raise LookConflict(
                    "the styling session is finalized; no further adjustments"
                )
        if self._active_id(payload.styling_session_id) != version.look_version_id:
            raise LookConflict("adjustments only apply to the Active Look")

        proposal: AdjustmentProposal | None = None
        if payload.adjustment_id:
            stored = self._proposals.get(payload.adjustment_id)
            if (
                stored is None
                or stored[0] != payload.styling_session_id
                or stored[1] != payload.look_version_id
                or payload.adjustment_id in self._decided_adjustment_ids
                or stored[2].canonical_action
                in self._decided_canonical.get(payload.styling_session_id, set())
            ):
                raise LookError("adjustment_id is invalid for the Active Look")
            proposal = stored[2]
            canonical = proposal.canonical_key
        else:
            canonical = "user_modified"

        scene = self._scene_for_version(version)
        next_version: LookVersion | None = None
        if payload.decision == "reject":
            with self._lock:
                if payload.styling_session_id in self._finals:
                    raise LookConflict(
                        "the styling session is finalized; no further adjustments"
                    )
                if self._active_id(payload.styling_session_id) != version.look_version_id:
                    raise LookConflict("adjustments only apply to the Active Look")
                self._rejected_canonical.setdefault(
                    payload.styling_session_id, set()
                ).add(canonical)
                self._decided_canonical.setdefault(
                    payload.styling_session_id, set()
                ).add(canonical)
                if payload.adjustment_id:
                    self._decided_adjustment_ids.add(payload.adjustment_id)
        else:
            item_ids = tuple(payload.item_ids) if payload.item_ids is not None else version.item_ids
            asset_ids = tuple(payload.asset_ids) if payload.asset_ids is not None else version.asset_ids
            self._validate_items(payload.user_id, scene, item_ids)
            self._validate_assets(asset_ids, payload.user_id, payload.styling_session_id)
            if payload.decision == "user_modified" and item_ids == version.item_ids and asset_ids == version.asset_ids:
                raise LookError("user_modified must make an actual item or asset change")
            accepted = version.accepted_adjustments
            partial = version.partially_accepted_adjustments
            if payload.decision == "accept":
                accepted = tuple(dict.fromkeys((*accepted, canonical)))
            elif payload.decision == "partial":
                partial = tuple(dict.fromkeys((*partial, canonical)))
            next_version = self._new_version(
                user_id=payload.user_id,
                session_id=payload.styling_session_id,
                request_id=version.request_id,
                parent=version,
                item_ids=item_ids,
                asset_ids=asset_ids,
                created_from=(
                    "user_revision"
                    if payload.decision == "user_modified"
                    else "adjustment"
                ),
                source_outfit_id=version.source_outfit_id,
                accepted_adjustments=accepted,
                partially_accepted_adjustments=partial,
            )
            with self._lock:
                if payload.styling_session_id in self._finals:
                    raise LookConflict(
                        "the styling session is finalized; no further adjustments"
                    )
                if self._active_id(payload.styling_session_id) != version.look_version_id:
                    raise LookConflict("adjustments only apply to the Active Look")
                self._versions[payload.styling_session_id].append(next_version)
                self._decided_canonical.setdefault(
                    payload.styling_session_id, set()
                ).add(canonical)
                if payload.adjustment_id:
                    self._decided_adjustment_ids.add(payload.adjustment_id)

        decision_record_id = self._new_id("decision")
        trace_id = self._trace(
            "adjust",
            payload.user_id,
            payload.styling_session_id,
            version.request_id,
            {
                "decision_record_id": decision_record_id,
                "source_version_id": version.look_version_id,
                "created_version_id": next_version.look_version_id if next_version else None,
                "adjustment_id": payload.adjustment_id,
                "canonical_key": canonical,
                "canonical_action": canonical,
                "decision": payload.decision,
                "reason_logged": False,
            },
        )
        record = AdjustmentDecisionRecord(
            decision_record_id=decision_record_id,
            styling_session_id=payload.styling_session_id,
            look_version_id=version.look_version_id,
            adjustment_id=payload.adjustment_id,
            canonical_action=canonical,
            canonical_key=canonical,
            decision=payload.decision,
            reason=payload.reason,
            trace_id=trace_id,
            created_at=utc_now(),
        )
        self._decisions[record.decision_record_id] = record
        target = next_version or version
        comparison = self.compare(version, target)
        if payload.decision == "reject":
            comparison = comparison.model_copy(
                update={
                    "rejected_adjustments": tuple(
                        sorted(
                            self._rejected_canonical.get(
                                payload.styling_session_id, set()
                            )
                        )
                    )
                },
                deep=True,
            )
        priority = self._priority_adjustments(target, trace_id)
        return AdjustLookResponse(
            decision_record=record,
            look_version=self._view_version(next_version) if next_version else None,
            comparison=comparison,
            priority_adjustments=priority,
        )

    def finalize(self, payload: FinalizeLookInput) -> FinalLook:
        version = self._owned_version(
            payload.user_id, payload.styling_session_id, payload.look_version_id
        )
        with self._lock:
            existing = self._finals.get(payload.styling_session_id)
        if existing is not None:
            if (
                existing.look_version_id == payload.look_version_id
                and existing.satisfaction == payload.satisfaction
                and existing.reason == payload.reason
            ):
                return existing.model_copy(deep=True)
            raise LookConflict("the styling session already has a different finalization")
        if self._active_id(payload.styling_session_id) != version.look_version_id:
            raise LookConflict("only the Active Look can be finalized")
        scorecard = self.latest_scorecard(version.look_version_id)
        trace_id = self._trace(
            "finalize",
            payload.user_id,
            payload.styling_session_id,
            version.request_id,
            {
                "look_version_id": version.look_version_id,
                "satisfied": True,
                "satisfaction": payload.satisfaction,
                "score_required": False,
                "advice_stopped": True,
                "reason_logged": False,
            },
        )
        final = FinalLook(
            final_look_id=self._new_id("final"),
            user_id=payload.user_id,
            styling_session_id=payload.styling_session_id,
            look_version_id=version.look_version_id,
            item_ids=version.item_ids,
            asset_ids=version.asset_ids,
            scorecard_id=scorecard.scorecard_id if scorecard else None,
            total_score=scorecard.total_score if scorecard else None,
            satisfaction=payload.satisfaction,
            reason=payload.reason,
            trace_id=trace_id,
            finalized_at=utc_now(),
        )
        with self._lock:
            existing = self._finals.get(payload.styling_session_id)
            if existing is not None:
                if (
                    existing.look_version_id == payload.look_version_id
                    and existing.satisfaction == payload.satisfaction
                    and existing.reason == payload.reason
                ):
                    return existing.model_copy(deep=True)
                raise LookConflict(
                    "the styling session already has a different finalization"
                )
            # Serialize Final against a concurrently committed revision or
            # rollback. Final must bind the Active Look observed at commit.
            if self._active_id(payload.styling_session_id) != version.look_version_id:
                raise LookConflict("only the Active Look can be finalized")
            self._finals[payload.styling_session_id] = final
        return final.model_copy(deep=True)
