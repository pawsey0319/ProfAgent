from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from .memory_candidates import COLOR_LABELS, COLOR_VALUES, SessionPreference
from .memory_service import MemorySignal
from .models import (
    InitialRecommendation,
    PreferenceClarification,
    PreferenceOption,
    SceneRequest,
    Urgency,
)
from .repository import FixtureRepository


@dataclass(frozen=True)
class PreferenceDecision:
    clarification: PreferenceClarification | None
    reordered_outfit_ids: tuple[str, ...]
    reason_code: str


@dataclass(frozen=True)
class PreferenceUncertaintyEvidence:
    gap_code: str
    neutral_margin: float
    counterfactual_winners: tuple[str, str]
    urgency: Urgency
    confirmed: bool
    already_asked: bool


class PreferenceUncertaintyPolicy:
    _MATERIAL_MARGIN = 0.05
    _COUNTERFACTUAL_BOOST = 0.06

    def __init__(self, repository: FixtureRepository | None = None) -> None:
        self.repository = repository

    @staticmethod
    def should_ask(evidence: PreferenceUncertaintyEvidence) -> bool:
        return (
            not evidence.confirmed
            and not evidence.already_asked
            and 0 <= evidence.neutral_margin <= 0.05
            and len(set(evidence.counterfactual_winners)) == 2
        )

    @staticmethod
    def _stable_id(prefix: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return f"{prefix}_{digest}"

    @staticmethod
    def _valid_outfits(recommendation: InitialRecommendation):
        return [
            outfit
            for outfit in recommendation.outfits
            if outfit.validation.all_ids_grounded
            and outfit.validation.hard_constraints_passed
            and outfit.validation.required_slots_complete
        ]

    def _colors(self, item_ids: list[str]) -> frozenset[str]:
        if self.repository is None:
            return frozenset()
        colors: set[str] = set()
        for item_id in item_ids:
            garment = self.repository.get_garment(item_id)
            if garment is None:
                return frozenset()
            colors.add(garment.color)
        return frozenset(colors)

    @staticmethod
    def _representative_color(colors: frozenset[str]) -> str | None:
        return next((color for color in COLOR_VALUES if color in colors), None)

    @classmethod
    def _ranked_valid_outfits(cls, recommendation: InitialRecommendation):
        outfits = cls._valid_outfits(recommendation)
        if len(outfits) < 2:
            return None
        for outfit in outfits:
            score = outfit.server_ranking_score
            if (
                score is None
                or not math.isfinite(float(score))
                or not 0 <= float(score) <= 1
            ):
                return None
        return sorted(
            outfits,
            key=lambda outfit: (
                -float(outfit.server_ranking_score),
                outfit.outfit_id,
            ),
        )

    @classmethod
    def _ranking_skip_reason(
        cls, recommendation: InitialRecommendation
    ) -> str | None:
        outfits = cls._valid_outfits(recommendation)
        if len(outfits) < 2:
            return "INSUFFICIENT_HARD_VALID_OUTFITS"
        if cls._ranked_valid_outfits(recommendation) is None:
            return "INVALID_SERVER_RANKING_EVIDENCE"
        return None

    @staticmethod
    def _has_applicable_preference(
        colors: tuple[str, str],
        confirmed_signals: tuple[MemorySignal, ...],
        session_preferences: tuple[SessionPreference, ...],
    ) -> bool:
        relevant_signals = {
            f"prefer_color:{color}" for color in colors
        } | {f"avoid_color:{color}" for color in colors}
        if any(
            signal.applied_signal in relevant_signals
            for signal in confirmed_signals
        ):
            return True
        return any(
            preference.canonical_kind
            in {"color_preference", "color_avoidance"}
            and preference.canonical_value in colors
            for preference in session_preferences
        )

    def build_evidence(
        self,
        *,
        scene: SceneRequest,
        recommendation: InitialRecommendation,
        confirmed_signals: tuple[MemorySignal, ...],
        session_preferences: tuple[SessionPreference, ...],
        asked_gap_codes: frozenset[str],
    ) -> PreferenceUncertaintyEvidence | None:
        outfits = self._ranked_valid_outfits(recommendation)
        if outfits is None:
            return None
        first, second = outfits[:2]
        first_colors = self._colors(first.items)
        second_colors = self._colors(second.items)
        left = self._representative_color(first_colors - second_colors)
        right = self._representative_color(second_colors - first_colors)
        if left is None or right is None or left == right:
            return None
        gap_code = f"color:{left}_vs_{right}"
        normalized_left = float(first.server_ranking_score)
        normalized_right = float(second.server_ranking_score)
        margin = round(abs(normalized_left - normalized_right), 12)
        left_winner = (
            first.outfit_id
            if normalized_left + self._COUNTERFACTUAL_BOOST
            >= normalized_right
            else second.outfit_id
        )
        right_winner = (
            second.outfit_id
            if normalized_right + self._COUNTERFACTUAL_BOOST
            > normalized_left
            else first.outfit_id
        )
        confirmed = self._has_applicable_preference(
            (left, right), confirmed_signals, session_preferences
        )
        # The budget belongs to the server-owned styling task/session, not to a
        # particular gap or urgency class. Once any preference question was
        # shown, changing the gap or wording cannot reopen the budget.
        already_asked = bool(asked_gap_codes)
        return PreferenceUncertaintyEvidence(
            gap_code=gap_code,
            neutral_margin=margin,
            counterfactual_winners=(left_winner, right_winner),
            urgency=scene.urgency,
            confirmed=confirmed,
            already_asked=already_asked,
        )

    def build_clarification(
        self, evidence: PreferenceUncertaintyEvidence
    ) -> PreferenceClarification:
        return self.pending_clarification(
            gap_code=evidence.gap_code,
            question_id=self._stable_id(
                "prefq",
                evidence.gap_code
                + "|"
                + "|".join(evidence.counterfactual_winners),
            ),
            urgency=evidence.urgency,
        )

    def pending_clarification(
        self, *, gap_code: str, question_id: str, urgency: Urgency
    ) -> PreferenceClarification:
        colors = self.colors_for_gap(gap_code)
        options = tuple(
            PreferenceOption(
                option_id=self._stable_id("prefopt", f"color:{color}"),
                label=COLOR_LABELS[color],
            )
            for color in colors
        ) + (
            PreferenceOption(
                option_id=self._stable_id("prefopt", "neutral"),
                label="你决定",
            ),
        )
        return PreferenceClarification(
            question_id=question_id,
            gap_code=gap_code,
            options=options,
            urgency_budget="last" if urgency == "high" else "normal",
        )

    @staticmethod
    def colors_for_gap(gap_code: str) -> tuple[str, str]:
        prefix, separator, pair = gap_code.partition(":")
        left, versus, right = pair.partition("_vs_")
        if (
            prefix != "color"
            or separator != ":"
            or versus != "_vs_"
            or left not in COLOR_VALUES
            or right not in COLOR_VALUES
            or left == right
        ):
            raise ValueError("unsupported controlled preference gap")
        return left, right

    def resolve_option(
        self, clarification: PreferenceClarification, option_id: str
    ) -> str | None:
        if option_id == self._stable_id("prefopt", "neutral"):
            return None
        for color in self.colors_for_gap(clarification.gap_code):
            if option_id == self._stable_id("prefopt", f"color:{color}"):
                return color
        raise ValueError("preference option is not authoritative")

    def question_copy(self, clarification: PreferenceClarification) -> str:
        left, right = self.colors_for_gap(clarification.gap_code)
        return f"这两套里，你更偏向{COLOR_LABELS[left]}，还是{COLOR_LABELS[right]}？"

    def evaluate(
        self,
        *,
        scene: SceneRequest,
        recommendation: InitialRecommendation,
        confirmed_signals: tuple[MemorySignal, ...],
        session_preferences: tuple[SessionPreference, ...],
        asked_gap_codes: frozenset[str],
    ) -> PreferenceDecision:
        original = tuple(outfit.outfit_id for outfit in recommendation.outfits)
        evidence = self.build_evidence(
            scene=scene,
            recommendation=recommendation,
            confirmed_signals=confirmed_signals,
            session_preferences=session_preferences,
            asked_gap_codes=asked_gap_codes,
        )
        if evidence is None:
            return PreferenceDecision(
                None,
                original,
                self._ranking_skip_reason(recommendation)
                or "NO_MATERIAL_GAP",
            )
        if not self.should_ask(evidence):
            reason = (
                "APPLICABLE_PREFERENCE_PRESENT"
                if evidence.confirmed
                else (
                    "QUESTION_BUDGET_SPENT"
                    if evidence.already_asked
                    else "NO_MATERIAL_GAP"
                )
            )
            return PreferenceDecision(None, original, reason)
        return PreferenceDecision(
            self.build_clarification(evidence),
            original,
            "MATERIAL_PREFERENCE_GAP",
        )

    def reorder_ids(
        self, recommendation: InitialRecommendation, preferred_color: str | None
    ) -> tuple[str, ...]:
        outfits = self._valid_outfits(recommendation)
        if len(outfits) != len(recommendation.outfits):
            raise ValueError("preference reorder requires hard-valid outfits")
        if preferred_color is None:
            return tuple(outfit.outfit_id for outfit in outfits)
        if preferred_color not in COLOR_VALUES:
            raise ValueError("preference option is outside the controlled closure")
        return tuple(
            outfit.outfit_id
            for outfit in sorted(
                outfits,
                key=lambda outfit: (
                    0 if preferred_color in self._colors(outfit.items) else 1,
                    next(
                        index
                        for index, current in enumerate(outfits)
                        if current.outfit_id == outfit.outfit_id
                    ),
                ),
            )
        )
