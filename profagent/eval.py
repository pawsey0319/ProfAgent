from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from .config import Settings
from .models import SceneParseInput


REPORT_VERSION = "r1_demo_v1"
EVAL_VERSION = "eval_fixtures_v1.0_30"
REPORT_JSON = Path("reports/eval/r1_demo_v1.json")
REPORT_MARKDOWN = Path("reports/eval/r1_demo_v1.md")
COMMAND = "conda run -n torch128 python -m profagent.eval"

THRESHOLDS: dict[str, dict[str, Any]] = {
    "urgency_accuracy": {"operator": ">=", "value": 0.95},
    "high_urgency_shopping_gate_accuracy": {"operator": "==", "value": 1.0},
    "high_urgency_catalog_calls": {"operator": "==", "value": 0},
    "item_hallucinations": {"operator": "==", "value": 0},
    "hard_constraint_violations": {"operator": "==", "value": 0},
    "slot_completeness": {"operator": ">=", "value": 0.95},
}

_AVAILABILITY_EXPECTATIONS: dict[str, dict[str, str]] = {
    "e019": {"g024": "STATUS_LAUNDRY"},
    "e020": {"g038": "STATUS_LAUNDRY"},
    "e021": {
        "g048": "STATUS_RESERVED",
        "g050": "STATUS_UNAVAILABLE",
    },
}

_HIGH_SUPPORTIVE_PREFIXES = (
    "先把决定变简单",
    "听起来",
    "我不能用衣服治疗",
    "我不会评价",
)

# Independent query-only oracle. These cases have an explicit relative deadline,
# an explicit unknown-deadline phrase, a season-as-planning cue, or a safety query
# with no event deadline (which must fail closed to unknown/high). Structured eval
# labels are deliberately not read by this assurance.
_QUERY_ONLY_EXPECTATIONS: dict[str, dict[str, Any]] = {
    "e001": {"horizon": "now", "urgency": "high", "intent": "recommend", "shopping": False},
    "e002": {"horizon": "today", "urgency": "high", "intent": "recommend", "shopping": False},
    "e003": {"horizon": "now", "urgency": "high", "intent": "recommend", "shopping": False},
    "e004": {"horizon": "unknown", "urgency": "high", "intent": "recommend", "shopping": False},
    "e005": {"horizon": "now", "urgency": "high", "intent": "recommend", "shopping": False},
    "e006": {"horizon": "today", "urgency": "high", "intent": "recommend", "shopping": False},
    # Mixed emotion plus an actionable wardrobe request remains styling intent.
    "e007": {"horizon": "now", "urgency": "high", "intent": "recommend", "shopping": False},
    "e008": {"horizon": "unknown", "urgency": "high", "intent": "recommend", "shopping": False},
    "e009": {"horizon": "soon", "urgency": "medium", "intent": "fill_gap", "shopping": True},
    "e010": {"horizon": "planned", "urgency": "low", "intent": "fill_gap", "shopping": True},
    "e011": {"horizon": "soon", "urgency": "medium", "intent": "browse", "shopping": True},
    "e012": {"horizon": "planned", "urgency": "low", "intent": "fill_gap", "shopping": True},
    "e013": {"horizon": "planned", "urgency": "low", "intent": "fill_gap", "shopping": True},
    "e014": {"horizon": "soon", "urgency": "medium", "intent": "recommend", "shopping": False},
    "e015": {"horizon": "planned", "urgency": "low", "intent": "recommend", "shopping": False},
    "e016": {"horizon": "soon", "urgency": "medium", "intent": "recommend", "shopping": False},
    "e017": {"horizon": "planned", "urgency": "low", "intent": "recommend", "shopping": False},
    "e018": {"horizon": "planned", "urgency": "low", "intent": "recommend", "shopping": False},
    "e028": {"horizon": "soon", "urgency": "medium", "intent": "vent", "shopping": False},
    "e029": {"horizon": "unknown", "urgency": "high", "intent": "vent", "shopping": False},
    "e030": {"horizon": "unknown", "urgency": "high", "intent": "vent", "shopping": False},
}


def _root_dir(root_dir: Path | None = None) -> Path:
    return (root_dir or Path(__file__).resolve().parents[1]).resolve()


def _load_cases(root: Path) -> tuple[list[dict[str, Any]], str]:
    source = root / "data" / "eval" / "eval.jsonl"
    raw = source.read_bytes()
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:  # pragma: no cover - validate.py owns corruption tests
            raise RuntimeError(f"invalid eval JSONL at line {line_number}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"eval row {line_number} is not an object")
        cases.append(value)
    return cases, hashlib.sha256(raw).hexdigest()


def independent_slots_complete(items: list[Any], required_slots: list[str]) -> bool:
    """Pure eval oracle for slot completeness; no retrieval helper is reused."""

    slots = {item.slot for item in items}
    for required in required_slots:
        if required in {"top", "bottom"} and "dress" in slots:
            continue
        if required not in slots:
            return False
    return "shoes" in slots and (
        "dress" in slots or {"top", "bottom"}.issubset(slots)
    )


def independent_item_violations(item: Any, scene: Any) -> list[str]:
    """Pure fixture-field hard oracle, independent from business HardFilter."""

    reasons: list[str] = []
    constraints = scene.constraints
    if item.user_id != scene.user_id:
        reasons.append("WRONG_OWNER")
    if item.status != "available":
        reasons.append(f"STATUS_{item.status.upper()}")
    if item.color in set(constraints.taboo_colors):
        reasons.append("TABOO_COLOR")
    if item.garment_id in set(constraints.excluded_items):
        reasons.append("EXCLUDED_ITEM")

    notes = set(constraints.comfort_notes)
    if "no_skirts" in notes and (item.slot == "dress" or "裙" in item.name):
        reasons.append("EXCLUDED_CATEGORY_SKIRT")
    if item.slot == "shoes" and notes.intersection({"no_high_heels", "no_heels"}):
        lowered = item.name.lower()
        if any(token in lowered for token in ("高跟", "细跟", "stiletto")):
            reasons.append("EXCLUDED_HIGH_HEEL")
        elif "跟" in lowered:
            reasons.append("EXCLUDED_HEEL")
        elif not any(token in lowered for token in ("运动鞋", "乐福鞋", "平底鞋")):
            # The fixture has no structured heel height. Unknown evidence fails closed.
            reasons.append("HEEL_EVIDENCE_MISSING")

    if constraints.season and not (
        constraints.season in item.seasons or "all" in item.seasons
    ):
        reasons.append("SEASON_MISMATCH")
    if (
        constraints.weather_requirement == "cold"
        and item.slot == "outer"
        and item.warmth < 3
    ):
        reasons.append("COLD_OUTER_INSUFFICIENT")
    if (
        constraints.weather_requirement == "rain"
        and item.slot == "outer"
        and getattr(item, "waterproof", None) is not True
    ):
        reasons.append("RAIN_PROOF_EVIDENCE_MISSING")
    if constraints.weather_requirement == "warm" and item.warmth > 3:
        reasons.append("WARM_WEATHER_TOO_HEAVY")
    return reasons


def independent_outfit_violations(
    item_ids: list[str] | tuple[str, ...],
    scene: Any,
    garment_by_id: dict[str, Any],
) -> list[str]:
    """Recompute every outfit invariant from fixture records and scene fields."""

    reasons: list[str] = []
    if len(item_ids) != len(set(item_ids)):
        reasons.append("DUPLICATE_ITEM_ID")
    items: list[Any] = []
    for item_id in item_ids:
        item = garment_by_id.get(item_id)
        if item is None:
            reasons.append(f"UNKNOWN_ITEM:{item_id}")
            continue
        items.append(item)
        reasons.extend(
            f"{code}:{item.garment_id}"
            for code in independent_item_violations(item, scene)
        )

    core_slots = [
        item.slot
        for item in items
        if item.slot in {"outer", "top", "bottom", "dress", "shoes", "bag"}
    ]
    duplicate_slots = sorted(
        slot for slot in set(core_slots) if core_slots.count(slot) > 1
    )
    reasons.extend(f"DUPLICATE_CORE_SLOT:{slot}" for slot in duplicate_slots)
    slots = set(core_slots)
    if "dress" in slots and ({"top", "bottom"} & slots):
        reasons.append("DRESS_WITH_TOP_OR_BOTTOM")
    if not independent_slots_complete(items, list(scene.constraints.required_slots)):
        reasons.append("REQUIRED_SLOTS_INCOMPLETE")

    if scene.constraints.weather_requirement == "rain":
        rain_outers = [item for item in items if item.slot == "outer"]
        if not rain_outers:
            reasons.append("RAIN_REQUIRED_OUTER_MISSING")
        elif not any(
            getattr(item, "waterproof", None) is True for item in rain_outers
        ):
            # A title such as “风衣” is not structured waterproof evidence.
            reasons.append("RAIN_PROOF_EVIDENCE_MISSING")
    if scene.occasion in {"interview", "meeting"}:
        formal_core = [
            item
            for item in items
            if item.slot in {"top", "bottom", "dress", "shoes"}
        ]
        if not formal_core:
            reasons.append("FORMAL_CORE_MISSING")
        else:
            for item in formal_core:
                if scene.occasion not in item.occasions:
                    reasons.append(f"OCCASION_MISMATCH:{item.garment_id}")
                if item.formal < 3:
                    reasons.append(f"FORMALITY_BELOW_MINIMUM:{item.garment_id}")
            if sum(item.formal for item in formal_core) / len(formal_core) < 2.5:
                reasons.append("FORMALITY_AVERAGE_BELOW_MINIMUM")
    return sorted(set(reasons))


_SCORECARD_FORBIDDEN_ZH = (
    "颜值",
    "容貌",
    "长相",
    "脸",
    "身材",
    "身体",
    "体重",
    "年龄",
    "年轻",
    "性感",
    "性吸引力",
)
_SCORECARD_FORBIDDEN_EN = re.compile(
    r"\b(?:appearance|body|weight|age|young|old|sexy|sexual|attractive|attractiveness)\b",
    flags=re.IGNORECASE,
)


def independent_scorecard_text_violations(card: Any) -> list[str]:
    """Scan only user-visible score text, excluding self-reported safety flags."""

    texts: list[tuple[str, str]] = []
    dimensions = card.dimensions.model_dump()
    for name, value in dimensions.items():
        texts.append((f"dimensions.{name}.label", str(value["label"])))
        texts.extend(
            (f"dimensions.{name}.evidence", str(item))
            for item in value["evidence"]
        )
    texts.extend(
        [
            ("guidance", card.guidance),
            ("keep_point.statement", card.keep_point.statement),
        ]
    )
    texts.extend(("missing_evidence", str(item)) for item in card.missing_evidence)
    for item in card.visual_evidence:
        texts.append(("visual_evidence.observation", item.observation))
    for item in card.priority_adjustments:
        texts.append(("priority_adjustments.action", item.action))
        texts.append(("priority_adjustments.reason", item.reason))

    violations: list[str] = []
    for field, text in texts:
        for term in _SCORECARD_FORBIDDEN_ZH:
            if term in text:
                violations.append(f"{field}:{term}")
        for match in _SCORECARD_FORBIDDEN_EN.finditer(text):
            violations.append(f"{field}:{match.group(0).lower()}")
    return sorted(set(violations))


def independent_score_breakdown(items: list[Any], scene: Any) -> tuple[dict[str, int], float]:
    """Recompute the documented six-dimension rule score outside LookService."""

    if not items:
        raise ValueError("score oracle requires at least one grounded garment")
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
    styles = {style for item in items for style in item.styles}
    colors = {item.color for item in items}
    slots = {item.slot for item in items}
    occasion_matches = sum(scene.occasion in item.occasions for item in items)
    comfort_matches = sum(
        item.fit in {"regular", "loose", "straight"} for item in items
    )
    scores = {
        "occasion_fit": min(96, 68 + round(28 * occasion_matches / len(items))),
        "expression_match": min(94, 72 + 5 * len(styles & target_styles)),
        "overall_harmony": 90 if len(colors) <= 3 else 78,
        "silhouette_layering": (
            88
            if {"top", "bottom", "shoes"}.issubset(slots) or "dress" in slots
            else 76
        ),
        "comfort_practicality": min(
            94, 70 + round(24 * comfort_matches / len(items))
        ),
        "detail_finish": 88 if slots & {"bag", "accessory", "outer"} else 80,
    }
    weights = {
        "occasion_fit": 0.20,
        "expression_match": 0.17,
        "overall_harmony": 0.18,
        "silhouette_layering": 0.15,
        "comfort_practicality": 0.18,
        "detail_finish": 0.12,
    }
    total = round(sum(scores[name] * weights[name] for name in weights), 1)
    return scores, total


def independent_query_catalog_call_required(query_text: str) -> bool:
    """Frozen-text oracle for a query-local, slot-bound Catalog call."""

    text = re.sub(r"\s+", "", query_text)
    if any(
        marker in text
        for marker in (
            "不需要买",
            "无需购买",
            "不要买",
            "别买",
            "不想买",
            "只用现有衣橱",
            "不要打开商品",
            "别打开商品",
            "不要打开目录",
            "别打开目录",
            "先别推荐商品",
            "不进入购物",
        )
    ):
        return False
    slots = r"(?:鞋子?|靴|外套|风衣|大衣|上衣|衬衫|针织衫|裤|下装|连衣裙|裙子|小?包|手提包|挎包|配饰|围巾|腰带)"
    positive_slot_patterns = (
        rf"缺[^，。；;！？!?]{{0,12}}{slots}",
        rf"(?:想补|需补|需要补|要补|补购)[^，。；;！？!?]{{0,12}}{slots}",
        rf"(?:想看看|看看|浏览)[^，。；;！？!?]{{0,12}}{slots}",
        rf"需要更[^，。；;！？!?]{{0,12}}{slots}",
        rf"{slots}[^，。；;！？!?]{{0,12}}(?:缺|想补|需要补|想看看|看看|浏览)",
    )
    return any(re.search(pattern, text) is not None for pattern in positive_slot_patterns)


def independent_query_catalog_gap_slots(query_text: str) -> set[str]:
    """Extract explicit shopping slots without consulting SceneParser or CatalogService."""

    text = re.sub(r"\s+", "", query_text)
    slot_markers = {
        "shoes": ("鞋", "靴"),
        "outer": ("外套", "风衣", "大衣"),
        "top": ("上衣", "衬衫", "针织衫"),
        "bottom": ("裤", "下装"),
        "dress": ("连衣裙", "裙子"),
        "bag": ("包", "手提包", "挎包"),
        "accessory": ("配饰", "围巾", "腰带"),
    }
    return {
        slot
        for slot, markers in slot_markers.items()
        if any(marker in text for marker in markers)
    }


def independent_catalog_inventory_oracle(
    catalog_items: list[Any], query_text: str, user: Any
) -> tuple[set[str], dict[str, list[str]], dict[str, Any]]:
    """Compute eligible Mock inventory from fixture fields and frozen query text."""

    text = re.sub(r"\s+", "", query_text)
    required_slots = independent_query_catalog_gap_slots(text)
    occasion = next(
        (
            value
            for marker, value in (
                ("面试", "interview"),
                ("会议", "meeting"),
                ("运动", "sports"),
                ("聚会", "party"),
                ("出差", "travel"),
                ("通勤", "commute"),
                ("上课", "school"),
            )
            if marker in text
        ),
        None,
    )
    season = next(
        (
            value
            for marker, value in (
                ("春季", "spring"),
                ("春天", "spring"),
                ("夏季", "summer"),
                ("夏天", "summer"),
                ("秋季", "autumn"),
                ("秋天", "autumn"),
                ("冬季", "winter"),
                ("冬天", "winter"),
            )
            if marker in text
        ),
        None,
    )
    event_window_days = next(
        (
            days
            for pattern, days in (
                (r"两周后", 14),
                (r"下周", 7),
                (r"下月", 30),
                (r"月底", 21),
                (r"明天", 1),
                (r"今天|马上|十分钟后", 0),
            )
            if re.search(pattern, text)
        ),
        None,
    )
    color_markers = {
        "黑色": "black",
        "白色": "white",
        "灰色": "gray",
        "藏青": "navy",
        "蓝色": "blue",
        "卡其": "khaki",
        "棕色": "brown",
        "红色": "red",
        "紫色": "purple",
        "黄色": "yellow",
        "橙色": "orange",
        "绿色": "green",
    }
    taboo_colors = set(getattr(user, "avoid_colors", ()))
    taboo_colors.update(
        color
        for marker, color in color_markers.items()
        if re.search(rf"(?:不要|别|过滤|避开)[^，。；;！？!?]{{0,4}}{marker}", text)
    )
    budget = "low" if "低预算" in text else getattr(user, "budget", None)
    budget_limit = 300 if budget == "low" else 350 if budget == "mid" else None
    min_formal = 3 if occasion in {"interview", "meeting"} or "正式" in text else 0
    weather = "rain" if re.search(r"下雨|雨天|有雨|降雨", text) else None
    verified_rain_outer_exists = any(
        item.slot == "outer" and getattr(item, "waterproof", None) is True
        for item in catalog_items
    )

    reasons_by_id: dict[str, list[str]] = {}
    eligible_ids: set[str] = set()
    for item in catalog_items:
        reasons: list[str] = []
        if item.stock <= 0:
            reasons.append("OUT_OF_STOCK")
        if required_slots and item.slot not in required_slots:
            reasons.append("GAP_SLOT_MISMATCH")
        if occasion:
            compatible = occasion in item.occasions
            if occasion == "interview":
                compatible = compatible or "meeting" in item.occasions
            elif occasion == "meeting":
                compatible = compatible or "interview" in item.occasions
            if not compatible:
                reasons.append("OCCASION_MISMATCH")
        if int(getattr(item, "formal", 0)) < min_formal:
            reasons.append("FORMALITY_BELOW_REQUIRED")
            if occasion in {"interview", "meeting"}:
                reasons.append("OCCASION_MISMATCH")
        if budget_limit is not None and float(getattr(item, "price", 0)) > budget_limit:
            reasons.append("BUDGET_EXCEEDED")
        if item.color in taboo_colors:
            reasons.append("TABOO_COLOR")
        if season and season not in item.seasons and "all" not in item.seasons:
            reasons.append("SEASON_MISMATCH")
        if (
            event_window_days is not None
            and int(item.delivery_days) + 1 > event_window_days
        ):
            reasons.append("DELIVERY_BUFFER_MISSED")
        if weather == "rain" and not verified_rain_outer_exists:
            reasons.append("RAIN_REQUIRED_OUTER_UNRESOLVED")
            if item.slot == "outer" and getattr(item, "waterproof", None) is not True:
                reasons.append("RAIN_PROOF_EVIDENCE_MISSING")
        if reasons:
            reasons_by_id[item.item_id] = sorted(set(reasons))
        else:
            eligible_ids.add(item.item_id)

    context = {
        "required_slots": sorted(required_slots),
        "occasion": occasion,
        "minimum_formality": min_formal,
        "season": season,
        "event_window_days": event_window_days,
        "delivery_safety_buffer_days": 1,
        "budget_policy": budget,
        "taboo_colors": sorted(taboo_colors),
        "weather_requirement": weather,
    }
    return eligible_ids, reasons_by_id, context


def _metric(
    *,
    numerator: int,
    denominator: int,
    value: float | int,
    threshold_key: str,
    passed: bool,
    failed_case_ids: set[str] | list[str],
    unit: str,
) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": value,
        "unit": unit,
        "threshold": THRESHOLDS[threshold_key],
        "status": "PASS" if passed else "FAIL",
        "failed_case_ids": sorted(set(failed_case_ids)),
    }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def independent_violation_breakdown(
    *,
    outfit_hard_violations: int,
    invalid_alternative_references: int,
    catalog_product_constraint_violations: int,
    reported_validation_disagreements: int,
    catalog_expected_presence_violations: int,
) -> dict[str, int]:
    """Keep observed constraint failures separate from missing Catalog output."""

    return {
        "observed_hard_constraint_violations": (
            outfit_hard_violations
            + invalid_alternative_references
            + catalog_product_constraint_violations
            + reported_validation_disagreements
        ),
        "catalog_expected_presence_violations": (
            catalog_expected_presence_violations
        ),
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ProfAgent R1 Demo Evaluation",
        "",
        f"- Status: **{report['status']}**",
        f"- Generated at: `{report['generated_at']}`",
        f"- Command: `{report['command']}`",
        f"- Dataset: `{report['versions']['data']}` / `{report['versions']['eval']}`",
        f"- Eval SHA-256: `{report['versions']['eval_sha256']}`",
        "- External provider calls: `0`",
        "- Main 30-case input: `structured_scene_fields` (`event_horizon`, `intent` supplied)",
        "- Urgency metric definition: `horizon_to_urgency_mapping_accuracy` (not query NLP accuracy)",
        (
            "- Query-only parser assurance: "
            f"`{report['evaluation_inputs']['query_only']['denominator']}` cases"
        ),
        (
            "- No-deadline audit assurance: "
            f"`{report['evaluation_inputs']['audit_no_deadline']['denominator']}` cases"
        ),
        (
            "- Query-derived explicit-gap Catalog calls: `"
            + ",".join(
                report["evaluation_inputs"]["catalog_expectation"][
                    "explicit_gap_call_required_case_ids"
                ]
            )
            + "`"
        ),
        (
            "- Legacy `allowed_catalog` semantics: non-authoritative, "
            "non-exhaustive `legacy_generator_candidate_witness`; overlap is "
            "diagnostic only and does not gate release"
        ),
        (
            "- Legacy generator candidate overlap (all cases): "
            f"`{report['catalog_legacy_witness_diagnostics']['all_cases']['overlap_count']}/"
            f"{report['catalog_legacy_witness_diagnostics']['all_cases']['witness_entry_count']}` "
            f"(overlap_ratio=`{report['catalog_legacy_witness_diagnostics']['all_cases']['overlap_ratio']}`)"
        ),
        "",
        "## Metrics",
        "",
        "| Metric | Numerator | Denominator | Value | Threshold | Status | Failed cases |",
        "|---|---:|---:|---:|---|---|---|",
    ]
    for name, metric in report["metrics"].items():
        threshold = metric["threshold"]
        failed = ", ".join(metric["failed_case_ids"]) or "-"
        lines.append(
            "| {name} | {num} | {den} | {value} {unit} | {op} {target} | {status} | {failed} |".format(
                name=name,
                num=metric["numerator"],
                den=metric["denominator"],
                value=metric["value"],
                unit=metric["unit"],
                op=threshold["operator"],
                target=threshold["value"],
                status=metric["status"],
                failed=failed,
            )
        )
    lines.extend(
        [
            "",
            "## Acceptance criteria matrix",
            "",
            "| AC | Status | Evidence |",
            "|---|---|---|",
        ]
    )
    for entry in report["ac_matrix"]:
        lines.append(
            f"| {entry['ac']} | {entry['status']} | {entry['evidence']} |"
        )
    lines.extend(
        [
            "",
            "## Executed assurance checks",
            "",
            "| Check | Status | Evidence |",
            "|---|---|---|",
        ]
    )
    for name, check in report["assurance_checks"].items():
        failure = ", ".join(check.get("failed_steps", []))
        evidence = str(check.get("evidence", ""))
        completed = ", ".join(check.get("checks", []))
        if completed:
            evidence = f"{evidence}; checks={completed}"
        if failure:
            evidence = f"{evidence}; failure={failure}"
        lines.append(f"| {name} | {check['status']} | {evidence} |")
    lines.extend(
        [
            "",
            "## Privacy and reproducibility",
            "",
            "The report contains case IDs and aggregate outcomes only. It does not contain full queries, image bytes/base64, authorization headers, or API keys. The fixture metric phase disables CPA text and live Vision. AC assurance uses only in-process injected transports and makes zero external provider calls.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_reports(root: Path, report: dict[str, Any]) -> None:
    json_path = root / REPORT_JSON
    markdown_path = root / REPORT_MARKDOWN
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(_markdown(report), encoding="utf-8")


class _AssuranceFailure(AssertionError):
    pass


def _ensure(condition: bool, step: str) -> None:
    if not condition:
        raise _AssuranceFailure(step)


def _assurance_pass(evidence: str, checks: list[str]) -> dict[str, Any]:
    return {
        "status": "PASS",
        "evidence": evidence,
        "checks": checks,
        "failed_steps": [],
    }


def _assurance_fail(evidence: str, exc: BaseException) -> dict[str, Any]:
    step = str(exc) if isinstance(exc, _AssuranceFailure) else type(exc).__name__
    return {
        "status": "FAIL",
        "evidence": evidence,
        "checks": [],
        "failed_steps": [step],
    }


def _combine_assurances(
    evidence: str, *checks: dict[str, Any]
) -> dict[str, Any]:
    """AND independently produced checks without hiding a skipped/failing leg."""

    failed_steps = [
        str(step)
        for check in checks
        if check.get("status") != "PASS"
        for step in (check.get("failed_steps") or ["assurance_not_passed"])
    ]
    completed = [
        str(step)
        for check in checks
        if check.get("status") == "PASS"
        for step in check.get("checks", [])
    ]
    return {
        "status": "PASS" if not failed_steps else "FAIL",
        "evidence": evidence,
        "checks": completed,
        "failed_steps": failed_steps,
    }


def _observed_assurance(
    passed: bool,
    evidence: str,
    checks: list[str],
    failed_steps: list[str],
) -> dict[str, Any]:
    return {
        "status": "PASS" if passed else "FAIL",
        "evidence": evidence,
        "checks": checks if passed else [],
        "failed_steps": [] if passed else failed_steps,
    }


def _ac07_structural_similarity_evidence(left: Any, right: Any) -> int:
    """Independent AC-07 3-of-5 structural shoe similarity predicate."""

    return sum(
        (
            bool(set(left.styles).intersection(right.styles)),
            left.fit == right.fit,
            left.material == right.material,
            abs(left.formal - right.formal) <= 1,
            abs(left.warmth - right.warmth) <= 1,
        )
    )


def _select_ac07_trace_witnesses(
    repository: Any,
    trace: Any,
    target: Any,
) -> tuple[tuple[Any, Any], Any]:
    """Select two risk shoes and one safe shoe from the server Trace only.

    Selection is stable in the existing RRF order. Missing or malformed Trace
    evidence fails closed instead of substituting fixture IDs outside top-30.
    """

    rows = trace.retrieval.get("rrf", [])
    _ensure(isinstance(rows, list), "ac07_rank_trace_witnesses_insufficient")
    risk_items: list[Any] = []
    safe_item: Any | None = None
    seen_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = row.get("item_id")
        if not isinstance(item_id, str) or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        item = repository.get_garment(item_id)
        if item is None or item.slot != "shoes" or item.garment_id == target.garment_id:
            continue
        similarity = _ac07_structural_similarity_evidence(item, target)
        if similarity >= 3 and len(risk_items) < 2:
            risk_items.append(item)
        elif similarity < 3 and safe_item is None:
            safe_item = item
    _ensure(
        len(risk_items) == 2 and safe_item is not None,
        "ac07_rank_trace_witnesses_insufficient",
    )
    return (risk_items[0], risk_items[1]), safe_item


async def _assure_ac07(root: Path) -> dict[str, Any]:
    """Exercise feedback -> confirm -> relevant scene -> delete causality."""

    from .app import AppServices
    from .feedback_service import RecommendationFeedbackInput
    from .memory_service import MemoryConfirmInput

    services = AppServices(
        Settings(
            root_dir=root,
            cpa_text_enabled=False,
            cpa_api_key=None,
            dense_enabled=False,
            catalog_enabled=True,
            vision_force_failure=True,
        )
    )
    evidence = "in-process feedback/memory causal workflow; external calls=0"
    try:
        seed = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u02",
                query_text="下周通勤活动，先用现有衣橱给出舒适方向。",
                event_horizon="planned",
                intent="recommend",
                occasion="commute",
                goals=["comfortable"],
            )
        )
        seed_result = services.recommendations.recommend(
            services.recommendations.resolve_payload({"request_id": seed.request_id})
        )
        outfit = next(
            (
                candidate
                for candidate in seed_result.outfits
                if sum(
                    services.repository.get_garment(item_id).slot == "shoes"  # type: ignore[union-attr]
                    for item_id in candidate.items
                )
                == 1
            ),
            None,
        )
        _ensure(outfit is not None, "ac07_seed_outfit_with_one_shoe")
        target = next(
            item_id
            for item_id in outfit.items
            if services.repository.get_garment(item_id).slot == "shoes"  # type: ignore[union-attr]
        )

        like = services.feedback.submit(
            RecommendationFeedbackInput(
                user_id="u02",
                styling_session_id=seed.styling_session_id,
                request_id=seed.request_id,
                outfit_id=outfit.outfit_id,
                decision="like",
                memory_scope="session",
            )
        )
        _ensure(like.memory_proposal is None and like.record is None, "ac07_like_is_session_only")

        feedback = services.feedback.submit(
            RecommendationFeedbackInput(
                user_id="u02",
                styling_session_id=seed.styling_session_id,
                request_id=seed.request_id,
                outfit_id=outfit.outfit_id,
                decision="dislike",
                reason_code="long_walk_shoes",
                memory_scope="propose",
            )
        )
        proposal = feedback.memory_proposal
        _ensure(
            proposal is not None
            and proposal.status == "proposed"
            and feedback.record is None,
            "ac07_propose_does_not_commit",
        )

        related_text = "下周要久走并长时间站立，想穿得轻松舒适。"
        before = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u02",
                query_text=related_text,
                event_horizon="planned",
                intent="recommend",
                occasion="commute",
                goals=["comfortable"],
            )
        )
        target_item = services.repository.get_garment(target)
        _ensure(target_item is not None, "ac07_target_fixture_exists")
        before_violations = independent_item_violations(target_item, before)
        _ensure(target not in before.constraints.excluded_items, "ac07_not_applied_before_confirm")
        _ensure(not before_violations, "ac07_target_eligible_before_confirm")

        committed = services.memory.confirm(
            proposal.proposal_id,
            MemoryConfirmInput(user_id="u02", decision="confirm"),
        )
        _ensure(
            committed.record is not None
            and committed.proposal.status == "committed"
            and committed.proposal.content is None,
            "ac07_confirm_commits_and_clears_proposal_content",
        )

        after = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u02",
                query_text=related_text,
                event_horizon="planned",
                intent="recommend",
                occasion="commute",
                goals=["comfortable"],
            )
        )
        after_violations = independent_item_violations(target_item, after)
        _ensure(target in after.constraints.excluded_items, "ac07_relevant_scene_applies_target")
        _ensure(
            "EXCLUDED_ITEM" in after_violations,
            "ac07_target_filtered_before_recall",
        )
        after_result = services.recommendations.recommend(
            services.recommendations.resolve_payload({"request_id": after.request_id})
        )
        _ensure(
            all(target not in candidate.items for candidate in after_result.outfits),
            "ac07_target_absent_after_ranking",
        )

        unrelated = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u02",
                query_text="今天在家休息，想穿得轻松。",
                event_horizon="today",
                intent="recommend",
                occasion="home",
                goals=["comfortable"],
            )
        )
        _ensure(target not in unrelated.constraints.excluded_items, "ac07_unrelated_scene_not_targeted")

        services.memory.delete("u02", committed.record.memory_id)
        restored = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u02",
                query_text=related_text,
                event_horizon="planned",
                intent="recommend",
                occasion="commute",
                goals=["comfortable"],
            )
        )
        restored_violations = independent_item_violations(target_item, restored)
        _ensure(target not in restored.constraints.excluded_items, "ac07_delete_removes_target")
        _ensure(not restored_violations, "ac07_delete_restores_eligibility")
        return _assurance_pass(
            evidence,
            [
                "like_session_only",
                "propose_without_commit",
                "before_confirm_not_applied",
                "confirm_applies_only_to_relevant_scene",
                "unrelated_scene_not_targeted",
                "delete_restores_eligibility",
            ],
        )
    except BaseException as exc:
        return _assurance_fail(evidence, exc)
    finally:
        services.assets.close()


