from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable

from .memory_candidates import (
    MemoryCandidateCard,
    MemoryCandidateExtractResponse,
    MemoryCandidateService,
    SessionPreference,
    canonicalize_candidate,
)
from .memory_repository import MemoryRepositoryError
from .memory_service import MemoryProposeInput, MemoryService
from .models import (
    ConversationMode,
    DialogueAction,
    DialogueProviderStatus,
    DialogueTurnInput,
    DialogueTurnResponse,
    PendingQuestionStatus,
    PreferenceClarification,
    PreferenceMemoryCandidateCard,
    SceneParseInput,
    SceneRequest,
    UICapabilities,
)
from .preference_uncertainty import PreferenceUncertaintyPolicy
from .providers import GrokLLMProvider, ProviderUnavailable
from .scene import SceneParser
from .service import RecommendationService
from .tracing import TraceStore


class DialogueNotFound(KeyError):
    pass


class DialogueConflict(ValueError):
    pass


@dataclass(frozen=True)
class SessionFitContext:
    """Purpose-bound measurements retained only inside one styling session."""

    height_cm: float | None = None
    weight_kg: float | None = None
    silhouette_goal: str | None = None

    def merged(self, update: "SessionFitContext | None") -> "SessionFitContext | None":
        if update is None:
            return self
        merged = SessionFitContext(
            height_cm=(
                update.height_cm
                if update.height_cm is not None
                else self.height_cm
            ),
            weight_kg=(
                update.weight_kg
                if update.weight_kg is not None
                else self.weight_kg
            ),
            silhouette_goal=(
                update.silhouette_goal
                if update.silhouette_goal is not None
                else self.silhouette_goal
            ),
        )
        return merged if merged.has_value else None

    @property
    def has_value(self) -> bool:
        return any(
            value is not None
            for value in (self.height_cm, self.weight_kg, self.silhouette_goal)
        )

    def provider_summary(self) -> dict[str, object]:
        return {
            "purpose": "garment_fit_only",
            "height_cm": self.height_cm,
            "weight_kg": self.weight_kg,
            "silhouette_goal": self.silhouette_goal,
        }


@dataclass
class DialogueEnvelope:
    user_id: str
    styling_session_id: str
    scene: SceneRequest | None
    conversation_mode: ConversationMode
    recommendation_paused: bool
    pending_question_status: PendingQuestionStatus
    clarification_asked: bool
    support_turn_count: int
    last_support_strategy: str | None
    explicit_pause_latched: bool
    fit_context: SessionFitContext | None
    asked_preference_gap_codes: frozenset[str]
    pending_preference_question_id: str | None
    pending_preference_gap_code: str | None
    history: tuple[dict[str, str], ...]
    turn_index: int
    expires_at: float

    def copy(self) -> "DialogueEnvelope":
        return DialogueEnvelope(
            user_id=self.user_id,
            styling_session_id=self.styling_session_id,
            scene=self.scene.model_copy(deep=True) if self.scene else None,
            conversation_mode=self.conversation_mode,
            recommendation_paused=self.recommendation_paused,
            pending_question_status=self.pending_question_status,
            clarification_asked=self.clarification_asked,
            support_turn_count=self.support_turn_count,
            last_support_strategy=self.last_support_strategy,
            explicit_pause_latched=self.explicit_pause_latched,
            fit_context=self.fit_context,
            asked_preference_gap_codes=frozenset(
                self.asked_preference_gap_codes
            ),
            pending_preference_question_id=self.pending_preference_question_id,
            pending_preference_gap_code=self.pending_preference_gap_code,
            history=tuple(dict(item) for item in self.history),
            turn_index=self.turn_index,
            expires_at=self.expires_at,
        )


