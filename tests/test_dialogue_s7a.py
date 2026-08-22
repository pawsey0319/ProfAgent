from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from profagent.app import create_app


# Synthetic sentinel measurements used only to verify purpose-limited handling.
# They are deliberately unrelated to any user-provided body data.
_SYNTH_HEIGHT_CM = 173
_SYNTH_WEIGHT_KG = 67
_SYNTH_ALT_HEIGHT_CM = 181
_SYNTH_ALT_WEIGHT_KG = 73
_SYNTH_OTHER_HEIGHT_CM = 167
_SYNTH_OTHER_WEIGHT_KG = 61


def _turn(
    client: TestClient,
    message: str,
    *,
    session_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"user_id": "u01", "message": message}
    if session_id is not None:
        payload["styling_session_id"] = session_id
    if request_id is not None:
        payload["request_id"] = request_id
    response = client.post("/dialogue/turn", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _trace(client: TestClient, response: dict[str, Any]) -> dict[str, Any]:
    trace = client.get(
        f"/trace/{response['trace_id']}", params={"user_id": "u01"}
    )
    assert trace.status_code == 200, trace.text
    return trace.json()["trace"]


def _install_cpa(
    monkeypatch: pytest.MonkeyPatch,
    *,
    advisory: object = None,
    unsafe_reply: str | None = None,
    unsafe_suggestion: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    contexts: list[dict[str, Any]] = []
    calls = {"cpa": 0}

    async def dialogue_post(_self, url, **kwargs):
        calls["cpa"] += 1
        context = json.loads(kwargs["json"]["messages"][1]["content"])
        contexts.append(context)
        policy = context["policy"]
        content = {
            "reply": unsafe_reply
            or "第一次约会前有点紧张很自然。我们用有结构感的衣物与层次强化服装轮廓。",
            "action": policy["required_action"],
            "control": policy["required_control"],
            "scene_advisory": advisory,
            "suggested_replies": [unsafe_suggestion or "看看首选方案"],
        }
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "grok-4.6-build",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(content, ensure_ascii=False)
                        }
                    }
                ],
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", dialogue_post)
    return contexts, calls


@pytest.mark.parametrize(
    "message",
    [
        f"今天第一次约会，有点紧张，你觉得我穿好，我{_SYNTH_HEIGHT_CM}cm，{_SYNTH_WEIGHT_KG}kg，我想让衣服更有量感",
        f"今天约会我有点慌，身高{_SYNTH_ALT_HEIGHT_CM}厘米，体重{_SYNTH_ALT_WEIGHT_KG}公斤，帮我搭一套，衣服想更有量感",
        f"今天第一次约会有点不安，我身高{_SYNTH_OTHER_HEIGHT_CM}，体重{_SYNTH_OTHER_WEIGHT_KG}，怎么搭才让衣服更有量感",
    ],
)
def test_mild_emotion_with_styling_measurements_stays_actionable_and_high_gate(
    monkeypatch, offline_settings, message: str
) -> None:
    contexts, calls = _install_cpa(monkeypatch)
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    with TestClient(app) as client:
        response = _turn(client, message)

        assert calls == {"cpa": 1}
        assert response["conversation_mode"] == "styling_active"
        assert response["action"] == "recommend"
        assert response["recommendation_paused"] is False
        assert response["recommendation"] is not None
        assert response["provider"]["status"] == "ok", (
            response["provider"].get("reason_code"),
            _trace(client, response)["provider"].get("reason_code"),
            _trace(client, response)["provider"].get("diagnostic_reason_code"),
        )
        assert response["provider"]["generation_source"] == "cpa"
        assert response["assistant_message"].startswith("第一次约会前有点紧张很自然")
        assert response["scene"]["urgency"] == "high"
        assert response["scene"]["shopping_allowed"] is False
        assert response["scene"]["ui_capabilities"]["shopping_cta"] is False

        context = contexts[0]
        assert context["current_turn"]["kind"] == "normal"
        assert "任务内版型信息已结构化" in context["current_turn"]["content"]
        assert not any(
            token in context["current_turn"]["content"]
            for token in (
                f"{_SYNTH_HEIGHT_CM}cm",
                f"{_SYNTH_WEIGHT_KG}kg",
                f"{_SYNTH_ALT_HEIGHT_CM}厘米",
                f"{_SYNTH_ALT_WEIGHT_KG}公斤",
                f"身高{_SYNTH_OTHER_HEIGHT_CM}",
                f"体重{_SYNTH_OTHER_WEIGHT_KG}",
            )
        )
        fit = context["session"]["fit_context"]
        assert fit["purpose"] == "garment_fit_only"
        assert fit["silhouette_goal"] == "add_garment_volume"
        trace = _trace(client, response)
        assert trace["catalog"]["attempted"] is False
        assert trace["catalog"]["call_count"] == 0


