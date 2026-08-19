from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from profagent.app import create_app
from profagent.config import CPA_DIALOGUE_BUDGET_MAX_SECONDS, Settings
from profagent.scene import SceneParser


SCREENSHOT_MESSAGE = "等会儿要约会了，怎么办，好紧张"


def _turn(
    client: TestClient,
    message: str,
    *,
    session_id: str | None = None,
    request_id: str | None = None,
) -> dict:
    payload = {"user_id": "u01", "message": message}
    if session_id is not None:
        payload["styling_session_id"] = session_id
    if request_id is not None:
        payload["request_id"] = request_id
    response = client.post("/dialogue/turn", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _trace(client: TestClient, response: dict) -> dict:
    trace = client.get(
        f"/trace/{response['trace_id']}", params={"user_id": "u01"}
    )
    assert trace.status_code == 200, trace.text
    return trace.json()["trace"]


def test_dialogue_budget_is_hard_capped_at_transport_aligned_120_seconds(
    monkeypatch, project_root
) -> None:
    assert CPA_DIALOGUE_BUDGET_MAX_SECONDS == 120.0
    monkeypatch.setenv("PROFAGENT_CPA_DIALOGUE_BUDGET_SECONDS", "999")
    assert Settings.from_env().effective_cpa_dialogue_budget_seconds == 120.0
    monkeypatch.setenv("PROFAGENT_CPA_DIALOGUE_BUDGET_SECONDS", "0.125")
    assert Settings.from_env().effective_cpa_dialogue_budget_seconds == 0.125
    assert (
        Settings(root_dir=project_root, cpa_dialogue_budget_seconds=999)
        .effective_cpa_dialogue_budget_seconds
        == 120.0
    )


def test_injected_short_dialogue_budget_times_out_once_and_receipt_calls_zero(
    monkeypatch, offline_settings
) -> None:
    calls = 0
    cancelled = False

    async def slow_post(_self, url, **_kwargs):
        nonlocal calls, cancelled
        calls += 1
        try:
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            cancelled = True
            raise
        raise AssertionError("unreachable")

    monkeypatch.setattr(httpx.AsyncClient, "post", slow_post)
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        cpa_timeout_seconds=120,
        cpa_dialogue_budget_seconds=0.05,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        started = time.perf_counter()
        response = _turn(
            client,
            SCREENSHOT_MESSAGE,
            request_id="s8-wall-clock-receipt",
        )
        elapsed = time.perf_counter() - started
        retry_started = time.perf_counter()
        retry = _turn(
            client,
            SCREENSHOT_MESSAGE,
            request_id="s8-wall-clock-receipt",
        )
        retry_elapsed = time.perf_counter() - retry_started

        assert elapsed < 1
        assert retry_elapsed < 1
        assert calls == 1
        assert cancelled is True
        assert retry == response
        assert response["provider"]["status"] == "fallback"
        assert response["provider"]["generation_source"] == "local_fallback"
        assert response["provider"]["reason_code"] == (
            "CPA_INTERACTION_BUDGET_EXCEEDED"
        )
        trace = _trace(client, response)
        assert trace["provider"]["interaction_budget_seconds"] == 0.05
        assert 20 <= trace["provider"]["latency_ms"] < 1000
        assert trace["catalog"]["call_count"] == 0


def test_successful_dialogue_waits_for_complete_validated_cpa_response_once(
    monkeypatch, offline_settings
) -> None:
    calls = 0
    completed_at = 0.0
    expected_reply = "约会前有些紧张很自然。我们先用现有衣橱完成三个方向。"

    async def delayed_valid_post(_self, url, **kwargs):
        nonlocal calls, completed_at
        calls += 1
        context = json.loads(kwargs["json"]["messages"][1]["content"])
        await asyncio.sleep(0.08)
        payload = {
            "reply": expected_reply,
            "action": context["policy"]["required_action"],
            "control": context["policy"]["required_control"],
            "scene_advisory": None,
            "suggested_replies": ["看看首选方向"],
        }
        completed_at = time.perf_counter()
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "grok-4.6-build",
                "choices": [
                    {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
                ],
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", delayed_valid_post)
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        cpa_timeout_seconds=120,
        # This test validates synchronous completion, not the timeout path.
        # Keep the injected budget comfortably above the 80 ms mock delay so
        # Windows scheduler/CI jitter cannot cancel a healthy child task.
        # Timeout cancellation is covered separately with a 50 ms budget.
        cpa_dialogue_budget_seconds=2.0,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        started = time.perf_counter()
        response = _turn(
            client,
            SCREENSHOT_MESSAGE,
            request_id="s9-complete-cpa-once",
        )
        returned_at = time.perf_counter()

        assert calls == 1
        assert completed_at > started
        assert returned_at >= completed_at
        assert returned_at - started >= 0.07
        assert response["provider"]["status"] == "ok"
        assert response["provider"]["generation_source"] == "cpa"
        assert response["provider"]["resolved_model"] == "grok-4.6-build"
        assert response["assistant_message"] == expected_reply
        assert response["recommendation"] is not None
        assert len(response["recommendation"]["outfits"]) == 3
        assert not any(
            key in response
            for key in ("provisional", "pending", "poll_url", "job_id")
        )
        trace = _trace(client, response)
        provider_trace = trace["provider"]
        assert provider_trace["status"] == "ok"
        assert provider_trace["latency_ms"] >= 50
        assert provider_trace["interaction_budget_seconds"] == 2.0
        serialized_trace = json.dumps(trace, ensure_ascii=False)
        assert expected_reply not in serialized_trace
        assert "看看首选方向" not in serialized_trace
        assert not {
            "reply",
            "assistant_message",
            "suggested_replies",
            "content",
        }.intersection(provider_trace)


def test_no_dialogue_pending_or_polling_endpoint_exists(offline_settings) -> None:
    app = create_app(offline_settings)
    dialogue_routes = [
        route
        for route in app.routes
        if getattr(route, "path", "").startswith("/dialogue/")
    ]
    turn_routes = [
        route
        for route in dialogue_routes
        if route.path == "/dialogue/turn" and "POST" in getattr(route, "methods", set())
    ]
    assert len(turn_routes) == 1
    assert {route.path for route in dialogue_routes} == {"/dialogue/turn"}
    openapi_paths = app.openapi()["paths"]
    assert set(path for path in openapi_paths if path.startswith("/dialogue/")) == {
        "/dialogue/turn"
    }
    assert not any(
        token in json.dumps(app.openapi(), ensure_ascii=False).lower()
        for token in ("provisional", "poll_url", "job_id")
    )


def test_business_wait_timeout_never_opens_cross_turn_circuit(
    monkeypatch, offline_settings
) -> None:
    calls = 0
    cancellations = 0

    async def slow_post(_self, url, **_kwargs):
        nonlocal calls, cancellations
        calls += 1
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancellations += 1
            raise
        raise AssertionError("unreachable")

    monkeypatch.setattr(httpx.AsyncClient, "post", slow_post)
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        cpa_timeout_seconds=120,
        cpa_dialogue_budget_seconds=0.03,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        first = _turn(
            client,
            "先聊聊天",
            request_id="s8-timeout-first",
        )
        first_retry = _turn(
            client,
            "先聊聊天",
            request_id="s8-timeout-first",
        )
        second = _turn(
            client,
            "继续聊聊",
            session_id=first["styling_session_id"],
            request_id="s8-timeout-second",
        )

        assert first_retry == first
        assert calls == 2
        assert cancellations == 2
        assert first["provider"]["reason_code"] == (
            "CPA_INTERACTION_BUDGET_EXCEEDED"
        )
        assert second["provider"]["reason_code"] == (
            "CPA_INTERACTION_BUDGET_EXCEEDED"
        )
        assert second["provider"]["reason_code"] != "CPA_CIRCUIT_OPEN"
        assert _trace(client, first)["catalog"]["call_count"] == 0
        assert _trace(client, second)["catalog"]["call_count"] == 0


@pytest.mark.parametrize(
    ("near_time", "expected_horizon"),
    [
        ("等会儿", "today"),
        ("待会儿", "today"),
        ("一会儿", "today"),
        ("过会儿", "today"),
        ("一会就", "today"),
        # “马上” retains the stricter existing `now` bucket; both are high,
        # and fixed query-only truth requires this stronger interpretation.
        ("马上", "now"),
    ],
)
def test_near_time_synonyms_are_high_urgency_without_deadline_clarification(
    offline_settings, near_time: str, expected_horizon: str
) -> None:
    app = create_app(offline_settings)
    with TestClient(app) as client:
        response = _turn(client, f"{near_time}要约会了，怎么办，好紧张")

        assert response["conversation_mode"] == "styling_active"
        assert response["action"] == "recommend"
        assert response["pending_question_status"] == "none"
        assert response["recommendation_paused"] is False
        assert response["scene"]["event_horizon"] == expected_horizon
        assert response["scene"]["urgency"] == "high"
        assert response["scene"]["shopping_allowed"] is False
        assert response["scene"]["ui_capabilities"]["shopping_cta"] is False
        assert response["recommendation"]["shopping_suggestions"] == []
        assert len(response["recommendation"]["outfits"]) == 3
        assert "最晚什么时候" not in response["assistant_message"]
        assert response["assistant_message"].count("紧张") == 1
        trace = _trace(client, response)
        assert trace["catalog"]["attempted"] is False
        assert trace["catalog"]["call_count"] == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("不是等会儿，是明天", ("soon", True)),
        ("不用马上决定", ("unknown", False)),
        ("不是一会儿", ("unknown", False)),
        ("骑在马上参加活动", ("unknown", False)),
        ("等待会计回复后再决定", ("unknown", False)),
    ],
)
def test_near_time_detection_does_not_match_negated_or_non_time_text(
    text: str, expected: tuple[str, bool]
) -> None:
    assert SceneParser._detect_horizon(text) == expected


