from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections import Counter
from typing import Any

import httpx

from .config import Settings


LOGICAL_GROK_MODEL = "grok4.6"
CPA_TRANSPORT_MODEL = "grok-4.6-high"
CPA_REPORTED_MODELS = frozenset({"grok-4.6-high", "grok-4.6-build"})


class ProviderUnavailable(RuntimeError):
    def __init__(
        self,
        message: str = "provider unavailable",
        *,
        reason_code: str = "CPA_PROVIDER_UNAVAILABLE",
        diagnostic_reason_code: str | None = None,
        advisory_diagnostic_reason_code: str | None = None,
        opens_circuit: bool = True,
    ):
        super().__init__(message)
        self.reason_code = reason_code
        self.diagnostic_reason_code = diagnostic_reason_code
        self.advisory_diagnostic_reason_code = advisory_diagnostic_reason_code
        self.opens_circuit = opens_circuit


class DialogueOutputRejected(ValueError):
    """Safe, content-free classification for one rejected model response."""

    def __init__(self, diagnostic_reason_code: str):
        super().__init__(diagnostic_reason_code)
        self.diagnostic_reason_code = diagnostic_reason_code


class GrokLLMProvider:
    """CPA-only OpenAI-compatible adapter for the frozen logical model grok4.6."""

    _TRANSPORT_MODEL_BY_LOGICAL = {LOGICAL_GROK_MODEL: CPA_TRANSPORT_MODEL}
    # CPA may report a concrete, explicitly reviewed build identifier while
    # accepting the stable transport alias.  Never accept arbitrary prefixes.
    _REPORTED_MODEL_ALLOWLIST = {CPA_TRANSPORT_MODEL: CPA_REPORTED_MODELS}
    _MAX_DIALOGUE_RESPONSE_BYTES = 64 * 1024
    # R1 has no 3D/360/video/dynamic path.  Model output mentioning one of
    # these modes is never necessary to satisfy a server-authorized action;
    # fail closed rather than attempting to infer whether it is a promise.
    _PROHIBITED_VISUAL_PROMISE = re.compile(
        r"(?i)(?:3d|三维|360(?:度)?|视频|动态|虚拟试穿)"
    )
    _PROHIBITED_PERSON_OR_MEDICAL = re.compile(
        r"(?i)(?:(?:你|本人).{0,10}(?:太胖|肥胖|很胖|太丑|很丑|难看|显老|"
        r"身材.{0,3}(?:差|不好))|(?:遮住|掩盖).{0,12}(?:身材|缺点|赘肉|胖)|"
        r"(?:你|这是|症状).{0,10}(?:患有|得了|确诊|是.{0,4}(?:病|症))|"
        r"(?:穿搭|衣服).{0,10}(?:治疗|治好|治愈|缓解症状|缓解胸闷))"
    )
    _PROHIBITED_SHOPPING_WHEN_BLOCKED = re.compile(
        r"(?:去买|购买|下单|加购|购物车|入手|同款|打开目录|打开商品|"
        r"看看商品|商品推荐|商品链接|购物链接|补购|淘宝|京东|搜同款|"
        r"逛商场|挑一双)"
    )
    _PROHIBITED_MEDICAL_ADVICE = re.compile(
        r"(?:吃药|服药|服用|用药|剂量|布洛芬|阿司匹林|对乙酰氨基酚|处方药)"
    )
    _PROHIBITED_PERSON_EVALUATION = re.compile(
        r"(?i)(?:(?:你|您|本人|用户)(?:的)?.{0,6}"
        r"(?:颜值|身材|体重|年龄|性吸引力).{0,12}"
        r"(?:很好|很差|好|不错|优秀|完美|正常|不正常|差|不好|胖|瘦|"
        r"高|低|老|年轻|大|小|性感|有吸引力|没吸引力)|"
        r"(?:你|您|本人).{0,8}(?:看起来|显得|是|很|太|比较|不够|没有)?"
        r".{0,4}(?:漂亮|好看|难看|丑|胖|瘦|老|年轻|性感|有魅力|没魅力)|"
        r"(?:你|您|本人)(?:的)?.{0,5}(?:身材比例|比例).{0,8}"
        r"(?:很好|很差|好|差|不错|优秀|完美|标准|正常|不正常)|"
        r"(?:你|您|本人).{0,6}(?:是|属于|拥有|有)?\s*"
        r"(?:标准身材|完美身材|黄金比例))"
    )
    _PROHIBITED_DIAGNOSIS = re.compile(
        r"(?i)(?:(?:你|您|本人|这|那|症状)?.{0,6}"
        r"(?:没有|没得|不是|并非|患有|得了|确诊(?:为)?|就是|属于|是)"
        r".{0,6}(?:焦虑症|抑郁症|精神疾病|心理疾病|疾病|病症)|"
        r"(?:焦虑症|抑郁症|精神疾病|心理疾病|疾病|病症).{0,6}"
        r"(?:没有|不存在|不是|并非|排除|确诊|成立))"
    )

    def __init__(self, settings: Settings):
        self.settings = settings
        self.requested_model = settings.grok_model
        if self.requested_model != LOGICAL_GROK_MODEL:
            raise ValueError("logical text model must match the frozen CPA contract")
        # Explicit user-approved CPA alias mapping; never auto-select another model.
        self.transport_model = self._TRANSPORT_MODEL_BY_LOGICAL[self.requested_model]
        self.resolved_model: str | None = None
        self._chat_attempted = False
        self._chat_model_verified = False
        self._last_chat_verification_error: str | None = None
        self._unavailable_until = 0.0
        self._health_cache: tuple[float, dict[str, Any]] | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.cpa_api_key:
            headers["Authorization"] = f"Bearer {self.settings.cpa_api_key}"
        return headers

    @staticmethod
    def _fit_value_echo_patterns(
        fit_context: dict[str, Any] | None,
    ) -> tuple[re.Pattern[str], ...]:
        if fit_context is None:
            return ()
        patterns: list[re.Pattern[str]] = []
        for field, label, units in (
            ("height_cm", "身高", r"(?:cm|厘米|公分)"),
            ("weight_kg", "体重", r"(?:kg|公斤|千克)"),
        ):
            value = fit_context.get(field)
            if value is None or isinstance(value, bool):
                continue
            numeric = float(value)
            rendered = format(numeric, ".10g")
            value_pattern = re.escape(rendered)
            if numeric.is_integer():
                value_pattern = rf"{re.escape(str(int(numeric)))}(?:\.0+)?"
            patterns.append(
                re.compile(
                    rf"(?i)(?:(?:{label})\s*[:：是为]?\s*"
                    rf"{value_pattern}(?:\s*{units})?|"
                    rf"(?<![\d.]){value_pattern}\s*{units})(?![a-z\d.])"
                )
            )
        return tuple(patterns)

    def _verify_reported_model(self, reported_model: Any, operation: str) -> str:
        allowed = self._REPORTED_MODEL_ALLOWLIST[self.transport_model]
        if not isinstance(reported_model, str) or reported_model not in allowed:
            raise ProviderUnavailable(
                f"CPA {operation} response model is not allowlisted",
                reason_code="CPA_MODEL_VERIFICATION_FAILED",
            )
        self.resolved_model = reported_model
        self._chat_attempted = True
        self._chat_model_verified = True
        self._last_chat_verification_error = None
        self._unavailable_until = 0.0
        self._health_cache = None
        return reported_model

    def mark_interaction_failure(
        self, reason_code: str, *, open_circuit: bool = True
    ) -> None:
        """Record an outer-budget failure without retaining provider text."""
        self._chat_attempted = True
        self._chat_model_verified = False
        self._last_chat_verification_error = reason_code
        self.resolved_model = None
        self._health_cache = None
        if open_circuit:
            self._unavailable_until = time.monotonic() + 30

    def mark_dialogue_output_rejection(self) -> None:
        """Record a single bad completion without poisoning later turns."""
        self._chat_attempted = True
        # A model echo may already have established exact routing before prose
        # validation failed.  Do not turn a per-response formatting/safety
        # failure into provider unavailability or open the transport breaker.
        self._unavailable_until = 0.0

    async def health(self) -> dict[str, Any]:
        if not self.settings.cpa_text_enabled:
            return {
                "enabled": False,
                "available": False,
                "requested_model": self.requested_model,
                "transport_model": self.transport_model,
                "resolved_model": self.resolved_model,
                "catalog_model_advertised": False,
                "chat_model_verified": False,
                "endpoint": self.settings.cpa_base_url,
                "reason": "disabled",
            }
        now = time.monotonic()
        if self._health_cache and now - self._health_cache[0] < 10:
            return dict(self._health_cache[1])
        started = time.perf_counter()
        interaction_budget = self.settings.effective_cpa_health_budget_seconds
        result: dict[str, Any]
        try:
            async with httpx.AsyncClient(
                timeout=min(self.settings.cpa_timeout_seconds, interaction_budget),
                trust_env=False,
            ) as client:
                response = await asyncio.wait_for(
                    client.get(
                        f"{self.settings.cpa_base_url}/models", headers=self._headers()
                    ),
                    timeout=interaction_budget,
                )
                response.raise_for_status()
                payload = response.json()
                model_ids = [
                    str(item.get("id"))
                    for item in payload.get("data", [])
                    if isinstance(item, dict) and item.get("id")
                ]
            # The catalog must advertise the exact client-facing transport ID.
            # Concrete build IDs are valid response echoes only; they are not
            # substitutes for the alias the application is frozen to request.
            advertised = self.transport_model in model_ids
            # `/models` is only an advertisement. It cannot prove that the
            # proxy actually routed a completion to the frozen transport model.
            # Resolution/availability therefore require an observed, exact
            # model echo from a successful chat response.
            chat_verified = self._chat_attempted and self._chat_model_verified
            available = (
                advertised
                and chat_verified
                and self._last_chat_verification_error is None
            )
            if not chat_verified:
                self.resolved_model = None
            result = {
                "enabled": True,
                "available": available,
                "requested_model": self.requested_model,
                "transport_model": self.transport_model,
                "resolved_model": self.resolved_model,
                "catalog_model_advertised": advertised,
                "chat_model_verified": chat_verified,
                "endpoint": self.settings.cpa_base_url,
                "interaction_budget_seconds": interaction_budget,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "reason": (
                    self._last_chat_verification_error
                    if self._last_chat_verification_error
                    else (
                        "resolved_model_not_advertised"
                        if not advertised
                        else (None if chat_verified else "chat_model_unverified")
                    )
                ),
            }
        except (TimeoutError, asyncio.TimeoutError):
            chat_verified = self._chat_attempted and self._chat_model_verified
            if not chat_verified:
                self.resolved_model = None
            result = {
                "enabled": True,
                "available": False,
                "requested_model": self.requested_model,
                "transport_model": self.transport_model,
                "resolved_model": self.resolved_model,
                "catalog_model_advertised": False,
                "chat_model_verified": chat_verified,
                "endpoint": self.settings.cpa_base_url,
                "interaction_budget_seconds": interaction_budget,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "reason": "CPA_HEALTH_BUDGET_EXCEEDED",
            }
        except Exception as exc:  # network and protocol failures are degradable
            chat_verified = self._chat_attempted and self._chat_model_verified
            if not chat_verified:
                self.resolved_model = None
            result = {
                "enabled": True,
                "available": False,
                "requested_model": self.requested_model,
                "transport_model": self.transport_model,
                "resolved_model": self.resolved_model,
                "catalog_model_advertised": False,
                "chat_model_verified": chat_verified,
                "endpoint": self.settings.cpa_base_url,
                "interaction_budget_seconds": interaction_budget,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "reason": type(exc).__name__,
            }
        self._health_cache = (now, result)
        return dict(result)

    async def parse_scene_advisory(self, query_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.settings.cpa_text_enabled:
            raise ProviderUnavailable(
                "CPA text provider is disabled",
                reason_code="CPA_PROVIDER_DISABLED",
            )
        now = time.monotonic()
        if now < self._unavailable_until:
            raise ProviderUnavailable(
                "CPA circuit breaker is open",
                reason_code="CPA_CIRCUIT_OPEN",
            )

        system_prompt = (
            "你是穿搭场景结构化解析器，只提取用户明说或可稳健推断的字段。"
            "输出 JSON，键仅限 intent, occasion, goals。"
            "intent 仅可为 recommend/buy/fill_gap/browse/vent；occasion 仅可为 "
            "daily/commute/interview/meeting/date/party/travel/outdoor/home/sports；"
            "goals 仅可为 reliable/modern/comfortable/polished/low_key/confident/cool。"
            "不要生成衣物或商品 ID，不决定 urgency、shopping_allowed 或任何硬约束。"
        )
        payload = {
            "model": self.transport_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query_text},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.cpa_timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"{self.settings.cpa_base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
            self._verify_reported_model(body.get("model"), "scene")
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("provider response is not a JSON object")
            metadata = {
                "attempted": True,
                "status": "ok",
                "requested_model": self.requested_model,
                "resolved_model": self.resolved_model,
                "model_verified": True,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }
            return parsed, metadata
        except ProviderUnavailable as exc:
            self.mark_interaction_failure(exc.reason_code)
            raise
        except Exception as exc:
            if isinstance(exc, httpx.TimeoutException):
                reason_code = "CPA_PROVIDER_TIMEOUT"
            elif isinstance(
                exc, (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError)
            ):
                reason_code = "CPA_PROVIDER_INVALID_RESPONSE"
            else:
                reason_code = "CPA_PROVIDER_UNAVAILABLE"
            self.mark_interaction_failure(reason_code)
            raise ProviderUnavailable(
                type(exc).__name__, reason_code=reason_code
            ) from exc

    async def stylist_dialogue(
        self,
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Generate the actual Stylist reply plus closed control fields.

        The caller constructs a bounded, privacy-filtered context.  This
        adapter validates both that input contract and the model output before
        any prose is exposed to the user.
        """

        if not self.settings.cpa_text_enabled:
            raise ProviderUnavailable(
                "CPA text provider is disabled",
                reason_code="CPA_PROVIDER_DISABLED",
            )
        if time.monotonic() < self._unavailable_until:
            raise ProviderUnavailable(
                "CPA circuit breaker is open",
                reason_code="CPA_CIRCUIT_OPEN",
            )
        if set(context) != {
            "persona",
            "profile",
            "session",
            "history",
            "current_turn",
            "policy",
        }:
            raise ProviderUnavailable(
                "dialogue context contract rejected",
                reason_code="CPA_DIALOGUE_INPUT_REJECTED",
            )
        profile = context.get("profile")
        if not isinstance(profile, dict) or set(profile) != {
            "styles",
            "favorite_colors",
            "avoid_colors",
            "common_occasions",
            "goals",
            "budget",
            "confirmed_memory_signals",
        }:
            raise ProviderUnavailable(
                "dialogue profile contract rejected",
                reason_code="CPA_DIALOGUE_INPUT_REJECTED",
            )
        if any(
            key != "budget"
            and (
                not isinstance(value, list)
                or len(value) > 8
                or any(not isinstance(item, str) or len(item) > 80 for item in value)
            )
            for key, value in profile.items()
        ) or not isinstance(profile.get("budget"), str):
            raise ProviderUnavailable(
                "dialogue profile values rejected",
                reason_code="CPA_DIALOGUE_INPUT_REJECTED",
            )
        persona = context.get("persona")
        if not isinstance(persona, dict) or persona != {
            "persona_id": "stylist",
            "role": "professional_fashion_stylist",
            "language": "zh-CN",
            "identity": "ai_team_member",
        }:
            raise ProviderUnavailable(
                "dialogue persona contract rejected",
                reason_code="CPA_DIALOGUE_INPUT_REJECTED",
            )
        session = context.get("session")
        if not isinstance(session, dict) or set(session) != {
            "previous_mode",
            "current_mode",
            "pending_question_status",
            "turn_index",
            "scene",
            "fit_context",
            "recommendation_summary",
        }:
            raise ProviderUnavailable(
                "dialogue session contract rejected",
                reason_code="CPA_DIALOGUE_INPUT_REJECTED",
            )
        fit_context = session.get("fit_context")
        if fit_context is not None:
            if (
                not isinstance(fit_context, dict)
                or set(fit_context) != {
                    "purpose",
                    "height_cm",
                    "weight_kg",
                    "silhouette_goal",
                }
                or fit_context.get("purpose") != "garment_fit_only"
                or fit_context.get("silhouette_goal")
                not in {None, "add_garment_volume"}
            ):
                raise ProviderUnavailable(
                    "dialogue fit context rejected",
                    reason_code="CPA_DIALOGUE_INPUT_REJECTED",
                )
            height = fit_context.get("height_cm")
            weight = fit_context.get("weight_kg")
            if (
                height is not None
                and (
                    isinstance(height, bool)
                    or not isinstance(height, (int, float))
                    or not 120 <= float(height) <= 230
                )
            ) or (
                weight is not None
                and (
                    isinstance(weight, bool)
                    or not isinstance(weight, (int, float))
                    or not 30 <= float(weight) <= 250
                )
            ):
                raise ProviderUnavailable(
                    "dialogue fit context values rejected",
                    reason_code="CPA_DIALOGUE_INPUT_REJECTED",
                )
        recommendation_summary = session.get("recommendation_summary")
        if recommendation_summary is not None:
            valid_summary = (
                isinstance(recommendation_summary, dict)
                and set(recommendation_summary) == {
                    "source",
                    "requested_outfit_count",
                    "actual_outfit_count",
                    "outfits",
                    "gap_explanation",
                }
                and recommendation_summary.get("source")
                == "current_owner_wardrobe_validated"
                and recommendation_summary.get("requested_outfit_count")
                in {None, 1, 2, 3}
                and isinstance(recommendation_summary.get("actual_outfit_count"), int)
                and 0 <= recommendation_summary["actual_outfit_count"] <= 3
                and isinstance(recommendation_summary.get("outfits"), list)
                and len(recommendation_summary["outfits"])
                == recommendation_summary["actual_outfit_count"]
                and (
                    recommendation_summary.get("gap_explanation") is None
                    or (
                        isinstance(recommendation_summary["gap_explanation"], str)
                        and len(recommendation_summary["gap_explanation"]) <= 180
                    )
                )
            )
            if valid_summary:
                for outfit in recommendation_summary["outfits"]:
                    valid_summary = bool(
                        isinstance(outfit, dict)
                        and set(outfit) == {"strategy_label", "pieces", "reasons"}
                        and isinstance(outfit.get("strategy_label"), str)
                        and len(outfit["strategy_label"]) <= 60
                        and isinstance(outfit.get("pieces"), list)
                        and 1 <= len(outfit["pieces"]) <= 7
                        and all(
                            isinstance(piece, dict)
                            and set(piece) == {"slot", "name", "color"}
                            and all(isinstance(piece[key], str) for key in piece)
                            and len(piece["name"]) <= 60
                            for piece in outfit["pieces"]
                        )
                        and isinstance(outfit.get("reasons"), list)
                        and len(outfit["reasons"]) <= 2
                        and all(
                            isinstance(reason, str) and len(reason) <= 100
                            for reason in outfit["reasons"]
                        )
                    )
                    if not valid_summary:
                        break
            if not valid_summary:
                raise ProviderUnavailable(
                    "dialogue recommendation summary rejected",
                    reason_code="CPA_DIALOGUE_INPUT_REJECTED",
                )
        current_turn = context.get("current_turn")
        if (
            not isinstance(current_turn, dict)
            or set(current_turn) != {"kind", "content"}
            or current_turn.get("kind")
            not in {
                "normal",
                "safety_summary",
                "sensitive_summary",
                "control_summary",
                "privacy_summary",
            }
            or not isinstance(current_turn.get("content"), str)
            or len(current_turn["content"]) > 600
        ):
            raise ProviderUnavailable(
                "dialogue current turn contract rejected",
                reason_code="CPA_DIALOGUE_INPUT_REJECTED",
            )
        history = context.get("history")
        if (
            not isinstance(history, list)
            or len(history) > 6
            or any(
                not isinstance(item, dict)
                or set(item) != {"role", "content"}
                or item.get("role") not in {"user", "assistant"}
                or not isinstance(item.get("content"), str)
                or len(item["content"]) > 600
                for item in history
            )
        ):
            raise ProviderUnavailable(
                "dialogue history contract rejected",
                reason_code="CPA_DIALOGUE_INPUT_REJECTED",
            )
        policy = context.get("policy")
        if not isinstance(policy, dict) or set(policy) != {
            "required_action",
            "required_control",
            "shopping_allowed",
            "recommendation_allowed",
            "deadline_question_allowed",
            "max_adjustments",
        }:
            raise ProviderUnavailable(
                "dialogue policy contract rejected",
                reason_code="CPA_DIALOGUE_INPUT_REJECTED",
            )
        if (
            policy.get("required_action")
            not in {"chat", "support", "clarify", "recommend", "acknowledge"}
            or policy.get("required_control")
            not in {"none", "pause", "resume", "close"}
            or any(
                not isinstance(policy.get(key), bool)
                for key in (
                    "shopping_allowed",
                    "recommendation_allowed",
                    "deadline_question_allowed",
                )
            )
            or policy.get("max_adjustments") != 2
        ):
            raise ProviderUnavailable(
                "dialogue policy values rejected",
                reason_code="CPA_DIALOGUE_INPUT_REJECTED",
            )
        serialized_context = json.dumps(context, ensure_ascii=False)
        if len(serialized_context) > 12000 or re.search(
            r"(?i)(?:[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|"
            r"(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)\d{17}[\dx](?!\d))",
            serialized_context,
        ):
            raise ProviderUnavailable(
                "dialogue context contains unbounded or private data",
                reason_code="CPA_DIALOGUE_INPUT_REJECTED",
            )

        system_prompt = (
            "你是 ProfAgent 私人团队中的专业服装造型师 Stylist，是 AI 专业成员，"
            "不得冒充真人、明星本人或声称现实从业经历。你要用自然、温暖、具体的中文"
            "持续对话，结合已授权的最小画像与本会话上下文回应；不要使用固定客服模板。"
            "只评价当前穿搭与当前目标，不评价人的颜值、身体、年龄、性吸引力或价值；"
            "不作心理/医疗诊断。不得编造衣物、商品或 ID，也不得自行写入长期记忆。"
            "服务器给出的 required_action、required_control 和购物门控是不可覆盖的硬规则。"
            "当当前回合同时包含轻度紧张、不安、担心冷场等轻情绪，以及明确场合、穿衣问题、"
            "造型目标或任务内版型信息时，"
            "reply 先用一句话自然承接情绪，"
            "随后立即提供 required_action 对应的专业穿搭帮助，不要自行暂停任务。"
            "session.fit_context 仅用于衣物版型、比例、层次与舒适度；不得复述测量原值，"
            "不得据此评价人的胖瘦、好坏或健康，也不得把它写入画像或长期记忆。"
            "当 session.recommendation_summary 非空时，它是已经从当前用户衣橱完成硬过滤、"
            "检索、拼套和最终验证的唯一权威推荐。reply 必须直接回应当前增量请求，"
            "按 actual_outfit_count 描述这些具体方向；不得改变套数或衣物集合，不得要求"
            "用户重新列出手头衣服，也不要大段复述上一轮。"
            "输出 JSON，且只能包含 reply, action, control, scene_advisory, suggested_replies。"
            "action 仅为 chat/support/clarify/recommend/acknowledge；control 仅为"
            "none/pause/resume/close；scene_advisory 为 null 或仅含 intent/occasion/goals。"
            "如果不能确定 scene_advisory 的闭集值，必须返回 null，不要创造新枚举。"
        )
        request_body = {
            "model": self.transport_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                },
            ],
            "temperature": 0.35,
            "max_tokens": 800,
            "response_format": {"type": "json_object"},
        }
        started = time.perf_counter()
        advisory_diagnostic_reason_code: str | None = None
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.cpa_timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"{self.settings.cpa_base_url}/chat/completions",
                    headers=self._headers(),
                    json=request_body,
                )
                response.raise_for_status()
                if len(response.content) > self._MAX_DIALOGUE_RESPONSE_BYTES:
                    raise DialogueOutputRejected(
                        "CPA_DIALOGUE_ENVELOPE_TOO_LARGE"
                    )
                try:
                    body = response.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    raise DialogueOutputRejected(
                        "CPA_DIALOGUE_ENVELOPE_JSON_INVALID"
                    ) from exc
            if not isinstance(body, dict):
                raise DialogueOutputRejected(
                    "CPA_DIALOGUE_ENVELOPE_SCHEMA_INVALID"
                )
            reported_model = self._verify_reported_model(
                body.get("model"), "dialogue"
            )
            try:
                content = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise DialogueOutputRejected(
                    "CPA_DIALOGUE_ENVELOPE_SCHEMA_INVALID"
                ) from exc
            if not isinstance(content, str):
                raise DialogueOutputRejected("CPA_DIALOGUE_CONTENT_TYPE_INVALID")
            candidate = content.strip()
            if candidate.startswith("```"):
                # Compatibility is intentionally narrow: unwrap only when the
                # whole content is exactly one json/JSON fence.  Never search
                # prose for an embedded object.
                fenced = re.fullmatch(
                    r"```(?:json|JSON)[ \t]*\r?\n(?P<payload>.*?)\r?\n```",
                    candidate,
                    flags=re.DOTALL,
                )
                if fenced is None:
                    raise DialogueOutputRejected(
                        "CPA_DIALOGUE_CONTENT_FENCE_INVALID"
                    )
                candidate = fenced.group("payload").strip()
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError as exc:
                raise DialogueOutputRejected(
                    "CPA_DIALOGUE_CONTENT_JSON_INVALID"
                ) from exc
            expected_fields = {
                "reply",
                "action",
                "control",
                "scene_advisory",
                "suggested_replies",
            }
            if not isinstance(parsed, dict) or set(parsed) != expected_fields:
                raise DialogueOutputRejected(
                    "CPA_DIALOGUE_FIELDS_INVALID"
                )
            reply = parsed.get("reply")
            if not isinstance(reply, str) or not reply.strip() or len(reply) > 600:
                raise DialogueOutputRejected("CPA_DIALOGUE_REPLY_INVALID")
            if parsed.get("action") != policy["required_action"]:
                raise DialogueOutputRejected("CPA_DIALOGUE_ACTION_CONFLICT")
            if parsed.get("control") != policy["required_control"]:
                raise DialogueOutputRejected("CPA_DIALOGUE_CONTROL_CONFLICT")
            suggestions = parsed.get("suggested_replies")
            if (
                not isinstance(suggestions, list)
                or len(suggestions) > 3
                or any(
                    not isinstance(item, str)
                    or not item.strip()
                    or len(item) > 40
                    for item in suggestions
                )
            ):
                raise DialogueOutputRejected(
                    "CPA_DIALOGUE_SUGGESTIONS_INVALID"
                )
            advisory = parsed.get("scene_advisory")
            if advisory is not None:
                advisory_valid = (
                    isinstance(advisory, dict)
                    and set(advisory) == {"intent", "occasion", "goals"}
                    and advisory.get("intent")
                    in {"recommend", "buy", "fill_gap", "browse", "vent"}
                    and advisory.get("occasion")
                    in {
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
                    }
                    and isinstance(advisory.get("goals"), list)
                    and len(advisory["goals"]) <= 3
                    and all(
                        goal
                        in {
                            "reliable",
                            "modern",
                            "comfortable",
                            "polished",
                            "low_key",
                            "confident",
                            "cool",
                        }
                        for goal in advisory["goals"]
                    )
                )
                if not advisory_valid:
                    # Advisory never has authority over the local scene parser.
                    # Drop malformed hints without hiding an otherwise safe,
                    # contract-valid reply, but retain a content-free diagnostic.
                    advisory = None
                    advisory_diagnostic_reason_code = (
                        "CPA_DIALOGUE_SCENE_ADVISORY_DROPPED"
                    )
            if not policy["deadline_question_allowed"] and re.search(
                r"(?:什么时候|哪天|截止时间|最晚.*(?:准备|需要))", reply
            ):
                raise DialogueOutputRejected("CPA_DIALOGUE_DEADLINE_REJECTED")
            output_texts = [reply, *suggestions]
            fit_echo_patterns = self._fit_value_echo_patterns(fit_context)
            if any(
                pattern.search(item)
                for item in output_texts
                for pattern in fit_echo_patterns
            ):
                raise DialogueOutputRejected(
                    "CPA_DIALOGUE_FIT_VALUE_ECHO_REJECTED"
                )
            if not policy["shopping_allowed"] and any(
                self._PROHIBITED_SHOPPING_WHEN_BLOCKED.search(item)
                for item in output_texts
            ):
                raise DialogueOutputRejected("CPA_DIALOGUE_SHOPPING_REJECTED")
            if not policy["deadline_question_allowed"] and any(
                re.search(r"什么时候|哪天|截止时间|最晚", item)
                for item in suggestions
            ):
                raise DialogueOutputRejected("CPA_DIALOGUE_DEADLINE_REJECTED")
            if any(
                self._PROHIBITED_VISUAL_PROMISE.search(item)
                for item in output_texts
            ):
                raise DialogueOutputRejected("CPA_DIALOGUE_VISUAL_REJECTED")
            if any(
                self._PROHIBITED_PERSON_OR_MEDICAL.search(item)
                or self._PROHIBITED_MEDICAL_ADVICE.search(item)
                or self._PROHIBITED_PERSON_EVALUATION.search(item)
                or self._PROHIBITED_DIAGNOSIS.search(item)
                or re.search(
                    r"(?:颜值|身材|体重|年龄|性吸引力).{0,8}"
                    r"(?:打|给|是)?\s*\d+\s*分",
                    item,
                )
                for item in output_texts
            ):
                raise DialogueOutputRejected(
                    "CPA_DIALOGUE_PERSON_SAFETY_REJECTED"
                )
            if any(
                re.search(
                    r"(?i)(?<![a-z0-9_])[gc]\d{3,}(?![a-z0-9_])",
                    item,
                )
                for item in output_texts
            ):
                raise DialogueOutputRejected("CPA_DIALOGUE_INTERNAL_ID_REJECTED")
            return (
                {
                    "reply": reply.strip(),
                    "action": parsed["action"],
                    "control": parsed["control"],
                    "scene_advisory": advisory,
                    "suggested_replies": [item.strip() for item in suggestions],
                },
                {
                    "attempted": True,
                    "status": "ok",
                    "requested_model": self.requested_model,
                    "transport_model": self.transport_model,
                    "resolved_model": reported_model,
                    "model_verified": True,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "input_contract": "stylist_dialogue_minimized_v1",
                    "output_contract": "stylist_dialogue_structured_v1",
                    "advisory_diagnostic_reason_code": (
                        advisory_diagnostic_reason_code
                    ),
                },
            )
        except DialogueOutputRejected as exc:
            self.mark_dialogue_output_rejection()
            raise ProviderUnavailable(
                "dialogue output rejected",
                reason_code="CPA_DIALOGUE_OUTPUT_REJECTED",
                diagnostic_reason_code=exc.diagnostic_reason_code,
                advisory_diagnostic_reason_code=(
                    advisory_diagnostic_reason_code
                ),
                opens_circuit=False,
            ) from exc
        except ProviderUnavailable as exc:
            self.mark_interaction_failure(
                exc.reason_code, open_circuit=exc.opens_circuit
            )
            raise
        except Exception as exc:
            if isinstance(exc, httpx.TimeoutException):
                reason_code = "CPA_PROVIDER_TIMEOUT"
            elif isinstance(
                exc, (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError)
            ):
                self.mark_dialogue_output_rejection()
                raise ProviderUnavailable(
                    "dialogue output rejected",
                    reason_code="CPA_DIALOGUE_OUTPUT_REJECTED",
                    diagnostic_reason_code="CPA_DIALOGUE_OUTPUT_UNCLASSIFIED",
                    opens_circuit=False,
                ) from exc
            else:
                reason_code = "CPA_PROVIDER_UNAVAILABLE"
            self.mark_interaction_failure(reason_code)
            raise ProviderUnavailable(
                type(exc).__name__, reason_code=reason_code
            ) from exc

def _tokens(text: str) -> list[str]:
    normalized = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    words = normalized.split()
    compact = "".join(words)
    grams = [compact[index : index + 2] for index in range(max(0, len(compact) - 1))]
    return words + grams


class DenseAdapter:
    """Optional local hashed-vector adapter with an explicit failure path."""

    def __init__(self, settings: Settings):
        self.enabled = settings.dense_enabled
        self.force_failure = settings.dense_force_failure
        self.version = "hashed_dense_v1"

    @staticmethod
    def _cosine(left: Counter[str], right: Counter[str]) -> float:
        numerator = sum(value * right.get(token, 0) for token, value in left.items())
        left_norm = math.sqrt(sum(value * value for value in left.values()))
        right_norm = math.sqrt(sum(value * value for value in right.values()))
        if not left_norm or not right_norm:
            return 0.0
        return numerator / (left_norm * right_norm)

    def rank(self, query: str, documents: dict[str, str]) -> list[tuple[str, float]]:
        if not self.enabled:
            return []
        if self.force_failure:
            raise ProviderUnavailable("forced dense failure")
        query_vector = Counter(_tokens(query))
        scores = [
            (item_id, self._cosine(query_vector, Counter(_tokens(text))))
            for item_id, text in documents.items()
        ]
        return sorted(scores, key=lambda pair: (-pair[1], pair[0]))