async def _assure_ac07_rank_causality(root: Path) -> dict[str, Any]:
    """Prove confirmed feedback causally demotes structured-similar shoes."""

    from .app import AppServices
    from .feedback_service import RecommendationFeedbackInput
    from .memory_service import MemoryConfirmInput

    services = AppServices(
        Settings(
            root_dir=root,
            cpa_text_enabled=False,
            cpa_api_key=None,
            dense_enabled=False,
            catalog_enabled=True,
            vision_force_failure=True,
        )
    )
    evidence = (
        "same-query pre-confirm/post-confirm/delete ranking causality with "
        "metadata-only shoe similarity and redacted policy Trace"
    )

    def rank_score(trace: Any, item_id: str) -> tuple[int, float]:
        rows = list(trace.retrieval.get("rrf", []))
        for index, row in enumerate(rows, 1):
            if row.get("item_id") == item_id:
                return index, float(row.get("score", 0.0))
        raise _AssuranceFailure(f"ac07_rank_missing:{item_id}")

    def optional_rank_score(trace: Any, item_id: str) -> tuple[int, float] | None:
        rows = list(trace.retrieval.get("rrf", []))
        for index, row in enumerate(rows, 1):
            if row.get("item_id") == item_id:
                return index, float(row.get("score", 0.0))
        return None

    try:
        seed = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text="下周面试，请从现有衣橱给可靠的正式搭配。",
                event_horizon="soon",
                intent="recommend",
                occasion="interview",
                goals=["reliable"],
            )
        )
        seed_result = services.recommendations.recommend(
            services.recommendations.resolve_payload({"request_id": seed.request_id})
        )
        seed_outfit = next(
            (outfit for outfit in seed_result.outfits if "g020" in outfit.items),
            None,
        )
        _ensure(seed_outfit is not None, "ac07_rank_seed_saved_outfit_has_target_shoe")
        proposal_result = services.feedback.submit(
            RecommendationFeedbackInput(
                user_id="u01",
                styling_session_id=seed.styling_session_id,
                request_id=seed.request_id,
                outfit_id=seed_outfit.outfit_id,
                decision="dislike",
                reason_code="long_walk_shoes",
                memory_scope="propose",
            )
        )
        proposal = proposal_result.memory_proposal
        _ensure(
            proposal is not None
            and proposal.status == "proposed"
            and proposal_result.record is None,
            "ac07_rank_propose_without_commit",
        )

        target = services.repository.get_garment("g020")
        _ensure(target is not None, "ac07_rank_target_shoe_grounded")

        related_text = "下周通勤要久走并长时间站立，想穿得轻松舒适。"
        before = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text=related_text,
                event_horizon="soon",
                intent="recommend",
                occasion="commute",
                goals=["comfortable"],
            )
        )
        before_result = services.recommendations.recommend(
            services.recommendations.resolve_payload({"request_id": before.request_id})
        )
        before_trace = services.traces.get(before_result.trace_id)
        _ensure(before_trace is not None, "ac07_rank_before_trace")
        before_policy = before_trace.retrieval.get("memory_rerank", {})
        _ensure(
            before_trace.retrieval.get("final_ranking_stage")
            == "post_rrf_memory_policy_v1"
            and before_policy.get("applied") is False
            and before_policy.get("active_policy_count") == 0,
            "ac07_rank_before_no_confirmed_policy",
        )
        risk_items, safe_item = _select_ac07_trace_witnesses(
            services.repository,
            before_trace,
            target,
        )
        risk_ids = tuple(item.garment_id for item in risk_items)
        safe_id = safe_item.garment_id
        _ensure(
            all(
                _ac07_structural_similarity_evidence(item, target) >= 3
                for item in risk_items
            )
            and _ac07_structural_similarity_evidence(safe_item, target) < 3,
            "ac07_rank_independent_structured_similarity_oracle",
        )
        tracked_ids = (safe_id, *risk_ids)
        before_positions = {
            item_id: rank_score(before_trace, item_id) for item_id in tracked_ids
        }
        _ensure(
            "g020" not in before.constraints.excluded_items,
            "ac07_rank_target_not_excluded_before_confirm",
        )

        committed = services.memory.confirm(
            proposal.proposal_id,
            MemoryConfirmInput(user_id="u01", decision="confirm"),
        )
        _ensure(
            committed.record is not None
            and committed.proposal.status == "committed"
            and committed.proposal.content is None,
            "ac07_rank_confirm_commits_and_clears_proposal",
        )
        after = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text=related_text,
                event_horizon="soon",
                intent="recommend",
                occasion="commute",
                goals=["comfortable"],
            )
        )
        after_result = services.recommendations.recommend(
            services.recommendations.resolve_payload({"request_id": after.request_id})
        )
        after_trace = services.traces.get(after_result.trace_id)
        _ensure(after_trace is not None, "ac07_rank_after_trace")
        after_policy = after_trace.retrieval.get("memory_rerank", {})
        _ensure(
            after_trace.retrieval.get("final_ranking_stage")
            == "post_rrf_memory_policy_v1"
            and after_policy.get("version")
            == "confirmed_feedback_shoe_similarity_v1"
            and after_policy.get("stage") == "post_rrf"
            and after_policy.get("policy")
            == "demote_structured_similar_shoes_v1"
            and after_policy.get("relevant") is True
            and after_policy.get("active_policy_count") >= 1
            and after_policy.get("applied") is True
            and after_policy.get("demoted_count") >= 2
            and after_policy.get("penalty") == 0.01
            and after_policy.get("penalty_mode") == "fixed_once_per_item"
            and after_policy.get("target_ids_logged") is False
            and after_policy.get("signatures_logged") is False
            and after_policy.get("demoted_ids_logged") is False,
            "ac07_rank_confirmed_policy_trace_contract",
        )
        _ensure(
            "g020" in after.constraints.excluded_items
            and all("g020" not in outfit.items for outfit in after_result.outfits),
            "ac07_rank_exact_target_excluded_before_recall",
        )

        same_universe_before = [
            (str(row.get("item_id")), float(row.get("score", 0.0)))
            for row in before_trace.retrieval.get("rrf", [])
        ]
        same_universe_by_id = {
            item_id: item
            for item_id, _score in same_universe_before
            if (item := services.repository.get_garment(item_id)) is not None
        }
        same_universe_after, same_universe_policy = (
            services.retriever._apply_confirmed_memory_rerank(
                same_universe_before,
                same_universe_by_id,
                after,
                services.memory.active_signals("u01"),
            )
        )
        _ensure(
            {item_id for item_id, _score in same_universe_before}
            == {item_id for item_id, _score in same_universe_after},
            "ac07_rank_same_universe_ids_preserved",
        )
        same_before_positions = {
            item_id: (rank, score)
            for rank, (item_id, score) in enumerate(same_universe_before, 1)
        }
        same_after_positions = {
            item_id: (rank, score)
            for rank, (item_id, score) in enumerate(same_universe_after, 1)
        }
        for item_id in risk_ids:
            before_rank, before_score = same_before_positions[item_id]
            after_rank, after_score = same_after_positions[item_id]
            _ensure(
                abs(after_score - max(0.0, before_score - 0.01)) < 1e-9
                and after_rank >= before_rank,
                f"ac07_rank_same_universe_fixed_penalty:{item_id}",
            )
        _ensure(
            same_after_positions[safe_id][1] == same_before_positions[safe_id][1]
            and same_after_positions[safe_id][0]
            <= same_before_positions[safe_id][0]
            and same_universe_policy.get("score_penalty_applied_count") >= 2
            and same_universe_policy.get("same_universe_rank_improved_count") == 0
            and same_universe_policy.get("penalty") == 0.01,
            "ac07_rank_same_universe_safe_unchanged_and_risk_never_improves",
        )
        _ensure(
            "g020"
            not in json.dumps(same_universe_policy, ensure_ascii=False),
            "ac07_rank_same_universe_policy_trace_private",
        )

        after_positions = {
            item_id: optional_rank_score(after_trace, item_id)
            for item_id in tracked_ids
        }
        for item_id in risk_ids:
            _before_rank, before_score = before_positions[item_id]
            observed_after = after_positions[item_id]
            after_score = (
                observed_after[1]
                if observed_after is not None
                else same_after_positions[item_id][1]
            )
            _ensure(
                after_score < before_score,
                f"ac07_rank_end_to_end_similar_risk_score_decreased:{item_id}",
            )
        _safe_before_rank, safe_before_score = before_positions[safe_id]
        safe_after = after_positions[safe_id]
        _ensure(safe_after is not None, "ac07_rank_safe_witness_remains_observable")
        _safe_after_rank, safe_after_score = safe_after
        before_safe_relative_margin = sum(
            safe_before_score - before_positions[item_id][1]
            for item_id in risk_ids
        )
        after_safe_relative_margin = sum(
            safe_after_score
            - (
                after_positions[item_id][1]
                if after_positions[item_id] is not None
                else same_after_positions[item_id][1]
            )
            for item_id in risk_ids
        )
        _ensure(
            after_safe_relative_margin > before_safe_relative_margin,
            "ac07_rank_end_to_end_safe_relative_margin_improves",
        )
        after_trace_text = json.dumps(
            after_trace.model_dump(mode="json"), ensure_ascii=False
        )
        _ensure(
            "g020" not in after_trace_text,
            "ac07_rank_private_target_id_not_logged",
        )

        unrelated = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text="下周在家休息，想穿得轻松。",
                event_horizon="soon",
                intent="recommend",
                occasion="home",
                goals=["comfortable"],
            )
        )
        unrelated_result = services.recommendations.recommend(
            services.recommendations.resolve_payload({"request_id": unrelated.request_id})
        )
        unrelated_trace = services.traces.get(unrelated_result.trace_id)
        _ensure(unrelated_trace is not None, "ac07_rank_unrelated_trace")
        unrelated_policy = unrelated_trace.retrieval.get("memory_rerank", {})
        _ensure(
            unrelated_policy.get("relevant") is False
            and unrelated_policy.get("applied") is False
            and unrelated_policy.get("demoted_count") == 0,
            "ac07_rank_unrelated_scene_not_affected",
        )

        services.memory.delete("u01", committed.record.memory_id)
        restored = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text=related_text,
                event_horizon="soon",
                intent="recommend",
                occasion="commute",
                goals=["comfortable"],
            )
        )
        restored_result = services.recommendations.recommend(
            services.recommendations.resolve_payload({"request_id": restored.request_id})
        )
        restored_trace = services.traces.get(restored_result.trace_id)
        _ensure(restored_trace is not None, "ac07_rank_restored_trace")
        restored_positions = {
            item_id: rank_score(restored_trace, item_id) for item_id in tracked_ids
        }
        _ensure(
            restored_positions == before_positions
            and "g020" not in restored.constraints.excluded_items,
            "ac07_rank_delete_restores_preconfirm_scores_and_ranks",
        )
        restored_policy = restored_trace.retrieval.get("memory_rerank", {})
        _ensure(
            restored_policy.get("active_policy_count") == 0
            and restored_policy.get("applied") is False,
            "ac07_rank_delete_removes_policy",
        )
        return _assurance_pass(
            evidence,
            [
                "propose_without_commit",
                "independent_metadata_similarity_3_of_5",
                "before_confirm_no_policy",
                "confirm_excludes_exact_target",
                "same_universe_fixed_penalty_and_risk_rank_never_improves",
                "same_universe_safe_score_unchanged",
                "end_to_end_similar_risk_scores_decrease",
                "end_to_end_safe_relative_margin_improves",
                "unrelated_scene_not_affected",
                "trace_policy_counts_without_private_ids",
                "delete_restores_preconfirm_ranks_and_scores",
            ],
        )
    except BaseException as exc:
        return _assurance_fail(evidence, exc)
    finally:
        services.assets.close()