def test_close_control_wins_over_near_time_phrase(offline_settings) -> None:
    app = create_app(offline_settings)
    with TestClient(app) as client:
        response = _turn(client, "结束本次任务，待会儿也不用推荐")

        assert response["conversation_mode"] == "task_closed"
        assert response["action"] == "acknowledge"
        assert response["recommendation_paused"] is True
        assert response["recommendation"] is None
        assert "最晚什么时候" not in response["assistant_message"]
        assert _trace(client, response)["catalog"]["call_count"] == 0


def test_screenshot_fallback_acknowledges_once_then_gives_three_wardrobe_directions(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    with TestClient(app) as client:
        response = _turn(client, SCREENSHOT_MESSAGE)

        assert response["provider"]["status"] == "fallback"
        assert response["provider"]["generation_source"] == "local_fallback"
        assert response["action"] == "recommend"
        assert response["scene"]["event_horizon"] == "today"
        assert response["scene"]["urgency"] == "high"
        assert response["assistant_message"].count("紧张") == 1
        assert "最晚什么时候" not in response["assistant_message"]
        assert "现有衣橱" in response["assistant_message"] or (
            "衣橱单品" in response["assistant_message"]
        )
        assert len(response["recommendation"]["outfits"]) == 3
        assert response["recommendation"]["shopping_suggestions"] == []
        trace = _trace(client, response)
        assert trace["catalog"]["attempted"] is False
        assert trace["catalog"]["call_count"] == 0
        assert "紧张" not in json.dumps(trace, ensure_ascii=False)
