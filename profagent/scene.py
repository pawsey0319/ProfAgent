from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timedelta
from threading import RLock
from typing import Any

from .config import Settings
from .memory_service import MemoryService, MemorySignal
from .models import (
    Horizon,
    Intent,
    Occasion,
    SceneConstraints,
    SceneParseInput,
    SceneRequest,
    UICapabilities,
)
from .providers import GrokLLMProvider, ProviderUnavailable
from .repository import FixtureRepository
from .tracing import TraceStore


COLOR_TERMS = {
    "黑": "black",
    "白": "white",
    "灰": "gray",
    "藏青": "navy",
    "海军蓝": "navy",
    "米色": "beige",
    "棕": "brown",
    "卡其": "khaki",
    "红": "red",
    "粉": "pink",
    "橙": "orange",
    "黄": "yellow",
    "绿": "green",
    "蓝": "blue",
    "紫": "purple",
}

VALID_GOALS = {
    "reliable",
    "modern",
    "comfortable",
    "polished",
    "low_key",
    "confident",
    "cool",
}


class SceneStateStore:
    def __init__(self):
        self._by_request: dict[str, SceneRequest] = {}
        self._by_session: dict[str, SceneRequest] = {}
        self._clarifications: dict[str, int] = {}
        self._lock = RLock()

    def save(self, scene: SceneRequest) -> None:
        with self._lock:
            existing_request = self._by_request.get(scene.request_id)
            if existing_request is not None and existing_request != scene:
                raise SceneStateConflict("request_id already belongs to another scene")
            existing_session = self._by_session.get(scene.styling_session_id)
            if existing_session is not None and existing_session.user_id != scene.user_id:
                raise SceneStateConflict("styling_session_id belongs to another user")
            self._by_request[scene.request_id] = scene.model_copy(deep=True)
            self._by_session[scene.styling_session_id] = scene.model_copy(deep=True)

    def replace_provider_backend(
        self, request_id: str, user_id: str, backend: str
    ) -> SceneRequest:
        """Update only the provider provenance after a deferred Dialogue call."""

        if backend not in {"grok4.6", "rule_fallback"}:
            raise SceneStateConflict("unsupported scene backend")
        with self._lock:
            current = self._by_request.get(request_id)
            if current is None or current.user_id != user_id:
                raise SceneStateConflict("scene not found for provider update")
            updated = current.model_copy(update={"backend": backend}, deep=True)
            self._by_request[request_id] = updated.model_copy(deep=True)
            self._by_session[updated.styling_session_id] = updated.model_copy(deep=True)
            return updated

    def by_request(self, request_id: str) -> SceneRequest | None:
        with self._lock:
            value = self._by_request.get(request_id)
            return value.model_copy(deep=True) if value else None

    def by_session(self, session_id: str) -> SceneRequest | None:
        with self._lock:
            value = self._by_session.get(session_id)
            return value.model_copy(deep=True) if value else None

    def consume_clarification(self, session_id: str) -> bool:
        with self._lock:
            count = self._clarifications.get(session_id, 0)
            if count >= 1:
                return False
            self._clarifications[session_id] = count + 1
            return True

    def active_sessions(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            records = [
                scene
                for scene in self._by_session.values()
                if scene.user_id == user_id
            ]
        return [
            {
                "styling_session_id": item.styling_session_id,
                "occasion": item.occasion,
                "event_horizon": item.event_horizon,
                "urgency": item.urgency,
            }
            for item in records
        ]

    def discard_session(self, session_id: str) -> None:
        """Expire Dialogue-owned Scene snapshots without exposing their IDs."""
        with self._lock:
            self._by_session.pop(session_id, None)
            expired_request_ids = [
                request_id
                for request_id, scene in self._by_request.items()
                if scene.styling_session_id == session_id
            ]
            for request_id in expired_request_ids:
                self._by_request.pop(request_id, None)
            self._clarifications.pop(session_id, None)


class SceneStateConflict(ValueError):
    pass


class SceneParser:
    _LOCAL_PROVIDER_SAFETY_TERMS = frozenset(
        {
            "胸闷",
            "治疗",
            "治好",
            "医疗",
            "太胖",
            "身材缺点",
            "很难看",
            "颜值",
            "很丑",
        }
    )

    def __init__(
        self,
        settings: Settings,
        repository: FixtureRepository,
        provider: GrokLLMProvider,
        traces: TraceStore,
        state: SceneStateStore,
        memory: MemoryService | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.provider = provider
        self.traces = traces
        self.state = state
        self.memory = memory
        self._memory_applied_by_request: dict[str, frozenset[str]] = {}
        self._memory_excluded_by_request: dict[str, frozenset[str]] = {}
        self._memory_lock = RLock()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _detect_horizon(text: str) -> tuple[Horizon, bool]:
        correction_terms: dict[str, Horizon] = {
            "时间还没定": "unknown",
            "时间不确定": "unknown",
            "还没定": "unknown",
            "马上": "now",
            "立刻": "now",
            "现在": "now",
            "等会儿": "today",
            "待会儿": "today",
            "一会儿": "today",
            "一会就": "today",
            "过会儿": "today",
            "今天": "today",
            "今日": "today",
            "今晚": "today",
            "明天": "soon",
            "后天": "soon",
            "下周": "soon",
            "周末": "soon",
            "下个月": "planned",
            "下月": "planned",
        }
        alternatives = "|".join(
            re.escape(term)
            for term in sorted(correction_terms, key=len, reverse=True)
        )
        correction = re.search(
            rf"(?:改成|改为)\s*({alternatives})",
            text,
        ) or re.search(
            rf"不是\s*(?:{alternatives})\s*[，,]?\s*(?:而)?是\s*({alternatives})",
            text,
        )
        if correction:
            return correction_terms[correction.group(1)], True
        evidence_text = re.sub(
            rf"(?:不是|并非|不要|不用|无需|不|别)\s*(?:{alternatives})",
            "",
            text,
        )
        # “马上” can also be the locative phrase “on horseback”.  Remove only
        # those narrow, explicit non-time forms before temporal detection.
        evidence_text = re.sub(r"(?:骑在|坐在|站在|人在)\s*马上", "", evidence_text)
        if any(term in evidence_text for term in ("时间不确定", "还没定", "尚未定", "不知道哪天")):
            return "unknown", True
        if any(term in evidence_text for term in ("马上", "立刻", "现在", "十分钟", "登机前", "出门前")):
            return "now", True
        if any(
            term in evidence_text
            for term in (
                "等会儿",
                "待会儿",
                "一会儿",
                "一会就",
                "过会儿",
            )
        ):
            return "today", True
        if any(term in evidence_text for term in ("今天", "今日", "今晚", "今早", "今下午")):
            return "today", True
        if any(term in evidence_text for term in ("明天", "后天", "下周", "周末", "这周")):
            return "soon", True
        if any(
            term in evidence_text
            for term in ("下月", "下个月", "月底", "两周后", "秋季", "冬季", "春季", "夏季")
        ):
            return "planned", True
        return "unknown", False

    @staticmethod
    def _event_time(horizon: Horizon, explicit: datetime | None) -> datetime | None:
        if explicit is not None:
            return explicit
        now = datetime.now().astimezone()
        delta = {
            "now": timedelta(minutes=30),
            "today": timedelta(hours=8),
            "soon": timedelta(days=7),
            "planned": timedelta(days=14),
        }.get(horizon)
        return now + delta if delta else None

    @staticmethod
    def _normalize_event_time(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.astimezone()
        return value.astimezone()

    @staticmethod
    def _horizon_from_event_time(value: datetime) -> Horizon:
        now = datetime.now().astimezone()
        delta = value - now
        if delta <= timedelta(hours=2):
            return "now"
        if value.date() == now.date():
            return "today"
        if delta <= timedelta(hours=48):
            return "soon"
        return "planned"

    @classmethod
    def _more_urgent(cls, primary: Horizon, *candidates: Horizon | None) -> Horizon:
        selected = primary
        urgency_rank = {"high": 3, "medium": 2, "low": 1}
        for candidate in candidates:
            if candidate is None:
                continue
            if urgency_rank[cls._urgency(candidate)] > urgency_rank[cls._urgency(selected)]:
                selected = candidate
        return selected

    @staticmethod
    def _urgency(horizon: Horizon) -> str:
        return {
            "now": "high",
            "today": "high",
            "unknown": "high",
            "soon": "medium",
            "planned": "low",
        }[horizon]

    def _shopping_allowed(
        self, urgency: str, intent: Intent, event_time: datetime | None
    ) -> bool:
        if urgency == "high" or intent not in {"buy", "fill_gap", "browse"}:
            return False
        if not self.settings.catalog_enabled:
            return False
        if urgency == "low":
            return True
        if event_time is None:
            return False
        # Conservative R1 Demo buffer: three delivery days plus one safety day.
        return event_time >= datetime.now().astimezone() + timedelta(days=4)

    @staticmethod
    def _detect_occasion(text: str) -> tuple[Occasion, bool]:
        mappings: list[tuple[Occasion, tuple[str, ...]]] = [
            ("interview", ("面试",)),
            ("travel", ("旅行", "出差", "登机", "行程", "出游")),
            ("sports", ("运动", "健身",)),
            ("outdoor", ("户外", "露营", "徒步")),
            ("party", ("聚会", "派对", "社团活动")),
            ("date", ("约会",)),
            ("commute", ("通勤", "上班")),
            (
                "meeting",
                ("会议", "开会", "客户", "汇报", "重要见面", "辩论", "演讲"),
            ),
            ("home", ("居家", "在家")),
            ("daily", ("上课", "日常")),
        ]
        correction_terms = {
            term: occasion for occasion, terms in mappings for term in terms
        }
        alternatives = "|".join(
            re.escape(term)
            for term in sorted(correction_terms, key=len, reverse=True)
        )
        correction = re.search(
            rf"(?:改成|改为)\s*({alternatives})",
            text,
        ) or re.search(
            rf"不是\s*(?:{alternatives})\s*[，,]?\s*(?:而)?是\s*({alternatives})",
            text,
        )
        if correction:
            return correction_terms[correction.group(1)], True
        evidence_text = re.sub(
            rf"(?:不是|并非)\s*(?:{alternatives})", "", text
        )
        for occasion, terms in mappings:
            if any(term in evidence_text for term in terms):
                return occasion, True
        return "daily", False

    @staticmethod
    def _detect_intent(text: str) -> tuple[Intent, bool]:
        # These phrases apply to the whole request, rather than to one named
        # slot.  Slot-local negatives (for example `别买鞋，只缺衬衫`) are
        # resolved below by the clause-bound gap parser and must not suppress
        # the independently confirmed shirt gap.
        if any(
            term in text
            for term in (
                "不购物",
                "先不考虑购买",
                "别推荐商品",
                "不要打开商品",
                "不要打开目录",
                "只用现有衣橱",
                "不要买任何",
                "什么都别买",
                "全部不用买",
            )
        ):
            return "recommend", True
        if any(term in text for term in ("胸闷", "治疗", "太胖", "身材缺点", "很难看")):
            return "vent", True
        if re.search(
            r"(?:穿什么|怎么穿)(?:都)?(?:觉得)?(?:不对|不合适|不行|不好看?)",
            text,
        ):
            # A global helplessness statement about every styling option is a
            # support request, even when the sentence also names an occasion.
            # Keep this narrower than ordinary nervousness so concrete styling
            # requests and lightweight emotion remain actionable.
            return "vent", True
        if any(term in text for term in ("紧张", "有点慌")):
            # Mixed emotion + a concrete styling goal remains a styling task.
            # Pure distress without an actionable goal stays in the support flow.
            occasion, occasion_detected = SceneParser._detect_occasion(text)
            if (
                occasion_detected and occasion not in {"home", "daily"}
            ) or any(
                term in text
                for term in (
                    "想显得",
                    "想可靠",
                    "想更",
                    "想穿",
                    "给我一套",
                    "推荐",
                    "帮我选",
                    "帮我搭",
                    "搭衣橱",
                    "帮我选衣",
                    "给我搭",
                )
            ):
                return "recommend", True
            return "vent", True

        # Shopping evidence must be attached to an actual slot.  This keeps a
        # negative shoe clause from disabling a separate shirt gap, while a
        # negative-only request has no confirmed gap and stays wardrobe-only.
        gap_slots = SceneParser._detect_gap_slots(text)
        if gap_slots:
            if any(term in text for term in ("看看", "浏览")):
                return "browse", True
            if re.search(
                r"(?<!不)缺|(?<!不)少|想补|需补|需要补|要补|补购|补一|补个|补双|"
                r"(?<!不)需要更",
                text,
            ):
                return "fill_gap", True
            return "buy", True

        if any(
            term in text
            for term in (
                "不要买",
                "别买",
                "不用买",
                "不需要买",
                "无需购买",
                "不想买",
                "不想补",
                "不补",
                "不缺",
                "已有",
                "已经有",
                "有了",
                "已备好",
                "备好了",
                "够用",
            )
        ):
            return "recommend", True
        # R1 shopping intent is slot-bound. Generic Catalog/inventory language
        # without a query-local garment gap remains wardrobe-only, even when it
        # contains words such as `商品`, `购物`, `看看`, or `浏览`.
        return "recommend", False

    @staticmethod
    def _detect_goals(text: str) -> list[str]:
        mapping = {
            "reliable": ("可靠", "稳妥", "得体", "正式"),
            "modern": ("现代", "别太老气", "年轻"),
            "comfortable": ("舒服", "舒适", "轻便", "久走", "放松"),
            "polished": ("利落", "精致", "整洁"),
            "low_key": ("低调", "不张扬"),
            "confident": ("自信",),
            "cool": ("酷", "街头"),
        }
        return [goal for goal, terms in mapping.items() if any(term in text for term in terms)]

    @staticmethod
    def _detect_season(text: str) -> str | None:
        for term, value in (
            ("春季", "spring"),
            ("夏季", "summer"),
            ("秋季", "autumn"),
            ("冬季", "winter"),
        ):
            if term in text:
                return value
        return None

    @staticmethod
    def _weather_requirement(text: str) -> str:
        if any(term in text for term in ("下雨", "雨天", "防雨")):
            return "rain"
        if any(term in text for term in ("降温", "保暖", "寒冷", "冬季")):
            return "cold"
        if any(term in text for term in ("炎热", "温暖城市", "高温", "夏季")):
            return "warm"
        return "none"

    @staticmethod
    def _detect_gap_slots(text: str) -> set[str]:
        """Return only slots with query-local purchase/gap evidence.

        The relation is deliberately clause and slot bound.  A marker such as
        ``缺`` is assigned to the nearest garment slot in its own clause; it
        cannot leak from ``只缺衬衫`` back to an earlier shoe mention.  A local
        fulfilled/negative marker always wins for that slot, including when a
        positive word is a substring of it (for example ``不需要买``).
        """
        slot_terms = {
            "shoes": ("鞋", "鞋子", "靴"),
            "outer": ("外套", "风衣", "大衣"),
            "top": ("上衣", "衬衫", "针织衫"),
            "bottom": ("裤", "下装"),
            "dress": ("连衣裙", "裙子"),
            "bag": ("包", "手提包", "挎包"),
            "accessory": ("配饰", "围巾", "腰带"),
        }
        term_to_slot = {
            term: slot for slot, terms in slot_terms.items() for term in terms
        }
        slot_pattern = re.compile(
            "|".join(
                re.escape(term)
                for term in sorted(term_to_slot, key=len, reverse=True)
            )
        )
        clause_pattern = re.compile(
            r"[，,。；;！？!?\r\n]+|"
            r"(?:但是|但|不过|然而|而是|另外|同时|然后|并且|而且|却)|"
            r"(?:只|仅)(?=(?:想|需|要|缺|少|补|买|看|用))"
        )
        coordination_pattern = re.compile(
            r"\s*(?:以及|还有|连同|和|与|及|、|跟|同|兼|并)\s*"
            r"(?:(?:[一二两三四五六七八九十]+|\d+)"
            r"(?:个|件|双|条|套|只|款)?)?\s*"
        )
        shared_bridge_pattern = re.compile(
            r"\s*(?:(?:都|各|均|也|还|再|一起|一并)|"
            r"(?:一|两)(?:个|件|双|条|套|只|款)?)?\s*"
        )
        positive_pattern = re.compile(
            r"(?<!不)缺|(?<!不)少|"
            r"想补|需补|需要补|要补|补购|补一|补个|补双|"
            r"想买|要买|需要买|需要购买|可以买|可买|打算买|"
            r"(?<!不)需要更|"
            r"想看看|看看"
        )
        negative_pattern = re.compile(
            r"已有|已经有(?:了)?|有了|"
            r"已(?:经)?备好|备好了?|够用|不缺|"
            r"不用(?:买|购买|补|补购)?|"
            r"无需(?:买|购买|补|补购)?|"
            r"不需要(?:买|购买|补|补购)?|"
            r"不想(?:买|购买|补|补购)|"
            r"别(?:买|购买|补|补购)|"
            r"不要(?:买|购买|补|补购)|不补|只用现有"
        )

        def marker_slot(
            marker: re.Match[str],
            mentions: list[tuple[re.Match[str], str]],
        ) -> str | None:
            def proximity(entry: tuple[re.Match[str], str]) -> tuple[float, int]:
                mention = entry[0]
                if marker.end() <= mention.start():
                    return float(mention.start() - marker.end()), 1
                if mention.end() <= marker.start():
                    # On an exact tie, Chinese post-position predicates such as
                    # `鞋缺` bind to the preceding slot.
                    return float(marker.start() - mention.end()), 0
                return 0.0, 0

            nearest = min(mentions, key=proximity)
            distance, _direction = proximity(nearest)
            # More distant prose is not sufficient evidence for a shopping
            # gap.  Twelve Han characters still permits ordinary modifiers.
            return nearest[1] if distance <= 12 else None

        def coordination_groups(
            clause: str,
            mentions: list[tuple[re.Match[str], str]],
        ) -> list[list[tuple[re.Match[str], str]]]:
            groups: list[list[tuple[re.Match[str], str]]] = []
            current = [mentions[0]]
            for mention in mentions[1:]:
                bridge = clause[current[-1][0].end() : mention[0].start()]
                if len(bridge) <= 8 and coordination_pattern.fullmatch(bridge):
                    current.append(mention)
                else:
                    if len(current) > 1:
                        groups.append(current)
                    current = [mention]
            if len(current) > 1:
                groups.append(current)
            return groups

        def shared_slots(
            clause: str,
            markers: list[re.Match[str]],
            groups: list[list[tuple[re.Match[str], str]]],
        ) -> set[str]:
            bound: set[str] = set()
            for group in groups:
                group_start = group[0][0].start()
                group_end = group[-1][0].end()
                for marker in markers:
                    if marker.end() <= group_start:
                        bridge = clause[marker.end() : group_start]
                    elif marker.start() >= group_end:
                        bridge = clause[group_end : marker.start()]
                    else:
                        # A predicate between two coordinated slot mentions is
                        # local to its nearest slot, not shared by the group.
                        continue
                    if len(bridge) <= 8 and shared_bridge_pattern.fullmatch(bridge):
                        bound.update(slot for _mention, slot in group)
            return bound

        detected: set[str] = set()
        for clause in filter(None, (part.strip() for part in clause_pattern.split(text))):
            mentions = [
                (match, term_to_slot[match.group()])
                for match in slot_pattern.finditer(clause)
            ]
            if not mentions:
                continue
            positive_markers = list(positive_pattern.finditer(clause))
            negative_markers = list(negative_pattern.finditer(clause))
            groups = coordination_groups(clause, mentions)
            positive_slots = {
                slot
                for marker in positive_markers
                if (slot := marker_slot(marker, mentions)) is not None
            }
            positive_slots.update(shared_slots(clause, positive_markers, groups))
            negative_slots = {
                slot
                for marker in negative_markers
                if (slot := marker_slot(marker, mentions)) is not None
            }
            negative_slots.update(shared_slots(clause, negative_markers, groups))
            detected.update(positive_slots - negative_slots)
        return detected

    def _constraints(
        self,
        text: str,
        user_id: str,
        explicit: SceneConstraints | None,
        previous: SceneRequest | None,
        memory_signals: tuple[MemorySignal, ...] = (),
        previous_memory_signals: frozenset[str] = frozenset(),
        previous_memory_excluded: frozenset[str] = frozenset(),
    ) -> SceneConstraints:
        user = self.repository.get_user(user_id)
        previous_constraints = previous.constraints if previous else None
        sources = [
            source
            for source in (previous_constraints, explicit)
            if source is not None
        ]
        taboo = {
            color for source in sources for color in source.taboo_colors
        }
        if user:
            taboo.update(user.avoid_colors)
        for chinese, color in COLOR_TERMS.items():
            if chinese in text and any(
                marker in text for marker in ("不要", "不穿", "不适合", "过滤", "避开")
            ):
                taboo.add(color)
        notes: list[str] = []
        if previous_constraints:
            for note in previous_constraints.comfort_notes:
                if (
                    note == "long_walk"
                    and not self._query_supports_signal(text, "long_walk")
                    and not (
                        explicit and "long_walk" in explicit.comfort_notes
                    )
                ):
                    continue
                # A memory-derived signal is recomputed on every new request.
                # It may be carried only when it was also directly established
                # by the previous user query; deleting memory therefore takes
                # effect on the next parse instead of lingering in session state.
                if (
                    note in previous_memory_signals
                    and not self._query_supports_signal(previous.query_text, note)
                ):
                    continue
                notes.append(note)
        if explicit:
            notes.extend(explicit.comfort_notes)
        if "不穿高跟鞋" in text or "不要高跟鞋" in text:
            notes.append("no_high_heels")
        if "不穿跟鞋" in text or "不要跟鞋" in text:
            notes.append("no_heels")
        if "不穿裙" in text or "不要裙" in text:
            notes.append("no_skirts")
        if any(
            term in text
            for term in (
                "久走",
                "走路多",
                "长时间站",
                "久站",
                "长站",
                "站很久",
                "步行很多",
            )
        ):
            notes.append("long_walk")
        notes.extend(signal.applied_signal for signal in memory_signals)
        text_excluded = {
            item
            for item in re.findall(r"\bg\d{3}\b", text, flags=re.IGNORECASE)
            if item in self.repository.garment_ids(user_id)
        }
        explicit_excluded = set(explicit.excluded_items) if explicit else set()
        memory_target_ids = {
            signal.target_item_id
            for signal in memory_signals
            if signal.target_item_id is not None
            and (
                (target := self.repository.get_garment(signal.target_item_id))
                is not None
                and target.user_id == user_id
                and target.slot == "shoes"
            )
        }
        excluded: set[str] = set(explicit_excluded)
        if previous_constraints:
            excluded.update(
                item
                for item in previous_constraints.excluded_items
                if item not in previous_memory_excluded
                or item in memory_target_ids
                or item in explicit_excluded
                or item in text_excluded
            )
        excluded.update(text_excluded)
        excluded.update(memory_target_ids)
        text_season = self._detect_season(text)
        if previous_constraints and previous_constraints.season:
            # R1 has no controlled constraint-removal flow. A new session is
            # required to replace an established season constraint.
            season = previous_constraints.season
        else:
            season = text_season or (explicit.season if explicit else None)
        text_weather = self._weather_requirement(text)
        if (
            previous_constraints
            and previous_constraints.weather_requirement != "none"
        ):
            weather = previous_constraints.weather_requirement
        elif text_weather != "none":
            weather = text_weather
        elif explicit:
            weather = explicit.weather_requirement
        else:
            weather = "none"
        required_slots = sorted(
            {"top", "bottom", "shoes"}
            | {
                slot
                for source in sources
                for slot in source.required_slots
            }
        )
        if weather in {"cold", "rain"} and "outer" not in required_slots:
            required_slots = ["outer", *required_slots]
        # Shopping gaps are not hard constraints and never come from an
        # untrusted structured override alone. They require current-query
        # evidence (or a future server-side assembler confirmation).
        gap_slots = self._detect_gap_slots(text)
        return SceneConstraints(
            taboo_colors=sorted(taboo),
            excluded_items=sorted(excluded),
            comfort_notes=sorted(set(notes)),
            required_slots=required_slots,
            gap_slots=sorted(gap_slots),
            season=season,
            weather_requirement=weather,
        )

    @staticmethod
    def _query_supports_signal(text: str, signal: str) -> bool:
        if signal == "long_walk":
            return any(
                term in text
                for term in (
                    "久走",
                    "走路多",
                    "长时间站",
                    "久站",
                    "长站",
                    "站很久",
                    "步行很多",
                )
            )
        if signal == "no_high_heels":
            return any(term in text for term in ("不穿高跟", "不要高跟", "避免高跟"))
        if signal == "no_skirts":
            return any(term in text for term in ("不穿裙", "不要裙", "避免裙"))
        return False

    def _request_retry_matches(
        self, payload: SceneParseInput, existing: SceneRequest
    ) -> bool:
        if payload.user_id != existing.user_id or payload.query_text != existing.query_text:
            return False
        if (
            payload.styling_session_id is not None
            and payload.styling_session_id != existing.styling_session_id
        ):
            return False
        supplied_fields = {
            "intent": payload.intent,
            "occasion": payload.occasion,
            "event_horizon": payload.event_horizon,
        }
        if any(
            value is not None and value != getattr(existing, field)
            for field, value in supplied_fields.items()
        ):
            return False
        if payload.event_time is not None:
            supplied_time = self._normalize_event_time(payload.event_time)
            existing_time = self._normalize_event_time(existing.event_time)
            if supplied_time != existing_time:
                return False
        if payload.goals is not None and payload.goals != existing.goals:
            return False
        if (
            payload.constraints is not None
            and payload.constraints != existing.constraints
        ):
            return False
        return True

    async def parse(
        self,
        payload: SceneParseInput,
        *,
        allow_provider: bool = True,
        horizon_evidence_text: str | None = None,
        provider_deferred_to_dialogue: bool = False,
    ) -> SceneRequest:
        user = self.repository.get_user(payload.user_id)
        if user is None:
            raise KeyError(payload.user_id)
        request_id = payload.request_id or self._new_id("req")
        existing_request = self.state.by_request(request_id)
        if existing_request is not None:
            if not self._request_retry_matches(payload, existing_request):
                raise SceneStateConflict(
                    "request_id reuse conflicts with the authoritative scene"
                )
            return existing_request
        session_id = payload.styling_session_id or self._new_id("session")
        previous = self.state.by_session(session_id)
        if previous is not None and previous.user_id != payload.user_id:
            raise SceneStateConflict("styling_session_id belongs to another user")
        trace_id = self._new_id("trace")
        self.traces.start(
            trace_id=trace_id,
            request_id=request_id,
            styling_session_id=session_id,
            query_text=payload.query_text,
            user_id=payload.user_id,
        )

        advisory: dict[str, Any] = {}
        provider_trace: dict[str, Any]
        backend = "rule_fallback"
        # Privacy/safety classification must happen before any external provider
        # receives the query. A mixed styling request is still fully local when
        # one of these closed-set signals is present.
        local_intent, local_intent_detected = self._detect_intent(payload.query_text)
        local_provider_blocked = (
            not allow_provider
            or payload.intent == "vent"
            or local_intent == "vent"
            or any(
                term in payload.query_text
                for term in self._LOCAL_PROVIDER_SAFETY_TERMS
            )
            or MemoryService.is_sensitive(payload.query_text)
        )
        if provider_deferred_to_dialogue:
            provider_trace = {
                "attempted": False,
                "status": "pending",
                "requested_model": self.settings.grok_model,
                "transport_model": self.provider.transport_model,
                "resolved_model": None,
                "model_verified": False,
                "reason_code": "DIALOGUE_PROVIDER_DEFERRED",
            }
        elif local_provider_blocked:
            block_reason = (
                "DIALOGUE_MINIMIZED_PROVIDER_PATH"
                if not allow_provider
                and payload.intent != "vent"
                and local_intent != "vent"
                else "LOCAL_SAFETY_OR_PRIVACY_SHORT_CIRCUIT"
            )
            provider_trace = {
                "attempted": False,
                "status": "blocked",
                "requested_model": self.settings.grok_model,
                "resolved_model": None,
                "transport_model": self.provider.transport_model,
                "model_verified": False,
                "reason_code": block_reason,
                "fallback": "scene_rules_v1",
            }
            self.traces.append_fallback(
                trace_id,
                {
                    "component": "llm",
                    "fallback": "scene_rules_v1",
                    "reason_code": block_reason,
                },
            )
        else:
            interaction_budget = self.settings.effective_cpa_scene_budget_seconds
            try:
                advisory, provider_trace = await asyncio.wait_for(
                    self.provider.parse_scene_advisory(payload.query_text),
                    timeout=interaction_budget,
                )
                provider_trace = {
                    **provider_trace,
                    "interaction_budget_seconds": interaction_budget,
                }
                backend = "grok4.6"
            except (TimeoutError, asyncio.TimeoutError):
                reason_code = "CPA_INTERACTION_BUDGET_EXCEEDED"
                self.provider.mark_interaction_failure(reason_code)
                provider_trace = {
                    "attempted": self.settings.cpa_text_enabled,
                    "status": "degraded",
                    "requested_model": self.settings.grok_model,
                    "resolved_model": None,
                    "transport_model": self.provider.transport_model,
                    "model_verified": False,
                    "reason_code": reason_code,
                    "interaction_budget_seconds": interaction_budget,
                    "fallback": "scene_rules_v1",
                }
                self.traces.append_fallback(
                    trace_id,
                    {
                        "component": "llm",
                        "fallback": "scene_rules_v1",
                        "reason_code": reason_code,
                    },
                )
            except asyncio.CancelledError:
                # External request cancellation is not a degradable Provider
                # result. Do not persist a Scene or leave an incomplete Trace.
                reason_code = "CPA_PROVIDER_CANCELLED"
                self.provider.mark_interaction_failure(reason_code)
                self.traces.discard(trace_id)
                raise
            except ProviderUnavailable as exc:
                reason_code = exc.reason_code
                provider_trace = {
                    "attempted": self.settings.cpa_text_enabled,
                    "status": "degraded",
                    "requested_model": self.settings.grok_model,
                    "resolved_model": None,
                    "transport_model": self.provider.transport_model,
                    "model_verified": False,
                    "reason_code": reason_code,
                    "interaction_budget_seconds": interaction_budget,
                    "fallback": "scene_rules_v1",
                }
                self.traces.append_fallback(
                    trace_id,
                    {
                        "component": "llm",
                        "fallback": "scene_rules_v1",
                        "reason_code": reason_code,
                    },
                )

        text_horizon, horizon_detected = self._detect_horizon(
            payload.query_text
            if horizon_evidence_text is None
            else horizon_evidence_text
        )
        supplied_time = self._normalize_event_time(payload.event_time)
        time_horizon = (
            self._horizon_from_event_time(supplied_time) if supplied_time else None
        )
        if horizon_detected:
            base_horizon = text_horizon
        elif previous:
            base_horizon = previous.event_horizon
        elif payload.event_horizon is not None:
            # Structured fixture/API inputs remain supported, but only when the
            # current query has no explicit deadline.  The final merge below is
            # fail-closed: neither this field nor a prior scene can lower a more
            # urgent text/event_time signal.
            base_horizon = payload.event_horizon
        elif time_horizon:
            base_horizon = time_horizon
        else:
            base_horizon = "unknown"
        horizon = self._more_urgent(
            base_horizon, payload.event_horizon, time_horizon
        )
        preserve_supplied_time = (
            supplied_time is not None
            and time_horizon is not None
            and self._urgency(time_horizon) == self._urgency(horizon)
        )
        if preserve_supplied_time:
            event_time = supplied_time
        elif previous and not horizon_detected and supplied_time is None:
            event_time = previous.event_time
        else:
            event_time = self._event_time(horizon, None)

        intent, intent_detected = local_intent, local_intent_detected
        query_supports_shopping = intent_detected and intent in {
            "buy",
            "fill_gap",
            "browse",
        }
        if payload.intent in {"recommend", "vent"}:
            # A structured override may tighten by disabling shopping.
            intent = payload.intent
        elif payload.intent in {"buy", "fill_gap", "browse"}:
            # Shopping can only be enabled by current-query evidence.
            if not query_supports_shopping:
                intent = "recommend"
        elif not intent_detected and previous and previous.intent in {
            "recommend",
            "vent",
        }:
            intent = previous.intent
        elif not intent_detected and advisory.get("intent") in {
            "recommend",
            "vent",
        }:
            intent = advisory["intent"]

        occasion, occasion_detected = self._detect_occasion(payload.query_text)
        if occasion_detected:
            pass
        elif previous:
            occasion = previous.occasion
        elif payload.occasion:
            occasion = payload.occasion
        elif not occasion_detected and advisory.get("occasion") in {
            "daily",
            "commute",
            "interview",
            "meeting",
            "date",
            "party",
            "travel",
            "outdoor",
            "home",
            "sports",
        }:
            occasion = advisory["occasion"]

        goals = payload.goals or self._detect_goals(payload.query_text)
        if not goals and previous:
            goals = previous.goals
        if not goals and isinstance(advisory.get("goals"), list):
            goals = [goal for goal in advisory["goals"] if goal in VALID_GOALS]
        if not goals:
            goals = list(user.goals[:2])

        available_memory_signals = (
            self.memory.active_signals(payload.user_id) if self.memory else ()
        )
        long_walk_relevant = self._query_supports_signal(
            payload.query_text, "long_walk"
        )
        memory_signals = tuple(
            signal
            for signal in available_memory_signals
            if signal.applied_signal != "long_walk" or long_walk_relevant
        )
        with self._memory_lock:
            previous_memory_signals = (
                self._memory_applied_by_request.get(previous.request_id, frozenset())
                if previous
                else frozenset()
            )
            previous_memory_excluded = (
                self._memory_excluded_by_request.get(
                    previous.request_id, frozenset()
                )
                if previous
                else frozenset()
            )
        constraints = self._constraints(
            payload.query_text,
            payload.user_id,
            payload.constraints,
            previous,
            memory_signals,
            previous_memory_signals,
            previous_memory_excluded,
        )
        urgency = self._urgency(horizon)
        shopping_allowed = self._shopping_allowed(urgency, intent, event_time)
        clarification_required = False
        clarification_question = None
        missing_fields: list[str] = []
        assumptions: list[str] = []
        if horizon == "unknown" and not previous:
            missing_fields.append("event_horizon")
            if self.state.consume_clarification(session_id):
                clarification_required = True
                clarification_question = "这次穿搭最晚什么时候需要准备好？"
            assumptions.append("截止时间未知，按高急切度处理并禁止购物")

        scene = SceneRequest(
            request_id=request_id,
            user_id=payload.user_id,
            styling_session_id=session_id,
            query_text=payload.query_text,
            intent=intent,
            occasion=occasion,
            event_time=event_time,
            event_horizon=horizon,
            urgency=urgency,
            shopping_allowed=shopping_allowed,
            goals=list(dict.fromkeys(goals))[:3],
            constraints=constraints,
            backend=backend,
            clarification_required=clarification_required,
            clarification_question=clarification_question,
            missing_fields=missing_fields,
            assumptions=assumptions,
            ui_capabilities=UICapabilities(shopping_cta=shopping_allowed),
            trace_id=trace_id,
        )
        # Final server authority: recompute immediately before persistence/output.
        scene.urgency = self._urgency(scene.event_horizon)
        scene.shopping_allowed = self._shopping_allowed(
            scene.urgency, scene.intent, scene.event_time
        )
        scene.ui_capabilities = UICapabilities(shopping_cta=scene.shopping_allowed)
        self.state.save(scene)
        explicit_notes = (
            set(payload.constraints.comfort_notes) if payload.constraints else set()
        )
        explicit_excluded = (
            set(payload.constraints.excluded_items) if payload.constraints else set()
        )
        query_excluded = {
            item.lower()
            for item in re.findall(
                r"\bg\d{3}\b", payload.query_text, flags=re.IGNORECASE
            )
            if item.lower() in self.repository.garment_ids(payload.user_id)
        }
        memory_target_ids = {
            signal.target_item_id
            for signal in memory_signals
            if signal.target_item_id is not None
        }
        with self._memory_lock:
            self._memory_applied_by_request[request_id] = frozenset(
                signal.applied_signal
                for signal in memory_signals
                if signal.applied_signal not in explicit_notes
                and not self._query_supports_signal(
                    payload.query_text, signal.applied_signal
                )
            )
            self._memory_excluded_by_request[request_id] = frozenset(
                item_id
                for item_id in memory_target_ids
                if item_id not in explicit_excluded and item_id not in query_excluded
            )
        trace_constraints = scene.constraints.model_dump()
        trace_constraints["excluded_items"] = [
            item_id
            for item_id in scene.constraints.excluded_items
            if item_id not in memory_target_ids
        ]
        trace_constraints["private_excluded_item_count"] = len(
            memory_target_ids
        )
        self.traces.update(
            trace_id,
            provider=provider_trace,
            scene={
                "event_horizon": scene.event_horizon,
                "urgency": scene.urgency,
                "shopping_allowed": scene.shopping_allowed,
                "intent": scene.intent,
                "occasion": scene.occasion,
                "goals": scene.goals,
                "constraint_summary": trace_constraints,
                "memory_signals": [
                    {
                        "memory_id": signal.memory_id,
                        "namespace": signal.namespace,
                        "type": signal.type,
                        "applied_signal": signal.applied_signal,
                        "targeted_policy_applied": signal.target_item_id is not None,
                        "similarity_policy": signal.similarity_policy,
                        "private_signature_logged": False,
                    }
                    for signal in memory_signals
                ],
                "server_recomputed": True,
                "carried_from_session": previous is not None,
            },
        )
        return scene