def test_explicit_pause_wins_even_with_task_emotion_and_fit_context(
    monkeypatch, offline_settings
) -> None:
    contexts, calls = _install_cpa(monkeypatch)
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    with TestClient(app) as client:
        response = _turn(
            client,
            f"今天第一次约会，有点紧张，先不推荐，我{_SYNTH_HEIGHT_CM}cm，{_SYNTH_WEIGHT_KG}kg，我想让衣服更有量感",
        )

        assert calls == {"cpa": 1}
        assert response["conversation_mode"] == "support_pause"
        assert response["action"] == "support"
        assert response["recommendation_paused"] is True
        assert response["recommendation"] is None
        assert contexts[0]["policy"]["required_control"] == "pause"
        assert contexts[0]["session"]["fit_context"]["purpose"] == "garment_fit_only"
        trace = _trace(client, response)
        assert trace["catalog"]["call_count"] == 0


def test_fit_context_is_session_only_and_never_written_to_memory_or_trace(
    monkeypatch, offline_settings
) -> None:
    contexts, calls = _install_cpa(monkeypatch)
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    with TestClient(app) as client:
        before_memory = client.get("/memory", params={"user_id": "u01"}).json()
        first = _turn(
            client,
            f"今天第一次约会，有点紧张，你觉得我穿好，我{_SYNTH_HEIGHT_CM}cm，{_SYNTH_WEIGHT_KG}kg，我想让衣服更有量感",
            request_id="dlgreq-s7a-fit-receipt",
        )
        retry = _turn(
            client,
            f"今天第一次约会，有点紧张，你觉得我穿好，我{_SYNTH_HEIGHT_CM}cm，{_SYNTH_WEIGHT_KG}kg，我想让衣服更有量感",
            request_id="dlgreq-s7a-fit-receipt",
        )
        followup = _turn(
            client, "继续推荐", session_id=first["styling_session_id"]
        )
        new_session = _turn(client, "下周约会，帮我搭一套")
        after_memory = client.get("/memory", params={"user_id": "u01"}).json()

        assert retry == first
        assert calls == {"cpa": 3}
        assert contexts[0]["session"]["fit_context"]["height_cm"] == _SYNTH_HEIGHT_CM
        assert contexts[0]["session"]["fit_context"]["weight_kg"] == _SYNTH_WEIGHT_KG
        assert contexts[1]["session"]["fit_context"] == contexts[0]["session"]["fit_context"]
        assert contexts[2]["session"]["fit_context"] is None
        assert followup["styling_session_id"] == first["styling_session_id"]
        assert new_session["styling_session_id"] != first["styling_session_id"]
        assert after_memory == before_memory

        serialized_trace = json.dumps(_trace(client, first), ensure_ascii=False)
        assert f"{_SYNTH_HEIGHT_CM}cm" not in serialized_trace
        assert f"{_SYNTH_WEIGHT_KG}kg" not in serialized_trace
        assert "height_cm" not in serialized_trace
        assert "weight_kg" not in serialized_trace