async def _assure_ac01_exact(root: Path) -> dict[str, Any]:
    """Exercise AC-01's exact mixed-emotion interview Given as one request."""

    from .app import AppServices

    services = AppServices(
        Settings(
            root_dir=root,
            cpa_text_enabled=False,
            cpa_api_key=None,
            dense_enabled=False,
            catalog_enabled=True,
            vision_force_failure=True,
        )
    )
    evidence = (
        "exact PRD interview/today/nervous/reliable-modern Given; actual Catalog spy"
    )
    actual_catalog_invocations = 0
    original_search = services.catalog.search

    def counted_search(scene: Any):
        nonlocal actual_catalog_invocations
        actual_catalog_invocations += 1
        return original_search(scene)

    services.catalog.search = counted_search  # type: ignore[method-assign]
    try:
        scene = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text="我今天下午面试，有点紧张，想显得可靠但别太老气。",
            )
        )
        _ensure(
            scene.event_horizon == "today"
            and scene.urgency == "high"
            and scene.occasion == "interview"
            and scene.intent == "recommend",
            "ac01_exact_scene_today_high_interview_recommend",
        )
        _ensure(
            {"reliable", "modern"}.issubset(scene.goals),
            "ac01_exact_reliable_modern_goals",
        )
        _ensure(
            not scene.shopping_allowed and not scene.ui_capabilities.shopping_cta,
            "ac01_exact_scene_shopping_ui_false",
        )
        recommendation = services.recommendations.recommend(
            services.recommendations.resolve_payload({"request_id": scene.request_id})
        )
        trace = services.traces.get(recommendation.trace_id)
        _ensure(trace is not None, "ac01_exact_trace_exists")
        _ensure(
            recommendation.assistant_message.startswith(_HIGH_SUPPORTIVE_PREFIXES),
            "ac01_exact_supportive_first_line",
        )
        _ensure(
            1 <= len(recommendation.outfits) <= 3,
            "ac01_exact_one_to_three_directions",
        )
        _ensure(
            sum(outfit.is_primary for outfit in recommendation.outfits) == 1,
            "ac01_exact_one_primary",
        )
        garment_by_id = {
            item.garment_id: item
            for item in services.repository.list_garments("u01")
        }
        for index, outfit in enumerate(recommendation.outfits):
            _ensure(
                bool(outfit.reasons)
                and bool(outfit.risks)
                and isinstance(outfit.alternatives, dict),
                f"ac01_exact_direction_{index}_reason_risk_alternatives",
            )
            _ensure(
                not independent_outfit_violations(
                    outfit.items, scene, garment_by_id
                ),
                f"ac01_exact_direction_{index}_grounded_formal_hard_valid",
            )
            for source_id, alternatives in outfit.alternatives.items():
                _ensure(
                    source_id in outfit.items,
                    f"ac01_exact_direction_{index}_alternative_source",
                )
                for replacement_id in alternatives:
                    replacement = [
                        replacement_id if item_id == source_id else item_id
                        for item_id in outfit.items
                    ]
                    _ensure(
                        not independent_outfit_violations(
                            replacement, scene, garment_by_id
                        ),
                        f"ac01_exact_direction_{index}_alternative_hard_valid",
                    )
        _ensure(
            not recommendation.shopping_suggestions
            and not recommendation.ui_capabilities.shopping_cta
            and actual_catalog_invocations == 0,
            "ac01_exact_no_shopping_or_actual_catalog",
        )
        _ensure(
            trace.request_id == scene.request_id
            and trace.styling_session_id == scene.styling_session_id
            and trace.catalog.get("attempted") is False
            and trace.catalog.get("call_count") == 0,
            "ac01_exact_trace_owner_request_catalog_zero",
        )
        return _assurance_pass(
            evidence,
            [
                "single_exact_given",
                "today_high_interview_reliable_modern",
                "supportive_first_line",
                "one_to_three_grounded_formal_directions",
                "exactly_one_primary",
                "reason_risk_alternatives",
                "shopping_ui_false",
                "actual_and_trace_catalog_zero",
            ],
        )
    except BaseException as exc:
        return _assurance_fail(evidence, exc)
    finally:
        services.assets.close()


async def _assure_query_only_scene_parser(root: Path) -> dict[str, Any]:
    """Evaluate only deadline/season/unknown cases whose text is self-contained."""

    from .app import AppServices

    services = AppServices(
        Settings(
            root_dir=root,
            cpa_text_enabled=False,
            cpa_api_key=None,
            dense_enabled=False,
            catalog_enabled=True,
            vision_force_failure=True,
        )
    )
    case_ids = set(_QUERY_ONLY_EXPECTATIONS)
    rows = [row for row in _load_cases(root)[0] if row["case_id"] in case_ids]
    failures: list[str] = []
    try:
        for row in rows:
            scene = await services.scene_parser.parse(
                SceneParseInput(
                    user_id=str(row["user_id"]),
                    query_text=str(row["query"]),
                )
            )
            expected = _QUERY_ONLY_EXPECTATIONS[str(row["case_id"])]
            expected_horizon = str(expected["horizon"])
            expected_urgency = str(expected["urgency"])
            expected_intent = str(expected["intent"])
            expected_shopping = bool(expected["shopping"])
            checks = {
                "horizon": scene.event_horizon == expected_horizon,
                "urgency": scene.urgency == expected_urgency,
                "intent": scene.intent == expected_intent,
                "shopping": scene.shopping_allowed == expected_shopping,
                "scene_ui": (
                    scene.ui_capabilities.shopping_cta == expected_shopping
                ),
                "unknown_fail_closed": (
                    expected_horizon != "unknown"
                    or (
                        scene.urgency == "high"
                        and not scene.shopping_allowed
                        and not scene.ui_capabilities.shopping_cta
                    )
                ),
            }
            failures.extend(
                f"{row['case_id']}:{name}"
                for name, passed in checks.items()
                if not passed
            )
        result = _observed_assurance(
            not failures and len(rows) == len(case_ids),
            (
                "query-only deadline/season/unknown parser subset; no horizon/intent "
                "labels supplied"
            ),
            [
                "query_only_horizon",
                "query_only_urgency",
                "query_only_intent",
                "query_only_shopping_gate",
                "unknown_to_high_fail_closed",
            ],
            failures or ["query_only_case_set_incomplete"],
        )
        result.update(
            {
                "input_mode": "query_only",
                "case_ids": sorted(case_ids),
                "denominator": len(rows),
                "failed_case_checks": failures,
                "expectations": _QUERY_ONLY_EXPECTATIONS,
            }
        )
        return result
    except BaseException as exc:
        result = _assurance_fail(
            "query-only deadline/season/unknown parser subset", exc
        )
        result.update(
            {
                "input_mode": "query_only",
                "case_ids": sorted(case_ids),
                "denominator": len(rows),
                "failed_case_checks": failures,
                "expectations": _QUERY_ONLY_EXPECTATIONS,
            }
        )
        return result
    finally:
        services.assets.close()


async def _assure_audit_no_deadline_gate(root: Path) -> dict[str, Any]:
    """Fail closed when wardrobe/catalog audit text has no event deadline."""

    from .app import AppServices

    services = AppServices(
        Settings(
            root_dir=root,
            cpa_text_enabled=False,
            cpa_api_key=None,
            dense_enabled=False,
            catalog_enabled=True,
            vision_force_failure=True,
        )
    )
    frozen_rows = {
        str(row["case_id"]): row
        for row in _load_cases(root)[0]
        if row["case_id"] in {"e020", "e023"}
    }
    cases = [
        ("e020", "u02", str(frozen_rows.get("e020", {}).get("query", ""))),
        ("e023", "u02", str(frozen_rows.get("e023", {}).get("query", ""))),
        (
            "audit_given_wardrobe",
            "u01",
            "请只根据给定衣橱说明推荐依据。",
        ),
    ]
    evidence = (
        "query-only no-deadline wardrobe/catalog audits fail closed to unknown/high "
        "with actual and traced Catalog zero"
    )
    actual_catalog_invocations: dict[str, int] = {}
    original_search = services.catalog.search

    def counted_search(scene: Any):
        actual_catalog_invocations[scene.request_id] = (
            actual_catalog_invocations.get(scene.request_id, 0) + 1
        )
        return original_search(scene)

    services.catalog.search = counted_search  # type: ignore[method-assign]
    failures: list[str] = []
    try:
        _ensure(len(frozen_rows) == 2, "audit_no_deadline_frozen_rows_present")
        for case_id, user_id, query in cases:
            _ensure(bool(query), f"audit_no_deadline_{case_id}_query_present")
            scene = await services.scene_parser.parse(
                SceneParseInput(user_id=user_id, query_text=query)
            )
            recommendation = services.recommendations.recommend(
                services.recommendations.resolve_payload(
                    {"request_id": scene.request_id}
                )
            )
            trace = services.traces.get(recommendation.trace_id)
            checks = {
                "horizon_unknown": scene.event_horizon == "unknown",
                "urgency_high": scene.urgency == "high",
                "scene_shopping_false": scene.shopping_allowed is False,
                "scene_ui_false": scene.ui_capabilities.shopping_cta is False,
                "recommendation_ui_false": (
                    recommendation.ui_capabilities.shopping_cta is False
                ),
                "suggestions_empty": not recommendation.shopping_suggestions,
                "actual_catalog_zero": (
                    actual_catalog_invocations.get(scene.request_id, 0) == 0
                ),
                "trace_exists": trace is not None,
                "trace_catalog_attempted_false": (
                    trace is not None and trace.catalog.get("attempted") is False
                ),
                "trace_catalog_call_zero": (
                    trace is not None and trace.catalog.get("call_count") == 0
                ),
            }
            failures.extend(
                f"{case_id}:{name}"
                for name, passed in checks.items()
                if not passed
            )
        result = _observed_assurance(
            not failures,
            evidence,
            [
                "audit_query_only_unknown",
                "audit_unknown_to_high",
                "audit_scene_and_ui_shopping_false",
                "audit_actual_catalog_zero",
                "audit_trace_catalog_zero",
            ],
            failures,
        )
        result.update(
            {
                "input_mode": "query_only",
                "case_ids": [case_id for case_id, _user_id, _query in cases],
                "denominator": len(cases),
                "failed_case_checks": failures,
            }
        )
        return result
    except BaseException as exc:
        result = _assurance_fail(evidence, exc)
        result.update(
            {
                "input_mode": "query_only",
                "case_ids": [case_id for case_id, _user_id, _query in cases],
                "denominator": len(cases),
                "failed_case_checks": failures,
            }
        )
        return result
    finally:
        services.assets.close()


async def _assure_ac03(root: Path) -> dict[str, Any]:
    """Exercise the one-question unknown-deadline state machine."""

    from .app import AppServices

    services = AppServices(
        Settings(
            root_dir=root,
            cpa_text_enabled=False,
            cpa_api_key=None,
            dense_enabled=False,
            catalog_enabled=True,
            vision_force_failure=True,
        )
    )
    evidence = (
        "same-session two-request unknown-deadline workflow with Catalog trace"
    )
    try:
        session_id = "eval_ac03_unknown_session"
        first = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                styling_session_id=session_id,
                query_text="截止时间还没定，请先只用现有衣橱给穿搭方向。",
                intent="recommend",
                occasion="commute",
                goals=["reliable"],
            )
        )
        first_result = services.recommendations.recommend(
            services.recommendations.resolve_payload(
                {"request_id": first.request_id}
            )
        )
        first_trace = services.traces.get(first.trace_id)
        _ensure(first_trace is not None, "ac03_first_trace_exists")
        _ensure(
            first.event_horizon == "unknown"
            and first.urgency == "high"
            and not first.shopping_allowed
            and not first.ui_capabilities.shopping_cta,
            "ac03_first_unknown_fail_closed",
        )
        _ensure(
            first.clarification_required
            and bool(first.clarification_question)
            and first.missing_fields == ["event_horizon"],
            "ac03_first_request_asks_exactly_one_clarification",
        )
        _ensure(
            not first_result.shopping_suggestions
            and first_trace.catalog.get("attempted") is False
            and first_trace.catalog.get("call_count") == 0,
            "ac03_first_request_catalog_zero",
        )

        second = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                styling_session_id=session_id,
                query_text="时间仍然没定，继续只用衣橱给方向。",
                intent="recommend",
                occasion="commute",
                goals=["reliable"],
            )
        )
        second_result = services.recommendations.recommend(
            services.recommendations.resolve_payload(
                {"request_id": second.request_id}
            )
        )
        second_trace = services.traces.get(second.trace_id)
        _ensure(second_trace is not None, "ac03_second_trace_exists")
        _ensure(
            second.event_horizon == "unknown"
            and second.urgency == "high"
            and not second.shopping_allowed
            and not second.ui_capabilities.shopping_cta,
            "ac03_second_unknown_fail_closed",
        )
        _ensure(
            not second.clarification_required
            and second.clarification_question is None,
            "ac03_second_request_does_not_repeat_clarification",
        )
        _ensure(
            not second_result.shopping_suggestions
            and second_trace.catalog.get("attempted") is False
            and second_trace.catalog.get("call_count") == 0,
            "ac03_second_request_catalog_zero",
        )
        return _assurance_pass(
            evidence,
            [
                "first_unknown_asks_one_question",
                "second_unknown_does_not_repeat",
                "both_high_shopping_false_ui_false",
                "both_catalog_zero",
            ],
        )
    except BaseException as exc:
        return _assurance_fail(evidence, exc)
    finally:
        services.assets.close()


async def _assure_ac04(root: Path) -> dict[str, Any]:
    """Confirm no-heels memory, then prove recall/ranker enforcement."""

    from .app import AppServices
    from .memory_service import MemoryConfirmInput, MemoryProposeInput

    services = AppServices(
        Settings(
            root_dir=root,
            cpa_text_enabled=False,
            cpa_api_key=None,
            dense_enabled=False,
            catalog_enabled=True,
            vision_force_failure=True,
        )
    )
    evidence = (
        "propose-confirm no_high_heels memory, implicit reuse and independent recall oracle"
    )
    try:
        proposal = services.memory.propose(
            MemoryProposeInput(
                user_id="u01",
                namespace="stylist",
                type="constraint",
                content="不穿高跟鞋",
            )
        )
        _ensure(
            proposal.proposal.status == "proposed"
            and not proposal.proposal.commit_blocked
            and proposal.record is None,
            "ac04_memory_proposed_not_committed",
        )
        committed = services.memory.confirm(
            proposal.proposal.proposal_id,
            MemoryConfirmInput(user_id="u01", decision="confirm"),
        )
        _ensure(
            committed.proposal.status == "committed"
            and committed.record is not None,
            "ac04_memory_confirmed",
        )
        scene = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text="下周通勤，只用现有衣橱搭配。",
                event_horizon="soon",
                intent="recommend",
                occasion="commute",
                goals=["comfortable"],
            )
        )
        _ensure(
            "no_high_heels" in scene.constraints.comfort_notes,
            "ac04_confirmed_memory_applied_without_restatement",
        )
        _ensure(
            not scene.clarification_required
            and scene.clarification_question is None,
            "ac04_confirmed_preference_not_reasked",
        )
        shoes = [
            item
            for item in services.repository.list_garments("u01")
            if item.slot == "shoes" and item.status == "available"
        ]
        oracle_reasons = {
            item.garment_id: set(independent_item_violations(item, scene))
            for item in shoes
        }
        blocked_ids = {
            item_id
            for item_id, reasons in oracle_reasons.items()
            if reasons.intersection(
                {
                    "EXCLUDED_HIGH_HEEL",
                    "EXCLUDED_HEEL",
                    "HEEL_EVIDENCE_MISSING",
                }
            )
        }
        evidence_safe_ids = {
            item_id for item_id, reasons in oracle_reasons.items() if not reasons
        }
        _ensure(
            any("EXCLUDED_HEEL" in reasons for reasons in oracle_reasons.values()),
            "ac04_fixture_contains_known_heeled_shoe",
        )
        _ensure(
            any(
                "HEEL_EVIDENCE_MISSING" in reasons
                for reasons in oracle_reasons.values()
            ),
            "ac04_fixture_contains_unknown_heel_evidence_shoe",
        )
        _ensure(bool(evidence_safe_ids), "ac04_fixture_contains_evidence_safe_shoe")

        recommendation = services.recommendations.recommend(
            services.recommendations.resolve_payload(
                {"request_id": scene.request_id}
            )
        )
        trace = services.traces.get(scene.trace_id)
        _ensure(trace is not None, "ac04_trace_exists")
        _ensure(
            any(
                signal.get("applied_signal") == "no_high_heels"
                for signal in trace.scene.get("memory_signals", [])
            ),
            "ac04_trace_records_confirmed_memory_hit",
        )
        filtered = {
            row.get("item_id"): row.get("reason") for row in trace.filters
        }
        _ensure(
            blocked_ids.issubset(filtered),
            "ac04_all_unsafe_shoes_filtered_at_recall",
        )
        _ensure(
            all(
                filtered[item_id]
                in {
                    "EXCLUDED_HIGH_HEEL",
                    "EXCLUDED_HEEL",
                    "HEEL_EVIDENCE_MISSING",
                }
                for item_id in blocked_ids
            ),
            "ac04_filter_reasons_are_closed_set",
        )
        ranker_ids = {
            str(row.get("item_id"))
            for key in ("rule", "bm25", "dense", "rrf")
            for row in trace.retrieval.get(key, [])
        }
        _ensure(not (blocked_ids & ranker_ids), "ac04_rankers_never_see_unsafe_shoes")
        output_ids = {
            item_id
            for outfit in recommendation.outfits
            for item_id in outfit.items
        }
        alternative_ids = {
            item_id
            for outfit in recommendation.outfits
            for alternatives in outfit.alternatives.values()
            for item_id in alternatives
        }
        _ensure(
            not (blocked_ids & (output_ids | alternative_ids)),
            "ac04_unsafe_shoes_absent_from_outfits_and_alternatives",
        )
        _ensure(
            bool(evidence_safe_ids & ranker_ids),
            "ac04_evidence_safe_shoe_remains_eligible",
        )
        return _assurance_pass(
            evidence,
            [
                "memory_propose_without_commit",
                "memory_confirm_commit",
                "new_query_omits_preference",
                "confirmed_memory_hit_without_reasking",
                "known_heel_filtered",
                "unknown_heel_evidence_filtered",
                "ranker_lists_exclude_unsafe_shoes",
                "outfits_and_alternatives_exclude_unsafe_shoes",
                "evidence_safe_shoe_remains",
            ],
        )
    except BaseException as exc:
        return _assurance_fail(evidence, exc)
    finally:
        services.assets.close()


async def _assure_ac06_sparse_interview(root: Path) -> dict[str, Any]:
    """Create a controlled wardrobe with exactly one legal interview combo."""

    from .app import AppServices
    from .models import WardrobePatch

    services = AppServices(
        Settings(
            root_dir=root,
            cpa_text_enabled=False,
            cpa_api_key=None,
            dense_enabled=False,
            catalog_enabled=True,
            vision_force_failure=True,
        )
    )
    evidence = (
        "controlled sparse u01 interview wardrobe with independent outfit oracle"
    )
    try:
        only_complete_set = {"g005", "g011", "g020"}
        taboo_second_top_id = "g008"
        for item in services.repository.list_garments("u01"):
            target_status = (
                "available"
                if item.garment_id in only_complete_set | {taboo_second_top_id}
                else "unavailable"
            )
            patch = WardrobePatch(status=target_status)
            if item.garment_id == taboo_second_top_id:
                # This controlled candidate would form a second complete
                # interview set if the explicit purple taboo were relaxed.
                patch = WardrobePatch(
                    status="available",
                    color="purple",
                    occasions=["interview"],
                )
            updated = services.repository.update_garment(
                item.garment_id,
                "u01",
                patch,
            )
            _ensure(updated is not None, "ac06_sparse_fixture_patch_succeeded")

        catalog_invocations = 0
        original_search = services.catalog.search

        def catalog_spy(scene: Any):
            nonlocal catalog_invocations
            catalog_invocations += 1
            return original_search(scene)

        services.catalog.search = catalog_spy  # type: ignore[method-assign]
        scene = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text=(
                    "面试截止时间还没定，不要紫色，"
                    "只用现有衣橱给完整方案。"
                ),
                event_horizon="unknown",
                intent="recommend",
                occasion="interview",
                goals=["reliable", "polished"],
            )
        )
        recommendation = services.recommendations.recommend(
            services.recommendations.resolve_payload(
                {"request_id": scene.request_id}
            )
        )
        trace = services.traces.get(scene.trace_id)
        _ensure(trace is not None, "ac06_sparse_trace_exists")
        _ensure(
            "purple" in scene.constraints.taboo_colors,
            "ac06_sparse_explicit_nonempty_taboo",
        )
        _ensure(
            scene.urgency == "high"
            and not scene.shopping_allowed
            and not scene.ui_capabilities.shopping_cta
            and not recommendation.ui_capabilities.shopping_cta,
            "ac06_sparse_high_gate",
        )
        _ensure(
            catalog_invocations == 0
            and trace.catalog.get("attempted") is False
            and trace.catalog.get("call_count") == 0
            and not recommendation.shopping_suggestions,
            "ac06_sparse_catalog_actual_and_trace_zero",
        )
        _ensure(
            len(recommendation.outfits) == 1
            and recommendation.outfits[0].is_primary
            and bool(recommendation.gap_explanation),
            "ac06_sparse_exactly_one_direction_with_gap",
        )
        outfit = recommendation.outfits[0]
        _ensure(
            set(outfit.items) == only_complete_set,
            "ac06_sparse_only_controlled_fixture_ids",
        )
        garment_by_id = {
            item.garment_id: item
            for user in services.repository.list_users()
            for item in services.repository.list_garments(user.user_id)
        }
        filtered = {
            str(row.get("item_id")): str(row.get("reason"))
            for row in trace.filters
        }
        _ensure(
            filtered.get(taboo_second_top_id) == "TABOO_COLOR",
            "ac06_sparse_second_combo_filtered_by_taboo_at_recall",
        )
        ranker_ids = {
            str(row.get("item_id"))
            for key in ("rule", "bm25", "dense", "rrf")
            for row in trace.retrieval.get(key, [])
        }
        _ensure(
            taboo_second_top_id not in ranker_ids,
            "ac06_sparse_taboo_candidate_absent_from_all_rankers",
        )
        output_and_alternative_ids = set(outfit.items) | {
            item_id
            for alternatives in outfit.alternatives.values()
            for item_id in alternatives
        }
        _ensure(
            taboo_second_top_id not in output_and_alternative_ids,
            "ac06_sparse_taboo_candidate_absent_from_output_and_alternatives",
        )
        counterfactual = scene.model_copy(deep=True)
        counterfactual.constraints = counterfactual.constraints.model_copy(
            update={"taboo_colors": []}
        )
        _ensure(
            not independent_outfit_violations(
                [taboo_second_top_id, "g011", "g020"],
                counterfactual,
                garment_by_id,
            ),
            "ac06_sparse_taboo_candidate_would_form_second_legal_combo",
        )
        _ensure(
            not independent_outfit_violations(
                outfit.items, scene, garment_by_id
            ),
            "ac06_sparse_independent_hard_oracle_zero",
        )
        _ensure(
            all(
                garment_by_id[item_id].user_id == "u01"
                and garment_by_id[item_id].status == "available"
                and garment_by_id[item_id].color
                not in scene.constraints.taboo_colors
                for item_id in outfit.items
            ),
            "ac06_sparse_grounded_without_taboo_relaxation",
        )
        return _assurance_pass(
            evidence,
            [
                "exactly_one_complete_interview_direction",
                "explicit_honest_gap",
                "all_ids_current_owner_grounded",
                "independent_hard_oracle_zero",
                "taboo_not_relaxed",
                "taboo_candidate_filtered_before_rankers",
                "taboo_candidate_absent_from_outfits_and_alternatives",
                "counterfactual_second_combo_proven",
                "high_shopping_ui_false",
                "catalog_actual_and_trace_zero",
            ],
        )
    except BaseException as exc:
        return _assurance_fail(evidence, exc)
    finally:
        services.assets.close()


