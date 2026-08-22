from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import replace
from datetime import datetime

import httpx
from fastapi.testclient import TestClient
import pytest

from profagent.app import create_app
from profagent.asset_service import AssetRecord
from profagent.config import (
    CPA_HEALTH_BUDGET_MAX_SECONDS,
    CPA_SCENE_BUDGET_MAX_SECONDS,
    VISION_INTERACTION_BUDGET_MAX_SECONDS,
    Settings,
)
from profagent.models import SceneParseInput
from profagent.vision import VisionAdapter, VisionUnavailable


SCENE_PAYLOAD = {
    "user_id": "u01",
    "query_text": "下周通勤，帮我用衣橱搭一套。",
    "event_horizon": "soon",
    "intent": "recommend",
    "occasion": "commute",
}


def _trace(client: TestClient, scene: dict) -> dict:
    response = client.get(
        f"/trace/{scene['trace_id']}", params={"user_id": scene["user_id"]}
    )
    assert response.status_code == 200
    return response.json()["trace"]


def test_scene_budget_is_hard_capped_and_can_only_tighten(
    monkeypatch, project_root
) -> None:
    monkeypatch.setenv("PROFAGENT_CPA_SCENE_BUDGET_SECONDS", "99")
    assert (
        Settings.from_env().effective_cpa_scene_budget_seconds
        == CPA_SCENE_BUDGET_MAX_SECONDS
    )
    monkeypatch.setenv("PROFAGENT_CPA_SCENE_BUDGET_SECONDS", "0.125")
    assert Settings.from_env().effective_cpa_scene_budget_seconds == 0.125
    assert (
        Settings(root_dir=project_root, cpa_scene_budget_seconds=99)
        .effective_cpa_scene_budget_seconds
        == CPA_SCENE_BUDGET_MAX_SECONDS
    )
    monkeypatch.setenv("PROFAGENT_CPA_HEALTH_BUDGET_SECONDS", "99")
    monkeypatch.setenv("PROFAGENT_VISION_INTERACTION_BUDGET_SECONDS", "99")
    bounded = Settings.from_env()
    assert bounded.effective_cpa_health_budget_seconds == CPA_HEALTH_BUDGET_MAX_SECONDS
    assert (
        bounded.effective_vision_interaction_budget_seconds
        == VISION_INTERACTION_BUDGET_MAX_SECONDS
    )


def test_scene_outer_budget_cancels_slow_cpa_and_returns_rule_fallback(
    offline_settings,
) -> None:
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        cpa_timeout_seconds=120,
        cpa_scene_budget_seconds=0.02,
    )
    app = create_app(settings)
    state = {"attempted": 0, "cancelled": False}

    async def slow_provider(_query_text: str):
        state["attempted"] += 1
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    app.state.services.llm.parse_scene_advisory = slow_provider
    with TestClient(app) as client:
        started = time.perf_counter()
        response = client.post("/scene/parse", json=SCENE_PAYLOAD)
        elapsed = time.perf_counter() - started
        assert response.status_code == 200
        scene = response.json()
        assert elapsed < 1
        assert scene["backend"] == "rule_fallback"
        assert state == {"attempted": 1, "cancelled": True}
        provider = _trace(client, scene)["provider"]
        assert provider["reason_code"] == "CPA_INTERACTION_BUDGET_EXCEEDED"
        assert provider["interaction_budget_seconds"] == 0.02
        assert "query_text" not in provider
        assert "error" not in provider


def test_memory_candidate_provider_external_cancellation_propagates_with_zero_writes(
    monkeypatch, tmp_path, offline_settings
) -> None:
    path = tmp_path / "cancelled_memory_candidate.sqlite3"
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        database_url=f"sqlite:///{path.as_posix()}",
    )
    app = create_app(settings)
    services = app.state.services
    state = {"attempted": 0, "cancelled": False}

    async def slow_post(_self, _url, **_kwargs):
        state["attempted"] += 1
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    monkeypatch.setattr(httpx.AsyncClient, "post", slow_post)

    async def exercise() -> None:
        task = asyncio.create_task(
            services.memory_candidates.extract("我偏爱藏青色")
        )
        while state["attempted"] == 0:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    connection = sqlite3.connect(path)
    counts = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "memory_proposals",
            "memory_records",
            "memory_outbox",
        )
    }
    connection.close()
    assert state == {"attempted": 1, "cancelled": True}
    assert counts == {
        "memory_proposals": 0,
        "memory_records": 0,
        "memory_outbox": 0,
    }
    assert services.traces._records == {}
    services.memory.close()


