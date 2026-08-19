from __future__ import annotations

import itertools
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from .memory_service import MemorySignal, ShoeSimilaritySignature
from .models import (
    Garment,
    OutfitValidation,
    RecommendedOutfit,
    SceneRequest,
)
from .providers import DenseAdapter, ProviderUnavailable
from .repository import FixtureRepository


@dataclass(frozen=True)
class FilterResult:
    eligible: tuple[Garment, ...]
    filtered: tuple[dict[str, Any], ...]

    @property
    def eligible_ids(self) -> set[str]:
        return {item.garment_id for item in self.eligible}


class HardFilter:
    """Recall-time hard filter. Downstream stages only receive this eligible set."""

    @staticmethod
    def _reason(item: Garment, scene: SceneRequest) -> str | None:
        constraints = scene.constraints
        if (
            constraints.weather_requirement == "rain"
            and item.slot == "outer"
            and getattr(item, "waterproof", None) is not True
        ):
            # fixtures_v1.0 has no structured waterproof field. A garment name
            # such as “风衣” is not evidence, so every unverified required outer
            # is removed before Rule/BM25/Dense/RRF see it.
            return "RAIN_PROOF_EVIDENCE_MISSING"
        if item.status != "available":
            return f"STATUS_{item.status.upper()}"
        if item.color in constraints.taboo_colors:
            return "TABOO_COLOR"
        if item.garment_id in constraints.excluded_items:
            return "EXCLUDED_ITEM"
        notes = set(constraints.comfort_notes)
        if "no_skirts" in notes and (
            item.slot == "dress" or "裙" in item.name
        ):
            return "EXCLUDED_CATEGORY_SKIRT"
        if item.slot == "shoes" and notes.intersection({"no_high_heels", "no_heels"}):
            name = item.name.lower()
            if any(token in name for token in ("高跟", "细跟", "stiletto")):
                return "EXCLUDED_HIGH_HEEL"
            if "跟" in name:
                return "EXCLUDED_HEEL"
            if not any(token in name for token in ("运动鞋", "乐福鞋", "平底鞋")):
                # The fixture has no heel_type/height field. Unknown boots or
                # generic shoes are not assumed safe under an explicit ban.
                return "HEEL_EVIDENCE_MISSING"
        if constraints.season and not (
            constraints.season in item.seasons or "all" in item.seasons
        ):
            return "SEASON_MISMATCH"
        if (
            constraints.weather_requirement == "cold"
            and item.slot == "outer"
            and item.warmth < 3
        ):
            return "COLD_OUTER_INSUFFICIENT"
        if constraints.weather_requirement == "warm" and item.warmth > 3:
            return "WARM_WEATHER_TOO_HEAVY"
        return None

    def apply(self, garments: Iterable[Garment], scene: SceneRequest) -> FilterResult:
        eligible: list[Garment] = []
        filtered: list[dict[str, Any]] = []
        for item in garments:
            if item.user_id != scene.user_id:
                filtered.append({"item_id": item.garment_id, "reason": "WRONG_OWNER"})
                continue
            reason = self._reason(item, scene)
            if reason:
                filtered.append({"item_id": item.garment_id, "reason": reason})
            else:
                eligible.append(item)
        return FilterResult(tuple(eligible), tuple(filtered))

    def item_passes(self, item: Garment, scene: SceneRequest) -> bool:
        return item.user_id == scene.user_id and self._reason(item, scene) is None


def tokenize(text: str) -> list[str]:
    chunks = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+", text.lower())
    tokens: list[str] = []
    for chunk in chunks:
        tokens.append(chunk)
        if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
            tokens.extend(chunk)
            tokens.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return tokens


GOAL_STYLE_MAP = {
    "reliable": {"business", "classic", "formal", "smart"},
    "modern": {"smart", "street", "simple"},
    "comfortable": {"soft", "sporty", "simple", "campus"},
    "polished": {"business", "formal", "smart", "classic"},
    "low_key": {"simple", "classic", "campus"},
    "confident": {"business", "formal", "smart"},
    "cool": {"street", "sporty", "campus"},
}