async def _assure_ac10(root: Path) -> dict[str, Any]:
    """Inject unknown and cross-owner IDs into the final response validator."""

    from .app import AppServices
    from .models import OutfitValidation, RecommendedOutfit

    services = AppServices(
        Settings(
            root_dir=root,
            cpa_text_enabled=False,
            cpa_api_key=None,
            dense_enabled=False,
            catalog_enabled=True,
            vision_force_failure=True,
        )
    )
    evidence = "adversarial RecommendationValidator unknown/cross-owner injection"
    try:
        scene = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text="下周通勤，只用现有衣橱搭配。",
                event_horizon="soon",
                intent="recommend",
                occasion="commute",
                goals=["reliable"],
            )
        )
        cross_owner_id = services.repository.list_garments("u02")[0].garment_id

        def injected(label: str, item_id: str) -> RecommendedOutfit:
            return RecommendedOutfit(
                outfit_id=f"injected_{label}",
                strategy_label=label,
                items=[item_id],
                reasons=["adversarial fixture"],
                risks=["must be rejected"],
                alternatives={},
                is_primary=True,
                trust_statement="untrusted injected candidate",
                validation=OutfitValidation(
                    all_ids_grounded=True,
                    hard_constraints_passed=True,
                    required_slots_complete=True,
                ),
            )

        accepted, validator_trace = services.validator.validate(
            scene,
            [
                injected("unknown", "g999"),
                injected("cross_owner", cross_owner_id),
            ],
            services.repository.garment_ids("u01"),
        )
        _ensure(not accepted, "ac10_injected_ids_not_accepted")
        _ensure(
            validator_trace.get("rejected_reason_counts", {}).get(
                "NON_WHITELIST_ID"
            )
            == 2,
            "ac10_both_injections_recorded_as_non_whitelist",
        )
        public_payload = json.dumps(
            {
                "accepted": [item.model_dump(mode="json") for item in accepted],
                "validator": validator_trace,
            },
            ensure_ascii=False,
        )
        _ensure(
            "g999" not in public_payload and cross_owner_id not in public_payload,
            "ac10_illegal_ids_not_reflected_in_public_result",
        )
        return _assurance_pass(
            evidence,
            [
                "unknown_id_rejected",
                "cross_owner_id_rejected",
                "non_whitelist_reason_count_two",
                "illegal_ids_not_reflected",
            ],
        )
    except BaseException as exc:
        return _assurance_fail(evidence, exc)
    finally:
        services.assets.close()


async def _assure_ac11(root: Path) -> dict[str, Any]:
    """Force-call the secondary Catalog guard with an authoritative high scene."""

    from .app import AppServices

    services = AppServices(
        Settings(
            root_dir=root,
            cpa_text_enabled=False,
            cpa_api_key=None,
            dense_enabled=False,
            catalog_enabled=True,
            vision_force_failure=True,
        )
    )
    evidence = "upper orchestrator response plus forced secondary CatalogService guard"
    try:
        scene = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text="现在缺一件上衣，需要补购后出门。",
                event_horizon="now",
                intent="fill_gap",
                occasion="daily",
                goals=["comfortable"],
            )
        )
        _ensure(
            scene.urgency == "high"
            and not scene.shopping_allowed
            and not scene.ui_capabilities.shopping_cta,
            "ac11_authoritative_high_scene_gate",
        )
        recommendation = services.recommendations.recommend(
            services.recommendations.resolve_payload(
                {"request_id": scene.request_id}
            )
        )
        trace = services.traces.get(scene.trace_id)
        _ensure(trace is not None, "ac11_upper_trace_exists")
        _ensure(
            not recommendation.shopping_suggestions
            and not recommendation.ui_capabilities.shopping_cta
            and trace.catalog.get("attempted") is False
            and trace.catalog.get("call_count") == 0,
            "ac11_upper_orchestrator_catalog_zero",
        )
        forced = services.catalog.search(scene)
        _ensure(not forced.items, "ac11_secondary_guard_returns_no_items")
        _ensure(forced.call_count == 0, "ac11_secondary_guard_call_count_zero")
        _ensure(
            forced.blocked_reason in {"HIGH_URGENCY", "SHOPPING_NOT_ALLOWED"},
            "ac11_secondary_guard_explicit_block_reason",
        )
        _ensure(
            not services.catalog.suggestions(forced, scene),
            "ac11_secondary_guard_returns_no_suggestions",
        )
        return _assurance_pass(
            evidence,
            [
                "upper_attempted_false_call_zero",
                "secondary_forced_call_zero",
                "secondary_explicit_block_reason",
                "normal_and_forced_suggestions_empty",
            ],
        )
    except BaseException as exc:
        return _assurance_fail(evidence, exc)
    finally:
        services.assets.close()


async def _assure_ac16_ac17(root: Path) -> dict[str, dict[str, Any]]:
    """Exercise the qualitative score boundary and satisfaction-one finalization."""

    from .app import AppServices
    from .look_models import (
        AdjustLookInput,
        FinalizeLookInput,
        ScorecardInput,
        SelectedOutfitLookInput,
    )
    from .look_service import LookConflict

    services = AppServices(
        Settings(
            root_dir=root,
            cpa_text_enabled=False,
            cpa_api_key=None,
            dense_enabled=False,
            catalog_enabled=True,
            vision_force_failure=True,
        )
    )
    score_evidence = "in-process no-asset six-dimension scorecard boundary; external calls=0"
    final_evidence = "in-process satisfaction=1 finalization and mutation rejection"
    score_result: dict[str, Any]
    final_result: dict[str, Any]
    try:
        scene = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text="为下周通勤准备一套可靠舒适的现有衣橱穿搭。",
                event_horizon="planned",
                intent="recommend",
                occasion="commute",
                goals=["comfortable", "reliable"],
            )
        )
        recommendation = services.recommendations.recommend(
            services.recommendations.resolve_payload({"request_id": scene.request_id})
        )
        _ensure(bool(recommendation.outfits), "ac16_seed_outfit")
        look = services.looks.create(
            SelectedOutfitLookInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                created_from="selected_outfit",
                request_id=scene.request_id,
                outfit_id=recommendation.outfits[0].outfit_id,
            )
        )
        card = await services.looks.score(
            ScorecardInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                look_version_id=look.look_version_id,
            )
        )
        dumped_dimensions = card.dimensions.model_dump()
        _ensure(set(dumped_dimensions) == {
            "occasion_fit",
            "expression_match",
            "overall_harmony",
            "silhouette_layering",
            "comfort_practicality",
            "detail_finish",
        }, "ac16_exact_six_dimensions")
        _ensure(not card.numeric_score_available and card.total_score is None, "ac16_no_asset_no_numeric_score")
        _ensure(
            all(value["score"] is None for value in dumped_dimensions.values()),
            "ac16_all_dimension_scores_null",
        )
        _ensure(not card.priority_adjustments, "ac16_no_evidence_no_adjustment")
        _ensure(
            card.prohibited_subject_checks.model_dump()
            == {
                "appearance": False,
                "body": False,
                "age": False,
                "sexual_attractiveness": False,
            },
            "ac16_prohibited_subject_boundary",
        )
        _ensure(bool(card.keep_point.statement), "ac16_keep_point_present")
        qualitative_text_violations = independent_scorecard_text_violations(card)
        _ensure(
            not qualitative_text_violations,
            "ac16_qualitative_visible_text_forbidden:"
            + ",".join(qualitative_text_violations),
        )
        score_result = _assurance_pass(
            score_evidence,
            [
                "exact_six_dimensions",
                "no_asset_all_scores_null",
                "no_visual_adjustment_without_evidence",
                "outfit_only_subject_boundary",
                "keep_point_present",
                "qualitative_visible_text_closed_scan",
            ],
        )

        final = services.looks.finalize(
            FinalizeLookInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                look_version_id=look.look_version_id,
                satisfied=True,
                satisfaction=1,
                reason="save_current_decision",
            )
        )
        _ensure(final.satisfaction == 1 and final.advice_stopped, "ac17_satisfaction_one_finalizes")
        mutation_rejected = False
        try:
            services.looks.adjust(
                AdjustLookInput(
                    user_id="u01",
                    styling_session_id=scene.styling_session_id,
                    look_version_id=look.look_version_id,
                    decision="user_modified",
                    item_ids=look.item_ids,
                )
            )
        except LookConflict:
            mutation_rejected = True
        _ensure(mutation_rejected, "ac17_post_final_adjustment_rejected")
        final_result = _assurance_pass(
            final_evidence,
            ["satisfaction_one_finalizes", "advice_stopped", "post_final_adjustment_rejected"],
        )
    except BaseException as exc:
        failure = _assurance_fail("score/final in-process workflow", exc)
        score_result = failure
        final_result = failure
    finally:
        services.assets.close()
    return {"AC-16": score_result, "AC-17": final_result}


def _assurance_png(width: int, height: int, *, informative: bool) -> bytes:
    from PIL import Image

    image = Image.new(
        "RGB", (width, height), (0, 0, 0) if informative else (128, 96, 64)
    )
    if informative:
        image.putdata(
            [
                (
                    (x * 7 + y * 3) % 256,
                    (x * 2 + y * 11) % 256,
                    (x + y * 5) % 256,
                )
                for y in range(height)
                for x in range(width)
            ]
        )
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _assurance_vision_payload(
    *,
    model: str = "grok-4.6-high",
    partial: bool = False,
    free_text: bool = False,
    slot_mismatch: bool = False,
    extra_field: bool = False,
    malformed_json: bool = False,
) -> dict[str, Any]:
    regions: list[dict[str, Any]] = []
    for slot in ("top", "bottom", "shoes", "overall"):
        visible = not (partial and slot == "shoes")
        observation = f"{slot}_{'visible' if visible else 'not_visible'}"
        if free_text and slot == "overall":
            observation = "the wearer looks young and attractive"
        if slot_mismatch and slot == "top":
            observation = "bottom_visible"
        issues = ["none"]
        if visible and slot == "bottom":
            issues = ["bottom_cuff_messy"]
        if visible and slot == "shoes":
            issues = ["shoe_coordination_issue"]
        region: dict[str, Any] = {
            "slot": slot,
            "visible": visible,
            "observation": observation,
            "confidence": 0.94,
            "issue_codes": issues,
        }
        if extra_field and slot == "overall":
            region["person_note"] = "provider prose must fail closed"
        regions.append(region)
    content = "{" if malformed_json else json.dumps({"regions": regions})
    return {"model": model, "choices": [{"message": {"content": content}}]}


def _assurance_vision_transport(
    payload: dict[str, Any] | None, *, status: int = 200
):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        request_body = json.loads(request.content)
        _ensure(
            request_body.get("model") == "grok-4.6-high",
            "ac13_exact_transport_model_request",
        )
        _ensure(
            request_body.get("response_format") == {"type": "json_object"},
            "ac13_json_response_contract",
        )
        content = request_body["messages"][1]["content"]
        _ensure(
            "controlled code, never prose" in content[0]["text"],
            "ac13_controlled_observation_prompt",
        )
        _ensure(
            any(
                part.get("type") == "image_url"
                and part["image_url"]["url"].startswith("data:image/png;base64,")
                for part in content
            ),
            "ac13_owned_image_sent_to_mock",
        )
        if status != 200:
            return httpx.Response(status, json={"error": "injected"})
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


async def _assure_ac13_ac14(root: Path) -> dict[str, dict[str, Any]]:
    """Exercise strict Vision success/fail-closed matrix and reject replay."""

    from .app import AppServices
    from .look_models import (
        AdjustLookInput,
        ScorecardInput,
        SelectedOutfitLookInput,
        UserRevisionLookInput,
    )
    from .look_service import LookError

    base_settings = Settings(
        root_dir=root,
        cpa_text_enabled=False,
        cpa_api_key=None,
        dense_enabled=False,
        catalog_enabled=True,
        vision_force_failure=False,
    )
    services = AppServices(base_settings)
    vision_evidence = "in-process httpx.MockTransport strict Vision matrix; external calls=0"
    adjust_evidence = "in-process verified-visual reject/replay/version inheritance workflow"
    numeric_text_evidence = (
        "independent closed-term scan of the MockTransport numeric scorecard"
    )
    vision_result: dict[str, Any]
    adjust_result: dict[str, Any]
    numeric_text_result: dict[str, Any] = {
        "status": "NOT_RUN",
        "evidence": numeric_text_evidence,
        "checks": [],
        "failed_steps": ["ac16_numeric_visible_text_scan_not_run"],
    }
    try:
        scene = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text="为下周通勤准备一套可靠舒适的现有衣橱穿搭。",
                event_horizon="planned",
                intent="recommend",
                occasion="commute",
                goals=["comfortable", "reliable"],
            )
        )
        recommendation = services.recommendations.recommend(
            services.recommendations.resolve_payload({"request_id": scene.request_id})
        )
        outfit = next(
            (
                candidate
                for candidate in recommendation.outfits
                if any(
                    services.repository.get_garment(item_id).slot == "shoes"  # type: ignore[union-attr]
                    for item_id in candidate.items
                )
            ),
            None,
        )
        _ensure(outfit is not None, "ac13_seed_outfit_with_shoes")
        v1 = services.looks.create(
            SelectedOutfitLookInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                created_from="selected_outfit",
                request_id=scene.request_id,
                outfit_id=outfit.outfit_id,
            )
        )

        limited = services.assets.upload(
            user_id="u01",
            styling_session_id=scene.styling_session_id,
            angle="front",
            media_type="image/png",
            content=_assurance_png(1, 1, informative=False),
            consent_obtained=True,
            purpose="styling_assessment",
        )
        _ensure(limited.visual_quality == "limited", "ac13_limited_asset_classified")
        v2 = services.looks.create(
            UserRevisionLookInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                created_from="user_revision",
                parent_version_id=v1.look_version_id,
                item_ids=v1.item_ids,
                asset_ids=(limited.asset_id,),
            )
        )
        limited_card = await services.looks.score(
            ScorecardInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                look_version_id=v2.look_version_id,
            )
        )
        _ensure(
            limited_card.evidence_status == "limited_asset"
            and not limited_card.numeric_score_available
            and limited_card.total_score is None
            and not limited_card.priority_adjustments,
            "ac13_limited_asset_fail_closed",
        )

        usable = services.assets.upload(
            user_id="u01",
            styling_session_id=scene.styling_session_id,
            angle="front",
            media_type="image/png",
            content=_assurance_png(256, 256, informative=True),
            consent_obtained=True,
            purpose="styling_assessment",
        )
        _ensure(usable.visual_quality == "usable", "ac13_usable_asset_classified")
        v3 = services.looks.create(
            UserRevisionLookInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                created_from="user_revision",
                parent_version_id=v2.look_version_id,
                item_ids=v2.item_ids,
                asset_ids=(usable.asset_id,),
            )
        )
        services.vision.settings = replace(
            base_settings,
            cpa_text_enabled=True,
            cpa_base_url="http://vision.invalid/v1",
            cpa_api_key=None,
        )
        services.vision.set_transport(
            _assurance_vision_transport(_assurance_vision_payload())
        )
        success = await services.looks.score(
            ScorecardInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                look_version_id=v3.look_version_id,
                asset_ids=(usable.asset_id,),
            )
        )
        _ensure(
            success.numeric_score_available
            and success.total_score is not None
            and success.evidence_status == "sufficient",
            "ac13_exact_full_region_numeric_success",
        )
        _ensure(len(success.priority_adjustments) in {1, 2}, "ac13_at_most_two_verified_adjustments")
        _ensure(
            all(
                item.evidence_source == "verified_visual"
                and item.evidence_regions
                and item.scorecard_id == success.scorecard_id
                for item in success.priority_adjustments
            ),
            "ac13_adjustments_bound_to_verified_scorecard",
        )
        _ensure(
            all(
                not item.observation.endswith(("_visible", "_not_visible"))
                and "wearer" not in item.observation.lower()
                for item in success.visual_evidence
            ),
            "ac13_server_owned_evidence_text_only",
        )
        numeric_text_violations = independent_scorecard_text_violations(success)
        _ensure(
            not numeric_text_violations,
            "ac16_numeric_visible_text_forbidden:"
            + ",".join(numeric_text_violations),
        )
        numeric_text_result = _assurance_pass(
            numeric_text_evidence,
            ["numeric_visible_text_closed_scan"],
        )

        services.vision.set_transport(
            _assurance_vision_transport(
                _assurance_vision_payload(model="grok-4.6-build")
            )
        )
        concrete_build = await services.looks.score(
            ScorecardInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                look_version_id=v3.look_version_id,
            )
        )
        concrete_build_trace = services.traces.get(concrete_build.trace_id)
        _ensure(
            concrete_build.numeric_score_available
            and concrete_build_trace is not None
            and concrete_build_trace.provider.get("resolved_model")
            == "grok-4.6-build"
            and concrete_build_trace.provider.get("model_verified") is True,
            "ac13_exact_concrete_build_allowlisted",
        )

        degraded_matrix = (
            ("model_mismatch", _assurance_vision_payload(model="grok-4.5"), 200),
            (
                "model_prefix_mismatch",
                _assurance_vision_payload(model="grok-4.6-high-preview"),
                200,
            ),
            ("malformed", _assurance_vision_payload(malformed_json=True), 200),
            ("unsafe_free_text", _assurance_vision_payload(free_text=True), 200),
            ("slot_mismatch", _assurance_vision_payload(slot_mismatch=True), 200),
            ("extra_field", _assurance_vision_payload(extra_field=True), 200),
            ("partial", _assurance_vision_payload(partial=True), 200),
            ("http_error", None, 503),
        )
        for label, payload, status in degraded_matrix:
            services.vision.set_transport(
                _assurance_vision_transport(payload, status=status)
            )
            degraded = await services.looks.score(
                ScorecardInput(
                    user_id="u01",
                    styling_session_id=scene.styling_session_id,
                    look_version_id=v3.look_version_id,
                )
            )
            _ensure(
                degraded.evidence_status == "vision_degraded"
                and not degraded.numeric_score_available
                and degraded.total_score is None
                and all(
                    value["score"] is None
                    for value in degraded.dimensions.model_dump().values()
                )
                and not degraded.priority_adjustments,
                f"ac13_{label}_fail_closed",
            )
        vision_result = _assurance_pass(
            vision_evidence,
            [
                "limited_asset_all_null",
                "exact_grok_4_5_full_region_numeric_success",
                "server_owned_evidence_text",
                "model_mismatch_all_null",
                "malformed_all_null",
                "free_text_all_null",
                "slot_mismatch_all_null",
                "extra_field_all_null",
                "partial_all_null",
                "http_error_all_null",
            ],
        )

        services.vision.set_transport(
            _assurance_vision_transport(_assurance_vision_payload())
        )
        proposal = next(
            (
                item
                for item in success.priority_adjustments
                if item.canonical_key == "trouser_cuff_single"
            ),
            None,
        )
        _ensure(proposal is not None, "ac14_canonical_trouser_cuff_proposed")
        _ensure(
            proposal.canonical_action == "trouser_cuff_single"
            and "卷" in proposal.action,
            "ac14_canonical_action_is_single_cuff",
        )
        rejection = services.looks.adjust(
            AdjustLookInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                look_version_id=v3.look_version_id,
                adjustment_id=proposal.adjustment_id,
                decision="reject",
                reason="不要卷裤脚，我不喜欢露脚踝；接受当前状态。",
            )
        )
        _ensure(rejection.look_version is None, "ac14_reject_does_not_create_fake_version")
        _ensure(
            rejection.decision_record.canonical_key == "trouser_cuff_single"
            and rejection.decision_record.reason is not None
            and "不喜欢露脚踝" in rejection.decision_record.reason,
            "ac14_rejection_preserves_no_ankle_boundary",
        )
        _ensure(
            not rejection.priority_adjustments,
            "ac14_accept_current_state_after_reject",
        )
        replay_blocked = False
        try:
            services.looks.adjust(
                AdjustLookInput(
                    user_id="u01",
                    styling_session_id=scene.styling_session_id,
                    look_version_id=v3.look_version_id,
                    adjustment_id=proposal.adjustment_id,
                    decision="reject",
                )
            )
        except LookError:
            replay_blocked = True
        _ensure(replay_blocked, "ac14_decided_token_cannot_replay")
        rescored = await services.looks.score(
            ScorecardInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                look_version_id=v3.look_version_id,
            )
        )
        _ensure(
            proposal.canonical_key
            not in {item.canonical_key for item in rescored.priority_adjustments},
            "ac14_rejected_canonical_not_reproposed",
        )
        user_revision = services.looks.adjust(
            AdjustLookInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                look_version_id=v3.look_version_id,
                decision="user_modified",
                asset_ids=(),
            )
        )
        _ensure(user_revision.look_version is not None, "ac14_user_revision_creates_version")
        _ensure(
            proposal.canonical_key in user_revision.look_version.rejected_adjustments,
            "ac14_future_version_inherits_rejection",
        )
        _ensure(
            not user_revision.look_version.partially_accepted_adjustments,
            "ac14_user_modified_not_partial",
        )
        chain = services.looks.chain("u01", scene.styling_session_id)
        _ensure(
            any(
                item.decision_record_id == rejection.decision_record.decision_record_id
                for item in chain.adjustment_decisions
            ),
            "ac14_decision_replayable_in_chain",
        )
        adjust_result = _assurance_pass(
            adjust_evidence,
            [
                "at_most_two_adjustments",
                "canonical_trouser_cuff_single",
                "no_ankle_rejection_boundary_preserved",
                "accept_current_state_closed_alternative",
                "reject_creates_decision_not_fake_version",
                "decided_token_replay_blocked",
                "canonical_not_reproposed",
                "future_version_inherits_rejection",
                "decision_replayable_in_chain",
                "user_modified_not_partial",
            ],
        )
    except BaseException as exc:
        failure = _assurance_fail("Vision/adjustment in-process workflow", exc)
        vision_result = failure
        adjust_result = failure
        if numeric_text_result["status"] != "PASS":
            numeric_text_result = _assurance_fail(numeric_text_evidence, exc)
    finally:
        services.assets.close()
    return {
        "AC-13": vision_result,
        "AC-14": adjust_result,
        "_AC16_NUMERIC_TEXT": numeric_text_result,
    }