def test_provider_timeout_degrades_with_controlled_trace_reason(
    monkeypatch, offline_settings
) -> None:
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        cpa_timeout_seconds=0.01,
        cpa_scene_budget_seconds=0.5,
    )

    async def timeout_post(_self, url, **kwargs):
        assert kwargs["json"]["model"] == "grok-4.6-high"
        raise httpx.ReadTimeout(
            "secret upstream timeout text",
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", timeout_post)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/scene/parse", json=SCENE_PAYLOAD)
        assert response.status_code == 200
        scene = response.json()
        assert scene["backend"] == "rule_fallback"
        provider = _trace(client, scene)["provider"]
        assert provider["reason_code"] == "CPA_PROVIDER_TIMEOUT"
        assert "secret upstream timeout text" not in json.dumps(provider)


def test_explicit_allowlisted_cpa_build_echo_is_traced_as_resolved_model(
    monkeypatch, offline_settings
) -> None:
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        cpa_scene_budget_seconds=0.5,
    )

    async def wrong_model_post(_self, url, **kwargs):
        assert kwargs["json"]["model"] == "grok-4.6-high"
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "grok-4.6-build",
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"recommend","occasion":"date",'
                                '"goals":["polished"]}'
                            )
                        }
                    }
                ],
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", wrong_model_post)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/scene/parse", json=SCENE_PAYLOAD)
        assert response.status_code == 200
        scene = response.json()
        assert scene["backend"] == "grok4.6"
        provider = _trace(client, scene)["provider"]
        assert provider["resolved_model"] == "grok-4.6-build"
        assert provider["model_verified"] is True
        assert provider["status"] == "ok"


def test_external_scene_cancellation_discards_trace_and_never_saves_scene(
    offline_settings,
) -> None:
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        cpa_scene_budget_seconds=0.5,
    )
    app = create_app(settings)
    services = app.state.services

    async def exercise() -> None:
        attempted = asyncio.Event()

        async def slow_provider(_query_text: str):
            attempted.set()
            await asyncio.sleep(10)

        services.llm.parse_scene_advisory = slow_provider
        payload = SceneParseInput(
            request_id="req_cancelled",
            user_id="u01",
            styling_session_id="session_cancelled",
            query_text="下周通勤，帮我搭一套。",
            event_horizon="soon",
            intent="recommend",
            occasion="commute",
        )
        task = asyncio.create_task(services.scene_parser.parse(payload))
        await attempted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert services.state.by_request("req_cancelled") is None
    assert services.state.by_session("session_cancelled") is None
    assert services.traces.list_for_session("session_cancelled") == []