class DialogueStateStore:
    """Owner-bound session envelopes and deterministic turn retry receipts."""

    def __init__(
        self,
        ttl_seconds: float,
        on_session_expired: Callable[[str], None],
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._on_session_expired = on_session_expired
        self._sessions: dict[str, DialogueEnvelope] = {}
        self._requests: dict[
            str, tuple[str, str, DialogueTurnResponse, float]
        ] = {}
        self._expired_request_ids: set[str] = set()
        self._lock = RLock()

    def _purge_expired_locked(self, now: float) -> None:
        expired_sessions = [
            session_id
            for session_id, envelope in self._sessions.items()
            if envelope.expires_at <= now
        ]
        for session_id in expired_sessions:
            self._sessions.pop(session_id, None)
            self._on_session_expired(session_id)
        expired_requests = [
            request_id
            for request_id, (_, _, _, expires_at) in self._requests.items()
            if expires_at <= now
        ]
        for request_id in expired_requests:
            self._requests.pop(request_id, None)
            self._expired_request_ids.add(request_id)

    @staticmethod
    def fingerprint(payload: DialogueTurnInput) -> str:
        canonical = json.dumps(
            {
                "user_id": payload.user_id,
                "message": payload.message,
                "styling_session_id": payload.styling_session_id,
                "preference_question_id": payload.preference_question_id,
                "preference_option_id": payload.preference_option_id,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def retry(
        self, request_id: str, payload: DialogueTurnInput
    ) -> DialogueTurnResponse | None:
        fingerprint = self.fingerprint(payload)
        with self._lock:
            now = time.monotonic()
            self._purge_expired_locked(now)
            if request_id in self._expired_request_ids:
                raise DialogueConflict("request_id receipt has expired")
            stored = self._requests.get(request_id)
            if stored is None:
                return None
            owner, stored_fingerprint, response, _ = stored
            if owner != payload.user_id or stored_fingerprint != fingerprint:
                raise DialogueConflict(
                    "request_id reuse conflicts with the authoritative dialogue turn"
                )
            expires_at = now + self._ttl_seconds
            self._requests[request_id] = (
                owner,
                stored_fingerprint,
                response,
                expires_at,
            )
            envelope = self._sessions.get(response.styling_session_id)
            if envelope is not None:
                envelope.expires_at = expires_at
            return response.model_copy(deep=True)

    def session(self, session_id: str, user_id: str) -> DialogueEnvelope:
        with self._lock:
            now = time.monotonic()
            self._purge_expired_locked(now)
            value = self._sessions.get(session_id)
            if value is None or value.user_id != user_id:
                raise DialogueNotFound("dialogue session not found")
            value.expires_at = now + self._ttl_seconds
            return value.copy()

    def commit(
        self,
        envelope: DialogueEnvelope,
        request_id: str,
        payload: DialogueTurnInput,
        response: DialogueTurnResponse,
    ) -> DialogueTurnResponse:
        fingerprint = self.fingerprint(payload)
        with self._lock:
            now = time.monotonic()
            self._purge_expired_locked(now)
            if request_id in self._expired_request_ids:
                raise DialogueConflict("request_id receipt has expired")
            current = self._sessions.get(envelope.styling_session_id)
            if current is not None and current.user_id != envelope.user_id:
                raise DialogueNotFound("dialogue session not found")
            stored = self._requests.get(request_id)
            if stored is not None:
                owner, stored_fingerprint, stored_response, _ = stored
                if owner != payload.user_id or stored_fingerprint != fingerprint:
                    raise DialogueConflict(
                        "request_id reuse conflicts with the authoritative dialogue turn"
                    )
                return stored_response.model_copy(deep=True)
            expires_at = now + self._ttl_seconds
            envelope.expires_at = expires_at
            self._sessions[envelope.styling_session_id] = envelope.copy()
            self._requests[request_id] = (
                payload.user_id,
                fingerprint,
                response.model_copy(deep=True),
                expires_at,
            )
            return response.model_copy(deep=True)


class DialogueService:
    _OUTFIT_COUNT = re.compile(
        r"(?<!第)(?P<count>[一二两三123])\s*(?:套|个(?:方向|方案|搭配))"
    )
    _CHAT_TERMS = (
        "先聊聊天",
        "我们先聊天吧",
        "可以先聊天吗",
        "先陪我说会儿话",
        "先陪我聊",
        "聊聊天",
        "聊会儿",
        "说会儿话",
    )
    _PAUSE_TERMS = (
        "先不推荐",
        "先别推荐",
        "先别给方案",
        "先不看穿搭",
        "先陪我聊",
        "先帮我缓解情绪",
        "缓解一下情绪",
    )
    _RESUME_TERMS = (
        "继续推荐",
        "现在开始搭配",
        "现在开始搭",
        "可以看穿搭",
        "现在可以推荐",
        "开始搭配",
        "继续帮我搭",
    )
    _CLOSE_TERMS = ("不用推荐了", "结束本次任务", "结束任务")
    _EXPLICIT_STYLING_TERMS = (
        "帮我搭",
        "给我一套",
        "推荐一套",
        "推荐穿搭",
        "帮我选",
        "穿什么",
        "穿什么好",
        "怎么穿",
        "应该怎么穿",
        "你觉得我穿",
        "怎么搭",
    )
    _MILD_EMOTION_TERMS = (
        "紧张",
        "有点慌",
        "焦虑",
        "不安",
        "怕冷场",
        "担心冷场",
        "压力大",
        "难过",
        "缓解情绪",
        "聊聊",
    )
    _HELPLESS_PATTERN = re.compile(
        r"(?:穿什么|怎么穿)(?:都)?(?:觉得)?(?:不对|不合适|不行|不好看?)"
    )
    _MEDICAL_TERMS = ("胸闷", "治疗", "治好", "医疗", "诊断")
    _BODY_TERMS = ("太胖", "身材缺点", "颜值", "很丑", "很难看")
    _DIRECT_PRIVATE = re.compile(
        r"(?i)(?:[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|"
        r"(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)\d{17}[\dx](?!\d)|"
        r"(?:住址|家庭地址|身份证|护照|银行卡|微信号|手机号|电子邮件|邮箱|"
        r"工资|收入)\s*[:：是为]?\s*\S+)"
    )
    _HEIGHT_LABELED = re.compile(
        r"(?:我的?)?身高\s*[:：是为]?\s*(?P<value>\d{2,3}(?:\.\d+)?)"
        r"\s*(?:cm|厘米|公分)?(?![\d.a-z])",
        flags=re.IGNORECASE,
    )
    _HEIGHT_UNIT = re.compile(
        r"(?<![\d.])(?P<value>\d{2,3}(?:\.\d+)?)\s*(?:cm|厘米|公分)"
        r"(?![a-z])",
        flags=re.IGNORECASE,
    )
    _WEIGHT_LABELED = re.compile(
        r"(?:我的?)?体重\s*[:：是为]?\s*(?P<value>\d{2,3}(?:\.\d+)?)"
        r"\s*(?:kg|公斤|千克)?(?![\d.a-z])",
        flags=re.IGNORECASE,
    )
    _WEIGHT_UNIT = re.compile(
        r"(?<![\d.])(?P<value>\d{2,3}(?:\.\d+)?)\s*(?:kg|公斤|千克)"
        r"(?![a-z])",
        flags=re.IGNORECASE,
    )
    _SILHOUETTE_GOAL = re.compile(
        r"(?:(?:我)?(?:想|希望)(?:让自己)?(?:看起来|显得)?"
        r"(?:不那么瘦|不那么单薄|不显得瘦|不显得单薄|更有量感|更有分量)|"
        r"(?:别|不要)(?:让我)?(?:看起来|显得)?(?:太瘦|太单薄)|"
        r"(?:怎么搭|穿什么).{0,8}(?:不显得瘦|不显得单薄|更有量感|更有分量)|"
        r"(?:衣服|服装|轮廓).{0,6}(?:更有量感|更有分量|增加量感|更挺括))"
    )

    def __init__(
        self,
        scene_parser: SceneParser,
        recommendations: RecommendationService,
        provider: GrokLLMProvider,
        traces: TraceStore,
        memory_candidates: MemoryCandidateService,
    ) -> None:
        self.scene_parser = scene_parser
        self.recommendations = recommendations
        self.provider = provider
        self.traces = traces
        self.memory_candidates = memory_candidates
        self.preference_policy = PreferenceUncertaintyPolicy(
            scene_parser.repository
        )
        self.state = DialogueStateStore(
            scene_parser.settings.effective_dialogue_ttl_seconds,
            scene_parser.state.discard_session,
        )
        # Conservative R1 single-flight: all Dialogue turns are serialized so
        # an idempotent retry can never duplicate Scene/CPA/recommend/Catalog.
        self._turn_lock = asyncio.Lock()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _contains(text: str, terms: tuple[str, ...]) -> bool:
        return any(term in text for term in terms)

    def _safety_kind(self, text: str) -> str | None:
        if self._contains(text, self._MEDICAL_TERMS):
            return "medical"
        if self._contains(text, self._BODY_TERMS):
            return "body"
        return None

    def _context_update(self, text: str) -> bool:
        _, horizon_detected = SceneParser._detect_horizon(text)
        _, occasion_detected = SceneParser._detect_occasion(text)
        return bool(
            horizon_detected
            or occasion_detected
            or SceneParser._detect_goals(text)
        )

    @staticmethod
    def _time_label(text: str) -> str:
        terms = ("时间还没定", "今天", "明天", "后天", "下周", "周末", "这周")
        positions = [(text.rfind(term), term) for term in terms if term in text]
        if positions:
            return max(positions)[1]
        return "新的时间"

    @staticmethod
    def _task_emotion_acknowledgement(text: str) -> str:
        if any(term in text for term in ("怕冷场", "担心冷场", "没话说")):
            return "担心重要场合冷场很自然，我们先把能确定的穿搭决定落下来。"
        if any(term in text for term in ("有点慌", "不安", "压力大")):
            return "重要场合前有点不安很自然，我们先把决定落到衣服上。"
        return "重要场合前有点紧张很自然，我们把决定落到衣服上。"

    @classmethod
    def _acknowledge_task_emotion_once(cls, text: str, message: str) -> str:
        # RecommendationService may already have supplied the one supportive
        # sentence.  Never stack a second emotion acknowledgement in fallback.
        if any(term in message for term in ("紧张", "不安", "冷场")):
            return message
        return cls._task_emotion_acknowledgement(text) + message

    @staticmethod
    def _concern_label(text: str) -> str:
        if "第一次" in text and "约会" in text:
            return "first_impression"
        mappings = (
            ("silence", ("冷场", "没话说", "不知道聊什么", "聊不下去")),
            ("rejection", ("被拒绝", "拒绝我", "不喜欢我")),
            ("performance", ("汇报", "面试", "表现不好", "搞砸")),
            (
                "appearance_pressure",
                ("穿什么都", "怎么穿都", "外表", "不好看"),
            ),
            (
                "first_impression",
                ("第一印象", "第一次约会", "第一次见"),
            ),
        )
        for label, terms in mappings:
            if any(term in text for term in terms):
                return label
        return "general"

    @staticmethod
    def _support_stage(previous: DialogueEnvelope | None) -> str:
        count = previous.support_turn_count if previous else 0
        if count == 0:
            return "opening"
        if count == 1:
            return "follow_up"
        return "continued"

    @staticmethod
    def _support_strategy(
        concern_label: str,
        support_stage: str,
        control_action: str,
        previous: DialogueEnvelope | None,
    ) -> str:
        if control_action == "pause":
            candidate = "pause_confirm"
        elif concern_label != "general":
            candidate = f"{concern_label}_reframe"
        else:
            candidate = f"general_{support_stage}"
        if previous and candidate == previous.last_support_strategy:
            return f"{candidate}_continued"
        return candidate

    @staticmethod
    def _local_support_message(
        emotion_label: str,
        concern_label: str,
        strategy: str,
    ) -> str:
        if strategy.startswith("pause_confirm"):
            return (
                "我先按你的意思把穿搭建议停下来，现在不需要解决衣服。"
                "你更想先说说紧张是从哪一部分开始的？"
                "等你明确说“现在开始搭配”，我再继续。"
            )
        messages = {
            "silence": (
                "担心冷场时，很容易把每个停顿都当成失误；短暂安静并不等于约会失败。"
                "你更担心开场，还是聊到一半没话题？"
            ),
            "first_impression": (
                "第一次见面想留下好印象，常会让人把每个细节都放大。"
                "你最希望对方感受到你哪一面？"
            ),
            "rejection": (
                "担心不被喜欢会让这次见面显得像一场考核，但一次回应并不能定义你的价值。"
                "你最担心出现哪一种回应？"
            ),
            "performance": (
                "面对汇报或面试时，压力常会把注意力拉到所有可能出错的地方。"
                "现在最想先稳住的是开场、内容还是节奏？"
            ),
            "appearance_pressure": (
                "当每个穿搭选择看起来都不对时，可以先暂停判断，而不是继续逼自己挑选。"
                "此刻最让你有压力的是场合要求还是选择本身？"
            ),
        }
        if concern_label in messages:
            core = messages[concern_label]
        elif emotion_label == "nervous":
            core = (
                "面对重要场合时有些紧张，是可以被理解的。"
                "此刻哪一部分最让你有压力？"
            )
        elif emotion_label == "overwhelmed":
            core = (
                "听起来现在每个选择都让你有些不安，先不用要求自己马上选对。"
                "你最想先放下哪一部分压力？"
            )
        else:
            core = (
                "我听到了，我们先把穿搭建议暂停，不急着马上做决定。"
                "你现在更想先聊哪一部分？"
            )
        return f"{core}等你明确说“现在开始搭配”，我再继续穿搭建议。"

    @staticmethod
    def _safety_message(kind: str) -> str:
        if kind == "medical":
            return (
                "我不能诊断症状，也不能说穿搭能够治疗身体不适。"
                "如果症状突然、严重或持续，请尽快寻求专业医疗帮助；本轮不会推荐穿搭或商品。"
            )
        return (
            "我不会评价你本人或所谓缺点。"
            "如果之后继续，我们只围绕场合、舒适度和你想表达的感觉讨论衣服。"
        )

    @classmethod
    def _provider_safe_text(cls, text: str) -> str:
        return cls._DIRECT_PRIVATE.sub("[私人信息已隐藏]", text)[:600]

    @classmethod
    def _contains_direct_private(cls, text: str) -> bool:
        return bool(cls._DIRECT_PRIVATE.search(text))

    @staticmethod
    def _first_plausible_measurement(
        text: str,
        patterns: tuple[re.Pattern[str], ...],
        minimum: float,
        maximum: float,
    ) -> float | None:
        for pattern in patterns:
            for match in pattern.finditer(text):
                value = float(match.group("value"))
                if minimum <= value <= maximum:
                    return value
        return None

    @classmethod
    def _fit_candidate(
        cls, text: str
    ) -> tuple[SessionFitContext | None, str]:
        height = cls._first_plausible_measurement(
            text,
            (cls._HEIGHT_LABELED, cls._HEIGHT_UNIT),
            120,
            230,
        )
        weight = cls._first_plausible_measurement(
            text,
            (cls._WEIGHT_LABELED, cls._WEIGHT_UNIT),
            30,
            250,
        )
        silhouette_goal = (
            "add_garment_volume" if cls._SILHOUETTE_GOAL.search(text) else None
        )
        candidate = SessionFitContext(height, weight, silhouette_goal)
        sanitized = text
        for pattern, minimum, maximum in (
            (cls._HEIGHT_LABELED, 120, 230),
            (cls._HEIGHT_UNIT, 120, 230),
            (cls._WEIGHT_LABELED, 30, 250),
            (cls._WEIGHT_UNIT, 30, 250),
        ):
            sanitized = pattern.sub(
                lambda match: (
                    "[任务内版型信息已结构化]"
                    if minimum <= float(match.group("value")) <= maximum
                    else match.group(0)
                ),
                sanitized,
            )
        sanitized = cls._SILHOUETTE_GOAL.sub(
            "[任务内版型信息已结构化]", sanitized
        )
        sanitized = re.sub(
            r"(?:\[任务内版型信息已结构化\][，,、。\s]*){2,}",
            "[任务内版型信息已结构化]，",
            sanitized,
        )
        return (candidate if candidate.has_value else None), sanitized[:600]

    def _profile_context(self, user_id: str, *, withhold: bool) -> dict[str, object]:
        user = self.scene_parser.repository.get_user(user_id)
        if user is None:
            raise KeyError(user_id)
        if withhold:
            return {
                "styles": [],
                "favorite_colors": [],
                "avoid_colors": [],
                "common_occasions": [],
                "goals": [],
                "budget": "withheld",
                "confirmed_memory_signals": [],
            }
        memory_signals = (
            self.scene_parser.memory.active_signals(user_id)
            if self.scene_parser.memory
            else ()
        )
        return {
            "styles": list(user.styles[:5]),
            "favorite_colors": list(user.favorite_colors[:5]),
            "avoid_colors": list(user.avoid_colors[:5]),
            "common_occasions": list(user.occasions[:5]),
            "goals": list(user.goals[:5]),
            "budget": user.budget[:80],
            # Only confirmed, content-free policy signals cross this boundary.
            "confirmed_memory_signals": sorted(
                {signal.applied_signal for signal in memory_signals}
            ),
        }

    @staticmethod
    def _scene_summary(scene: SceneRequest | None) -> dict[str, object] | None:
        if scene is None:
            return None
        return {
            "occasion": scene.occasion,
            "event_horizon": scene.event_horizon,
            "urgency": scene.urgency,
            "shopping_allowed": scene.shopping_allowed,
            "goals": list(scene.goals),
            "required_slots": list(scene.constraints.required_slots),
            "taboo_colors": list(scene.constraints.taboo_colors),
            "comfort_notes": list(scene.constraints.comfort_notes),
        }

    @classmethod
    def _requested_outfit_count(cls, text: str) -> int | None:
        matches = list(cls._OUTFIT_COUNT.finditer(text))
        if not matches:
            return None
        mapping = {"一": 1, "二": 2, "两": 2, "三": 3}
        positive: list[tuple[re.Match[str], int]] = []
        negative: list[tuple[re.Match[str], int]] = []
        for match in matches:
            token = match.group("count")
            value = mapping[token] if token in mapping else int(token)
            before = text[max(0, match.start() - 8) : match.start()]
            after = text[match.end() : min(len(text), match.end() + 8)]
            is_negative = bool(
                re.search(r"(?:不要|不用|不是|别|不需要|不想要|不看)\s*$", before)
                or re.match(r"\s*(?:不要|不用|不需要|不看|太多)", after)
            )
            (negative if is_negative else positive).append((match, value))
        if len(positive) == 1:
            return positive[0][1]
        if not positive:
            return None
        # Multiple positive counts are ambiguous unless the final mention is
        # introduced by a closed, explicit correction phrase. A plain “给我”
        # is only a request verb and does not resolve conflicting counts.
        final_match, final_value = positive[-1]
        final_prefix = text[max(0, final_match.start() - 10) : final_match.start()]
        explicit_correction = bool(
            re.search(
                r"(?:改成|改为|换成|更正为|最终要|最后要|那就|就要|只要)\s*$",
                final_prefix,
            )
        )
        return final_value if explicit_correction else None

    def _recommendation_summary(self, recommendation) -> dict[str, object]:
        outfits: list[dict[str, object]] = []
        for outfit in recommendation.outfits[:3]:
            pieces: list[dict[str, str]] = []
            for garment_id in outfit.items:
                garment = self.scene_parser.repository.get_garment(garment_id)
                if garment is None:
                    continue
                pieces.append(
                    {
                        "slot": garment.slot,
                        "name": garment.name[:60],
                        "color": garment.color,
                    }
                )
            outfits.append(
                {
                    "strategy_label": outfit.strategy_label[:60],
                    "pieces": pieces,
                    "reasons": [reason[:100] for reason in outfit.reasons[:2]],
                }
            )
        return {
            "source": "current_owner_wardrobe_validated",
            "requested_outfit_count": recommendation.requested_outfit_count,
            "actual_outfit_count": len(outfits),
            "outfits": outfits,
            "gap_explanation": (
                recommendation.gap_explanation[:180]
                if recommendation.gap_explanation
                else None
            ),
        }

    def _grounded_local_recommendation(self, recommendation) -> str:
        count = len(recommendation.outfits)
        if not count:
            return recommendation.assistant_message + (
                f" {recommendation.gap_explanation}"
                if recommendation.gap_explanation
                else ""
            )
        directions: list[str] = []
        for index, outfit in enumerate(recommendation.outfits, 1):
            names = []
            for garment_id in outfit.items:
                garment = self.scene_parser.repository.get_garment(garment_id)
                if garment is not None:
                    names.append(garment.name)
            directions.append(
                f"{index}）{outfit.strategy_label.replace('先试这个：', '').replace('备选：', '')}："
                + "、".join(names)
            )
        message = (
            f"我已经按当前场景读完你的衣橱，整理出 {count} 套可直接试的方向："
            + "；".join(directions)
            + "。这些衣橱单品都来自你的当前衣橱，并已通过可用状态和硬约束复检。"
        )
        if recommendation.gap_explanation:
            message += recommendation.gap_explanation
        return message[:600]

    @staticmethod
    def _last_assistant_reply(previous: DialogueEnvelope | None) -> str | None:
        if previous is None:
            return None
        for item in reversed(previous.history):
            if item.get("role") == "assistant":
                return str(item.get("content", ""))
        return None

    @classmethod
    def _recommendation_reply_rejection(
        cls,
        reply: str,
        previous: DialogueEnvelope | None,
        actual_count: int,
    ) -> str | None:
        if re.search(
            r"(?:手头|衣橱|现有衣服).{0,12}(?:有什么|有哪些|列一下|告诉我|说说)|"
            r"(?:告诉我|说说|列一下).{0,12}(?:上衣|裤子|鞋子|衣服|衣橱)|"
            r"你(?:目前|现在)?(?:可以|能)?(?:拿来)?搭配的(?:衣服|单品).{0,8}(?:是什么|有哪些)|"
            r"(?:请|麻烦)?(?:先)?把(?:你)?(?:的)?(?:可选|现有|能搭的)?(?:衣服|单品)"
            r".{0,10}(?:发给?我|告诉我|列出来|说一下).{0,12}(?:再|然后)?(?:搭|推荐)?",
            reply,
        ):
            return "CPA_DIALOGUE_WARDROBE_REASK_REJECTED"
        claimed = {
            cls._requested_outfit_count(match.group(0))
            for match in cls._OUTFIT_COUNT.finditer(reply)
        }
        claimed.discard(None)
        if claimed and claimed != {actual_count}:
            return "CPA_DIALOGUE_OUTFIT_COUNT_CONFLICT"
        prior = cls._last_assistant_reply(previous)
        if prior:
            normalize = lambda value: re.sub(r"[\W_]+", "", value)
            left, right = normalize(prior), normalize(reply)
            if min(len(left), len(right)) >= 24 and difflib.SequenceMatcher(
                None, left, right
            ).ratio() >= 0.82:
                return "CPA_DIALOGUE_HIGH_REPETITION_REJECTED"
        return None

    def _provider_context(
        self,
        *,
        payload: DialogueTurnInput,
        previous: DialogueEnvelope | None,
        scene: SceneRequest | None,
        mode: ConversationMode,
        action: DialogueAction,
        control: str,
        safety_kind: str | None,
        close_or_closed: bool,
        sensitive: bool,
        current_text: str,
        fit_context: SessionFitContext | None,
        recommendation_summary: dict[str, object] | None,
    ) -> dict[str, object]:
        direct_private = self._contains_direct_private(payload.message)
        minimized = bool(
            safety_kind or close_or_closed or direct_private or sensitive
        )
        shopping_allowed = bool(
            scene and scene.shopping_allowed and action == "recommend"
        )
        if direct_private:
            current_turn = {
                "kind": "privacy_summary",
                "content": "private_identifier_removed; respond without requesting it",
            }
        elif sensitive:
            current_turn = {
                "kind": "sensitive_summary",
                "content": (
                    f"{safety_kind}_safety_boundary_required"
                    if safety_kind
                    else "sensitive_content_removed; respond without requesting details"
                ),
            }
        elif safety_kind:
            current_turn = {
                "kind": "safety_summary",
                "content": f"{safety_kind}_safety_boundary_required",
            }
        elif close_or_closed:
            current_turn = {
                "kind": "control_summary",
                "content": "task_close_or_closed_acknowledgement",
            }
        else:
            current_turn = {
                "kind": "normal",
                "content": self._provider_safe_text(current_text),
            }
        profile = self._profile_context(payload.user_id, withhold=minimized)
        # Budget is purpose-bound: it is not needed for chat, support, safety,
        # clarification, or wardrobe-only/high-urgency recommendations.
        if not shopping_allowed:
            profile["budget"] = "withheld"
        return {
            "persona": {
                "persona_id": "stylist",
                "role": "professional_fashion_stylist",
                "language": "zh-CN",
                "identity": "ai_team_member",
            },
            "profile": profile,
            "session": {
                "previous_mode": previous.conversation_mode if previous else None,
                "current_mode": mode,
                "pending_question_status": (
                    previous.pending_question_status if previous else "none"
                ),
                "turn_index": previous.turn_index + 1 if previous else 1,
                "scene": self._scene_summary(scene),
                "fit_context": (
                    fit_context.provider_summary()
                    if fit_context is not None and not minimized
                    else None
                ),
                "recommendation_summary": (
                    recommendation_summary if not minimized else None
                ),
            },
            "history": (
                []
                if minimized
                else [dict(item) for item in (previous.history[-6:] if previous else ())]
            ),
            "current_turn": current_turn,
            "policy": {
                "required_action": action,
                "required_control": control,
                "shopping_allowed": shopping_allowed,
                "recommendation_allowed": action == "recommend",
                "deadline_question_allowed": action == "clarify",
                "max_adjustments": 2,
            },
        }

    @staticmethod
    def _default_suggestions(
        mode: ConversationMode, action: DialogueAction
    ) -> list[str]:
        if mode == "task_closed":
            return []
        if action == "clarify":
            return ["今天", "明天", "时间还没定"]
        if mode in {"stylist_chat", "support_pause"}:
            return ["继续聊聊", "现在开始搭配", "结束本次任务"]
        if mode == "safety_response":
            return ["结束本次任务"]
        return ["先暂停推荐", "继续调整", "结束本次任务"]

    async def _cpa_reply(
        self,
        *,
        payload: DialogueTurnInput,
        previous: DialogueEnvelope | None,
        scene: SceneRequest | None,
        mode: ConversationMode,
        action: DialogueAction,
        control: str,
        safety_kind: str | None,
        close_or_closed: bool,
        sensitive: bool,
        current_text: str,
        fit_context: SessionFitContext | None,
        recommendation_summary: dict[str, object] | None,
    ) -> tuple[dict[str, Any] | None, DialogueProviderStatus, dict[str, Any]]:
        context = self._provider_context(
            payload=payload,
            previous=previous,
            scene=scene,
            mode=mode,
            action=action,
            control=control,
            safety_kind=safety_kind,
            close_or_closed=close_or_closed,
            sensitive=sensitive,
            current_text=current_text,
            fit_context=fit_context,
            recommendation_summary=recommendation_summary,
        )
        budget = self.provider.settings.effective_cpa_dialogue_budget_seconds
        started = time.perf_counter()
        diagnostic_reason_code: str | None = None
        advisory_diagnostic_reason_code: str | None = None
        try:
            generated, metadata = await asyncio.wait_for(
                self.provider.stylist_dialogue(context),
                timeout=budget,
            )
            response_status = DialogueProviderStatus(
                status="ok",
                attempted=True,
                requested_model="grok4.6",
                transport_model="grok-4.6-high",
                resolved_model=metadata.get("resolved_model"),
                model_verified=bool(metadata.get("model_verified")),
                degraded=False,
                generation_source="cpa",
            )
            return generated, response_status, {
                **metadata,
                "latency_ms": metadata.get(
                    "latency_ms",
                    round((time.perf_counter() - started) * 1000, 2),
                ),
                "interaction_budget_seconds": budget,
                "generation_source": response_status.generation_source,
            }
        except (TimeoutError, asyncio.TimeoutError):
            reason_code = "CPA_INTERACTION_BUDGET_EXCEEDED"
        except ProviderUnavailable as exc:
            reason_code = exc.reason_code
            diagnostic_reason_code = exc.diagnostic_reason_code
            advisory_diagnostic_reason_code = (
                exc.advisory_diagnostic_reason_code
            )
        response_status = DialogueProviderStatus(
            status="fallback",
            attempted=True,
            requested_model="grok4.6",
            transport_model="grok-4.6-high",
            resolved_model=None,
            model_verified=False,
            degraded=True,
            generation_source="local_fallback",
            reason_code=reason_code,
        )
        return None, response_status, {
            "attempted": True,
            "status": "fallback",
            "requested_model": self.provider.requested_model,
            "transport_model": self.provider.transport_model,
            "resolved_model": None,
            "model_verified": False,
            "degraded": True,
            "generation_source": "local_fallback",
            "reason_code": reason_code,
            "diagnostic_reason_code": diagnostic_reason_code,
            "advisory_diagnostic_reason_code": advisory_diagnostic_reason_code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "interaction_budget_seconds": budget,
            "input_contract": "stylist_dialogue_minimized_v1",
            "output_contract": "stylist_dialogue_structured_v1",
        }

    def _pending_status(
        self,
        scene: SceneRequest | None,
        mode: ConversationMode,
        previous: DialogueEnvelope | None,
    ) -> PendingQuestionStatus:
        if scene is None:
            return "none"
        prior = previous.pending_question_status if previous else "none"
        if mode in {"safety_response", "task_closed"}:
            return "cancelled" if prior != "none" or scene.event_horizon == "unknown" else "none"
        if mode == "support_pause":
            if scene.event_horizon == "unknown":
                return "suspended"
            return "resolved" if prior in {"active", "suspended"} else "none"
        if scene.event_horizon == "unknown":
            return "active"
        return "resolved" if prior in {"active", "suspended"} else "none"

    @staticmethod
    def _controlled_candidate_fingerprint(*parts: str | None) -> str:
        canonical = "\x1f".join("" if part is None else part for part in parts)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _create_controlled_memory_candidate(
        self,
        *,
        user_id: str,
        styling_session_id: str,
        dialogue_request_id: str,
        trace_id: str,
        canonical_value: str,
    ) -> PreferenceMemoryCandidateCard:
        request_id = f"prefcand_{dialogue_request_id}"
        fingerprint = self._controlled_candidate_fingerprint(
            user_id,
            styling_session_id,
            "stylist",
            "color_preference",
            canonical_value,
        )
        existing = self.memory_candidates.memory.repository.candidate_request(
            request_id
        )
        if existing is not None:
            if (
                existing["user_id"] != user_id
                or existing["fingerprint"] != fingerprint
            ):
                raise DialogueConflict("controlled preference candidate conflicts")
            stored = MemoryCandidateExtractResponse.model_validate(
                existing["response"]
            )
            if len(stored.candidates) != 1:
                raise DialogueConflict("controlled preference candidate is invalid")
            return PreferenceMemoryCandidateCard.model_validate(
                stored.candidates[0].model_dump(mode="json")
            )

        canonical = canonicalize_candidate(
            "color_preference", canonical_value
        )
        proposal_operation = (
            self.memory_candidates.memory.prepare_candidate_proposal(
                MemoryProposeInput(
                    user_id=user_id,
                    styling_session_id=styling_session_id,
                    namespace="stylist",
                    type=canonical.memory_type,
                    content=canonical.content,
                )
            )
        )
        proposal = proposal_operation.proposal
        if proposal.commit_blocked:
            self.traces.discard(proposal_operation.trace_id)
            raise DialogueConflict("controlled preference candidate was rejected")
        candidate_id = f"mcand_{uuid.uuid4().hex}"
        expires_at = (
            time.time()
            + self.scene_parser.settings.effective_dialogue_ttl_seconds
        )
        card = MemoryCandidateCard(
            candidate_id=candidate_id,
            confirmation_copy=canonical.confirmation_copy,
            confidence_band="high",
            conflict_copy=None,
            allowed_actions=("remember", "session_only", "reject", "rephrase"),
        )
        response = MemoryCandidateExtractResponse(
            request_id=request_id,
            status="ready",
            candidates=(card,),
            trace_id=trace_id,
        )
        state = {
            "candidate_id": candidate_id,
            "user_id": user_id,
            "styling_session_id": styling_session_id,
            "namespace": "stylist",
            "proposal_id": proposal.proposal_id,
            "canonical_kind": canonical.canonical_kind,
            "canonical_value": canonical.canonical_value,
            "confidence_band": "high",
            "expires_at": expires_at,
            "status": "active",
        }
        try:
            self.memory_candidates.memory.repository.put_candidate_batch(
                {
                    "request_id": request_id,
                    "user_id": user_id,
                    "fingerprint": fingerprint,
                    "expires_at": expires_at,
                    "response": response.model_dump(mode="json"),
                },
                [state],
                [proposal.model_dump(mode="json")],
            )
        except MemoryRepositoryError as exc:
            self.traces.discard(proposal_operation.trace_id)
            self.memory_candidates.memory.refresh_state()
            durable = self.memory_candidates.memory.repository.candidate_request(
                request_id
            )
            if (
                durable is None
                or durable["user_id"] != user_id
                or durable["fingerprint"] != fingerprint
            ):
                raise DialogueConflict(
                    "controlled preference candidate conflicts"
                ) from exc
            stored = MemoryCandidateExtractResponse.model_validate(
                durable["response"]
            )
            return PreferenceMemoryCandidateCard.model_validate(
                stored.candidates[0].model_dump(mode="json")
            )
        self.memory_candidates.memory.refresh_state()
        return PreferenceMemoryCandidateCard.model_validate(
            card.model_dump(mode="json")
        )

    async def turn(self, payload: DialogueTurnInput) -> DialogueTurnResponse:
        async with self._turn_lock:
            return await self._execute_turn(payload)

    async def _execute_turn(
        self, payload: DialogueTurnInput
    ) -> DialogueTurnResponse:
        request_id = payload.request_id or self._new_id("dlgreq")
        retry = self.state.retry(request_id, payload)
        if retry is not None:
            return retry

        previous = (
            self.state.session(payload.styling_session_id, payload.user_id)
            if payload.styling_session_id
            else None
        )
        preference_answer_supplied = payload.preference_question_id is not None
        if preference_answer_supplied and (
            previous is None
            or previous.pending_preference_question_id
            != payload.preference_question_id
            or previous.pending_preference_gap_code is None
        ):
            raise DialogueConflict(
                "preference question is not active for this dialogue session"
            )
        asked_preference_gap_codes = set(
            previous.asked_preference_gap_codes if previous else ()
        )
        pending_preference_question_id = (
            previous.pending_preference_question_id if previous else None
        )
        pending_preference_gap_code = (
            previous.pending_preference_gap_code if previous else None
        )
        preference_clarification: PreferenceClarification | None = None
        memory_candidate_cards: list[PreferenceMemoryCandidateCard] = []
        preference_trace: dict[str, object] = {
            "reason_code": "NOT_EVALUATED",
            "question_status": "none",
        }
        previous_mode = previous.conversation_mode if previous else None
        text = payload.message.strip()
        fit_candidate, fit_sanitized_text = self._fit_candidate(text)
        safety_kind = self._safety_kind(text)
        raw_sensitive = MemoryService.is_sensitive(text)
        non_fit_sensitive = MemoryService.is_sensitive(fit_sanitized_text)
        close_requested = self._contains(text, self._CLOSE_TERMS)
        pause_requested = self._contains(text, self._PAUSE_TERMS)
        resume_requested = self._contains(text, self._RESUME_TERMS)
        chat_requested = self._contains(text, self._CHAT_TERMS)
        helpless = bool(self._HELPLESS_PATTERN.search(text))
        mild_emotion = self._contains(text, self._MILD_EMOTION_TERMS)
        explicit_styling = self._contains(
            text, self._EXPLICIT_STYLING_TERMS
        ) or bool(SceneParser._detect_goals(text))
        ordinary_styling_task = bool(explicit_styling and not helpless)
        closed_already = previous_mode == "task_closed"
        context_update = self._context_update(text)
        _local_intent, local_intent_detected = SceneParser._detect_intent(text)
        occasion, occasion_detected = SceneParser._detect_occasion(text)
        fit_goal_task = bool(
            fit_candidate is not None
            and fit_candidate.silhouette_goal is not None
        )
        current_turn_task_evidence = bool(
            resume_requested
            or ordinary_styling_task
            or fit_goal_task
            or (occasion_detected and occasion not in {"daily", "home"})
            or (
                local_intent_detected
                and _local_intent in {"buy", "fill_gap", "browse"}
            )
        )
        transactional_intent = bool(
            local_intent_detected
            and _local_intent in {"buy", "fill_gap", "browse"}
        )
        force_recommend_intent = bool(
            fit_goal_task
            or (ordinary_styling_task and not transactional_intent)
        )
        task_evidence = bool(
            (previous and previous.scene is not None)
            or current_turn_task_evidence
            or helpless
        )
        # Keep fit data purpose-bound to an explicit styling signal or an
        # already-established task.  Conversation routing below uses the
        # broader authoritative task_evidence (including a concrete occasion),
        # as frozen by the S7 state priority contract.
        current_turn_styling_task = bool(
            resume_requested
            or ordinary_styling_task
            or fit_goal_task
            or (
                local_intent_detected
                and _local_intent in {"buy", "fill_gap", "browse"}
            )
        )
        fit_update_allowed = bool(
            fit_candidate is not None
            and (
                current_turn_styling_task
                or (previous and previous.scene is not None)
                or (occasion_detected and occasion not in {"daily", "home"})
            )
            and not non_fit_sensitive
            and safety_kind is None
            and not self._contains_direct_private(text)
            and not close_requested
            and not closed_already
        )
        fit_update = fit_candidate if fit_update_allowed else None
        if previous and previous.fit_context is not None:
            active_fit_context = previous.fit_context.merged(fit_update)
        elif fit_update is not None:
            active_fit_context = fit_update
        else:
            active_fit_context = None
        sensitive = (
            non_fit_sensitive
            if fit_update_allowed
            else bool(raw_sensitive or fit_candidate is not None)
        )
        current_text_for_provider = (
            fit_sanitized_text if fit_update_allowed else text
        )
        # Mild emotion and an actionable styling task coexist.  Mild emotion
        # alone cannot suspend the task; explicit control or the pre-existing
        # stronger helplessness flow still can.
        support_intent = pause_requested or helpless or (
            mild_emotion and not task_evidence
        )
        mixed_emotion_task = bool(
            mild_emotion
            and task_evidence
            and not pause_requested
            and not chat_requested
            and not helpless
        )
        horizon_evidence_text: str | None = None
        if resume_requested:
            horizon_evidence_text = text
            for term in sorted(self._RESUME_TERMS, key=len, reverse=True):
                horizon_evidence_text = horizon_evidence_text.replace(term, "")
        scene: SceneRequest | None
        preference_source_recommendation = None
        selected_preference_value: str | None = None
        preference_answer_neutral = False
        if preference_answer_supplied:
            assert previous is not None and previous.scene is not None
            assert pending_preference_question_id is not None
            assert pending_preference_gap_code is not None
            active_question = self.preference_policy.pending_clarification(
                gap_code=pending_preference_gap_code,
                question_id=pending_preference_question_id,
                urgency=previous.scene.urgency,
            )
            try:
                selected_preference_value = self.preference_policy.resolve_option(
                    active_question, str(payload.preference_option_id)
                )
            except ValueError as exc:
                raise DialogueConflict(
                    "preference option is not active for this question"
                ) from exc
            _prior_scene, preference_source_recommendation = (
                self.recommendations.get_saved_recommendation(
                    payload.user_id,
                    previous.styling_session_id,
                    previous.scene.request_id,
                )
            )
            session_id = previous.styling_session_id
            trace_id = self._new_id("trace")
            self.traces.start(
                trace_id=trace_id,
                request_id=request_id,
                styling_session_id=session_id,
                query_text=text,
                user_id=payload.user_id,
            )
            scene = previous.scene.model_copy(
                update={"request_id": request_id, "trace_id": trace_id},
                deep=True,
            )
            self.scene_parser.state.save(scene)
            preference_answer_neutral = selected_preference_value is None
            pending_preference_question_id = None
            pending_preference_gap_code = None
            preference_trace = {
                "gap_code": active_question.gap_code,
                "reason_code": (
                    "NEUTRAL_SESSION_DECISION"
                    if preference_answer_neutral
                    else "CONTROLLED_OPTION_SELECTED"
                ),
                "question_status": "resolved",
            }
        elif task_evidence:
            scene = await self.scene_parser.parse(
                SceneParseInput(
                    user_id=payload.user_id,
                    query_text=text,
                    request_id=request_id,
                    styling_session_id=(
                        payload.styling_session_id
                        or (previous.styling_session_id if previous else None)
                    ),
                    intent=(
                        "recommend"
                        if resume_requested or force_recommend_intent
                        else None
                    ),
                ),
                allow_provider=False,
                horizon_evidence_text=horizon_evidence_text,
                provider_deferred_to_dialogue=True,
            )
            session_id = scene.styling_session_id
            trace_id = scene.trace_id
        else:
            session_id = (
                previous.styling_session_id
                if previous
                else (payload.styling_session_id or self._new_id("session"))
            )
            trace_id = self._new_id("trace")
            self.traces.start(
                trace_id=trace_id,
                request_id=request_id,
                styling_session_id=session_id,
                query_text=text,
                user_id=payload.user_id,
            )
            scene = previous.scene.model_copy(deep=True) if previous and previous.scene else None

        if (
            scene is not None
            and scene.intent == "vent"
            and not (ordinary_styling_task or fit_goal_task)
        ):
            support_intent = True

        transition_reason: str
        support_stage: str | None = None
        concern_label: str | None = None
        support_strategy: str | None = None
        recommendation = None
        if closed_already:
            mode = "task_closed"
            action = "acknowledge"
            paused = True
            transition_reason = (
                "TASK_CLOSED_SAFETY_ACK" if safety_kind else "TASK_ALREADY_CLOSED"
            )
        elif safety_kind and close_requested:
            mode = "task_closed"
            action = "acknowledge"
            paused = True
            transition_reason = "SAFETY_RESPONSE_AND_USER_CLOSED_TASK"
        elif safety_kind:
            mode: ConversationMode = "safety_response"
            action: DialogueAction = "support"
            paused = True
            transition_reason = f"SAFETY_{safety_kind.upper()}"
        elif close_requested:
            mode = "task_closed"
            action = "acknowledge"
            paused = True
            transition_reason = "USER_CLOSED_TASK"
        elif scene is None:
            mode = "stylist_chat"
            action = "chat"
            paused = True
            transition_reason = (
                "USER_REQUESTED_STYLIST_CHAT"
                if chat_requested
                else "GENERAL_CONVERSATION_WITHOUT_STYLING_TASK"
            )
        elif pause_requested or support_intent or chat_requested:
            mode = "support_pause"
            action = "chat" if chat_requested else "support"
            paused = True
            transition_reason = (
                "USER_PAUSED_RECOMMENDATION"
                if pause_requested
                else (
                    "USER_REQUESTED_CHAT_WITHIN_STYLING_TASK"
                    if chat_requested
                    else "CURRENT_TURN_SUPPORT_INTENT"
                )
            )
        elif (
            previous_mode in {"stylist_chat", "support_pause", "safety_response"}
            and not resume_requested
            and (
                bool(previous and previous.explicit_pause_latched)
                or not current_turn_task_evidence
            )
        ):
            mode = "support_pause" if scene is not None else "stylist_chat"
            action = (
                "acknowledge"
                if context_update and scene is not None
                else (
                    "support"
                    if helpless
                    or mild_emotion
                    or self._concern_label(text) != "general"
                    else "chat"
                )
            )
            paused = True
            transition_reason = (
                "SUPPORT_CONTEXT_UPDATED"
                if context_update and scene is not None
                else "SUPPORT_CONTINUED"
            )
        else:
            mode = "styling_active"
            action = "recommend"
            paused = False
            transition_reason = (
                "USER_RESUMED_RECOMMENDATION"
                if resume_requested
                else "STYLING_ACTIVE"
            )

        pending_status = self._pending_status(scene, mode, previous)
        clarification_asked = previous.clarification_asked if previous else False
        if mode == "styling_active" and pending_status == "active":
            if not clarification_asked:
                action = "clarify"
                paused = True
                clarification_asked = True
                transition_reason = "PENDING_HORIZON_CLARIFICATION"
            else:
                # AC-03 is a hard upper bound: never repeat the same deadline
                # question. Continue fail-closed under unknown/high urgency.
                pending_status = "cancelled"
                action = "recommend"
                transition_reason = "CLARIFICATION_LIMIT_REACHED"

        if scene is not None and (mode != "styling_active" or action == "clarify"):
            scene = scene.model_copy(
                update={
                    "shopping_allowed": False,
                    "ui_capabilities": UICapabilities(shopping_cta=False),
                },
                deep=True,
            )
        control = (
            "close"
            if mode == "task_closed"
            else (
                "resume"
                if resume_requested and mode == "styling_active"
                else ("pause" if mode in {"support_pause", "safety_response"} else "none")
            )
        )
        recommendation_summary: dict[str, object] | None = None
        grounded_recommendation_message: str | None = None
        if (
            not preference_answer_supplied
            and pending_preference_question_id is not None
            and pending_preference_gap_code is not None
        ):
            preference_trace = {
                "gap_code": pending_preference_gap_code,
                "reason_code": "UNANSWERED_NEUTRAL_CONTINUE",
                "question_status": "resolved",
            }
            pending_preference_question_id = None
            pending_preference_gap_code = None
        if action == "recommend":
            if scene is None:
                raise DialogueConflict("recommendation requires an authoritative scene")
            if preference_answer_supplied:
                if preference_source_recommendation is None:
                    raise DialogueConflict(
                        "preference answer has no grounded recommendation"
                    )
                ordered_ids = self.preference_policy.reorder_ids(
                    preference_source_recommendation,
                    selected_preference_value,
                )
                recommendation = self.recommendations.reorder_validated_outfits(
                    recommendation=preference_source_recommendation,
                    ordered_outfit_ids=ordered_ids,
                    request_id=request_id,
                    trace_id=trace_id,
                )
                if not preference_answer_neutral:
                    assert selected_preference_value is not None
                    memory_candidate_cards = [
                        self._create_controlled_memory_candidate(
                            user_id=payload.user_id,
                            styling_session_id=session_id,
                            dialogue_request_id=request_id,
                            trace_id=trace_id,
                            canonical_value=selected_preference_value,
                        )
                    ]
                    self.memory_candidates.session_preferences.put(
                        SessionPreference(
                            user_id=payload.user_id,
                            styling_session_id=session_id,
                            canonical_kind="color_preference",
                            canonical_value=selected_preference_value,
                            expires_at=(
                                time.time()
                                + self.scene_parser.settings.effective_dialogue_ttl_seconds
                            ),
                        )
                    )
            else:
                requested_outfit_count = self._requested_outfit_count(text)
                recommendation = self.recommendations.recommend(scene)
                recommendation = self.recommendations.apply_requested_outfit_count(
                    scene, recommendation, requested_outfit_count
                )
                policy_asked_codes = set(asked_preference_gap_codes)
                if scene.urgency == "high" and clarification_asked:
                    policy_asked_codes.add("high:question_budget_spent")
                confirmed_signals = (
                    self.recommendations.memory.active_signals(payload.user_id)
                    if self.recommendations.memory is not None
                    else ()
                )
                session_preferences = (
                    self.memory_candidates.session_preferences.active(
                        payload.user_id, session_id, now=time.time()
                    )
                )
                preference_decision = self.preference_policy.evaluate(
                    scene=scene,
                    recommendation=recommendation,
                    confirmed_signals=confirmed_signals,
                    session_preferences=session_preferences,
                    asked_gap_codes=frozenset(policy_asked_codes),
                )
                preference_clarification = preference_decision.clarification
                preference_trace = {
                    "reason_code": preference_decision.reason_code,
                    "question_status": (
                        "open"
                        if preference_clarification is not None
                        else "none"
                    ),
                }
                if preference_clarification is not None:
                    preference_trace["gap_code"] = (
                        preference_clarification.gap_code
                    )
                    asked_preference_gap_codes.add(
                        preference_clarification.gap_code
                    )
                    pending_preference_question_id = (
                        preference_clarification.question_id
                    )
                    pending_preference_gap_code = (
                        preference_clarification.gap_code
                    )
            recommendation_summary = self._recommendation_summary(recommendation)
            grounded_recommendation_message = self._grounded_local_recommendation(
                recommendation
            )
        generated, provider_status, provider_trace = await self._cpa_reply(
            payload=payload,
            previous=previous,
            scene=scene,
            mode=mode,
            action=action,
            control=control,
            safety_kind=safety_kind,
            close_or_closed=bool(close_requested or closed_already),
            sensitive=sensitive,
            current_text=current_text_for_provider,
            fit_context=active_fit_context,
            recommendation_summary=recommendation_summary,
        )

        if generated is not None and action == "recommend" and recommendation is not None:
            semantic_rejection = self._recommendation_reply_rejection(
                str(generated["reply"]), previous, len(recommendation.outfits)
            )
            if semantic_rejection is not None:
                generated = None
                provider_status = DialogueProviderStatus(
                    status="fallback",
                    attempted=True,
                    requested_model="grok4.6",
                    transport_model="grok-4.6-high",
                    resolved_model=None,
                    model_verified=False,
                    degraded=True,
                    generation_source="local_fallback",
                    reason_code="CPA_DIALOGUE_OUTPUT_REJECTED",
                )
                provider_trace = {
                    **provider_trace,
                    "status": "fallback",
                    "resolved_model": None,
                    "model_verified": False,
                    "degraded": True,
                    "generation_source": "local_fallback",
                    "reason_code": "CPA_DIALOGUE_OUTPUT_REJECTED",
                    "diagnostic_reason_code": semantic_rejection,
                }

        if scene is not None:
            scene = self.scene_parser.state.replace_provider_backend(
                scene.request_id,
                payload.user_id,
                "grok4.6" if provider_status.status == "ok" else "rule_fallback",
            )
            if mode != "styling_active" or action == "clarify":
                scene = scene.model_copy(
                    update={
                        "shopping_allowed": False,
                        "ui_capabilities": UICapabilities(shopping_cta=False),
                    },
                    deep=True,
                )
        self.traces.update(trace_id, provider=provider_trace)
        if provider_status.degraded:
            self.traces.append_fallback(
                trace_id,
                {
                    "component": "dialogue_llm",
                    "fallback": "local_stylist_reply_v1",
                    "reason_code": provider_status.reason_code,
                },
            )

        if mode == "task_closed":
            local_message = (
                f"{self._safety_message(safety_kind)}本次任务仍保持结束状态。"
                if safety_kind
                else "本次任务已结束。我不会继续推荐穿搭或商品。"
            )
        elif safety_kind:
            local_message = self._safety_message(safety_kind)
        elif action == "clarify":
            local_message = "这次穿搭最晚什么时候需要准备好？"
        elif action == "acknowledge":
            local_message = (
                f"我记住了：时间更新为{self._time_label(text)}。"
                "穿搭建议仍保持暂停；等你明确说“现在开始搭配”，我再继续。"
                if context_update and mode == "support_pause"
                else "我会保持暂停，不继续给出穿搭或商品建议。"
            )
        elif action == "support":
            emotion_label = (
                "overwhelmed" if helpless else ("nervous" if mild_emotion else "unspecified")
            )
            concern_label = self._concern_label(text)
            support_stage = self._support_stage(previous)
            support_strategy = self._support_strategy(
                concern_label,
                support_stage,
                "pause" if pause_requested else "support",
                previous,
            )
            local_message = self._local_support_message(
                emotion_label, concern_label, support_strategy
            )
        elif action == "chat":
            local_message = (
                self._local_support_message(
                    "unspecified", "general", "pause_confirm"
                )
                if pause_requested
                else (
                    "我可以继续听你说。现在不会启动推荐、购物或追问穿搭截止时间；"
                    "等你想谈具体场合或衣服时再告诉我。"
                )
            )
        else:
            if recommendation is None or grounded_recommendation_message is None:
                raise DialogueConflict("recommendation requires an authoritative scene")
            local_message = grounded_recommendation_message

        if mixed_emotion_task and action in {"clarify", "recommend"}:
            local_message = self._acknowledge_task_emotion_once(
                text, local_message
            )

        if generated is not None:
            assistant_message = str(generated["reply"])
            provider_suggestions = [
                str(item)
                for item in generated.get("suggested_replies", [])
                if isinstance(item, str)
                and not (
                    not (scene and scene.shopping_allowed)
                    and any(term in item for term in ("购买", "下单", "加购", "商品"))
                )
                and not (
                    action in {"chat", "support"}
                    and any(term in item for term in ("什么时候", "哪天", "截止时间"))
                )
            ][:3]
        else:
            assistant_message = local_message
            provider_suggestions = []
        suggested_replies = (
            provider_suggestions
            or self._default_suggestions(mode, action)
        )
        if preference_clarification is not None:
            assistant_message = (
                assistant_message.rstrip()
                + " "
                + self.preference_policy.question_copy(
                    preference_clarification
                )
            )[:600]
            suggested_replies = [
                option.label for option in preference_clarification.options
            ][:3]

        current_mode = mode
        dialogue_trace: dict[str, object] = {
            "previous_mode": previous_mode,
            "current_mode": current_mode,
            "transition_reason_code": transition_reason,
            "pending_question_status": pending_status,
            "recommendation_paused": paused,
            "provider_status": provider_status.status,
            "provider_generation_source": provider_status.generation_source,
            "preference_uncertainty": preference_trace,
        }
        self.traces.update(trace_id, dialogue=dialogue_trace)

        if self._contains_direct_private(text):
            user_history_text = "[private_content_omitted]"
        elif sensitive:
            user_history_text = "[sensitive_content_omitted]"
        elif safety_kind:
            user_history_text = f"[{safety_kind}_safety_content_omitted]"
        elif close_requested or closed_already:
            user_history_text = "[task_control_omitted]"
        else:
            user_history_text = self._provider_safe_text(current_text_for_provider)
        _, assistant_history_text = self._fit_candidate(assistant_message[:600])
        assistant_history_text = (
            "[sensitive_response_omitted]"
            if sensitive
            else assistant_history_text
        )
        history = tuple(
            (
                *((previous.history if previous else ())),
                {"role": "user", "content": user_history_text},
                {"role": "assistant", "content": assistant_history_text},
            )[-12:]
        )
        turn_index = previous.turn_index + 1 if previous else 1

        response = DialogueTurnResponse(
            turn_id=self._new_id("turn"),
            turn_index=turn_index,
            history_version=turn_index,
            request_id=request_id,
            styling_session_id=session_id,
            trace_id=trace_id,
            conversation_mode=current_mode,
            action=action,
            assistant_message=assistant_message,
            suggested_replies=suggested_replies,
            recommendation_paused=paused,
            pending_question_status=pending_status,
            provider=provider_status,
            scene=scene,
            recommendation=recommendation,
            preference_clarification=preference_clarification,
            memory_candidates=memory_candidate_cards,
        )
        envelope = DialogueEnvelope(
            user_id=payload.user_id,
            styling_session_id=session_id,
            scene=scene,
            conversation_mode=current_mode,
            recommendation_paused=paused,
            pending_question_status=pending_status,
            clarification_asked=clarification_asked,
            support_turn_count=(
                (previous.support_turn_count if previous else 0)
                + (1 if action == "support" else 0)
            ),
            last_support_strategy=(
                support_strategy
                if support_strategy is not None
                else (previous.last_support_strategy if previous else None)
            ),
            explicit_pause_latched=(
                False
                if resume_requested
                else (
                    True
                    if pause_requested or (chat_requested and scene is not None)
                    else (
                        previous.explicit_pause_latched
                        if previous is not None
                        else False
                    )
                )
            ),
            fit_context=active_fit_context,
            asked_preference_gap_codes=frozenset(
                asked_preference_gap_codes
            ),
            pending_preference_question_id=pending_preference_question_id,
            pending_preference_gap_code=pending_preference_gap_code,
            history=history,
            turn_index=turn_index,
            expires_at=0.0,
        )
        return self.state.commit(envelope, request_id, payload, response)