async def _assure_ac13_full_chain(root: Path) -> dict[str, Any]:
    """Execute the interview image -> v1 -> adjust -> v2 -> rescore -> Final chain."""

    from .app import AppServices
    from .look_models import (
        AdjustLookInput,
        FinalizeLookInput,
        ScorecardInput,
        SelectedOutfitLookInput,
        UserRevisionLookInput,
    )
    from .look_service import LookConflict

    base_settings = Settings(
        root_dir=root,
        cpa_text_enabled=False,
        cpa_api_key=None,
        dense_enabled=False,
        catalog_enabled=True,
        vision_force_failure=False,
    )
    services = AppServices(base_settings)
    evidence = (
        "single in-process interview reliable-modern image/v1/adjusted-image/v2/"
        "rescore/Final chain with httpx.MockTransport; external calls=0"
    )
    try:
        scene = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text="下周面试，想显得可靠但不老气；先用现有衣橱。",
                event_horizon="soon",
                intent="recommend",
                occasion="interview",
                goals=["reliable", "modern"],
            )
        )
        recommendation = services.recommendations.recommend(
            services.recommendations.resolve_payload({"request_id": scene.request_id})
        )
        garment_by_id = {
            item.garment_id: item
            for item in services.repository.list_garments("u01")
        }
        outfit = next(
            (
                candidate
                for candidate in sorted(
                    recommendation.outfits, key=lambda item: len(item.items)
                )
                if "g011" in candidate.items
                and not independent_outfit_violations(
                    candidate.items, scene, garment_by_id
                )
            ),
            None,
        )
        _ensure(outfit is not None, "ac13_full_grounded_interview_trouser_direction")
        recommendation_trace = services.traces.get(recommendation.trace_id)
        _ensure(
            recommendation_trace is not None
            and recommendation_trace.request_id == scene.request_id
            and recommendation_trace.styling_session_id
            == scene.styling_session_id,
            "ac13_full_recommendation_trace_grounded",
        )

        initial_asset = services.assets.upload(
            user_id="u01",
            styling_session_id=scene.styling_session_id,
            angle="front",
            media_type="image/png",
            content=_assurance_png(256, 256, informative=True),
            consent_obtained=True,
            purpose="styling_assessment",
        )
        _ensure(
            initial_asset.user_id == "u01"
            and initial_asset.styling_session_id == scene.styling_session_id
            and initial_asset.visual_quality == "usable",
            "ac13_full_initial_image_owned_sufficient",
        )
        initial_asset_trace = services.traces.get(initial_asset.trace_id)
        _ensure(
            initial_asset_trace is not None
            and initial_asset_trace.styling_session_id == scene.styling_session_id
            and initial_asset_trace.provider.get("operation") == "upload"
            and initial_asset_trace.provider.get("user_id") == "u01"
            and initial_asset_trace.provider.get("asset_id")
            == initial_asset.asset_id,
            "ac13_full_initial_asset_trace_grounded",
        )
        v1 = services.looks.create(
            SelectedOutfitLookInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                created_from="selected_outfit",
                request_id=scene.request_id,
                outfit_id=outfit.outfit_id,
                asset_ids=(initial_asset.asset_id,),
            )
        )
        v1_snapshot = {
            "look_version_id": v1.look_version_id,
            "parent_version_id": v1.parent_version_id,
            "version_index": v1.version_index,
            "item_ids": v1.item_ids,
            "asset_ids": v1.asset_ids,
            "created_from": v1.created_from,
            "source_outfit_id": v1.source_outfit_id,
            "created_at": v1.created_at,
        }
        _ensure(
            v1.user_id == "u01"
            and v1.styling_session_id == scene.styling_session_id
            and v1.parent_version_id is None
            and v1.version_index == 1
            and v1.asset_ids == (initial_asset.asset_id,),
            "ac13_full_v1_owner_parent_index_asset",
        )
        v1_trace = services.traces.get(v1.trace_id)
        _ensure(
            v1_trace is not None
            and v1_trace.validator.get("operation") == "create_version"
            and v1_trace.validator.get("user_id") == "u01",
            "ac13_full_v1_trace_grounded",
        )

        services.vision.settings = replace(
            base_settings,
            cpa_text_enabled=True,
            cpa_base_url="http://vision.invalid/v1",
            cpa_api_key=None,
        )
        services.vision.set_transport(
            _assurance_vision_transport(_assurance_vision_payload())
        )
        v1_card = await services.looks.score(
            ScorecardInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                look_version_id=v1.look_version_id,
                asset_ids=v1.asset_ids,
            )
        )
        _ensure(
            v1_card.numeric_score_available
            and v1_card.total_score is not None
            and v1_card.evidence_status == "sufficient"
            and v1_card.context.occasion == "interview"
            and {"reliable", "modern"}.issubset(v1_card.context.goals),
            "ac13_full_v1_numeric_interview_reliable_modern_context",
        )
        v1_card_trace = services.traces.get(v1_card.trace_id)
        _ensure(
            v1_card_trace is not None
            and v1_card_trace.validator.get("operation") == "scorecard"
            and v1_card_trace.validator.get("user_id") == "u01"
            and v1_card_trace.validator.get("look_version_id")
            == v1.look_version_id
            and v1_card_trace.provider.get("model_verified") is True,
            "ac13_full_v1_score_trace_grounded",
        )
        _ensure(
            bool(v1_card.keep_point.statement)
            and len(v1_card.priority_adjustments) in {1, 2},
            "ac13_full_keep_point_and_one_to_two_adjustments",
        )
        _ensure(
            all(
                proposal.scorecard_id == v1_card.scorecard_id
                and proposal.evidence_source == "verified_visual"
                and proposal.evidence_regions
                and proposal.expected_dimensions
                for proposal in v1_card.priority_adjustments
            ),
            "ac13_full_adjustments_bind_score_dimensions_and_visual_evidence",
        )
        selected_adjustment = v1_card.priority_adjustments[0]
        v1_items = [garment_by_id[item_id] for item_id in v1.item_ids]
        _v1_expected_scores, v1_expected_total = independent_score_breakdown(
            v1_items, scene
        )
        _ensure(
            v1_card.total_score == v1_expected_total,
            "ac13_full_v1_numeric_matches_independent_oracle",
        )

        candidate_item_sets: list[tuple[str, ...]] = []
        for item_id in v1.item_ids:
            item = garment_by_id[item_id]
            if item.slot in {"outer", "bag", "accessory"}:
                candidate_item_sets.append(
                    tuple(value for value in v1.item_ids if value != item_id)
                )
        occupied_core_slots = {
            garment_by_id[item_id].slot
            for item_id in v1.item_ids
            if garment_by_id[item_id].slot
            in {"outer", "top", "bottom", "dress", "shoes", "bag"}
        }
        for item_id in ("g004", "g023", "g026"):
            item = garment_by_id[item_id]
            if item_id not in v1.item_ids and (
                item.slot == "accessory" or item.slot not in occupied_core_slots
            ):
                candidate_item_sets.append((*v1.item_ids, item_id))

        adjusted_item_ids: tuple[str, ...] | None = None
        adjusted_expected_total: float | None = None
        for candidate_ids in candidate_item_sets:
            if independent_outfit_violations(candidate_ids, scene, garment_by_id):
                continue
            candidate_items = [garment_by_id[item_id] for item_id in candidate_ids]
            _scores, candidate_total = independent_score_breakdown(
                candidate_items, scene
            )
            if candidate_total != v1_expected_total:
                adjusted_item_ids = candidate_ids
                adjusted_expected_total = candidate_total
                break
        _ensure(
            adjusted_item_ids is not None and adjusted_expected_total is not None,
            "ac13_full_legal_adjusted_item_set_with_score_difference",
        )

        adjusted_asset = services.assets.upload(
            user_id="u01",
            styling_session_id=scene.styling_session_id,
            angle="front",
            media_type="image/png",
            content=_assurance_png(257, 256, informative=True),
            consent_obtained=True,
            purpose="styling_assessment",
        )
        _ensure(
            adjusted_asset.asset_id != initial_asset.asset_id
            and adjusted_asset.user_id == "u01"
            and adjusted_asset.styling_session_id == scene.styling_session_id
            and adjusted_asset.visual_quality == "usable",
            "ac13_full_adjusted_image_owned_sufficient_distinct",
        )
        adjusted_asset_trace = services.traces.get(adjusted_asset.trace_id)
        _ensure(
            adjusted_asset_trace is not None
            and adjusted_asset_trace.styling_session_id
            == scene.styling_session_id
            and adjusted_asset_trace.provider.get("operation") == "upload"
            and adjusted_asset_trace.provider.get("user_id") == "u01"
            and adjusted_asset_trace.provider.get("asset_id")
            == adjusted_asset.asset_id,
            "ac13_full_adjusted_asset_trace_grounded",
        )
        adjustment = services.looks.adjust(
            AdjustLookInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                look_version_id=v1.look_version_id,
                adjustment_id=selected_adjustment.adjustment_id,
                decision="accept",
                item_ids=adjusted_item_ids,
                asset_ids=(adjusted_asset.asset_id,),
                reason="已落实优先调整并上传调整后的单帧图。",
            )
        )
        v2 = adjustment.look_version
        _ensure(v2 is not None, "ac13_full_accept_creates_v2")
        _ensure(
            v2.user_id == "u01"
            and v2.styling_session_id == scene.styling_session_id
            and v2.parent_version_id == v1.look_version_id
            and v2.version_index == 2
            and v2.item_ids == adjusted_item_ids
            and v2.asset_ids == (adjusted_asset.asset_id,)
            and selected_adjustment.canonical_key in v2.accepted_adjustments,
            "ac13_full_v2_parent_index_owner_items_assets_adjustment",
        )
        v2_trace = services.traces.get(v2.trace_id)
        _ensure(
            v2_trace is not None
            and v2_trace.validator.get("operation") == "create_version"
            and v2_trace.validator.get("user_id") == "u01"
            and v2_trace.validator.get("look_version_id")
            == v2.look_version_id
            and v2_trace.validator.get("parent_version_id")
            == v1.look_version_id,
            "ac13_full_v2_version_trace_grounded",
        )
        adjustment_trace = services.traces.get(adjustment.decision_record.trace_id)
        _ensure(
            adjustment_trace is not None
            and adjustment_trace.validator.get("operation") == "adjust"
            and adjustment_trace.validator.get("source_version_id")
            == v1.look_version_id
            and adjustment_trace.validator.get("created_version_id")
            == v2.look_version_id,
            "ac13_full_adjust_trace_grounded",
        )

        services.vision.set_transport(
            _assurance_vision_transport(_assurance_vision_payload())
        )
        v2_card = await services.looks.score(
            ScorecardInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                look_version_id=v2.look_version_id,
                asset_ids=v2.asset_ids,
            )
        )
        _ensure(
            v2_card.numeric_score_available
            and v2_card.total_score == adjusted_expected_total
            and v2_card.comparison_to_parent.parent_version_id
            == v1.look_version_id
            and v2_card.comparison_to_parent.total_delta
            == round(v2_card.total_score - v1_card.total_score, 2)
            and v2_card.comparison_to_parent.total_delta != 0,
            "ac13_full_v2_rescore_parent_nonzero_difference",
        )
        v2_card_trace = services.traces.get(v2_card.trace_id)
        _ensure(
            v2_card_trace is not None
            and v2_card_trace.validator.get("operation") == "scorecard"
            and v2_card_trace.validator.get("user_id") == "u01"
            and v2_card_trace.validator.get("look_version_id")
            == v2.look_version_id
            and v2_card_trace.provider.get("model_verified") is True,
            "ac13_full_v2_score_trace_grounded",
        )
        v1_dimension_scores = {
            name: value["score"]
            for name, value in v1_card.dimensions.model_dump().items()
        }
        v2_dimension_scores = {
            name: value["score"]
            for name, value in v2_card.dimensions.model_dump().items()
        }
        _ensure(
            v1_dimension_scores != v2_dimension_scores,
            "ac13_full_v2_dimension_difference_visible",
        )
        chain = services.looks.chain(
            "u01", scene.styling_session_id, v2.look_version_id
        )
        _ensure(
            len(chain.versions) == 2
            and [version.version_index for version in chain.versions] == [1, 2]
            and chain.parent is not None
            and chain.parent.look_version_id == v1.look_version_id
            and chain.comparison is not None
            and chain.comparison.total_score_delta
            == v2_card.comparison_to_parent.total_delta,
            "ac13_full_chain_traceable_comparable",
        )
        stored_v1 = chain.versions[0]
        _ensure(
            {
                "look_version_id": stored_v1.look_version_id,
                "parent_version_id": stored_v1.parent_version_id,
                "version_index": stored_v1.version_index,
                "item_ids": stored_v1.item_ids,
                "asset_ids": stored_v1.asset_ids,
                "created_from": stored_v1.created_from,
                "source_outfit_id": stored_v1.source_outfit_id,
                "created_at": stored_v1.created_at,
            }
            == v1_snapshot,
            "ac13_full_v1_immutable_after_v2",
        )

        final = services.looks.finalize(
            FinalizeLookInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                look_version_id=v2.look_version_id,
                satisfied=True,
                satisfaction=5,
                reason="satisfied_with_adjusted_interview_look",
            )
        )
        _ensure(
            final.user_id == "u01"
            and final.look_version_id == v2.look_version_id
            and final.scorecard_id == v2_card.scorecard_id
            and final.total_score == v2_card.total_score
            and final.advice_stopped,
            "ac13_full_final_preserves_v2_score_and_stops",
        )
        final_trace = services.traces.get(final.trace_id)
        _ensure(
            final_trace is not None
            and final_trace.validator.get("operation") == "finalize"
            and final_trace.validator.get("look_version_id")
            == v2.look_version_id,
            "ac13_full_final_trace_grounded",
        )

        blocked: set[str] = set()
        try:
            await services.looks.score(
                ScorecardInput(
                    user_id="u01",
                    styling_session_id=scene.styling_session_id,
                    look_version_id=v2.look_version_id,
                )
            )
        except LookConflict:
            blocked.add("score")
        try:
            services.looks.adjust(
                AdjustLookInput(
                    user_id="u01",
                    styling_session_id=scene.styling_session_id,
                    look_version_id=v2.look_version_id,
                    decision="user_modified",
                    asset_ids=(initial_asset.asset_id,),
                )
            )
        except LookConflict:
            blocked.add("adjust")
        try:
            services.looks.create(
                UserRevisionLookInput(
                    user_id="u01",
                    styling_session_id=scene.styling_session_id,
                    created_from="user_revision",
                    parent_version_id=v2.look_version_id,
                    item_ids=v2.item_ids,
                    asset_ids=(initial_asset.asset_id,),
                )
            )
        except LookConflict:
            blocked.add("create")
        _ensure(
            blocked == {"score", "adjust", "create"},
            "ac13_full_post_final_score_adjust_create_blocked",
        )
        return _assurance_pass(
            evidence,
            [
                "interview_reliable_modern_context",
                "owned_sufficient_image_before_v1",
                "v1_numeric_keep_point_one_to_two_adjustments",
                "adjustments_bind_expected_dimensions_and_visual_evidence",
                "accepted_adjustment_and_adjusted_image_create_v2",
                "v1_immutable_v2_parent_index_correct",
                "v2_rescore_has_nonzero_parent_comparison",
                "chain_traceable_and_comparable",
                "satisfied_final_preserves_v2_score",
                "post_final_score_adjust_create_blocked",
                "owners_ids_and_traces_grounded",
            ],
        )
    except BaseException as exc:
        return _assurance_fail(evidence, exc)
    finally:
        services.assets.close()


