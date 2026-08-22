from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from profagent.app import create_app


_LEAK_MARKER = "UPSTREAM_PRIVATE_BODY_MUST_NOT_LEAK"
_SCENE_CONTENT = json.dumps(
    {
        "intent": "recommend",
        "occasion": "date",
        "goals": ["polished"],
    },
    ensure_ascii=False,
    separators=(",", ":"),
)
_DIALOGUE_CONTENT = json.dumps(
    {
        "reply": "我在，慢慢说。",
        "action": "chat",
        "control": "none",
        "scene_advisory": None,
        "suggested_replies": [],
    },
    ensure_ascii=False,
    separators=(",", ":"),
)
_MEMORY_CONTENT = (
    '{"candidates":[{"canonical_kind":"color_preference",'
    '"canonical_value":"navy","applicability_tags":[],'
    '"confidence_band":"high"}]}'
)


def _valid_outer(content: str) -> str:
    return json.dumps(
        {
            "model": "grok-4.6-build",
            "choices": [{"message": {"content": content}}],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _malformed_outer(content: str, case: str) -> str:
    encoded = json.dumps(content, ensure_ascii=False)
    valid_message = f'{{"content":{encoded}}}'
    valid_choice = f'{{"message":{valid_message}}}'
    if case == "outer_duplicate":
        return (
            '{"model":"' + _LEAK_MARKER + '","model":"grok-4.6-build",'
            f'"choices":[{valid_choice}]}}'
        )
    if case == "outer_extra":
        return (
            '{"model":"grok-4.6-build",'
            f'"choices":[{valid_choice}],"unknown":"{_LEAK_MARKER}"}}'
        )
    if case == "choice_duplicate":
        bad_message = json.dumps({"content": _LEAK_MARKER}, separators=(",", ":"))
        return (
            '{"model":"grok-4.6-build","choices":['
            f'{{"message":{bad_message},"message":{valid_message}}}]}}'
        )
    if case == "choice_extra":
        return (
            '{"model":"grok-4.6-build","choices":['
            f'{{"message":{valid_message},"unknown":"{_LEAK_MARKER}"}}]}}'
        )
    if case == "message_duplicate":
        return (
            '{"model":"grok-4.6-build","choices":[{"message":{'
            f'"content":"{_LEAK_MARKER}","content":{encoded}}}}}]}}'
        )
    if case == "message_extra":
        return (
            '{"model":"grok-4.6-build","choices":[{"message":{'
            f'"content":{encoded},"unknown":"{_LEAK_MARKER}"}}}}]}}'
        )
    if case == "outer_missing":
        return f'{{"choices":[{valid_choice}]}}'
    if case == "choices_wrong_type":
        return '{"model":"grok-4.6-build","choices":{}}'
    if case == "choices_multiple":
        return (
            '{"model":"grok-4.6-build",'
            f'"choices":[{valid_choice},{valid_choice}]}}'
        )
    raise AssertionError(case)


_OUTER_CASES = (
    "outer_duplicate",
    "outer_extra",
    "choice_duplicate",
    "choice_extra",
    "message_duplicate",
    "message_extra",
    "outer_missing",
    "choices_wrong_type",
    "choices_multiple",
)


def _inner_case(route: str, case: str) -> str:
    if route == "scene":
        if case == "inner_duplicate":
            return (
                '{"intent":"buy","intent":"recommend",'
                '"occasion":"date","goals":["polished"]}'
            )
        if case == "inner_extra":
            return (
                '{"intent":"recommend","occasion":"date",'
                f'"goals":["polished"],"unknown":"{_LEAK_MARKER}"}}'
            )
        if case == "inner_missing":
            return '{"intent":"recommend","occasion":"date"}'
        if case == "inner_wrong_type":
            return '{"intent":"recommend","occasion":"date","goals":"polished"}'
    if route == "dialogue":
        prefix = (
            '{"reply":"我在，慢慢说。","action":"chat","control":"none",'
        )
        if case == "inner_duplicate":
            return (
                prefix
                + '"action":"support","scene_advisory":null,'
                '"suggested_replies":[]}'
            )
        if case == "inner_extra":
            return (
                prefix
                + f'"scene_advisory":null,"suggested_replies":[],"unknown":"{_LEAK_MARKER}"}}'
            )
        if case == "inner_missing":
            return prefix + '"scene_advisory":null}'
        if case == "inner_wrong_type":
            return prefix + '"scene_advisory":null,"suggested_replies":{}}'
        if case == "nested_duplicate":
            return (
                prefix
                + '"scene_advisory":{"intent":"buy","intent":"recommend",'
                '"occasion":"date","goals":["polished"]},'
                '"suggested_replies":[]}'
            )
        if case == "nested_extra":
            return (
                prefix
                + '"scene_advisory":{"intent":"recommend","occasion":"date",'
                f'"goals":["polished"],"unknown":"{_LEAK_MARKER}"}},'
                '"suggested_replies":[]}'
            )
    if route == "memory":
        if case == "inner_duplicate":
            return '{"candidates":[],"candidates":[]}'
        if case == "inner_extra":
            return f'{{"candidates":[],"unknown":"{_LEAK_MARKER}"}}'
        if case == "inner_missing":
            return '{}'
        if case == "inner_wrong_type":
            return '{"candidates":{}}'
        if case == "nested_duplicate":
            return (
                '{"candidates":[{"canonical_kind":"color_avoidance",'
                '"canonical_kind":"color_preference","canonical_value":"navy",'
                '"applicability_tags":[],"confidence_band":"high"}]}'
            )
        if case == "nested_extra":
            return (
                '{"candidates":[{"canonical_kind":"color_preference",'
                '"canonical_value":"navy","applicability_tags":[],'
                f'"confidence_band":"high","unknown":"{_LEAK_MARKER}"}}]}}'
            )
    raise AssertionError((route, case))


def _response(url: str, raw_body: str) -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("POST", url),
        content=raw_body.encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def _sqlite_dump(path) -> str:
    connection = sqlite3.connect(path)
    try:
        return "\n".join(
            str(value)
            for table in (
                "memory_proposals",
                "memory_records",
                "memory_outbox",
                "memory_candidate_requests",
                "memory_candidates",
            )
            for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            for value in row
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "case",
    _OUTER_CASES
    + ("inner_duplicate", "inner_extra", "inner_missing", "inner_wrong_type"),
)
def test_scene_cpa_closed_response_contract_degrades_without_raw_leak(
    case, monkeypatch, tmp_path, offline_settings
) -> None:
    path = tmp_path / f"scene_contract_{case}.sqlite3"
    content = _SCENE_CONTENT if case in _OUTER_CASES else _inner_case("scene", case)
    raw_body = (
        _malformed_outer(content, case)
        if case in _OUTER_CASES
        else _valid_outer(content)
    )
    calls = 0

    async def malformed_post(_self, url, **_kwargs):
        nonlocal calls
        calls += 1
        return _response(url, raw_body)

    monkeypatch.setattr(httpx.AsyncClient, "post", malformed_post)
    app = create_app(
        replace(
            offline_settings,
            cpa_text_enabled=True,
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/scene/parse",
            json={
                "user_id": "u01",
                "query_text": "今晚约会想穿得利落一些",
                "request_id": f"strict_scene_{case}",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert calls == 1
        assert body["backend"] == "rule_fallback"
        trace_response = client.get(
            f"/trace/{body['trace_id']}", params={"user_id": "u01"}
        )
        assert trace_response.status_code == 200, trace_response.text
        trace = trace_response.json()["trace"]
        assert trace["provider"]["status"] == "degraded"
        assert trace["provider"]["reason_code"] == "CPA_PROVIDER_INVALID_RESPONSE"
        assert _LEAK_MARKER not in json.dumps(
            {"response": body, "trace": trace}, ensure_ascii=False
        )
        assert _LEAK_MARKER not in json.dumps(
            app.state.services.state.__dict__, ensure_ascii=False, default=str
        )


@pytest.mark.parametrize(
    "case",
    _OUTER_CASES
    + (
        "inner_duplicate",
        "inner_extra",
        "inner_missing",
        "inner_wrong_type",
        "nested_duplicate",
        "nested_extra",
    ),
)
def test_dialogue_cpa_closed_response_contract_falls_back_without_raw_leak(
    case, monkeypatch, tmp_path, offline_settings
) -> None:
    path = tmp_path / f"dialogue_contract_{case}.sqlite3"
    content = (
        _DIALOGUE_CONTENT
        if case in _OUTER_CASES
        else _inner_case("dialogue", case)
    )
    raw_body = (
        _malformed_outer(content, case)
        if case in _OUTER_CASES
        else _valid_outer(content)
    )
    calls = 0

    async def malformed_post(_self, url, **_kwargs):
        nonlocal calls
        calls += 1
        return _response(url, raw_body)

    monkeypatch.setattr(httpx.AsyncClient, "post", malformed_post)
    app = create_app(
        replace(
            offline_settings,
            cpa_text_enabled=True,
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/dialogue/turn",
            json={
                "user_id": "u01",
                "message": "先聊聊天",
                "request_id": f"strict_dialogue_{case}",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert calls == 1
        assert body["provider"]["status"] == "fallback"
        assert body["provider"]["generation_source"] == "local_fallback"
        assert body["provider"]["reason_code"] == "CPA_DIALOGUE_OUTPUT_REJECTED"
        trace_response = client.get(
            f"/trace/{body['trace_id']}", params={"user_id": "u01"}
        )
        assert trace_response.status_code == 200, trace_response.text
        trace = trace_response.json()["trace"]
        assert trace["provider"]["status"] == "fallback"
        assert _LEAK_MARKER not in json.dumps(
            {"response": body, "trace": trace}, ensure_ascii=False
        )
        assert _LEAK_MARKER not in json.dumps(
            app.state.services.dialogue.__dict__, ensure_ascii=False, default=str
        )


@pytest.mark.parametrize(
    "case",
    _OUTER_CASES
    + (
        "inner_duplicate",
        "inner_extra",
        "inner_missing",
        "inner_wrong_type",
        "nested_duplicate",
        "nested_extra",
    ),
)
def test_memory_cpa_closed_response_contract_writes_nothing_and_leaks_nothing(
    case, monkeypatch, tmp_path, offline_settings
) -> None:
    path = tmp_path / f"memory_contract_{case}.sqlite3"
    content = (
        _MEMORY_CONTENT
        if case in _OUTER_CASES
        else _inner_case("memory", case)
    )
    raw_body = (
        _malformed_outer(content, case)
        if case in _OUTER_CASES
        else _valid_outer(content)
    )
    calls = 0

    async def malformed_post(_self, url, **_kwargs):
        nonlocal calls
        calls += 1
        return _response(url, raw_body)

    monkeypatch.setattr(httpx.AsyncClient, "post", malformed_post)
    app = create_app(
        replace(
            offline_settings,
            cpa_text_enabled=True,
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/memory/candidates/extract",
            json={
                "user_id": "u01",
                "namespace": "stylist",
                "text": "我偏爱藏青色",
                "request_id": f"strict_memory_{case}",
            },
        )
        assert response.status_code == 503, response.text
        assert calls == 1
        assert response.json()["error"]["message"] == (
            "memory candidate provider unavailable"
        )
        assert app.state.services.traces._records == {}
        persisted = _sqlite_dump(path)
        assert persisted == ""
        assert _LEAK_MARKER not in response.text
        assert _LEAK_MARKER not in persisted