@pytest.mark.parametrize("sensitive_detail", ["我30岁", "最近胸闷"])
def test_age_or_medical_content_is_not_generalized_into_fit_context(
    monkeypatch, offline_settings, sensitive_detail: str
) -> None:
    contexts, calls = _install_cpa(monkeypatch)
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    with TestClient(app) as client:
        before_memory = client.get("/memory", params={"user_id": "u01"}).json()
        response = _turn(
            client,
            f"今天约会，帮我搭一套，{sensitive_detail}，身高{_SYNTH_HEIGHT_CM}cm，体重{_SYNTH_WEIGHT_KG}kg",
        )
        after_memory = client.get("/memory", params={"user_id": "u01"}).json()

        assert calls == {"cpa": 1}
        assert contexts[0]["current_turn"]["kind"] == "sensitive_summary"
        assert contexts[0]["session"]["fit_context"] is None
        assert contexts[0]["history"] == []
        assert after_memory == before_memory
        stored = app.state.services.dialogue.state.session(
            response["styling_session_id"], "u01"
        )
        assert stored.fit_context is None
        serialized_trace = json.dumps(_trace(client, response), ensure_ascii=False)
        assert f"{_SYNTH_HEIGHT_CM}cm" not in serialized_trace
        assert f"{_SYNTH_WEIGHT_KG}kg" not in serialized_trace


@pytest.mark.parametrize(
    "message",
    [
        "我有点紧张，穿什么？",
        f"有点紧张，我{_SYNTH_HEIGHT_CM}cm，{_SYNTH_WEIGHT_KG}kg，想让衣服更有量感",
    ],
)
def test_clothing_question_or_fit_goal_is_authoritative_task_evidence(
    monkeypatch, offline_settings, message: str
) -> None:
    contexts, calls = _install_cpa(monkeypatch)
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    with TestClient(app) as client:
        response = _turn(client, message)

        assert calls == {"cpa": 1}
        assert response["conversation_mode"] == "styling_active"
        assert response["action"] in {"clarify", "recommend"}
        assert response["scene"] is not None
        assert response["scene"]["intent"] == "recommend"
        assert response["recommendation_paused"] is (
            response["action"] == "clarify"
        )
        assert contexts[0]["policy"]["required_action"] == response["action"]
        if f"{_SYNTH_HEIGHT_CM}cm" in message:
            assert contexts[0]["session"]["fit_context"] == {
                "purpose": "garment_fit_only",
                "height_cm": float(_SYNTH_HEIGHT_CM),
                "weight_kg": float(_SYNTH_WEIGHT_KG),
                "silhouette_goal": "add_garment_volume",
            }
        trace = _trace(client, response)
        assert trace["catalog"]["call_count"] == 0


def test_isolated_measurements_do_not_create_task_or_fit_profile(
    monkeypatch, offline_settings
) -> None:
    contexts, calls = _install_cpa(monkeypatch)
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    with TestClient(app) as client:
        response = _turn(client, f"我{_SYNTH_HEIGHT_CM}cm，{_SYNTH_WEIGHT_KG}kg")

        assert calls == {"cpa": 1}
        assert response["conversation_mode"] == "stylist_chat"
        assert response["scene"] is None
        assert response["recommendation"] is None
        assert contexts[0]["current_turn"]["kind"] == "sensitive_summary"
        assert contexts[0]["session"]["fit_context"] is None
        stored = app.state.services.dialogue.state.session(
            response["styling_session_id"], "u01"
        )
        assert stored.fit_context is None
        trace_text = json.dumps(_trace(client, response), ensure_ascii=False)
        assert f"{_SYNTH_HEIGHT_CM}cm" not in trace_text
        assert f"{_SYNTH_WEIGHT_KG}kg" not in trace_text