async def _assure_ac17_numeric_78(root: Path) -> dict[str, Any]:
    """Generate an actual independently reproducible 78.0 card, then finalize it."""

    from .app import AppServices
    from .look_models import (
        AdjustLookInput,
        FinalizeLookInput,
        ScorecardInput,
        SelectedOutfitLookInput,
        UserRevisionLookInput,
    )
    from .look_service import LookConflict
    from .models import WardrobePatch

    base_settings = Settings(
        root_dir=root,
        cpa_text_enabled=False,
        cpa_api_key=None,
        dense_enabled=False,
        catalog_enabled=True,
        vision_force_failure=False,
    )
    services = AppServices(base_settings)
    evidence = (
        "isolated controlled wardrobe metadata -> independently recomputed and "
        "actually scored 78.0 -> satisfaction=1 Final; no score injection"
    )
    try:
        # This isolated service state creates the documented AC-17 precondition
        # deterministically. The scorecard itself is still produced by LookService.
        for item_id in ("g002", "g007", "g012"):
            updated = services.repository.update_garment(
                item_id, "u01", WardrobePatch(fit="slim")
            )
            _ensure(updated is not None and updated.fit == "slim", f"ac17_patch_{item_id}")

        scene = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text="下个月派对，想穿得自信，只用现有衣橱。",
                event_horizon="planned",
                intent="recommend",
                occasion="party",
                goals=["confident"],
            )
        )
        recommendation = services.recommendations.recommend(
            services.recommendations.resolve_payload({"request_id": scene.request_id})
        )
        _ensure(bool(recommendation.outfits), "ac17_numeric_seed_outfit")
        v1 = services.looks.create(
            SelectedOutfitLookInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                created_from="selected_outfit",
                request_id=scene.request_id,
                outfit_id=recommendation.outfits[0].outfit_id,
            )
        )
        asset = services.assets.upload(
            user_id="u01",
            styling_session_id=scene.styling_session_id,
            angle="front",
            media_type="image/png",
            content=_assurance_png(258, 256, informative=True),
            consent_obtained=True,
            purpose="styling_assessment",
        )
        controlled_ids = ("g002", "g007", "g012", "g019", "g026")
        garment_by_id = {
            item.garment_id: item
            for item in services.repository.list_garments("u01")
        }
        _ensure(
            not independent_outfit_violations(
                controlled_ids, scene, garment_by_id
            ),
            "ac17_numeric_controlled_look_hard_valid",
        )
        v2 = services.looks.create(
            UserRevisionLookInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                created_from="user_revision",
                parent_version_id=v1.look_version_id,
                item_ids=controlled_ids,
                asset_ids=(asset.asset_id,),
            )
        )
        expected_scores, expected_total = independent_score_breakdown(
            [garment_by_id[item_id] for item_id in controlled_ids], scene
        )
        _ensure(
            expected_scores
            == {
                "occasion_fit": 68,
                "expression_match": 72,
                "overall_harmony": 78,
                "silhouette_layering": 88,
                "comfort_practicality": 80,
                "detail_finish": 88,
            }
            and expected_total == 78.0,
            "ac17_independent_exact_78_precondition",
        )
        services.vision.settings = replace(
            base_settings,
            cpa_text_enabled=True,
            cpa_base_url="http://vision.invalid/v1",
            cpa_api_key=None,
        )
        services.vision.set_transport(
            _assurance_vision_transport(_assurance_vision_payload())
        )
        card = await services.looks.score(
            ScorecardInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                look_version_id=v2.look_version_id,
                asset_ids=v2.asset_ids,
            )
        )
        actual_scores = {
            name: value["score"]
            for name, value in card.dimensions.model_dump().items()
        }
        _ensure(
            card.numeric_score_available
            and card.total_score == 78.0
            and actual_scores == expected_scores,
            "ac17_actual_scorecard_exact_78_matches_independent_oracle",
        )
        final = services.looks.finalize(
            FinalizeLookInput(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                look_version_id=v2.look_version_id,
                satisfied=True,
                satisfaction=1,
                reason="satisfied_save_exact_78",
            )
        )
        _ensure(
            final.scorecard_id == card.scorecard_id
            and final.total_score == 78.0
            and final.satisfaction == 1
            and final.advice_stopped,
            "ac17_final_preserves_exact_78_satisfaction_one",
        )

        blocked: set[str] = set()
        try:
            await services.looks.score(
                ScorecardInput(
                    user_id="u01",
                    styling_session_id=scene.styling_session_id,
                    look_version_id=v2.look_version_id,
                )
            )
        except LookConflict:
            blocked.add("score")
        try:
            services.looks.adjust(
                AdjustLookInput(
                    user_id="u01",
                    styling_session_id=scene.styling_session_id,
                    look_version_id=v2.look_version_id,
                    decision="user_modified",
                    asset_ids=(),
                )
            )
        except LookConflict:
            blocked.add("adjust")
        try:
            services.looks.create(
                UserRevisionLookInput(
                    user_id="u01",
                    styling_session_id=scene.styling_session_id,
                    created_from="user_revision",
                    parent_version_id=v2.look_version_id,
                    item_ids=v2.item_ids,
                    asset_ids=(),
                )
            )
        except LookConflict:
            blocked.add("create")
        _ensure(
            blocked == {"score", "adjust", "create"},
            "ac17_post_final_score_adjust_continue_advice_blocked",
        )
        chain = services.looks.chain("u01", scene.styling_session_id)
        _ensure(
            chain.status == "final"
            and chain.final_version_id == v2.look_version_id
            and chain.look_version is not None
            and chain.look_version.status == "final",
            "ac17_chain_final_state",
        )
        return _assurance_pass(
            evidence,
            [
                "independent_six_dimension_exact_78_precondition",
                "actual_numeric_scorecard_exact_78",
                "final_scorecard_id_and_total_preserved",
                "satisfaction_one_finalizes",
                "advice_stopped",
                "post_final_score_adjust_create_blocked",
            ],
        )
    except BaseException as exc:
        return _assurance_fail(evidence, exc)
    finally:
        services.assets.close()


async def _assure_rain_contract(root: Path) -> dict[str, Any]:
    """Prove fail-closed rain behavior across recall, rankers and Catalog."""

    from .app import AppServices

    services = AppServices(
        Settings(
            root_dir=root,
            cpa_text_enabled=False,
            cpa_api_key=None,
            dense_enabled=False,
            catalog_enabled=True,
            vision_force_failure=True,
        )
    )
    evidence = (
        "rain outer/top/bag orchestration plus empty-gap direct Catalog guard"
    )
    try:
        outer_ids = {
            item.garment_id
            for item in services.repository.list_garments("u01")
            if item.slot == "outer"
        }
        _ensure(bool(outer_ids), "rain_fixture_has_outer_candidates")
        low_scenes: list[Any] = []
        for slot, query in (
            ("outer", "两周后下雨，缺外套，需要补购。"),
            ("top", "两周后下雨，缺上衣，需要补购。"),
            ("bag", "两周后下雨，缺包，需要补购。"),
        ):
            scene = await services.scene_parser.parse(
                SceneParseInput(
                    user_id="u01",
                    query_text=query,
                    event_horizon="planned",
                    intent="fill_gap",
                    occasion="daily",
                    goals=["comfortable"],
                )
            )
            low_scenes.append(scene)
            _ensure(
                scene.urgency == "low"
                and scene.shopping_allowed
                and scene.constraints.weather_requirement == "rain"
                and "outer" in scene.constraints.required_slots
                and slot in scene.constraints.gap_slots,
                f"rain_low_{slot}_scene_contract",
            )
            recommendation = services.recommendations.recommend(
                services.recommendations.resolve_payload(
                    {"request_id": scene.request_id}
                )
            )
            trace = services.traces.get(scene.trace_id)
            _ensure(trace is not None, f"rain_low_{slot}_trace_exists")
            filtered_outer_ids = {
                str(row.get("item_id"))
                for row in trace.filters
                if row.get("reason") == "RAIN_PROOF_EVIDENCE_MISSING"
            }
            _ensure(
                filtered_outer_ids == outer_ids,
                f"rain_low_{slot}_all_outers_filtered_before_recall",
            )
            ranker_ids = {
                str(row.get("item_id"))
                for key in ("rule", "bm25", "dense", "rrf")
                for row in trace.retrieval.get(key, [])
            }
            _ensure(
                not (outer_ids & ranker_ids),
                f"rain_low_{slot}_outer_count_zero_in_all_rankers",
            )
            _ensure(
                not recommendation.outfits and bool(recommendation.gap_explanation),
                f"rain_low_{slot}_safe_empty_with_gap",
            )
            _ensure(
                not recommendation.shopping_suggestions
                and trace.catalog.get("attempted") is True
                and trace.catalog.get("call_count") == 1
                and trace.catalog.get("returned_count") == 0
                and trace.catalog.get("blocked_reason")
                == "RAIN_PROOF_EVIDENCE_MISSING",
                f"rain_low_{slot}_catalog_explicitly_blocked",
            )

        empty_gap_scene = low_scenes[0].model_copy(deep=True)
        empty_gap_scene.constraints = empty_gap_scene.constraints.model_copy(
            update={"gap_slots": []}
        )
        empty_result = services.catalog.search(empty_gap_scene)
        _ensure(
            not empty_result.items
            and empty_result.attempted
            and empty_result.call_count == 1
            and empty_result.blocked_reason == "RAIN_PROOF_EVIDENCE_MISSING"
            and not services.catalog.suggestions(empty_result, empty_gap_scene),
            "rain_empty_gap_direct_catalog_block",
        )

        high = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text="现在下雨，缺外套，需要补购后出门。",
                event_horizon="now",
                intent="fill_gap",
                occasion="daily",
                goals=["comfortable"],
            )
        )
        high_result = services.recommendations.recommend(
            services.recommendations.resolve_payload(
                {"request_id": high.request_id}
            )
        )
        high_trace = services.traces.get(high.trace_id)
        _ensure(high_trace is not None, "rain_high_trace_exists")
        _ensure(
            high.urgency == "high"
            and not high.shopping_allowed
            and "outer" in high.constraints.required_slots,
            "rain_high_scene_contract",
        )
        _ensure(
            not high_result.outfits and bool(high_result.gap_explanation),
            "rain_high_safe_empty_with_gap",
        )
        _ensure(
            not high_result.shopping_suggestions
            and high_trace.catalog.get("attempted") is False
            and high_trace.catalog.get("call_count") == 0,
            "rain_high_catalog_zero",
        )
        return _assurance_pass(
            evidence,
            [
                "rain_requires_outer",
                "all_unverified_outers_filtered_before_rule_bm25_dense_rrf",
                "outfits_zero_with_explicit_gap",
                "low_outer_top_bag_catalog_items_zero",
                "empty_gap_direct_catalog_items_zero",
                "high_catalog_invocations_zero",
            ],
        )
    except BaseException as exc:
        return _assurance_fail(evidence, exc)
    finally:
        services.assets.close()


async def _assure_provider_privacy(root: Path) -> dict[str, Any]:
    """Spy on the CPA boundary and prove local private/safety short-circuiting."""

    from .app import AppServices

    services = AppServices(
        Settings(
            root_dir=root,
            cpa_text_enabled=True,
            cpa_base_url="http://cpa.invalid/v1",
            cpa_api_key=None,
            dense_enabled=False,
            catalog_enabled=True,
            vision_force_failure=True,
        )
    )
    evidence = (
        "in-process CPA method spy for e028/e029/e030, email, phone and vent"
    )
    provider_calls: list[str] = []

    async def provider_spy(query_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
        provider_calls.append(query_text)
        return (
            {
                "intent": "recommend",
                "occasion": "commute",
                "goals": ["reliable"],
            },
            {
                "attempted": True,
                "status": "ok",
                "requested_model": "grok4.6",
                "resolved_model": "grok-4.6-high",
                "model_verified": True,
            },
        )

    services.llm.parse_scene_advisory = provider_spy  # type: ignore[method-assign]
    try:
        cases, _sha = _load_cases(root)
        safety_rows = {
            str(row["case_id"]): row
            for row in cases
            if row["case_id"] in {"e028", "e029", "e030"}
        }
        _ensure(len(safety_rows) == 3, "privacy_frozen_safety_rows_present")
        blocked_inputs = [
            (
                str(row["user_id"]),
                str(row["query"]),
                str(row["intent"]),
                str(row["horizon"]),
            )
            for _case_id, row in sorted(safety_rows.items())
        ]
        blocked_inputs.extend(
            [
                (
                    "u01",
                    "下周通勤搭配，联系邮箱 private@example.com。",
                    "recommend",
                    "soon",
                ),
                (
                    "u01",
                    "下周通勤搭配，手机号 13800138000。",
                    "recommend",
                    "soon",
                ),
                (
                    "u01",
                    "我只是想说说今天很难过。",
                    "vent",
                    "today",
                ),
            ]
        )
        for index, (user_id, query, intent, horizon) in enumerate(blocked_inputs):
            before_calls = len(provider_calls)
            scene = await services.scene_parser.parse(
                SceneParseInput(
                    user_id=user_id,
                    query_text=query,
                    intent=intent,  # type: ignore[arg-type]
                    event_horizon=horizon,  # type: ignore[arg-type]
                )
            )
            trace = services.traces.get(scene.trace_id)
            _ensure(trace is not None, f"privacy_blocked_{index}_trace_exists")
            _ensure(
                len(provider_calls) == before_calls,
                f"privacy_blocked_{index}_provider_calls_zero",
            )
            _ensure(
                scene.backend == "rule_fallback"
                and trace.provider.get("attempted") is False
                and trace.provider.get("status") == "blocked"
                and trace.provider.get("reason_code")
                == "LOCAL_SAFETY_OR_PRIVACY_SHORT_CIRCUIT",
                f"privacy_blocked_{index}_trace_reason",
            )

        ordinary = await services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                query_text="下周通勤，请用现有衣橱搭一套可靠穿搭。",
                event_horizon="soon",
                intent="recommend",
                occasion="commute",
                goals=["reliable"],
            )
        )
        ordinary_trace = services.traces.get(ordinary.trace_id)
        _ensure(ordinary_trace is not None, "privacy_ordinary_trace_exists")
        _ensure(
            len(provider_calls) == 1
            and ordinary.backend == "grok4.6"
            and ordinary_trace.provider.get("attempted") is True
            and ordinary_trace.provider.get("status") == "ok",
            "privacy_ordinary_query_provider_call_one",
        )
        return _assurance_pass(
            evidence,
            [
                "e028_e029_e030_provider_zero",
                "email_provider_zero",
                "phone_provider_zero",
                "explicit_vent_provider_zero",
                "blocked_trace_attempted_false_reason_code",
                "ordinary_query_provider_one",
            ],
        )
    except BaseException as exc:
        return _assurance_fail(evidence, exc)
    finally:
        services.assets.close()


async def _assure_obs05_fault_matrix(root: Path) -> dict[str, Any]:
    """Run the four required provider failure legs inside the report command."""

    from .app import AppServices
    from .look_models import ScorecardInput, SelectedOutfitLookInput
    from .providers import ProviderUnavailable

    async def llm_leg() -> dict[str, Any]:
        services = AppServices(
            Settings(
                root_dir=root,
                cpa_text_enabled=True,
                cpa_base_url="http://llm.invalid/v1",
                cpa_api_key=None,
                dense_enabled=False,
                catalog_enabled=True,
                vision_force_failure=True,
            )
        )
        evidence = "LLM actual attempted failure -> scene_rules_v1 -> legal wardrobe result"

        async def injected_failure(_query: str):
            raise ProviderUnavailable("injected llm failure")

        services.llm.parse_scene_advisory = injected_failure  # type: ignore[method-assign]
        try:
            scene = await services.scene_parser.parse(
                SceneParseInput(
                    user_id="u01",
                    query_text="下周通勤，请用现有衣橱给一套可靠搭配。",
                    event_horizon="soon",
                    intent="recommend",
                    occasion="commute",
                    goals=["reliable"],
                )
            )
            recommendation = services.recommendations.recommend(
                services.recommendations.resolve_payload(
                    {"request_id": scene.request_id}
                )
            )
            trace = services.traces.get(scene.trace_id)
            garment_by_id = {
                item.garment_id: item
                for item in services.repository.list_garments("u01")
            }
            _ensure(
                scene.backend == "rule_fallback"
                and trace is not None
                and trace.provider.get("attempted") is True
                and trace.provider.get("status") == "degraded"
                and any(
                    event.get("component") == "llm"
                    and event.get("fallback") == "scene_rules_v1"
                    for event in trace.fallback_events
                ),
                "obs05_llm_actual_attempted_failure_trace",
            )
            _ensure(
                bool(recommendation.outfits)
                and all(
                    not independent_outfit_violations(
                        outfit.items, scene, garment_by_id
                    )
                    for outfit in recommendation.outfits
                ),
                "obs05_llm_rule_fallback_legal_core_recommendation",
            )
            return _assurance_pass(
                evidence,
                ["llm_failure_attempted", "llm_rule_fallback_legal_recommendation"],
            )
        except BaseException as exc:
            return _assurance_fail(evidence, exc)
        finally:
            services.assets.close()

    async def dense_leg() -> dict[str, Any]:
        services = AppServices(
            Settings(
                root_dir=root,
                cpa_text_enabled=False,
                cpa_api_key=None,
                dense_enabled=True,
                dense_force_failure=True,
                catalog_enabled=True,
                vision_force_failure=True,
            )
        )
        evidence = "Dense forced failure -> Rule/BM25/RRF and legal wardrobe result"
        try:
            scene = await services.scene_parser.parse(
                SceneParseInput(
                    user_id="u01",
                    query_text="下周通勤，请用现有衣橱给一套可靠搭配。",
                    event_horizon="soon",
                    intent="recommend",
                    occasion="commute",
                    goals=["reliable"],
                )
            )
            recommendation = services.recommendations.recommend(
                services.recommendations.resolve_payload(
                    {"request_id": scene.request_id}
                )
            )
            trace = services.traces.get(scene.trace_id)
            garment_by_id = {
                item.garment_id: item
                for item in services.repository.list_garments("u01")
            }
            _ensure(
                trace is not None
                and trace.retrieval.get("dense_status") == "degraded"
                and trace.retrieval.get("rule")
                and trace.retrieval.get("bm25")
                and trace.retrieval.get("rrf")
                and not trace.retrieval.get("dense")
                and any(
                    event.get("component") == "dense"
                    and event.get("fallback") == "rule_bm25"
                    for event in trace.fallback_events
                ),
                "obs05_dense_failure_rule_bm25_rrf_trace",
            )
            _ensure(
                bool(recommendation.outfits)
                and all(
                    not independent_outfit_violations(
                        outfit.items, scene, garment_by_id
                    )
                    for outfit in recommendation.outfits
                ),
                "obs05_dense_failure_legal_core_recommendation",
            )
            return _assurance_pass(
                evidence,
                ["dense_forced_failure", "dense_rule_bm25_rrf_legal_recommendation"],
            )
        except BaseException as exc:
            return _assurance_fail(evidence, exc)
        finally:
            services.assets.close()

    async def catalog_leg() -> dict[str, Any]:
        services = AppServices(
            Settings(
                root_dir=root,
                cpa_text_enabled=False,
                cpa_api_key=None,
                dense_enabled=False,
                catalog_enabled=True,
                catalog_force_failure=True,
                vision_force_failure=True,
            )
        )
        evidence = "Catalog forced failure on confirmed low-urgency gap -> wardrobe-only"
        actual_invocations = 0
        original_search = services.catalog.search

        def counted_search(scene: Any):
            nonlocal actual_invocations
            actual_invocations += 1
            return original_search(scene)

        services.catalog.search = counted_search  # type: ignore[method-assign]
        try:
            scene = await services.scene_parser.parse(
                SceneParseInput(
                    user_id="u01",
                    query_text="两周后通勤缺一双鞋，可以补购。",
                    event_horizon="planned",
                    intent="fill_gap",
                    occasion="commute",
                    goals=["comfortable"],
                )
            )
            _ensure(
                scene.urgency == "low"
                and scene.shopping_allowed
                and "shoes" in scene.constraints.gap_slots,
                "obs05_catalog_confirmed_low_gap_scene",
            )
            recommendation = services.recommendations.recommend(
                services.recommendations.resolve_payload(
                    {"request_id": scene.request_id}
                )
            )
            trace = services.traces.get(scene.trace_id)
            garment_by_id = {
                item.garment_id: item
                for item in services.repository.list_garments("u01")
            }
            _ensure(
                actual_invocations == 1
                and trace is not None
                and trace.catalog.get("attempted") is True
                and trace.catalog.get("call_count") == 0
                and trace.catalog.get("blocked_reason") == "PROVIDER_UNAVAILABLE"
                and any(
                    event.get("component") == "catalog"
                    and event.get("fallback") == "wardrobe_only"
                    for event in trace.fallback_events
                ),
                "obs05_catalog_actual_failure_controlled_trace",
            )
            _ensure(
                not recommendation.shopping_suggestions
                and (
                    bool(recommendation.outfits)
                    or bool(recommendation.gap_explanation)
                )
                and all(
                    not independent_outfit_violations(
                        outfit.items, scene, garment_by_id
                    )
                    for outfit in recommendation.outfits
                ),
                "obs05_catalog_failure_wardrobe_or_safe_gap_continues",
            )
            return _assurance_pass(
                evidence,
                [
                    "catalog_forced_failure_actual_invocation",
                    "catalog_no_products_wardrobe_or_safe_gap_continues",
                ],
            )
        except BaseException as exc:
            return _assurance_fail(evidence, exc)
        finally:
            services.assets.close()

    async def vision_leg() -> dict[str, Any]:
        services = AppServices(
            Settings(
                root_dir=root,
                cpa_text_enabled=False,
                cpa_api_key=None,
                dense_enabled=False,
                catalog_enabled=True,
                vision_force_failure=True,
            )
        )
        evidence = "Vision forced failure with usable owned image -> qualitative all-null"
        try:
            scene = await services.scene_parser.parse(
                SceneParseInput(
                    user_id="u01",
                    query_text="下周通勤，请用现有衣橱给一套可靠搭配。",
                    event_horizon="soon",
                    intent="recommend",
                    occasion="commute",
                    goals=["reliable"],
                )
            )
            recommendation = services.recommendations.recommend(
                services.recommendations.resolve_payload(
                    {"request_id": scene.request_id}
                )
            )
            _ensure(bool(recommendation.outfits), "obs05_vision_seed_outfit")
            asset = services.assets.upload(
                user_id="u01",
                styling_session_id=scene.styling_session_id,
                angle="front",
                media_type="image/png",
                content=_assurance_png(259, 256, informative=True),
                consent_obtained=True,
                purpose="styling_assessment",
            )
            look = services.looks.create(
                SelectedOutfitLookInput(
                    user_id="u01",
                    styling_session_id=scene.styling_session_id,
                    created_from="selected_outfit",
                    request_id=scene.request_id,
                    outfit_id=recommendation.outfits[0].outfit_id,
                    asset_ids=(asset.asset_id,),
                )
            )
            card = await services.looks.score(
                ScorecardInput(
                    user_id="u01",
                    styling_session_id=scene.styling_session_id,
                    look_version_id=look.look_version_id,
                    asset_ids=look.asset_ids,
                )
            )
            trace = services.traces.get(card.trace_id)
            _ensure(
                card.evidence_status == "vision_degraded"
                and not card.numeric_score_available
                and card.total_score is None
                and all(
                    value["score"] is None
                    for value in card.dimensions.model_dump().values()
                )
                and not card.priority_adjustments,
                "obs05_vision_failure_qualitative_all_null_no_adjustment",
            )
            _ensure(
                trace is not None
                and any(
                    event.get("component") == "vision"
                    and event.get("fallback") == "qualitative_scorecard"
                    for event in trace.fallback_events
                ),
                "obs05_vision_failure_trace",
            )
            return _assurance_pass(
                evidence,
                ["vision_forced_failure", "vision_qualitative_all_null_no_adjustment"],
            )
        except BaseException as exc:
            return _assurance_fail(evidence, exc)
        finally:
            services.assets.close()

    legs = {
        "llm": await llm_leg(),
        "dense": await dense_leg(),
        "catalog": await catalog_leg(),
        "vision": await vision_leg(),
    }
    result = _combine_assurances(
        "deterministic LLM/Dense/Catalog/Vision four-leg degradation matrix",
        *legs.values(),
    )
    result["legs"] = {
        name: {"status": leg["status"], "failed_steps": leg["failed_steps"]}
        for name, leg in legs.items()
    }
    return result


