from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import CatalogItem, SceneRequest, ShoppingSuggestion
from .repository import FixtureRepository


class CatalogBlocked(PermissionError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class CatalogUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogResult:
    items: tuple[CatalogItem, ...]
    attempted: bool
    call_count: int
    blocked_reason: str | None
    filtered: tuple[dict[str, Any], ...]


class CatalogService:
    """Mock catalog with its own fail-closed policy guard."""

    def __init__(self, repository: FixtureRepository, force_failure: bool = False):
        self.repository = repository
        self.force_failure = force_failure

    @staticmethod
    def _occasion_compatible(item: CatalogItem, scene: SceneRequest) -> bool:
        formal = int(getattr(item, "formal", 0))
        if scene.occasion in item.occasions:
            return scene.occasion not in {"interview", "meeting"} or formal >= 3
        if scene.occasion == "interview":
            return "meeting" in item.occasions and formal >= 3
        if scene.occasion == "meeting":
            return "interview" in item.occasions and formal >= 3
        return False

    def search(self, scene: SceneRequest) -> CatalogResult:
        # Secondary gate: even a mistaken upper-layer call cannot reach the catalog.
        if scene.urgency == "high" or not scene.shopping_allowed:
            reason = (
                "HIGH_URGENCY" if scene.urgency == "high" else "SHOPPING_NOT_ALLOWED"
            )
            return CatalogResult((), True, 0, reason, ())
        if self.force_failure:
            raise CatalogUnavailable("forced catalog failure")
        eligible: list[CatalogItem] = []
        filtered: list[dict[str, Any]] = []
        user = self.repository.get_user(scene.user_id)
        catalog_items = self.repository.list_catalog()
        now_to_event_days = None
        if scene.event_time is not None:
            now_to_event_days = max(
                0, (scene.event_time - datetime.now().astimezone()).days
            )
        for item in catalog_items:
            reason = None
            if (
                scene.constraints.weather_requirement == "rain"
                and item.slot == "outer"
                and getattr(item, "waterproof", None) is not True
            ):
                # Do not infer rain protection from a product title/search text.
                # Only a strict structured boolean can satisfy this hard gate.
                reason = "RAIN_PROOF_EVIDENCE_MISSING"
            elif item.stock <= 0:
                reason = "OUT_OF_STOCK"
            elif scene.constraints.gap_slots and item.slot not in scene.constraints.gap_slots:
                reason = "GAP_SLOT_MISMATCH"
            elif not self._occasion_compatible(item, scene):
                reason = "OCCASION_MISMATCH"
            elif user and user.budget == "low" and float(getattr(item, "price", 0)) > 300:
                reason = "BUDGET_EXCEEDED"
            elif user and user.budget == "mid" and float(getattr(item, "price", 0)) > 350:
                reason = "BUDGET_EXCEEDED"
            elif item.color in scene.constraints.taboo_colors:
                reason = "TABOO_COLOR"
            elif scene.constraints.season and not (
                scene.constraints.season in item.seasons or "all" in item.seasons
            ):
                reason = "SEASON_MISMATCH"
            elif now_to_event_days is not None and item.delivery_days + 1 > now_to_event_days:
                reason = "DELIVERY_BUFFER_MISSED"
            if reason:
                filtered.append({"item_id": item.item_id, "reason": reason})
            else:
                eligible.append(item)
        if user and user.budget == "low":
            eligible.sort(key=lambda item: (getattr(item, "price", 0), item.item_id))
        else:
            eligible.sort(
                key=lambda item: (
                    scene.occasion not in item.occasions,
                    item.delivery_days,
                    item.item_id,
                )
            )
        rain_outer_unresolved = (
            scene.constraints.weather_requirement == "rain"
            and not any(
                item.slot == "outer"
                and getattr(item, "waterproof", None) is True
                for item in catalog_items
            )
        )
        if rain_outer_unresolved:
            # Rain makes a verified outer mandatory regardless of which optional
            # shopping gap the user named. If the catalog cannot satisfy that
            # prerequisite, suppress every otherwise eligible partial product
            # instead of presenting it as an overall-valid solution.
            filtered.extend(
                {
                    "item_id": item.item_id,
                    "reason": "RAIN_REQUIRED_OUTER_UNRESOLVED",
                }
                for item in eligible
            )
            eligible = []
        return CatalogResult(
            tuple(eligible[:3]),
            True,
            1,
            "RAIN_PROOF_EVIDENCE_MISSING" if rain_outer_unresolved else None,
            tuple(filtered),
        )

    @staticmethod
    def suggestions(
        result: CatalogResult, scene: SceneRequest
    ) -> list[ShoppingSuggestion]:
        slot_names = {
            "outer": "外套",
            "top": "上装",
            "bottom": "下装",
            "dress": "连衣裙",
            "shoes": "鞋履",
            "bag": "包袋",
            "accessory": "配饰",
        }
        return [
            ShoppingSuggestion(
                item_id=item.item_id,
                title=item.title,
                reason=(
                    f"这是为 {scene.occasion} 场景补足"
                    f"{slot_names.get(item.slot, item.slot)}缺口的可选 Mock 候选；"
                    "现有衣橱方案仍优先。"
                ),
                declaration=item.declaration,
            )
            for item in result.items
        ]