class HybridRetriever:
    _MEMORY_SIMILARITY_POLICY = "demote_structured_similar_shoes_v1"
    _MEMORY_SIMILARITY_THRESHOLD = 3
    _MEMORY_POST_RRF_PENALTY = 0.01

    def __init__(self, dense: DenseAdapter):
        self.dense = dense

    @staticmethod
    def _rule_score(item: Garment, scene: SceneRequest, favorite: set[str]) -> float:
        score = 0.0
        if scene.occasion in item.occasions:
            score += 4.0
        formal_target = {
            "interview": 4,
            "meeting": 4,
            "commute": 3,
            "date": 2,
            "party": 2,
            "travel": 1,
            "daily": 1,
            "outdoor": 1,
            "sports": 0,
            "home": 0,
        }[scene.occasion]
        score += max(0.0, 2.0 - abs(item.formal - formal_target) * 0.6)
        desired_styles = set().union(
            *(GOAL_STYLE_MAP.get(goal, set()) for goal in scene.goals)
        )
        score += len(desired_styles.intersection(item.styles)) * 1.3
        if item.color in favorite:
            score += 0.7
        if scene.constraints.weather_requirement == "cold":
            score += item.warmth * 0.35
        elif scene.constraints.weather_requirement == "warm":
            score += (6 - item.warmth) * 0.25
        if "long_walk" in scene.constraints.comfort_notes and item.slot == "shoes":
            if "运动" in item.name or "sporty" in item.styles:
                score += 3.0
        return round(score, 5)

    @staticmethod
    def _bm25(query: str, documents: dict[str, str]) -> list[tuple[str, float]]:
        query_tokens = tokenize(query)
        tokenized = {item_id: tokenize(text) for item_id, text in documents.items()}
        if not tokenized:
            return []
        average_length = sum(map(len, tokenized.values())) / len(tokenized)
        document_frequency: Counter[str] = Counter()
        for tokens in tokenized.values():
            document_frequency.update(set(tokens))
        scores: list[tuple[str, float]] = []
        k1, b = 1.5, 0.75
        for item_id, tokens in tokenized.items():
            frequency = Counter(tokens)
            score = 0.0
            for token in query_tokens:
                count = frequency.get(token, 0)
                if not count:
                    continue
                df = document_frequency[token]
                inverse_frequency = math.log(
                    1 + (len(tokenized) - df + 0.5) / (df + 0.5)
                )
                denominator = count + k1 * (
                    1 - b + b * len(tokens) / max(average_length, 1)
                )
                score += inverse_frequency * count * (k1 + 1) / denominator
            scores.append((item_id, round(score, 6)))
        return sorted(scores, key=lambda pair: (-pair[1], pair[0]))

    @staticmethod
    def _rrf(
        ranked_lists: list[tuple[str, list[tuple[str, float]], float]],
        eligible_ids: set[str],
        k: int = 60,
    ) -> list[tuple[str, float]]:
        scores: defaultdict[str, float] = defaultdict(float)
        for _name, ranking, weight in ranked_lists:
            for rank, (item_id, _score) in enumerate(ranking, 1):
                if item_id in eligible_ids:
                    scores[item_id] += weight / (k + rank)
        return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))

    @classmethod
    def _is_structurally_similar_shoe(
        cls, item: Garment, signature: ShoeSimilaritySignature
    ) -> bool:
        """Closed, metadata-only similarity; names and IDs are not evidence."""

        if item.slot != "shoes":
            return False
        evidence_count = sum(
            (
                bool(set(item.styles).intersection(signature.styles)),
                item.fit == signature.fit,
                item.material == signature.material,
                abs(item.formal - signature.formal) <= 1,
                abs(item.warmth - signature.warmth) <= 1,
            )
        )
        return evidence_count >= cls._MEMORY_SIMILARITY_THRESHOLD

    @classmethod
    def _apply_confirmed_memory_rerank(
        cls,
        fused: list[tuple[str, float]],
        eligible_by_id: dict[str, Garment],
        scene: SceneRequest,
        memory_signals: Iterable[MemorySignal],
    ) -> tuple[list[tuple[str, float]], dict[str, Any]]:
        policies = tuple(
            signal
            for signal in memory_signals
            if signal.type == "feedback"
            and signal.applied_signal == "long_walk"
            and signal.similarity_policy == cls._MEMORY_SIMILARITY_POLICY
            and signal.shoe_signature is not None
        )
        relevant = "long_walk" in scene.constraints.comfort_notes
        policy_trace: dict[str, Any] = {
            "version": "confirmed_feedback_shoe_similarity_v1",
            "stage": "post_rrf",
            "policy": cls._MEMORY_SIMILARITY_POLICY,
            "relevant": relevant,
            "active_policy_count": len(policies),
            "applied": False,
            "demoted_count": 0,
            "penalty": cls._MEMORY_POST_RRF_PENALTY,
            "penalty_mode": "fixed_once_per_item",
            "score_penalty_applied_count": 0,
            "total_score_penalty": 0.0,
            "same_universe_rank_lowered_count": 0,
            "same_universe_rank_unchanged_count": 0,
            "same_universe_rank_improved_count": 0,
            "target_ids_logged": False,
            "signatures_logged": False,
            "demoted_ids_logged": False,
        }
        if not policies:
            policy_trace["reason"] = "NO_CONFIRMED_SIMILARITY_POLICY"
            return fused, policy_trace
        if not relevant:
            policy_trace["reason"] = "SCENE_NOT_LONG_WALK_RELEVANT"
            return fused, policy_trace

        demoted_ids = {
            item_id
            for item_id, _score in fused
            if (item := eligible_by_id.get(item_id)) is not None
            and any(
                item.garment_id != signal.target_item_id
                and signal.shoe_signature is not None
                and cls._is_structurally_similar_shoe(
                    item, signal.shoe_signature
                )
                for signal in policies
            )
        }
        if not demoted_ids:
            policy_trace["reason"] = "NO_STRUCTURALLY_SIMILAR_ELIGIBLE_SHOES"
            return fused, policy_trace

        adjusted = [
            (
                item_id,
                max(
                    0.0,
                    score
                    - (
                        cls._MEMORY_POST_RRF_PENALTY
                        if item_id in demoted_ids
                        else 0.0
                    ),
                ),
            )
            for item_id, score in fused
        ]
        adjusted.sort(key=lambda pair: (-pair[1], pair[0]))
        before_ranks = {item_id: rank for rank, (item_id, _score) in enumerate(fused, 1)}
        after_ranks = {
            item_id: rank for rank, (item_id, _score) in enumerate(adjusted, 1)
        }
        before_scores = dict(fused)
        after_scores = dict(adjusted)
        rank_deltas = [
            after_ranks[item_id] - before_ranks[item_id] for item_id in demoted_ids
        ]
        actual_total_score_penalty = round(
            sum(
                before_scores[item_id] - after_scores[item_id]
                for item_id in demoted_ids
            ),
            6,
        )
        policy_trace.update(
            {
                "applied": True,
                "demoted_count": len(demoted_ids),
                "score_penalty_applied_count": len(demoted_ids),
                "total_score_penalty": actual_total_score_penalty,
                "same_universe_rank_lowered_count": sum(
                    delta > 0 for delta in rank_deltas
                ),
                "same_universe_rank_unchanged_count": sum(
                    delta == 0 for delta in rank_deltas
                ),
                "same_universe_rank_improved_count": sum(
                    delta < 0 for delta in rank_deltas
                ),
                "reason": "CONFIRMED_FEEDBACK_STRUCTURED_SIMILARITY",
            }
        )
        return adjusted, policy_trace

    def retrieve(
        self,
        scene: SceneRequest,
        result: FilterResult,
        favorite_colors: Iterable[str],
        memory_signals: Iterable[MemorySignal] = (),
        soft_memory_terms: Iterable[str] = (),
    ) -> tuple[list[Garment], dict[str, Any], list[dict[str, Any]]]:
        eligible_by_id = {item.garment_id: item for item in result.eligible}
        eligible_ids = set(eligible_by_id)
        documents = {
            item.garment_id: (
                f"{item.name} {item.search_text} slot:{item.slot} "
                f"occasion:{' '.join(item.occasions)} style:{' '.join(item.styles)} "
                f"fit:{item.fit} color:{item.color} "
                f"formal:{item.formal} warmth:{item.warmth}"
            )
            for item in result.eligible
        }
        rule = sorted(
            [
                (
                    item.garment_id,
                    self._rule_score(item, scene, set(favorite_colors)),
                )
                for item in result.eligible
            ],
            key=lambda pair: (-pair[1], pair[0]),
        )
        semantic_query = (
            f"{scene.query_text} occasion:{scene.occasion} "
            f"goal:{' '.join(scene.goals)} weather:{scene.constraints.weather_requirement}"
        )
        controlled_soft_terms = tuple(
            term
            for term in soft_memory_terms
            if re.fullmatch(r"(?:fit|style|comfort):[a-z_]+", term)
        )
        if controlled_soft_terms:
            semantic_query += " " + " ".join(controlled_soft_terms)
        bm25 = self._bm25(semantic_query, documents)
        fallback_events: list[dict[str, Any]] = []
        dense: list[tuple[str, float]] = []
        dense_status = "disabled"
        try:
            dense = self.dense.rank(semantic_query, documents)
            dense_status = "ok" if self.dense.enabled else "disabled"
        except ProviderUnavailable as exc:
            dense_status = "degraded"
            fallback_events.append(
                {"component": "dense", "fallback": "rule_bm25", "reason": str(exc)}
            )
        ranked_lists = [("rule", rule, 1.4), ("bm25", bm25, 1.0)]
        if dense:
            ranked_lists.append(("dense", dense, 0.8))
        fused = self._rrf(ranked_lists, eligible_ids)
        fused, memory_rerank = self._apply_confirmed_memory_rerank(
            fused, eligible_by_id, scene, memory_signals
        )
        # Defensive intersection prevents a buggy ranker from restoring filtered IDs.
        ranked_items = [eligible_by_id[item_id] for item_id, _ in fused if item_id in eligible_ids]

        def summarize(ranking: list[tuple[str, float]]) -> list[dict[str, Any]]:
            return [
                {"item_id": item_id, "score": round(score, 6)}
                for item_id, score in ranking
                if item_id in eligible_ids
            ][:30]

        trace = {
            "eligible_count": len(eligible_ids),
            "rule": summarize(rule),
            "bm25": summarize(bm25),
            "dense": summarize(dense),
            "dense_status": dense_status,
            "dense_version": self.dense.version,
            "rrf": summarize(fused),
            "rrf_k": 60,
            "post_fusion_intersection": True,
            "final_ranking_stage": "post_rrf_memory_policy_v1",
            "soft_memory_term_count": len(controlled_soft_terms),
            "memory_rerank": memory_rerank,
        }
        return ranked_items, trace, fallback_events