def test_fear_of_awkward_silence_gets_acknowledgement_before_local_styling(
    monkeypatch, offline_settings
) -> None:
    calls = 0

    async def failed_post(_self, url, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", failed_post)
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    with TestClient(app) as client:
        response = _turn(client, "今天约会有点怕冷场，帮我搭一套")

        assert calls == 1
        assert response["conversation_mode"] == "styling_active"
        assert response["action"] == "recommend"
        assert response["provider"]["generation_source"] == "local_fallback"
        assert response["assistant_message"].startswith("担心重要场合冷场很自然")
        assert response["recommendation"] is not None
        assert response["scene"]["shopping_allowed"] is False
        assert _trace(client, response)["catalog"]["call_count"] == 0


def test_stylist_chat_can_transition_to_new_task_without_resume_keyword(
    monkeypatch, offline_settings
) -> None:
    contexts, calls = _install_cpa(monkeypatch)
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    with TestClient(app) as client:
        chat = _turn(client, "先聊聊天")
        task = _turn(
            client,
            "明天面试，帮我搭一套",
            session_id=chat["styling_session_id"],
        )

        assert calls == {"cpa": 2}
        assert chat["conversation_mode"] == "stylist_chat"
        assert task["conversation_mode"] == "styling_active"
        assert task["action"] == "recommend"
        assert task["recommendation_paused"] is False
        assert task["recommendation"] is not None
        assert contexts[1]["policy"]["required_action"] == "recommend"


def test_explicit_pause_latch_requires_explicit_resume_even_after_new_task(
    monkeypatch, offline_settings
) -> None:
    _contexts, calls = _install_cpa(monkeypatch)
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    with TestClient(app) as client:
        paused = _turn(client, "今天约会，先不推荐")
        new_task = _turn(
            client,
            "明天面试，帮我搭一套",
            session_id=paused["styling_session_id"],
        )
        resumed = _turn(
            client,
            "继续推荐",
            session_id=paused["styling_session_id"],
        )

        assert calls == {"cpa": 3}
        assert paused["conversation_mode"] == "support_pause"
        assert new_task["conversation_mode"] == "support_pause"
        assert new_task["recommendation_paused"] is True
        assert new_task["recommendation"] is None
        assert _trace(client, new_task)["catalog"]["call_count"] == 0
        assert resumed["conversation_mode"] == "styling_active"
        assert resumed["action"] == "recommend"
        assert resumed["recommendation_paused"] is False
        assert resumed["recommendation"] is not None


def test_invalid_scene_advisory_rejects_the_whole_provider_payload(
    monkeypatch, offline_settings
) -> None:
    contexts, calls = _install_cpa(
        monkeypatch,
        advisory={"intent": "invented", "occasion": "mars", "goals": ["hot"]},
    )
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    with TestClient(app) as client:
        response = _turn(client, "今天第一次约会，有点紧张，帮我搭一套")

        assert calls == {"cpa": 1}
        assert response["provider"]["status"] == "fallback"
        assert response["provider"]["generation_source"] == "local_fallback"
        assert response["provider"]["reason_code"] == "CPA_DIALOGUE_OUTPUT_REJECTED"
        assert response["scene"]["occasion"] == "date"
        assert response["scene"]["urgency"] == "high"
        assert contexts[0]["policy"]["shopping_allowed"] is False
        trace = _trace(client, response)
        assert trace["provider"]["diagnostic_reason_code"] == (
            "CPA_DIALOGUE_SCENE_ADVISORY_INVALID"
        )
        assert trace["catalog"]["call_count"] == 0


def test_invalid_advisory_never_weakens_unsafe_reply_rejection(
    monkeypatch, offline_settings
) -> None:
    _contexts, calls = _install_cpa(
        monkeypatch,
        advisory={"intent": "invented", "occasion": "mars", "goals": ["hot"]},
        unsafe_reply="打开视频看看吧。",
    )
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    with TestClient(app) as client:
        response = _turn(client, "今天第一次约会，帮我搭一套")

        assert calls == {"cpa": 1}
        assert response["provider"]["status"] == "fallback"
        assert response["provider"]["generation_source"] == "local_fallback"
        assert response["provider"]["reason_code"] == "CPA_DIALOGUE_OUTPUT_REJECTED"
        assert "视频" not in response["assistant_message"]
        assert "智能对话暂时不可用" not in response["assistant_message"]
        assert "安全降级" not in response["assistant_message"]
        trace = _trace(client, response)
        assert trace["provider"]["diagnostic_reason_code"] == (
            "CPA_DIALOGUE_SCENE_ADVISORY_INVALID"
        )
        assert trace["catalog"]["call_count"] == 0


@pytest.mark.parametrize(
    ("location", "echo"),
    [
        ("reply", f"按你{_SYNTH_HEIGHT_CM}cm、{_SYNTH_WEIGHT_KG}kg的数据来搭。"),
        ("reply", f"参考身高{_SYNTH_HEIGHT_CM}厘米和体重{_SYNTH_WEIGHT_KG}公斤来搭。"),
        ("suggestion", f"用{_SYNTH_HEIGHT_CM}公分和{_SYNTH_WEIGHT_KG}千克搭配"),
    ],
)
def test_cpa_cannot_repeat_current_fit_measurement_values(
    monkeypatch, offline_settings, location: str, echo: str
) -> None:
    _contexts, calls = _install_cpa(
        monkeypatch,
        unsafe_reply=echo if location == "reply" else None,
        unsafe_suggestion=echo if location == "suggestion" else None,
    )
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    with TestClient(app) as client:
        response = _turn(
            client,
            f"今天约会，帮我搭一套，我{_SYNTH_HEIGHT_CM}cm，{_SYNTH_WEIGHT_KG}kg，衣服想更有量感",
        )

        assert calls == {"cpa": 1}
        assert response["provider"]["status"] == "fallback"
        assert response["provider"]["generation_source"] == "local_fallback"
        assert echo not in json.dumps(response, ensure_ascii=False)
        trace = _trace(client, response)
        assert trace["provider"]["diagnostic_reason_code"] == (
            "CPA_DIALOGUE_FIT_VALUE_ECHO_REJECTED"
        )
        assert trace["catalog"]["call_count"] == 0


@pytest.mark.parametrize(
    "person_evaluation",
    [
        "你的比例很好。",
        "你是标准身材。",
        "你拥有黄金比例。",
        "你的身材比例完美。",
    ],
)
def test_cpa_person_proportion_or_standard_body_evaluation_fails_closed(
    monkeypatch, offline_settings, person_evaluation: str
) -> None:
    _contexts, calls = _install_cpa(
        monkeypatch, unsafe_reply=person_evaluation
    )
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    with TestClient(app) as client:
        response = _turn(client, "今天约会，帮我搭一套")

        assert calls == {"cpa": 1}
        assert response["provider"]["status"] == "fallback"
        assert person_evaluation not in response["assistant_message"]
        trace = _trace(client, response)
        assert trace["provider"]["diagnostic_reason_code"] == (
            "CPA_DIALOGUE_PERSON_SAFETY_REJECTED"
        )
        assert trace["catalog"]["call_count"] == 0


def test_local_fallback_copy_has_no_provider_failure_banner(
    monkeypatch, offline_settings
) -> None:
    calls = 0

    async def failed_post(_self, url, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", failed_post)
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    with TestClient(app) as client:
        response = _turn(client, "今天第一次约会，有点紧张，帮我搭一套")

        assert calls == 1
        assert response["provider"]["status"] == "fallback"
        assert response["provider"]["generation_source"] == "local_fallback"
        assert "智能对话暂时不可用" not in response["assistant_message"]
        assert "安全降级" not in response["assistant_message"]
        assert response["assistant_message"].count("紧张") == 1
        assert "衣橱单品" in response["assistant_message"]
        assert _trace(client, response)["catalog"]["call_count"] == 0