def test_health_probe_budget_returns_ready_degraded_without_blocking_core(
    monkeypatch, offline_settings
) -> None:
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        cpa_timeout_seconds=120,
        cpa_health_budget_seconds=0.02,
    )
    state = {"attempted": 0, "cancelled": False}
    dialogue_calls = 0

    async def slow_get(_self, _url, **_kwargs):
        state["attempted"] += 1
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    async def dialogue_post(_self, url, **kwargs):
        nonlocal dialogue_calls
        dialogue_calls += 1
        context = json.loads(kwargs["json"]["messages"][1]["content"])
        policy = context["policy"]
        content = {
            "reply": "当然可以，我们先轻松聊聊。",
            "action": policy["required_action"],
            "control": policy["required_control"],
            "scene_advisory": None,
            "suggested_replies": ["继续聊聊"],
        }
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "grok-4.6-build",
                "choices": [{"message": {"content": json.dumps(content)}}],
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", slow_get)
    monkeypatch.setattr(httpx.AsyncClient, "post", dialogue_post)
    app = create_app(settings)
    with TestClient(app) as client:
        started = time.perf_counter()
        response = client.get("/health")
        elapsed = time.perf_counter() - started
        dialogue = client.post(
            "/dialogue/turn",
            json={"user_id": "u01", "message": "先聊聊天"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert elapsed < 1
    assert payload["ready"] is True
    assert payload["status"] == "degraded"
    assert payload["providers"]["llm"]["reason"] == "CPA_HEALTH_BUDGET_EXCEEDED"
    assert state == {"attempted": 1, "cancelled": True}
    assert dialogue.status_code == 200
    assert dialogue.json()["provider"]["status"] == "ok"
    assert dialogue.json()["provider"]["resolved_model"] == "grok-4.6-build"
    assert dialogue_calls == 1


def test_health_timeout_preserves_prior_text_verification_and_next_dialogue_attempt(
    monkeypatch, offline_settings
) -> None:
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        cpa_timeout_seconds=120,
        cpa_health_budget_seconds=0.02,
    )
    dialogue_calls = 0

    async def dialogue_post(_self, url, **kwargs):
        nonlocal dialogue_calls
        dialogue_calls += 1
        context = json.loads(kwargs["json"]["messages"][1]["content"])
        policy = context["policy"]
        content = {
            "reply": "我们继续聊聊。",
            "action": policy["required_action"],
            "control": policy["required_control"],
            "scene_advisory": None,
            "suggested_replies": ["继续聊聊"],
        }
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "grok-4.6-build",
                "choices": [{"message": {"content": json.dumps(content)}}],
            },
        )

    async def slow_get(_self, _url, **_kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(httpx.AsyncClient, "post", dialogue_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", slow_get)
    app = create_app(settings)
    with TestClient(app) as client:
        first = client.post(
            "/dialogue/turn",
            json={"user_id": "u01", "message": "先聊聊天"},
        )
        assert first.status_code == 200
        first_payload = first.json()
        assert first_payload["provider"]["status"] == "ok"

        health = client.get("/health")
        assert health.status_code == 200
        llm = health.json()["providers"]["llm"]
        assert llm["reason"] == "CPA_HEALTH_BUDGET_EXCEEDED"
        assert llm["chat_model_verified"] is True
        assert llm["resolved_model"] == "grok-4.6-build"

        second = client.post(
            "/dialogue/turn",
            json={
                "user_id": "u01",
                "message": "继续聊聊",
                "styling_session_id": first_payload["styling_session_id"],
            },
        )
        assert second.status_code == 200
        assert second.json()["provider"]["status"] == "ok"
        assert second.json()["provider"]["resolved_model"] == "grok-4.6-build"
    assert dialogue_calls == 2


def test_vision_interaction_budget_cancels_slow_provider_for_qualitative_fallback(
    monkeypatch, offline_settings
) -> None:
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        cpa_timeout_seconds=120,
        vision_interaction_budget_seconds=0.02,
    )
    state = {"attempted": 0, "cancelled": False}

    async def slow_post(_self, _url, **_kwargs):
        state["attempted"] += 1
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    monkeypatch.setattr(httpx.AsyncClient, "post", slow_post)
    record = AssetRecord(
        asset_id="asset_budget",
        user_id="u01",
        styling_session_id="session_budget",
        angle="front",
        media_type="image/png",
        size_bytes=8,
        width=2,
        height=2,
        visual_quality="usable",
        created_at=datetime.now().astimezone(),
        trace_id="trace_asset_budget",
    )
    adapter = VisionAdapter(settings)
    started = time.perf_counter()
    with pytest.raises(VisionUnavailable) as captured:
        asyncio.run(adapter.inspect([(record, b"fakepng")]))
    elapsed = time.perf_counter() - started
    assert elapsed < 1
    assert captured.value.provider_trace["status"] == "timeout"
    assert captured.value.provider_trace["interaction_budget_seconds"] == 0.02
    assert state == {"attempted": 1, "cancelled": True}