def _core_complete(items: list[Garment]) -> bool:
    slots = {item.slot for item in items}
    if "shoes" not in slots:
        return False
    has_dress = "dress" in slots
    has_separates = "top" in slots and "bottom" in slots
    if has_dress and ("top" in slots or "bottom" in slots):
        return False
    return has_dress or has_separates


def _required_complete(items: list[Garment], required_slots: list[str]) -> bool:
    slots = {item.slot for item in items}
    for slot in required_slots:
        if slot in {"top", "bottom"} and "dress" in slots:
            continue
        if slot not in slots:
            return False
    return _core_complete(items)


def _occasion_semihard_pass(items: Iterable[Garment], scene: SceneRequest) -> bool:
    if scene.occasion not in {"interview", "meeting"}:
        return True
    core = [
        item
        for item in items
        if item.slot in {"top", "bottom", "dress", "shoes"}
    ]
    return bool(core) and all(
        scene.occasion in item.occasions and item.formal >= 3 for item in core
    )


class OutfitAssembler:
    def __init__(self, repository: FixtureRepository, hard_filter: HardFilter):
        self.repository = repository
        self.hard_filter = hard_filter

    @staticmethod
    def _combo_score(
        combo: tuple[Garment, ...], rank_score: dict[str, float], scene: SceneRequest
    ) -> float:
        score = sum(rank_score.get(item.garment_id, 0) for item in combo)
        score += sum(scene.occasion in item.occasions for item in combo) * 0.03
        styles = set(itertools.chain.from_iterable(item.styles for item in combo))
        desired = set().union(*(GOAL_STYLE_MAP.get(goal, set()) for goal in scene.goals))
        score += len(styles.intersection(desired)) * 0.02
        return score

    @staticmethod
    def _signature(combo: tuple[Garment, ...]) -> tuple[Any, ...]:
        slots = {item.slot for item in combo}
        path = "dress" if "dress" in slots else "separates"
        average_formal = sum(item.formal for item in combo) / len(combo)
        formal_bucket = round(average_formal)
        styles = Counter(itertools.chain.from_iterable(item.styles for item in combo))
        dominant_style = styles.most_common(1)[0][0] if styles else "none"
        comfort = sum(item.fit in {"loose", "regular"} for item in combo)
        return path, formal_bucket, dominant_style, comfort

    @staticmethod
    def _jaccard(left: tuple[Garment, ...], right: tuple[Garment, ...]) -> float:
        a = {item.garment_id for item in left}
        b = {item.garment_id for item in right}
        return len(a & b) / max(1, len(a | b))

    def _valid_combo(self, combo: tuple[Garment, ...], scene: SceneRequest) -> bool:
        if len({item.garment_id for item in combo}) != len(combo):
            return False
        if not _required_complete(list(combo), scene.constraints.required_slots):
            return False
        if not all(self.hard_filter.item_passes(item, scene) for item in combo):
            return False
        if not _occasion_semihard_pass(combo, scene):
            return False
        if scene.constraints.weather_requirement == "rain":
            # Fixture contract has no rain-proof evidence; fail closed rather than guess.
            return False
        if scene.occasion in {"interview", "meeting"}:
            core = [item for item in combo if item.slot in {"top", "bottom", "dress", "shoes"}]
            if core and sum(item.formal for item in core) / len(core) < 2.5:
                return False
        return True

    def _candidate_combos(
        self, ranked: list[Garment], scene: SceneRequest
    ) -> list[tuple[Garment, ...]]:
        by_slot: defaultdict[str, list[Garment]] = defaultdict(list)
        for item in ranked:
            if len(by_slot[item.slot]) < 6:
                by_slot[item.slot].append(item)
        combos: list[tuple[Garment, ...]] = []
        required_outer = "outer" in scene.constraints.required_slots
        outers: list[Garment | None] = by_slot["outer"][:3] if required_outer else [None]
        for top, bottom, shoes, outer in itertools.product(
            by_slot["top"][:5],
            by_slot["bottom"][:5],
            by_slot["shoes"][:4],
            outers,
        ):
            combo = tuple(item for item in (top, bottom, shoes, outer) if item)
            if self._valid_combo(combo, scene):
                combos.append(combo)
        for dress, shoes, outer in itertools.product(
            by_slot["dress"][:5], by_slot["shoes"][:4], outers
        ):
            combo = tuple(item for item in (dress, shoes, outer) if item)
            if self._valid_combo(combo, scene):
                combos.append(combo)
        # Reuse only positive fixture outfits, with a full current-constraint recheck.
        eligible_ids = {item.garment_id for item in ranked}
        for seed in self.repository.list_outfits(scene.user_id):
            if not seed.positive or not set(seed.items).issubset(eligible_ids):
                continue
            items = tuple(self.repository.get_garment(item_id) for item_id in seed.items)
            if all(items) and self._valid_combo(items, scene):  # type: ignore[arg-type]
                combos.append(items)  # type: ignore[arg-type]
        unique: dict[tuple[str, ...], tuple[Garment, ...]] = {}
        for combo in combos:
            key = tuple(sorted(item.garment_id for item in combo))
            unique[key] = combo
        return list(unique.values())

    @staticmethod
    def _profile_score(combo: tuple[Garment, ...], profile: str) -> float:
        if profile == "comfortable":
            return sum(
                item.fit in {"loose", "regular"}
                or "sporty" in item.styles
                or "soft" in item.styles
                for item in combo
            )
        if profile == "modern":
            return sum(
                bool({"smart", "street", "simple"}.intersection(item.styles))
                for item in combo
            )
        return sum(item.formal for item in combo) / len(combo)

    def _select_diverse(
        self,
        combos: list[tuple[Garment, ...]],
        rank_score: dict[str, float],
        scene: SceneRequest,
    ) -> list[tuple[str, tuple[Garment, ...]]]:
        labels = [
            ("先试这个：最稳妥", "reliable"),
            ("备选：更舒适", "comfortable"),
            ("备选：更现代", "modern"),
        ]
        selected: list[tuple[str, tuple[Garment, ...]]] = []
        for label, profile in labels:
            candidates = sorted(
                combos,
                key=lambda combo: (
                    -(
                        self._combo_score(combo, rank_score, scene)
                        + self._profile_score(combo, profile) * 0.01
                    ),
                    tuple(item.garment_id for item in combo),
                ),
            )
            for candidate in candidates:
                if any(
                    self._jaccard(candidate, prior) > 0.67
                    or self._signature(candidate) == self._signature(prior)
                    for _, prior in selected
                ):
                    continue
                selected.append((label, candidate))
                break
        return selected[:3]

    def _alternatives(
        self,
        combo: tuple[Garment, ...],
        ranked: list[Garment],
        scene: SceneRequest,
    ) -> dict[str, list[str]]:
        alternatives: dict[str, list[str]] = {}
        current_ids = {item.garment_id for item in combo}
        for current in combo:
            candidates: list[str] = []
            for candidate in ranked:
                if candidate.slot != current.slot or candidate.garment_id in current_ids:
                    continue
                replaced = tuple(
                    candidate if item.garment_id == current.garment_id else item
                    for item in combo
                )
                if self._valid_combo(replaced, scene):
                    candidates.append(candidate.garment_id)
                if len(candidates) == 2:
                    break
            if candidates:
                alternatives[current.garment_id] = candidates
        return alternatives

    @staticmethod
    def _reasons(combo: tuple[Garment, ...], scene: SceneRequest) -> list[str]:
        average_formal = sum(item.formal for item in combo) / len(combo)
        reasons = ["全部单品来自当前用户衣橱且状态可用"]
        reasons.append(
            f"核心单品平均正式度 {average_formal:.1f}/4，与{scene.occasion}场景共同校验"
        )
        if scene.goals:
            reasons.append(f"排序重点响应目标：{'、'.join(scene.goals)}")
        return reasons[:3]

    @staticmethod
    def _risks(combo: tuple[Garment, ...], scene: SceneRequest) -> list[str]:
        slots = {item.slot for item in combo}
        if scene.constraints.weather_requirement == "cold" and "outer" not in slots:
            return ["降温场景缺少可验证的保暖外套"]
        if "long_walk" in scene.constraints.comfort_notes and not any(
            item.slot == "shoes" and ("运动" in item.name or "sporty" in item.styles)
            for item in combo
        ):
            return ["需要长时间走路，出门前请确认这双鞋的实际舒适度"]
        return ["活动时长和室内外温差未知，出门前请做一次舒适度确认"]

    def assemble(
        self, ranked: list[Garment], scene: SceneRequest, request_suffix: str
    ) -> tuple[list[RecommendedOutfit], str | None]:
        if scene.constraints.weather_requirement == "rain":
            return [], "现有 Fixture 没有防雨属性证据，无法在不猜测的前提下组成雨天必需方案。"
        rank_score = {
            item.garment_id: (len(ranked) - index) / max(1, len(ranked))
            for index, item in enumerate(ranked)
        }
        combos = self._candidate_combos(ranked, scene)
        selected = self._select_diverse(combos, rank_score, scene)
        outfits: list[RecommendedOutfit] = []
        for index, (label, combo) in enumerate(selected, 1):
            outfits.append(
                RecommendedOutfit(
                    outfit_id=f"generated_{request_suffix}_{index}",
                    strategy_label=label,
                    items=[item.garment_id for item in combo],
                    reasons=self._reasons(combo, scene),
                    risks=self._risks(combo, scene),
                    alternatives=self._alternatives(combo, ranked, scene),
                    is_primary=index == 1,
                    trust_statement="全部来自现有衣橱",
                    validation=OutfitValidation(
                        all_ids_grounded=True,
                        hard_constraints_passed=True,
                        required_slots_complete=True,
                    ),
                )
            )
        gap = None
        if len(outfits) < 3:
            if outfits:
                gap = (
                    f"当前硬约束下只能形成 {len(outfits)} 个有明显差异的完整方向；"
                    "没有放宽禁忌、可用状态或天气要求。"
                )
            else:
                missing = []
                by_slot = Counter(item.slot for item in ranked)
                for slot in scene.constraints.required_slots:
                    if slot in {"top", "bottom"} and by_slot["dress"]:
                        continue
                    if by_slot[slot] == 0:
                        missing.append(slot)
                if scene.occasion in {"interview", "meeting"}:
                    compatible_shoes = [
                        item
                        for item in ranked
                        if item.slot == "shoes"
                        and scene.occasion in item.occasions
                        and item.formal >= 3
                    ]
                    if not compatible_shoes:
                        occasion_name = "正式面试" if scene.occasion == "interview" else "正式会议"
                        gap = (
                            f"现有衣橱缺少可用且符合{occasion_name}要求的鞋履；"
                            "未用低正式度或场合不匹配的鞋凑数。"
                        )
                    else:
                        gap = "现有衣橱的关键槽位无法同时满足当前场合与正式度要求。"
                else:
                    gap = (
                        "现有衣橱无法在当前硬约束下组成完整方向。"
                        + (f" 缺少合法槽位：{', '.join(missing)}。" if missing else "")
                    )
        return outfits, gap