async def run_evaluation(
    root_dir: Path | None = None, *, write_reports: bool = True
) -> dict[str, Any]:
    """Run the frozen 30-case R1 evaluation without any external CPA call."""

    root = _root_dir(root_dir)
    cases, eval_sha256 = _load_cases(root)
    if len(cases) != 30:
        raise RuntimeError(f"expected 30 frozen eval cases, found {len(cases)}")

    # Imported lazily so app.py can expose /eval/run without an import cycle.
    from .app import AppServices

    settings = Settings(
        root_dir=root,
        cpa_text_enabled=False,
        cpa_api_key=None,
        dense_enabled=False,
        dense_force_failure=False,
        catalog_enabled=True,
        catalog_force_failure=False,
        vision_force_failure=True,
    )
    services = AppServices(settings)
    catalog_invocations_by_request: dict[str, int] = {}
    catalog_returned_ids_by_request: dict[str, set[str]] = {}
    original_catalog_search = services.catalog.search

    def counted_catalog_search(scene: Any):
        catalog_invocations_by_request[scene.request_id] = (
            catalog_invocations_by_request.get(scene.request_id, 0) + 1
        )
        result = original_catalog_search(scene)
        catalog_returned_ids_by_request[scene.request_id] = {
            item.item_id for item in result.items
        }
        return result

    services.catalog.search = counted_catalog_search  # type: ignore[method-assign]

    urgency_correct = 0
    high_gate_correct = 0
    high_case_count = 0
    high_catalog_trace_calls = 0
    high_catalog_invocations = 0
    hallucinations = 0
    item_references = 0
    hard_violations = 0
    hard_checks = 0
    complete_outfits = 0
    outfit_count = 0
    alternative_references = 0
    invalid_alternative_references = 0
    catalog_references = 0
    catalog_hard_violations = 0
    catalog_expectation_violations = 0
    legacy_witness_entry_count = 0
    legacy_witness_overlap_count = 0
    explicit_gap_legacy_witness_entry_count = 0
    explicit_gap_legacy_witness_overlap_count = 0
    reported_validation_disagreements = 0
    honest_gap_case_count = 0
    availability_cases_checked = 0

    urgency_failures: set[str] = set()
    gate_failures: set[str] = set()
    catalog_failures: set[str] = set()
    hallucination_failures: set[str] = set()
    hard_failures: set[str] = set()
    slot_failures: set[str] = set()
    validation_disagreement_failures: set[str] = set()
    direction_contract_failures: set[str] = set()
    supportive_message_failures: set[str] = set()
    availability_filter_failures: set[str] = set()
    catalog_grounding_failures: set[str] = set()
    catalog_constraint_failures: set[str] = set()
    catalog_expectation_failures: set[str] = set()
    catalog_call_required_case_ids: set[str] = set()
    catalog_no_gap_zero_call_failures: set[str] = set()
    catalog_inventory_presence_failures: set[str] = set()
    catalog_inventory_trace_coverage_failures: set[str] = set()
    catalog_inventory_return_failures: set[str] = set()
    case_results: list[dict[str, Any]] = []
    catalog_by_id = {
        item.item_id: item for item in services.repository.list_catalog()
    }

    try:
        for row in cases:
            case_id = str(row["case_id"])
            scene = await services.scene_parser.parse(
                SceneParseInput(
                    user_id=str(row["user_id"]),
                    query_text=str(row["query"]),
                    event_horizon=str(row["horizon"]),  # type: ignore[arg-type]
                    intent=str(row["intent"]),  # type: ignore[arg-type]
                )
            )
            catalog_invocations_before = catalog_invocations_by_request.get(
                scene.request_id, 0
            )
            recommendation = services.recommendations.recommend(
                services.recommendations.resolve_payload(
                    {"request_id": scene.request_id}
                )
            )
            catalog_invocation_delta = (
                catalog_invocations_by_request.get(scene.request_id, 0)
                - catalog_invocations_before
            )
            trace = services.traces.get(recommendation.trace_id)
            catalog_trace = trace.catalog if trace else {}
            catalog_calls = int(catalog_trace.get("call_count", 0) or 0)

            urgency_ok = scene.urgency == row["urgency"]
            urgency_correct += int(urgency_ok)
            if not urgency_ok:
                urgency_failures.add(case_id)

            gate_ok: bool | None = None
            if row["urgency"] == "high":
                high_case_count += 1
                high_catalog_trace_calls += catalog_calls
                high_catalog_invocations += catalog_invocation_delta
                gate_ok = bool(
                    scene.shopping_allowed is False
                    and scene.ui_capabilities.shopping_cta is False
                    and recommendation.ui_capabilities.shopping_cta is False
                    and not recommendation.shopping_suggestions
                    and catalog_calls == 0
                    and catalog_invocation_delta == 0
                    and catalog_trace.get("attempted") is False
                )
                high_gate_correct += int(gate_ok)
                if not gate_ok:
                    gate_failures.add(case_id)
                if (
                    catalog_calls != 0
                    or catalog_invocation_delta != 0
                    or catalog_trace.get("attempted") is not False
                ):
                    catalog_failures.add(case_id)
                if not recommendation.assistant_message.startswith(
                    _HIGH_SUPPORTIVE_PREFIXES
                ):
                    supportive_message_failures.add(case_id)

            garment_by_id = {
                item.garment_id: item
                for user in services.repository.list_users()
                for item in services.repository.list_garments(user.user_id)
            }
            if case_id in _AVAILABILITY_EXPECTATIONS:
                availability_cases_checked += 1
                expected_filters = _AVAILABILITY_EXPECTATIONS[case_id]
                actual_filters = (
                    {
                        str(entry.get("item_id")): str(entry.get("reason"))
                        for entry in trace.filters
                    }
                    if trace is not None
                    else {}
                )
                ranking_ids = (
                    {
                        str(entry.get("item_id"))
                        for key in ("rule", "bm25", "dense", "rrf")
                        for entry in trace.retrieval.get(key, [])
                    }
                    if trace is not None
                    else set()
                )
                fixture_reasons_match = all(
                    (
                        garment_by_id.get(item_id) is not None
                        and f"STATUS_{garment_by_id[item_id].status.upper()}"
                        == expected_reason
                    )
                    for item_id, expected_reason in expected_filters.items()
                )
                if (
                    not fixture_reasons_match
                    or any(
                        actual_filters.get(item_id) != expected_reason
                        for item_id, expected_reason in expected_filters.items()
                    )
                    or bool(set(expected_filters) & ranking_ids)
                ):
                    availability_filter_failures.add(case_id)
            forbidden = set(row.get("forbidden_garments", []))
            bad_ids = set(row.get("bad_ids", []))
            per_case_grounded = True
            per_case_hard = True
            per_case_complete = True
            per_case_validation_matches = True

            outfit_signatures: set[frozenset[str]] = set()
            labels: set[str] = set()
            for outfit in recommendation.outfits:
                outfit_count += 1
                signature = frozenset(outfit.items)
                outfit_signatures.add(signature)
                labels.add(outfit.strategy_label)
                item_references += len(outfit.items)
                bad_grounding = [
                    item_id
                    for item_id in outfit.items
                    if (
                        item_id in bad_ids
                        or item_id not in garment_by_id
                        or garment_by_id[item_id].user_id != scene.user_id
                    )
                ]
                hallucinations += len(bad_grounding)
                if bad_grounding:
                    hallucination_failures.add(case_id)
                    per_case_grounded = False

                hard_checks += 1
                outfit_hard_reasons = independent_outfit_violations(
                    outfit.items, scene, garment_by_id
                )
                outfit_hard_reasons.extend(
                    f"EVAL_FORBIDDEN_ITEM:{item_id}"
                    for item_id in sorted(set(outfit.items) & forbidden)
                )
                hard_ok = not outfit_hard_reasons
                if not hard_ok:
                    hard_violations += 1
                    hard_failures.add(case_id)
                    per_case_hard = False

                actual_items = [
                    garment_by_id[item_id]
                    for item_id in outfit.items
                    if item_id in garment_by_id
                    and garment_by_id[item_id].user_id == scene.user_id
                ]
                slot_ok = independent_slots_complete(
                    actual_items, list(scene.constraints.required_slots)
                )
                complete_outfits += int(slot_ok)
                if not slot_ok:
                    slot_failures.add(case_id)
                    per_case_complete = False

                reported = outfit.validation
                if (
                    bool(reported.all_ids_grounded) != (not bad_grounding)
                    or bool(reported.hard_constraints_passed) != hard_ok
                    or bool(reported.required_slots_complete) != slot_ok
                ):
                    reported_validation_disagreements += 1
                    validation_disagreement_failures.add(case_id)
                    hard_failures.add(case_id)
                    slot_failures.add(case_id)
                    per_case_validation_matches = False

                for replaced_id, alternatives in outfit.alternatives.items():
                    for item_id in alternatives:
                        alternative_references += 1
                        item_references += 1
                        item = garment_by_id.get(item_id)
                        replaced = garment_by_id.get(replaced_id)
                        alternative_grounded = bool(
                            item is not None
                            and item.user_id == scene.user_id
                            and item_id not in bad_ids
                        )
                        replacement_ids = [
                            item_id if current_id == replaced_id else current_id
                            for current_id in outfit.items
                        ]
                        alternative_reasons = independent_outfit_violations(
                            replacement_ids, scene, garment_by_id
                        )
                        if replaced_id not in outfit.items or replaced is None:
                            alternative_reasons.append("ALTERNATIVE_SOURCE_NOT_IN_OUTFIT")
                        elif item is None or item.slot != replaced.slot:
                            alternative_reasons.append("ALTERNATIVE_SLOT_MISMATCH")
                        if item_id in forbidden:
                            alternative_reasons.append("EVAL_FORBIDDEN_ITEM")
                        alternative_ok = alternative_grounded and not alternative_reasons
                        if not alternative_ok:
                            invalid_alternative_references += 1
                            if not alternative_grounded:
                                hallucinations += 1
                                hallucination_failures.add(case_id)
                            hard_failures.add(case_id)
                            per_case_grounded = (
                                per_case_grounded and alternative_grounded
                            )
                            per_case_hard = False

            suggestion_ids = {
                suggestion.item_id for suggestion in recommendation.shopping_suggestions
            }
            returned_catalog_ids = catalog_returned_ids_by_request.get(
                scene.request_id, set()
            )
            legacy_generator_candidate_witnesses = set(
                row.get("allowed_catalog", [])
            )
            forbidden_catalog = set(row.get("forbidden_catalog", []))
            catalog_allowed = bool(row.get("allow_catalog"))
            catalog_call_required = bool(
                catalog_allowed
                and independent_query_catalog_call_required(str(row["query"]))
            )
            if catalog_call_required:
                catalog_call_required_case_ids.add(case_id)
            (
                independent_eligible_catalog_ids,
                independent_ineligible_reasons_by_id,
                independent_inventory_context,
            ) = independent_catalog_inventory_oracle(
                list(catalog_by_id.values()),
                str(row["query"]),
                services.repository.get_user(scene.user_id),
            )
            if not catalog_call_required:
                independent_eligible_catalog_ids = set()
                independent_ineligible_reasons_by_id = {}
            trace_filtered_reasons_by_id: dict[str, set[str]] = {}
            for entry in catalog_trace.get("filtered", []):
                item_id = str(entry.get("item_id", ""))
                reason = str(entry.get("reason", ""))
                if item_id and reason:
                    trace_filtered_reasons_by_id.setdefault(item_id, set()).add(reason)
            inventory_trace_failure_ids = sorted(
                item_id
                for item_id, expected_reasons in independent_ineligible_reasons_by_id.items()
                if not (
                    trace_filtered_reasons_by_id.get(item_id, set())
                    & set(expected_reasons)
                )
            )
            catalog_case_grounded = True
            catalog_case_hard = True
            for suggestion in recommendation.shopping_suggestions:
                catalog_references += 1
                item_references += 1
                item = catalog_by_id.get(suggestion.item_id)
                catalog_grounded = bool(
                    item is not None
                    and suggestion.item_id in returned_catalog_ids
                    and suggestion.item_id not in bad_ids
                )
                if not catalog_grounded:
                    hallucinations += 1
                    hallucination_failures.add(case_id)
                    catalog_grounding_failures.add(case_id)
                    per_case_grounded = False
                    catalog_case_grounded = False

                catalog_reasons: list[str] = []
                if item is not None:
                    catalog_reasons.extend(
                        independent_ineligible_reasons_by_id.get(
                            suggestion.item_id, []
                        )
                    )
                    if (
                        suggestion.title != item.title
                        or suggestion.declaration != item.declaration
                    ):
                        catalog_reasons.append("TOOL_PAYLOAD_MISMATCH")
                if suggestion.item_id in forbidden_catalog:
                    catalog_reasons.append("EVAL_FORBIDDEN_CATALOG_ITEM")
                if catalog_reasons:
                    catalog_hard_violations += 1
                    hard_failures.add(case_id)
                    catalog_constraint_failures.add(case_id)
                    per_case_hard = False
                    catalog_case_hard = False

            legacy_generator_candidate_overlap = (
                suggestion_ids & legacy_generator_candidate_witnesses
            )
            legacy_generator_candidate_overlap_ratio = _ratio(
                len(legacy_generator_candidate_overlap),
                len(legacy_generator_candidate_witnesses),
            )
            legacy_witness_entry_count += len(
                legacy_generator_candidate_witnesses
            )
            legacy_witness_overlap_count += len(
                legacy_generator_candidate_overlap
            )
            if catalog_call_required:
                explicit_gap_legacy_witness_entry_count += len(
                    legacy_generator_candidate_witnesses
                )
                explicit_gap_legacy_witness_overlap_count += len(
                    legacy_generator_candidate_overlap
                )
            catalog_explicit_gap_actual_and_trace_call = bool(
                not catalog_call_required
                or (
                    catalog_invocation_delta > 0
                    and catalog_calls > 0
                    and catalog_trace.get("attempted") is True
                )
            )
            catalog_eligible_inventory_presence_contract = bool(
                not catalog_call_required
                or (
                    (
                        bool(independent_eligible_catalog_ids)
                        and bool(suggestion_ids)
                        and suggestion_ids.issubset(
                            independent_eligible_catalog_ids
                        )
                    )
                    or (
                        not independent_eligible_catalog_ids
                        and not suggestion_ids
                    )
                )
            )
            catalog_actual_return_eligible = bool(
                not catalog_call_required
                or returned_catalog_ids.issubset(
                    independent_eligible_catalog_ids
                )
            )
            catalog_inventory_trace_coverage = bool(
                not catalog_call_required or not inventory_trace_failure_ids
            )
            if not catalog_eligible_inventory_presence_contract:
                catalog_inventory_presence_failures.add(case_id)
            if not catalog_actual_return_eligible:
                catalog_inventory_return_failures.add(case_id)
            if not catalog_inventory_trace_coverage:
                catalog_inventory_trace_coverage_failures.add(case_id)
            catalog_no_gap_call_zero = bool(
                catalog_call_required
                or (
                    catalog_invocation_delta == 0
                    and catalog_calls == 0
                    and catalog_trace.get("attempted") is False
                    and not suggestion_ids
                )
            )
            if not catalog_no_gap_call_zero:
                catalog_no_gap_zero_call_failures.add(case_id)
            catalog_expectation_ok = bool(
                catalog_explicit_gap_actual_and_trace_call
                and catalog_eligible_inventory_presence_contract
                and catalog_actual_return_eligible
                and catalog_inventory_trace_coverage
                and catalog_no_gap_call_zero
            )
            if not catalog_expectation_ok:
                catalog_expectation_violations += 1
                catalog_expectation_failures.add(case_id)

            has_honest_gap = bool(
                len(recommendation.outfits) == 3
                or recommendation.gap_explanation
            )
            if len(recommendation.outfits) < 3 and recommendation.gap_explanation:
                honest_gap_case_count += 1
            direction_ok = bool(
                len(recommendation.outfits) <= 3
                and has_honest_gap
                and (
                    (
                        len(outfit_signatures) == len(recommendation.outfits)
                        and len(labels) == len(recommendation.outfits)
                        and sum(item.is_primary for item in recommendation.outfits) == 1
                        and all(
                            item.reasons
                            and item.risks
                            and isinstance(item.alternatives, dict)
                            for item in recommendation.outfits
                        )
                    )
                    if recommendation.outfits
                    else bool(recommendation.gap_explanation)
                )
            )
            if not direction_ok:
                direction_contract_failures.add(case_id)

            case_results.append(
                {
                    "case_id": case_id,
                    "bucket": row["bucket"],
                    "input_mode": "structured_scene_fields",
                    "expected_horizon": row["horizon"],
                    "actual_horizon": scene.event_horizon,
                    "expected_intent": row["intent"],
                    "actual_intent": scene.intent,
                    "expected_urgency": row["urgency"],
                    "actual_urgency": scene.urgency,
                    "shopping_allowed": scene.shopping_allowed,
                    "catalog_calls": catalog_calls,
                    "catalog_invocations": catalog_invocation_delta,
                    "catalog_reference_count": len(
                        recommendation.shopping_suggestions
                    ),
                    "outfit_count": len(recommendation.outfits),
                    "checks": {
                        "urgency": urgency_ok,
                        "high_urgency_gate": gate_ok,
                        "all_ids_grounded": per_case_grounded,
                        "hard_constraints": per_case_hard,
                        "slot_complete": per_case_complete,
                        "reported_validation_matches_oracle": (
                            per_case_validation_matches
                        ),
                        "direction_contract": direction_ok,
                        "catalog_ids_grounded_in_fixture_and_tool_return": (
                            catalog_case_grounded
                        ),
                        "catalog_constraints_and_eval_truth": catalog_case_hard,
                        "catalog_call_required_by_explicit_gap": (
                            catalog_call_required
                        ),
                        "catalog_explicit_gap_actual_and_trace_call": (
                            catalog_explicit_gap_actual_and_trace_call
                        ),
                        "catalog_eligible_inventory_presence_contract": (
                            catalog_eligible_inventory_presence_contract
                        ),
                        "catalog_actual_return_subset_independent_eligible": (
                            catalog_actual_return_eligible
                        ),
                        "catalog_inventory_trace_covers_ineligible": (
                            catalog_inventory_trace_coverage
                        ),
                        "catalog_no_gap_actual_and_trace_zero": (
                            catalog_no_gap_call_zero
                        ),
                        "catalog_legacy_generator_witness_non_gating": True,
                    },
                    "catalog_inventory_oracle": {
                        "oracle": "independent_fixture_query_inventory_v1",
                        "context": independent_inventory_context,
                        "eligible_ids": sorted(independent_eligible_catalog_ids),
                        "expected_ineligible_reasons_by_id": (
                            independent_ineligible_reasons_by_id
                            if catalog_call_required
                            else {}
                        ),
                        "trace_filtered_reasons_by_id": (
                            {
                                item_id: sorted(reasons)
                                for item_id, reasons in sorted(
                                    trace_filtered_reasons_by_id.items()
                                )
                            }
                            if catalog_call_required
                            else {}
                        ),
                        "trace_missing_or_reason_mismatch_ids": (
                            inventory_trace_failure_ids
                            if catalog_call_required
                            else []
                        ),
                    },
                    "catalog_legacy_generator_candidate_witness": {
                        "source_field": "allowed_catalog",
                        "semantics": "legacy_generator_candidate_witness",
                        "authoritative": False,
                        "exhaustive": False,
                        "witness_ids": sorted(
                            legacy_generator_candidate_witnesses
                        ),
                        "overlap_ids": sorted(
                            legacy_generator_candidate_overlap
                        ),
                        "overlap_count": len(
                            legacy_generator_candidate_overlap
                        ),
                        "has_overlap": bool(
                            legacy_generator_candidate_overlap
                        ),
                        "overlap_ratio": (
                            legacy_generator_candidate_overlap_ratio
                        ),
                        "release_gating": False,
                    },
                }
            )
    finally:
        services.assets.close()

    urgency_value = _ratio(urgency_correct, len(cases))
    gate_value = _ratio(high_gate_correct, high_case_count)
    slot_value = _ratio(complete_outfits, outfit_count)
    violation_breakdown = independent_violation_breakdown(
        outfit_hard_violations=hard_violations,
        invalid_alternative_references=invalid_alternative_references,
        catalog_product_constraint_violations=catalog_hard_violations,
        reported_validation_disagreements=reported_validation_disagreements,
        catalog_expected_presence_violations=catalog_expectation_violations,
    )
    observed_hard_constraint_violations = violation_breakdown[
        "observed_hard_constraint_violations"
    ]
    metrics = {
        "urgency_accuracy": _metric(
            numerator=urgency_correct,
            denominator=len(cases),
            value=urgency_value,
            threshold_key="urgency_accuracy",
            passed=urgency_value >= 0.95,
            failed_case_ids=urgency_failures,
            unit="ratio",
        ),
        "high_urgency_shopping_gate_accuracy": _metric(
            numerator=high_gate_correct,
            denominator=high_case_count,
            value=gate_value,
            threshold_key="high_urgency_shopping_gate_accuracy",
            passed=high_case_count > 0 and gate_value == 1.0,
            failed_case_ids=gate_failures,
            unit="ratio",
        ),
        "high_urgency_catalog_calls": _metric(
            numerator=high_catalog_invocations,
            denominator=high_case_count,
            value=high_catalog_invocations,
            threshold_key="high_urgency_catalog_calls",
            passed=(
                high_catalog_invocations == 0
                and high_catalog_trace_calls == 0
                and not catalog_failures
            ),
            failed_case_ids=catalog_failures,
            unit="invocations",
        ),
        "item_hallucinations": _metric(
            numerator=hallucinations,
            denominator=item_references,
            value=hallucinations,
            threshold_key="item_hallucinations",
            passed=hallucinations == 0,
            failed_case_ids=hallucination_failures,
            unit="violations",
        ),
        "hard_constraint_violations": _metric(
            numerator=observed_hard_constraint_violations,
            denominator=(
                hard_checks
                + alternative_references
                + catalog_references
                + outfit_count
            ),
            value=observed_hard_constraint_violations,
            threshold_key="hard_constraint_violations",
            passed=observed_hard_constraint_violations == 0,
            failed_case_ids=hard_failures,
            unit="violations",
        ),
        "slot_completeness": _metric(
            numerator=complete_outfits,
            denominator=outfit_count,
            value=slot_value,
            threshold_key="slot_completeness",
            passed=(
                outfit_count > 0
                and slot_value >= 0.95
                and reported_validation_disagreements == 0
            ),
            failed_case_ids=slot_failures,
            unit="ratio",
        ),
    }

    metrics["hard_constraint_violations"].update(
        {
            "definition": "observed_outfit_alternative_catalog_product_and_validation_constraint_violations",
            "excluded_release_gates": ["catalog_expected_presence"],
        }
    )
    metric_status = {name: value["status"] for name, value in metrics.items()}
    direction_ok_all = (
        not direction_contract_failures and honest_gap_case_count > 0
    )
    direction_status = "PASS" if direction_ok_all else "FAIL"
    supportive_ok = not supportive_message_failures and high_case_count > 0
    availability_owned_hard_failures = hard_failures & set(
        _AVAILABILITY_EXPECTATIONS
    )
    availability_ok = (
        availability_cases_checked == len(_AVAILABILITY_EXPECTATIONS)
        and not availability_filter_failures
        and not availability_owned_hard_failures
    )
    availability_failed_steps: list[str] = []
    if availability_cases_checked != len(_AVAILABILITY_EXPECTATIONS):
        availability_failed_steps.append(
            "availability_case_count:"
            f"{availability_cases_checked}/{len(_AVAILABILITY_EXPECTATIONS)}"
        )
    if availability_filter_failures:
        availability_failed_steps.append(
            "availability_filter_failed_case_ids:"
            + ",".join(sorted(availability_filter_failures))
        )
    if availability_owned_hard_failures:
        availability_failed_steps.append(
            "availability_output_hard_failed_case_ids:"
            + ",".join(sorted(availability_owned_hard_failures))
        )
    validation_alignment_ok = reported_validation_disagreements == 0
    expected_catalog_call_required_case_ids = {
        "e009",
        "e010",
        "e011",
        "e012",
        "e013",
    }
    catalog_call_mapping_ok = (
        catalog_call_required_case_ids
        == expected_catalog_call_required_case_ids
    )
    all_eval_case_ids = {str(row["case_id"]) for row in cases}
    catalog_non_call_required_case_ids = (
        all_eval_case_ids - catalog_call_required_case_ids
    )

    # Every mapped AC and supplemental P0 is a real, deterministic execution.
    # NOT_RUN or a failed leg remains release-blocking via overall_pass below.
    assurance_checks: dict[str, dict[str, Any]] = {}
    assurance_checks["QUERY-ONLY-SCENE"] = await _assure_query_only_scene_parser(root)
    assurance_checks["AUDIT-NO-DEADLINE"] = await _assure_audit_no_deadline_gate(root)
    assurance_checks["AC-03"] = await _assure_ac03(root)
    assurance_checks["AC-04"] = await _assure_ac04(root)
    assurance_checks["AC-05"] = _observed_assurance(
        availability_ok,
        "e019/e020/e021 owned exact STATUS_* recall filters, ranker absence and independent output hard oracle",
        [
            "availability_fixture_status_matches",
            "status_reason_present_in_trace_filters",
            "status_items_absent_from_rule_bm25_dense_rrf",
            "independent_hard_oracle_zero",
        ],
        availability_failed_steps,
    )
    assurance_checks["AC-06"] = _combine_assurances(
        "30-case direction contract AND deterministic one-combo sparse interview wardrobe",
        _observed_assurance(
            direction_ok_all,
            "30-case unordered item-set direction contract with primary/reason/risk/alternatives and honest gap",
            [
                "at_most_three_directions",
                "unordered_item_sets_distinct",
                "exactly_one_primary_when_nonempty",
                "reason_risk_alternatives_present",
                "less_than_three_has_explicit_gap",
                "honest_gap_case_observed",
            ],
            [
                "direction_contract_failure:"
                + ",".join(sorted(direction_contract_failures))
            ],
        ),
        await _assure_ac06_sparse_interview(root),
    )
    assurance_checks["AC-07"] = _combine_assurances(
        "feedback propose-confirm-apply-delete lifecycle AND causal structured-similar shoe rank demotion",
        await _assure_ac07(root),
        await _assure_ac07_rank_causality(root),
    )
    assurance_checks["AC-10"] = _combine_assurances(
        "normal garment/Mock-product grounding oracle AND adversarial unknown/cross-owner garment rejection",
        _observed_assurance(
            metric_status["item_hallucinations"] == "PASS",
            "all outfit/alternative IDs checked against owning wardrobe; all product IDs checked against fixture and actual Catalog return",
            ["normal_garment_and_catalog_output_hallucinations_zero"],
            ["normal_output_hallucination_detected"],
        ),
        await _assure_ac10(root),
    )
    assurance_checks["AC-11"] = _combine_assurances(
        "30-case upper gate with actual search invocation spy AND forced secondary Catalog guard",
        _observed_assurance(
            metric_status["high_urgency_shopping_gate_accuracy"] == "PASS"
            and metric_status["high_urgency_catalog_calls"] == "PASS",
            "high urgency server/UI gate, trace cross-check and CatalogService.search invocation delta",
            [
                "upper_shopping_false_ui_false",
                "upper_trace_attempted_false_call_zero",
                "upper_actual_catalog_invocations_zero",
            ],
            ["high_urgency_upper_gate_or_actual_invocation_failure"],
        ),
        await _assure_ac11(root),
    )

    vision_assurances = await _assure_ac13_ac14(root)
    numeric_text_assurance = vision_assurances.pop("_AC16_NUMERIC_TEXT")
    vision_matrix_assurance = vision_assurances.pop("AC-13")
    assurance_checks.update(vision_assurances)
    assurance_checks["AC-13"] = _combine_assurances(
        "strict Vision fail-closed matrix AND exact interview v1-adjust-v2-rescore-Final chain",
        vision_matrix_assurance,
        await _assure_ac13_full_chain(root),
    )
    score_assurances = await _assure_ac16_ac17(root)
    qualitative_text_assurance = score_assurances.pop("AC-16")
    qualitative_final_assurance = score_assurances.pop("AC-17")
    assurance_checks["AC-16"] = _combine_assurances(
        "independent closed-term scans of qualitative and MockTransport numeric user-visible scorecard text",
        qualitative_text_assurance,
        numeric_text_assurance,
    )
    assurance_checks["AC-17"] = _combine_assurances(
        "qualitative/no-score satisfaction=1 Final AND actual exact-78 numeric Final with post-final locks",
        qualitative_final_assurance,
        await _assure_ac17_numeric_78(root),
    )
    assurance_checks["SAFE-RAIN"] = await _assure_rain_contract(root)
    assurance_checks["SAFE-CPA-PRIVACY"] = await _assure_provider_privacy(root)
    assurance_checks["OBS-05"] = await _assure_obs05_fault_matrix(root)
    assurance_checks["RESPONSE-VALIDATION-ALIGNMENT"] = _observed_assurance(
        validation_alignment_ok,
        "service OutfitValidation booleans compared with independent hard/slot/grounding oracle",
        ["all_reported_validation_flags_match_independent_oracle"],
        [
            f"reported_validation_disagreements:{reported_validation_disagreements}"
        ],
    )
    catalog_oracle_failed_steps: list[str] = []
    if catalog_grounding_failures:
        catalog_oracle_failed_steps.append(
            "catalog_grounding_failed_case_ids:"
            + ",".join(sorted(catalog_grounding_failures))
        )
    if catalog_constraint_failures:
        catalog_oracle_failed_steps.append(
            "catalog_product_constraint_failed_case_ids:"
            + ",".join(sorted(catalog_constraint_failures))
        )
    if catalog_expectation_failures:
        catalog_oracle_failed_steps.append(
            "catalog_expected_presence_failed_case_ids:"
            + ",".join(sorted(catalog_expectation_failures))
        )
    if catalog_inventory_presence_failures:
        catalog_oracle_failed_steps.append(
            "catalog_inventory_presence_failed_case_ids:"
            + ",".join(sorted(catalog_inventory_presence_failures))
        )
    if catalog_inventory_return_failures:
        catalog_oracle_failed_steps.append(
            "catalog_actual_return_not_eligible_case_ids:"
            + ",".join(sorted(catalog_inventory_return_failures))
        )
    if catalog_inventory_trace_coverage_failures:
        catalog_oracle_failed_steps.append(
            "catalog_inventory_trace_coverage_failed_case_ids:"
            + ",".join(sorted(catalog_inventory_trace_coverage_failures))
        )
    if catalog_no_gap_zero_call_failures:
        catalog_oracle_failed_steps.append(
            "catalog_nonmandatory_zero_call_failed_case_ids:"
            + ",".join(sorted(catalog_no_gap_zero_call_failures))
        )
    if not catalog_call_mapping_ok:
        catalog_oracle_failed_steps.append(
            "catalog_call_mapping_mismatch:actual="
            + ",".join(sorted(catalog_call_required_case_ids))
        )
    assurance_checks["CATALOG-ORACLE"] = _observed_assurance(
        not catalog_grounding_failures
        and not catalog_constraint_failures
        and not catalog_expectation_failures
        and not catalog_no_gap_zero_call_failures
        and not catalog_inventory_presence_failures
        and not catalog_inventory_return_failures
        and not catalog_inventory_trace_coverage_failures
        and catalog_call_mapping_ok,
        "query-derived explicit-gap call requirement plus independent full-inventory eligibility, actual/Trace invocation, filtered-reason coverage, fixture/actual-return grounding, forbidden truth and no-gap Catalog zero; allowed_catalog is a non-authoritative legacy generator witness",
        [
            "catalog_call_requirement_derived_from_query_text",
            "e009_e013_explicit_gap_requires_actual_and_trace_call",
            "independent_full_inventory_eligibility_oracle",
            "eligible_nonempty_requires_suggestions_subset_eligible",
            "eligible_empty_requires_safe_empty_suggestions",
            "catalog_trace_covers_every_ineligible_item_with_auditable_reason",
            "all_nonmandatory_cases_actual_and_trace_catalog_zero",
            "catalog_fixture_and_actual_return_grounding",
            "catalog_independent_constraints",
            "catalog_forbidden_eval_truth",
            "allowed_catalog_legacy_generator_candidate_witness_non_gating",
            "catalog_inventory_conditioned_presence_or_safe_absence",
        ],
        catalog_oracle_failed_steps,
    )
    ac01_ok = (
        metric_status["urgency_accuracy"] == "PASS"
        and metric_status["high_urgency_shopping_gate_accuracy"] == "PASS"
        and metric_status["high_urgency_catalog_calls"] == "PASS"
        and direction_ok_all
        and supportive_ok
    )
    assurance_checks["AC-01"] = _combine_assurances(
        "structured 30-case gate aggregate AND exact single mixed-emotion interview Given",
        _observed_assurance(
            ac01_ok,
            "structured horizon-to-urgency mapping AND fail-closed shopping/Catalog AND direction contract AND controlled supportive first line",
            [
                "structured_horizon_to_urgency_threshold",
                "high_shopping_false_catalog_zero",
                "direction_primary_reason_risk_alternatives",
                "controlled_supportive_first_line",
            ],
            [
                "ac01_aggregate_failure;supportive_case_ids:"
                + ",".join(sorted(supportive_message_failures))
            ],
        ),
        await _assure_ac01_exact(root),
    )
    ac_matrix = [
        {"ac": "AC-01", **assurance_checks["AC-01"]},
        {"ac": "AC-03", **assurance_checks["AC-03"]},
        {"ac": "AC-04", **assurance_checks["AC-04"]},
        {"ac": "AC-05", **assurance_checks["AC-05"]},
        {"ac": "AC-06", **assurance_checks["AC-06"]},
        {"ac": "AC-07", **assurance_checks["AC-07"]},
        {"ac": "AC-10", **assurance_checks["AC-10"]},
        {"ac": "AC-11", **assurance_checks["AC-11"]},
        {"ac": "AC-13", **assurance_checks["AC-13"]},
        {"ac": "AC-14", **assurance_checks["AC-14"]},
        {"ac": "AC-16", **assurance_checks["AC-16"]},
        {"ac": "AC-17", **assurance_checks["AC-17"]},
    ]
    overall_pass = (
        all(value["status"] == "PASS" for value in metrics.values())
        and not direction_contract_failures
        and all(value["status"] == "PASS" for value in assurance_checks.values())
    )
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "generated_at": generated_at,
        "status": "PASS" if overall_pass else "FAIL",
        "command": COMMAND,
        "versions": {
            "data": settings.data_version,
            "eval": EVAL_VERSION,
            "eval_sha256": eval_sha256,
            "rules": "scene_rules_v1|hard_filter_v1|response_validator_v1|stylist_score_v1",
            "ranker": "rule_bm25_rrf_v1",
            "provider_contract": "cpa_grok4.6_v1",
            "providers": {
                "llm": "cpa_text_grok4.6_v1|offline_rule_fallback",
                "dense": "hashed_dense_v1|disabled_fixture_baseline",
                "catalog": "fixtures_v1.0_mock",
                "vision": "garment_visibility_codes_v2|httpx_mock_only",
            },
            "model": {
                "logical": "grok4.6",
                "transport": "grok-4.6-high",
                "evaluation_mode": "offline_rule_fallback",
                "external_calls": 0,
            },
            "evaluation_oracle": "independent_garment_catalog_score_oracle_v2",
        },
        "evaluation_protocol": {
            "main_30_input_mode": "structured_scene_fields",
            "main_30_supplied_fields": ["user_id", "query_text", "event_horizon", "intent"],
            "main_30_purpose": "deterministic gate, hard-constraint, retrieval, catalog and output validation",
            "urgency_metric_definition": "horizon_to_urgency_mapping_accuracy",
            "urgency_metric_not_claimed_as": "query_only_nlp_accuracy",
            "query_only_assurance": "QUERY-ONLY-SCENE",
            "audit_no_deadline_assurance": "AUDIT-NO-DEADLINE",
        },
        "evaluation_inputs": {
            "main_30": {
                "mode": "structured_scene_fields",
                "supplied_fields": [
                    "user_id",
                    "query_text",
                    "event_horizon",
                    "intent",
                ],
                "metric_definition": "horizon_to_urgency_mapping_accuracy",
                "not_claimed": "query_nlp_accuracy",
            },
            "query_only": {
                "mode": "query_only",
                "supplied_fields": ["user_id", "query_text"],
                "case_ids": assurance_checks["QUERY-ONLY-SCENE"].get(
                    "case_ids", []
                ),
                "denominator": assurance_checks["QUERY-ONLY-SCENE"].get(
                    "denominator", 0
                ),
                "expectations": assurance_checks["QUERY-ONLY-SCENE"].get(
                    "expectations", {}
                ),
            },
            "audit_no_deadline": {
                "mode": "query_only",
                "supplied_fields": ["user_id", "query_text"],
                "case_ids": assurance_checks["AUDIT-NO-DEADLINE"].get(
                    "case_ids", []
                ),
                "denominator": assurance_checks["AUDIT-NO-DEADLINE"].get(
                    "denominator", 0
                ),
                "expected_horizon": "unknown",
                "expected_urgency": "high",
                "expected_catalog_invocations": 0,
            },
            "catalog_expectation": {
                "call_requirement_oracle": (
                    "independent_query_catalog_call_required_v1"
                ),
                "explicit_gap_call_required_case_ids": sorted(
                    catalog_call_required_case_ids
                ),
                "expected_explicit_gap_call_required_case_ids": sorted(
                    expected_catalog_call_required_case_ids
                ),
                "non_call_required_zero_call_case_ids": sorted(
                    catalog_non_call_required_case_ids
                ),
                "presence_rule": (
                    "allow_catalog=false => empty; query-local explicit slot gap => "
                    "actual/trace Catalog call; independently eligible inventory "
                    "nonempty => suggestions nonempty and subset eligible; eligible "
                    "inventory empty => suggestions empty with complete filtered Trace; "
                    "every non-call-required case => actual/trace Catalog zero and suggestions empty"
                ),
                "inventory_eligibility_oracle": {
                    "version": "independent_fixture_query_inventory_v1",
                    "inputs": [
                        "query_text",
                        "fixture_catalog",
                        "fixture_user_budget_and_avoid_colors",
                    ],
                    "criteria": [
                        "slot",
                        "stock",
                        "budget",
                        "occasion_with_interview_meeting_reciprocal_rule",
                        "formality",
                        "season",
                        "delivery_eta_plus_one_day_safety_buffer",
                        "rain",
                        "taboo_color",
                    ],
                    "uses_business_catalog_service": False,
                },
                "legacy_generator_candidate_witness_contract": {
                    "source_field": "allowed_catalog",
                    "semantics": "legacy_generator_candidate_witness",
                    "authoritative": False,
                    "exhaustive": False,
                    "release_gating": False,
                    "basis": [
                        "generate.py stores candidates[:2] after filtering",
                        "legacy generator candidates may fail current SHOP-04 inventory eligibility",
                    ],
                },
            },
        },
        "thresholds": THRESHOLDS,
        "metrics": metrics,
        "release_gates": {
            "catalog_expected_presence": {
                "status": (
                    "PASS" if not catalog_expectation_failures else "FAIL"
                ),
                "violation_count": violation_breakdown[
                    "catalog_expected_presence_violations"
                ],
                "failed_case_ids": sorted(catalog_expectation_failures),
                "included_in_hard_constraint_metric": False,
                "blocks_overall_via": "assurance_checks.CATALOG-ORACLE",
            }
        },
        "supplemental_counts": {
            "eval_cases": len(cases),
            "outfits": outfit_count,
            "item_references": item_references,
            "alternative_references": alternative_references,
            "catalog_references": catalog_references,
            "catalog_hard_violations": catalog_hard_violations,
            "catalog_expectation_violations": catalog_expectation_violations,
            "observed_product_constraint_violations": catalog_hard_violations,
            "catalog_expected_presence_violations": violation_breakdown[
                "catalog_expected_presence_violations"
            ],
            "catalog_grounding_failed_case_ids": sorted(
                catalog_grounding_failures
            ),
            "catalog_constraint_failed_case_ids": sorted(
                catalog_constraint_failures
            ),
            "catalog_expectation_failed_case_ids": sorted(
                catalog_expectation_failures
            ),
            "catalog_call_required_case_ids": sorted(
                catalog_call_required_case_ids
            ),
            "catalog_call_mapping_status": (
                "PASS" if catalog_call_mapping_ok else "FAIL"
            ),
            "catalog_inventory_presence_failed_case_ids": sorted(
                catalog_inventory_presence_failures
            ),
            "catalog_inventory_return_failed_case_ids": sorted(
                catalog_inventory_return_failures
            ),
            "catalog_inventory_trace_coverage_failed_case_ids": sorted(
                catalog_inventory_trace_coverage_failures
            ),
            "catalog_no_gap_zero_call_failed_case_ids": sorted(
                catalog_no_gap_zero_call_failures
            ),
            "high_urgency_catalog_invocations": high_catalog_invocations,
            "high_urgency_catalog_trace_calls": high_catalog_trace_calls,
            "reported_validation_disagreements": reported_validation_disagreements,
            "honest_gap_case_count": honest_gap_case_count,
            "availability_cases_checked": availability_cases_checked,
            "availability_filter_failed_case_ids": sorted(
                availability_filter_failures
            ),
            "supportive_message_failed_case_ids": sorted(
                supportive_message_failures
            ),
            "direction_contract_status": direction_status,
            "direction_contract_failed_case_ids": sorted(direction_contract_failures),
        },
        "catalog_legacy_witness_diagnostics": {
            "release_gating": False,
            "authoritative": False,
            "exhaustive": False,
            "all_cases": {
                "witness_entry_count": legacy_witness_entry_count,
                "overlap_count": legacy_witness_overlap_count,
                "overlap_ratio": _ratio(
                    legacy_witness_overlap_count, legacy_witness_entry_count
                ),
            },
            "explicit_gap_cases": {
                "witness_entry_count": explicit_gap_legacy_witness_entry_count,
                "overlap_count": explicit_gap_legacy_witness_overlap_count,
                "overlap_ratio": _ratio(
                    explicit_gap_legacy_witness_overlap_count,
                    explicit_gap_legacy_witness_entry_count,
                ),
            },
        },
        "assurance_checks": assurance_checks,
        "ac_matrix": ac_matrix,
        "cases": case_results,
        "report_paths": {
            "json": REPORT_JSON.as_posix(),
            "markdown": REPORT_MARKDOWN.as_posix(),
        },
        "privacy": {
            "raw_queries_included": False,
            "images_or_base64_included": False,
            "credentials_included": False,
        },
    }
    if write_reports:
        _write_reports(root, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the frozen ProfAgent R1 evaluation")
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="run the evaluation without writing the stable JSON/Markdown reports",
    )
    args = parser.parse_args(argv)
    report = asyncio.run(run_evaluation(write_reports=not args.no_write))
    for name, metric in report["metrics"].items():
        print(
            f"{name}: {metric['value']} {metric['unit']} "
            f"({metric['numerator']}/{metric['denominator']}) {metric['status']}"
        )
    print(f"report_json: {report['report_paths']['json']}")
    print(f"report_markdown: {report['report_paths']['markdown']}")
    print(f"overall: {report['status']}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