class RecommendationValidator:
    def __init__(self, repository: FixtureRepository, hard_filter: HardFilter):
        self.repository = repository
        self.hard_filter = hard_filter

    def validate(
        self,
        scene: SceneRequest,
        outfits: list[RecommendedOutfit],
        eligible_ids: set[str],
    ) -> tuple[list[RecommendedOutfit], dict[str, Any]]:
        user_whitelist = self.repository.garment_ids(scene.user_id)
        accepted: list[RecommendedOutfit] = []
        rejected_reasons: Counter[str] = Counter()
        for outfit in outfits:
            item_ids = set(outfit.items)
            if not item_ids.issubset(user_whitelist):
                rejected_reasons["NON_WHITELIST_ID"] += 1
                continue
            if not item_ids.issubset(eligible_ids):
                rejected_reasons["FILTERED_ID_RESTORED"] += 1
                continue
            items = [self.repository.get_garment(item_id) for item_id in outfit.items]
            if any(item is None for item in items):
                rejected_reasons["MISSING_REPOSITORY_ITEM"] += 1
                continue
            typed_items: list[Garment] = [item for item in items if item is not None]
            if not _required_complete(typed_items, scene.constraints.required_slots):
                rejected_reasons["SLOT_INCOMPLETE"] += 1
                continue
            if not all(self.hard_filter.item_passes(item, scene) for item in typed_items):
                rejected_reasons["HARD_CONSTRAINT_FAILURE"] += 1
                continue
            if not _occasion_semihard_pass(typed_items, scene):
                rejected_reasons["OCCASION_SEMIHARD_FAILURE"] += 1
                continue
            safe_alternatives: dict[str, list[str]] = {}
            for source_id, alternatives in outfit.alternatives.items():
                if source_id not in item_ids:
                    continue
                source = self.repository.get_garment(source_id)
                if source is None:
                    continue
                valid: list[str] = []
                for candidate_id in alternatives:
                    candidate = self.repository.get_garment(candidate_id)
                    if (
                        candidate_id not in user_whitelist
                        or candidate_id not in eligible_ids
                        or candidate is None
                        or candidate.slot != source.slot
                        or not self.hard_filter.item_passes(candidate, scene)
                    ):
                        continue
                    replaced = [
                        candidate if item.garment_id == source_id else item
                        for item in typed_items
                    ]
                    if not _required_complete(replaced, scene.constraints.required_slots):
                        continue
                    if not _occasion_semihard_pass(replaced, scene):
                        continue
                    valid.append(candidate_id)
                if valid:
                    safe_alternatives[source_id] = valid[:2]
            outfit.alternatives = safe_alternatives
            outfit.validation = OutfitValidation(
                all_ids_grounded=True,
                hard_constraints_passed=True,
                required_slots_complete=True,
            )
            accepted.append(outfit)
        if accepted:
            for index, outfit in enumerate(accepted):
                outfit.is_primary = index == 0
        return accepted[:3], {
            "whitelist_size": len(user_whitelist),
            "eligible_whitelist_size": len(eligible_ids),
            "accepted_outfit_count": len(accepted[:3]),
            "rejected_outfit_count": sum(rejected_reasons.values()),
            "rejected_reason_counts": dict(rejected_reasons),
            "all_ids_grounded": not rejected_reasons.get("NON_WHITELIST_ID", 0),
            "hard_constraints_passed": not any(
                rejected_reasons[reason]
                for reason in (
                    "FILTERED_ID_RESTORED",
                    "HARD_CONSTRAINT_FAILURE",
                    "OCCASION_SEMIHARD_FAILURE",
                )
            ),
            "invalid_ids_redacted": True,
        }
