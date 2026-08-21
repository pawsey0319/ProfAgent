from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from profagent.app import create_app
from profagent.memory_candidates import (
    COLOR_VALUES,
    CanonicalMemoryCandidate,
    MemoryCandidateError,
    MemoryCandidatePrefilter,
    SessionPreference,
    SessionPreferenceStore,
    canonicalize_candidate,
)
from profagent.memory_service import (
    MemoryConfirmInput,
    MemoryProposeInput,
    MemoryService,
)
from profagent.memory_repository import SqlMemoryRepository
from profagent.models import SceneParseInput
from profagent.providers import ProviderUnavailable
from profagent.tracing import TraceStore


_PROVIDER_SUCCESS_CONTENT = (
    '{"candidates":[{"canonical_kind":"color_preference",'
    '"canonical_value":"navy","applicability_tags":[],'
    '"confidence_band":"high"}]}'
)


def _provider_response(
    url: str,
    *,
    content: str = _PROVIDER_SUCCESS_CONTENT,
    model: str = "grok-4.6-build",
) -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("POST", url),
        json={
            "model": model,
            "choices": [{"message": {"content": content}}],
        },
    )


def test_color_candidates_map_to_closed_content_without_raw_text() -> None:
    mapped = canonicalize_candidate("color_preference", "navy")

    assert mapped.memory_type == "preference"
    assert mapped.memory_class == "preference_event"
    assert mapped.content == "偏爱藏青色"
    assert mapped.soft_term == "color:navy"
    assert "navy" not in mapped.confirmation_copy


def test_all_fifteen_color_candidates_are_explicit_and_unknown_values_fail() -> None:
    assert COLOR_VALUES == (
        "black",
        "white",
        "gray",
        "navy",
        "beige",
        "brown",
        "khaki",
        "red",
        "pink",
        "orange",
        "yellow",
        "green",
        "blue",
        "purple",
        "multi",
    )
    preferred = {
        canonicalize_candidate("color_preference", value).content
        for value in COLOR_VALUES
    }
    avoided = {
        canonicalize_candidate("color_avoidance", value).content
        for value in COLOR_VALUES
    }
    assert len(preferred) == len(avoided) == 15
    assert canonicalize_candidate(" color_avoidance ", " PURPLE ").content == "不穿紫色"
    with pytest.raises(MemoryCandidateError, match="controlled closure"):
        canonicalize_candidate("color_preference", "ultraviolet")
    with pytest.raises(MemoryCandidateError, match="controlled closure"):
        canonicalize_candidate("arbitrary_kind", "navy")


def test_canonical_candidate_is_frozen_and_forbids_extra_fields() -> None:
    mapped = canonicalize_candidate("style_preference", "simple")
    with pytest.raises(ValidationError):
        CanonicalMemoryCandidate.model_validate(
            {**mapped.model_dump(), "raw_text": "把这句原文存下来"}
        )
    with pytest.raises(ValidationError):
        mapped.content = "任意改写"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("kind", "value", "content", "soft_term"),
    (
        ("style_preference", "simple", "偏爱简洁风格", "style:simple"),
        ("garment_preference", "straight_trousers", "偏爱直筒裤", "fit:straight"),
        ("garment_avoidance", "high_heels", "不穿高跟鞋", None),
        (
            "comfort_preference",
            "long_walk",
            "长时间站立时优先选择适合久走的鞋",
            "comfort:long_walk",
        ),
        (
            "occasion_preference",
            "date",
            "在约会场合优先沿用已确认偏好",
            None,
        ),
    ),
)
def test_current_non_color_vocabulary_is_closed(
    kind: str, value: str, content: str, soft_term: str | None
) -> None:
    candidate = canonicalize_candidate(kind, value)
    assert candidate.content == content
    assert candidate.soft_term == soft_term
    with pytest.raises(MemoryCandidateError):
        canonicalize_candidate(kind, f"{value}_from_provider")


def _repository_write_counts(path) -> dict[str, int]:
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
    return counts


def _provider_evidence() -> dict[str, object]:
    return {
        "attempted": True,
        "status": "ok",
        "requested_model": "grok4.6",
        "transport_model": "grok-4.6-high",
        "resolved_model": "grok-4.6-build",
        "model_verified": True,
        "latency_ms": 1.0,
    }


def _install_candidate_batch(app, *rows: tuple[str, str, str]) -> list[int]:
    calls = [0]

    async def extract(_text, *, allowed_kinds, allowed_values):
        calls[0] += 1
        return (
            tuple(
                {
                    "canonical_kind": kind,
                    "canonical_value": value,
                    "applicability_tags": [],
                    "confidence_band": confidence,
                }
                for kind, value, confidence in rows
            ),
            _provider_evidence(),
        )

    app.state.services.llm.extract_memory_candidates = extract
    return calls


def _candidate_extract_payload(
    *,
    request_id: str,
    text: str = "我偏爱藏青色和简洁风格",
    styling_session_id: str | None = None,
) -> dict[str, object]:
    return {
        "user_id": "u01",
        "styling_session_id": styling_session_id,
        "namespace": "stylist",
        "text": text,
        "request_id": request_id,
    }


def _open_styling_session(
    client: TestClient, styling_session_id: str, request_id: str
) -> None:
    response = client.post(
        "/scene/parse",
        json={
            "user_id": "u01",
            "query_text": "下周通勤想穿得简洁一点",
            "request_id": request_id,
            "styling_session_id": styling_session_id,
            "event_horizon": "soon",
        },
    )
    assert response.status_code == 200, response.text


def _sqlite_text(path) -> str:
    connection = sqlite3.connect(path)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if not str(row[0]).startswith("sqlite_")
        ]
        return "\n".join(
            str(value)
            for table in tables
            for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()
            for value in row
        )
    finally:
        connection.close()


def test_http_extract_is_multicard_opaque_idempotent_and_raw_free(
    tmp_path, offline_settings
) -> None:
    raw = "我偏爱藏青色和简洁风格"
    path = tmp_path / "candidate_http_extract.sqlite3"
    settings = replace(
        offline_settings,
        database_url=f"sqlite:///{path.as_posix()}",
    )
    app = create_app(settings)
    calls = _install_candidate_batch(
        app,
        ("color_preference", "navy", "high"),
        ("style_preference", "simple", "medium"),
    )

    with TestClient(app) as client:
        payload = _candidate_extract_payload(request_id="memory_extract_01", text=raw)
        response = client.post("/memory/candidates/extract", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"request_id", "status", "candidates", "trace_id"}
        assert body["request_id"] == "memory_extract_01"
        assert body["status"] == "ready"
        assert len(body["candidates"]) == 2
        assert calls == [1]
        for card in body["candidates"]:
            assert set(card) == {
                "candidate_id",
                "confirmation_copy",
                "confidence_band",
                "conflict_copy",
                "allowed_actions",
            }
            assert card["candidate_id"].startswith("mcand_")
            assert raw not in json.dumps(card, ensure_ascii=False)
            assert "canonical" not in json.dumps(card, ensure_ascii=False)
            assert card["allowed_actions"] == [
                "remember",
                "reject",
                "rephrase",
            ]

        replay = client.post("/memory/candidates/extract", json=payload)
        assert replay.status_code == 200
        assert replay.json() == body
        assert calls == [1]
        listed = client.get("/memory", params={"user_id": "u01"}).json()
        assert listed["proposals"] == []

        conflicting = client.post(
            "/memory/candidates/extract",
            json={**payload, "text": "我偏爱米色"},
        )
        assert conflicting.status_code == 409
        assert raw not in json.dumps(
            app.state.services.traces._records, ensure_ascii=False, default=str
        )
        assert raw not in _sqlite_text(path)


def test_http_sensitive_extract_blocks_before_provider_and_leaks_no_raw_text(
    tmp_path, offline_settings
) -> None:
    raw = "我的手机号是13812345678，请记住我喜欢藏青色"
    path = tmp_path / "candidate_http_sensitive.sqlite3"
    app = create_app(
        replace(
            offline_settings,
            cpa_text_enabled=True,
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    provider = app.state.services.llm
    before = provider.memory_candidate_operation_count
    with TestClient(app) as client:
        response = client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(
                request_id="memory_sensitive_01", text=raw
            ),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "blocked"
        assert response.json()["candidates"] == []
        assert provider.memory_candidate_operation_count == before == 0
        assert _repository_write_counts(path) == {
            "memory_proposals": 0,
            "memory_records": 0,
            "memory_outbox": 0,
        }
        connection = sqlite3.connect(path)
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_candidate_requests"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_candidates"
        ).fetchone()[0] == 0
        connection.close()
        assert raw not in response.text
        assert raw not in _sqlite_text(path)
        assert raw not in json.dumps(
            app.state.services.traces._records, ensure_ascii=False, default=str
        )


def test_http_provider_failure_is_honest_and_creates_no_candidate_state(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "candidate_http_provider_failure.sqlite3"
    app = create_app(
        replace(
            offline_settings,
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(request_id="memory_failure_01"),
        )
        assert response.status_code == 503
        assert response.json()["error"]["message"] == (
            "memory candidate provider unavailable"
        )
        assert "CPA 生成" not in response.text
        assert _repository_write_counts(path) == {
            "memory_proposals": 0,
            "memory_records": 0,
            "memory_outbox": 0,
        }
        connection = sqlite3.connect(path)
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_candidate_requests"
        ).fetchone()[0] == 0
        connection.close()
        assert app.state.services.traces._records == {}


def test_http_decision_rejects_browser_authority_fields(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "candidate_http_authority.sqlite3"
    app = create_app(
        replace(
            offline_settings,
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    _install_candidate_batch(app, ("color_preference", "navy", "high"))

    with TestClient(app) as client:
        extracted = client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(request_id="memory_authority_01"),
        )
        assert extracted.status_code == 200, extracted.text
        candidate_id = extracted.json()["candidates"][0]["candidate_id"]
        forbidden = {
            "canonical_kind": "color_preference",
            "canonical_value": "navy",
            "memory_class": "preference_event",
            "supersedes_memory_id": "mem_fake",
            "confirmation_count": 999,
            "source": "user_confirmed",
            "acl_visibility": "team",
            "ranking_score": 1.0,
            "commit": True,
        }
        base = {
            "user_id": "u01",
            "styling_session_id": None,
            "decision": "remember",
            "idempotency_key": "memory_decision_authority",
        }
        for field, value in forbidden.items():
            response = client.post(
                f"/memory/candidates/{candidate_id}/decide",
                json={**base, field: value},
            )
            assert response.status_code == 422, (field, response.text)
            assert str(value) not in response.text
        listed = client.get("/memory", params={"user_id": "u01"}).json()
        assert listed["proposals"] == []
        assert listed["records"] == []
        assert app.state.services.memory.repository.candidate_state(
            candidate_id
        )["status"] == "active"


def test_http_remember_is_acl_bound_idempotent_and_restart_safe(
    tmp_path, offline_settings
) -> None:
    raw = "请长期记住我偏爱藏青色"
    path = tmp_path / "candidate_http_remember.sqlite3"
    settings = replace(
        offline_settings,
        database_url=f"sqlite:///{path.as_posix()}",
    )
    app = create_app(settings)
    _install_candidate_batch(app, ("color_preference", "navy", "high"))

    with TestClient(app) as client:
        extracted = client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(
                request_id="memory_remember_01", text=raw
            ),
        )
        assert extracted.status_code == 200, extracted.text
        candidate_id = extracted.json()["candidates"][0]["candidate_id"]
        decision = {
            "user_id": "u01",
            "styling_session_id": None,
            "decision": "remember",
            "idempotency_key": "memory_decision_remember_01",
        }
        cross_owner = client.post(
            f"/memory/candidates/{candidate_id}/decide",
            json={**decision, "user_id": "u02"},
        )
        unknown = client.post(
            "/memory/candidates/mcand_unknown/decide", json=decision
        )
        assert cross_owner.status_code == unknown.status_code == 404
        assert cross_owner.json()["error"]["message"] == unknown.json()["error"]["message"]

        committed = client.post(
            f"/memory/candidates/{candidate_id}/decide", json=decision
        )
        assert committed.status_code == 200, committed.text
        assert committed.json()["status"] == "committed"
        assert committed.json()["record_id"].startswith("mem_")
        replay = client.post(
            f"/memory/candidates/{candidate_id}/decide", json=decision
        )
        assert replay.status_code == 200
        assert replay.json() == committed.json()
        conflict = client.post(
            f"/memory/candidates/{candidate_id}/decide",
            json={**decision, "decision": "reject"},
        )
        assert conflict.status_code == 409
        assert _repository_write_counts(path)["memory_records"] == 1
        assert _repository_write_counts(path)["memory_outbox"] == 1
        assert raw not in _sqlite_text(path)

    restarted = create_app(settings)
    with TestClient(restarted) as client:
        listed = client.get("/memory", params={"user_id": "u01"})
        assert listed.status_code == 200
        body = listed.json()
        assert body["proposals"] == []
        assert [record["content"] for record in body["records"]] == ["偏爱藏青色"]
        assert raw not in listed.text


def test_http_candidate_receipt_survives_restart_and_legacy_confirm_cannot_bypass(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "candidate_http_restart.sqlite3"
    settings = replace(
        offline_settings,
        database_url=f"sqlite:///{path.as_posix()}",
    )
    app = create_app(settings)
    calls = _install_candidate_batch(
        app, ("style_preference", "simple", "high")
    )
    payload = _candidate_extract_payload(request_id="memory_restart_01")
    with TestClient(app) as client:
        extracted = client.post(
            "/memory/candidates/extract", json=payload
        )
        assert extracted.status_code == 200, extracted.text
        body = extracted.json()
        candidate_id = body["candidates"][0]["candidate_id"]
        connection = sqlite3.connect(path)
        proposal_id = connection.execute(
            "SELECT proposal_id FROM memory_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()[0]
        connection.close()
        listed = client.get("/memory", params={"user_id": "u01"}).json()
        assert listed["proposals"] == []
        bypass = client.post(
            f"/memory/{proposal_id}/confirm",
            json={"user_id": "u01", "decision": "confirm"},
        )
        assert bypass.status_code == 404
        assert calls == [1]

    restarted = create_app(settings)
    restarted_calls = _install_candidate_batch(
        restarted, ("style_preference", "business", "low")
    )
    with TestClient(restarted) as client:
        restarted_bypass = client.post(
            f"/memory/{proposal_id}/confirm",
            json={"user_id": "u01", "decision": "confirm"},
        )
        assert restarted_bypass.status_code == 404
        replay = client.post("/memory/candidates/extract", json=payload)
        assert replay.status_code == 200
        assert replay.json()["candidates"] == body["candidates"]
        assert restarted_calls == [0]
        decided = client.post(
            f"/memory/candidates/{candidate_id}/decide",
            json={
                "user_id": "u01",
                "styling_session_id": None,
                "decision": "remember",
                "idempotency_key": "memory_decision_restart_01",
            },
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "committed"
        listed = client.get("/memory", params={"user_id": "u01"}).json()
        assert [record["content"] for record in listed["records"]] == [
            "偏爱简洁风格"
        ]


def test_http_candidate_batch_fault_rolls_back_proposals_receipt_and_legacy_surface(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "candidate_batch_atomic_fault.sqlite3"
    app = create_app(
        replace(
            offline_settings,
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    _install_candidate_batch(
        app,
        ("color_preference", "navy", "high"),
        ("style_preference", "simple", "medium"),
    )
    fault_points: list[str] = []

    def fail_inside_candidate_transaction(point: str) -> None:
        fault_points.append(point)
        if point == "after_candidate_proposals":
            raise RuntimeError("injected candidate batch transaction fault")

    app.state.services.memory.repository._candidate_fault_injector = (
        fail_inside_candidate_transaction
    )
    with TestClient(app) as client:
        response = client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(request_id="memory_batch_fault_01"),
        )
        assert response.status_code == 409
        assert fault_points == ["after_candidate_proposals"]
        listed = client.get("/memory", params={"user_id": "u01"}).json()
        assert listed["proposals"] == []
        assert listed["records"] == []
        assert app.state.services.traces._records == {}

    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_proposals"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_candidate_requests"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_candidates"
    ).fetchone()[0] == 0
    connection.close()

    restarted = create_app(
        replace(
            offline_settings,
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    with TestClient(restarted) as client:
        listed = client.get("/memory", params={"user_id": "u01"}).json()
        assert listed["proposals"] == []
        assert listed["records"] == []
        guessed = client.post(
            "/memory/mprop_fault_guess/confirm",
            json={"user_id": "u01", "decision": "confirm"},
        )
        assert guessed.status_code == 404


@pytest.mark.parametrize(
    ("decision", "expected_status", "expected_records", "expected_outbox", "expected_working"),
    (
        ("remember", "committed", 1, 1, 0),
        ("session_only", "session_only", 0, 0, 1),
        ("reject", "rejected", 0, 0, 0),
        ("rephrase", "rejected", 0, 0, 0),
    ),
)
def test_http_candidate_decision_fault_rolls_back_truth_receipt_and_context_then_retries(
    decision,
    expected_status,
    expected_records,
    expected_outbox,
    expected_working,
    tmp_path,
    offline_settings,
) -> None:
    path = tmp_path / f"candidate_decision_atomic_fault_{decision}.sqlite3"
    settings = replace(
        offline_settings,
        database_url=f"sqlite:///{path.as_posix()}",
    )
    app = create_app(settings)
    _install_candidate_batch(app, ("style_preference", "simple", "high"))
    fault_points: list[str] = []

    with TestClient(app) as client:
        _open_styling_session(
            client, f"session_atomic_{decision}", f"scene_atomic_{decision}"
        )
        extracted = client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(
                request_id=f"memory_atomic_{decision}",
                styling_session_id=f"session_atomic_{decision}",
            ),
        )
        assert extracted.status_code == 200, extracted.text
        candidate_id = extracted.json()["candidates"][0]["candidate_id"]
        connection = sqlite3.connect(path)
        proposal_id = connection.execute(
            "SELECT proposal_id FROM memory_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()[0]
        connection.close()

        def fail_inside_decision_transaction(point: str) -> None:
            fault_points.append(point)
            if point == "after_candidate_truth":
                raise RuntimeError("injected candidate decision transaction fault")

        app.state.services.memory.repository._candidate_fault_injector = (
            fail_inside_decision_transaction
        )
        trace_ids_before_fault = set(app.state.services.traces._records)
        payload = {
            "user_id": "u01",
            "styling_session_id": f"session_atomic_{decision}",
            "decision": decision,
            "idempotency_key": f"candidate_atomic_key_{decision}",
        }
        failed = client.post(
            f"/memory/candidates/{candidate_id}/decide", json=payload
        )
        assert failed.status_code == 409
        assert "after_candidate_truth" in fault_points
        assert set(app.state.services.traces._records) == trace_ids_before_fault
        bypass = client.post(
            f"/memory/{proposal_id}/confirm",
            json={"user_id": "u01", "decision": "confirm"},
        )
        assert bypass.status_code == 404

    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT status FROM memory_proposals WHERE proposal_id=?", (proposal_id,)
    ).fetchone()[0] == "proposed"
    assert connection.execute(
        "SELECT status FROM memory_candidates WHERE candidate_id=?", (candidate_id,)
    ).fetchone()[0] == "active"
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_candidate_decisions"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_candidate_working_context"
    ).fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 0
    connection.close()

    restarted = create_app(settings)
    with TestClient(restarted) as client:
        _open_styling_session(
            client,
            f"session_atomic_{decision}",
            f"scene_atomic_restart_{decision}",
        )
        first = client.post(
            f"/memory/candidates/{candidate_id}/decide", json=payload
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] == expected_status
        replay = client.post(
            f"/memory/candidates/{candidate_id}/decide", json=payload
        )
        assert replay.status_code == 200
        assert replay.json() == first.json()
        if decision == "session_only":
            active = restarted.state.services.memory_candidates.session_preferences.active(
                "u01", f"session_atomic_{decision}", now=time.time()
            )
            assert len(active) == 1
            assert active[0].canonical_kind == "style_preference"

    connection = sqlite3.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == expected_records
    assert connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == expected_outbox
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_candidate_decisions"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_candidate_working_context"
    ).fetchone()[0] == expected_working
    connection.close()


def test_http_candidate_same_idempotency_concurrency_commits_truth_once(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "candidate_decision_concurrency.sqlite3"
    settings = replace(
        offline_settings,
        database_url=f"sqlite:///{path.as_posix()}",
    )
    app = create_app(settings)
    _install_candidate_batch(app, ("style_preference", "simple", "high"))

    with TestClient(app) as first_client:
        extracted = first_client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(request_id="memory_concurrency_01"),
        )
        assert extracted.status_code == 200, extracted.text
        candidate_id = extracted.json()["candidates"][0]["candidate_id"]
        payload = {
            "user_id": "u01",
            "decision": "remember",
            "idempotency_key": "candidate_concurrency_key_01",
        }
        competing_app = create_app(settings)

        with TestClient(competing_app) as second_client:
            clients = (first_client, second_client)

            def decide_once(index: int):
                return clients[index].post(
                    f"/memory/candidates/{candidate_id}/decide", json=payload
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(decide_once, range(2)))

        assert [response.status_code for response in responses] == [200, 200], [
            response.text for response in responses
        ]
        assert responses[0].json() == responses[1].json()

    connection = sqlite3.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_candidate_decisions"
    ).fetchone()[0] == 1
    connection.close()


def test_http_session_only_requires_active_owner_session_and_never_commits(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "candidate_http_session.sqlite3"
    app = create_app(
        replace(
            offline_settings,
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    _install_candidate_batch(app, ("style_preference", "simple", "medium"))

    with TestClient(app) as client:
        _open_styling_session(client, "session_memory_01", "scene_memory_01")
        extracted = client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(
                request_id="memory_session_01",
                styling_session_id="session_memory_01",
            ),
        )
        assert extracted.status_code == 200, extracted.text
        card = extracted.json()["candidates"][0]
        assert "session_only" in card["allowed_actions"]
        candidate_id = card["candidate_id"]
        decision = {
            "user_id": "u01",
            "styling_session_id": "session_memory_01",
            "decision": "session_only",
            "idempotency_key": "memory_decision_session_01",
        }
        response = client.post(
            f"/memory/candidates/{candidate_id}/decide", json=decision
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "session_only"
        assert response.json()["record_id"] is None
        replay = client.post(
            f"/memory/candidates/{candidate_id}/decide", json=decision
        )
        assert replay.json() == response.json()
        active = app.state.services.memory_candidates.session_preferences.active(
            "u01", "session_memory_01", now=time.time()
        )
        assert len(active) == 1
        assert active[0].canonical_kind == "style_preference"
        assert _repository_write_counts(path)["memory_records"] == 0
        assert _repository_write_counts(path)["memory_outbox"] == 0
        assert client.get("/memory", params={"user_id": "u01"}).json() == {
            "api_version": "r1_demo_v1",
            "user_id": "u01",
            "namespace": None,
            "proposals": [],
            "records": [],
            "retrieval_index_count": 0,
        }


def test_http_session_only_without_bound_active_session_is_rejected(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "candidate_http_missing_session.sqlite3"
    app = create_app(
        replace(
            offline_settings,
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    _install_candidate_batch(app, ("style_preference", "simple", "low"))
    with TestClient(app) as client:
        extracted = client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(request_id="memory_missing_session_01"),
        )
        candidate_id = extracted.json()["candidates"][0]["candidate_id"]
        response = client.post(
            f"/memory/candidates/{candidate_id}/decide",
            json={
                "user_id": "u01",
                "styling_session_id": None,
                "decision": "session_only",
                "idempotency_key": "memory_decision_missing_session_01",
            },
        )
        assert response.status_code == 422
        assert _repository_write_counts(path)["memory_records"] == 0
        assert _repository_write_counts(path)["memory_outbox"] == 0


def test_http_reject_suppresses_same_session_while_rephrase_does_not_persist(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "candidate_http_reject.sqlite3"
    app = create_app(
        replace(
            offline_settings,
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    _install_candidate_batch(app, ("color_preference", "navy", "high"))
    with TestClient(app) as client:
        _open_styling_session(client, "session_memory_02", "scene_memory_02")
        first = client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(
                request_id="memory_reject_01",
                styling_session_id="session_memory_02",
            ),
        ).json()
        candidate_id = first["candidates"][0]["candidate_id"]
        rejected = client.post(
            f"/memory/candidates/{candidate_id}/decide",
            json={
                "user_id": "u01",
                "styling_session_id": "session_memory_02",
                "decision": "reject",
                "idempotency_key": "memory_decision_reject_01",
            },
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"
        repeated = client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(
                request_id="memory_reject_02",
                styling_session_id="session_memory_02",
            ),
        )
        assert repeated.status_code == 200
        assert repeated.json()["status"] == "needs_rephrase"
        assert repeated.json()["candidates"] == []

        _open_styling_session(client, "session_memory_03", "scene_memory_03")
        rephrased_candidate = client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(
                request_id="memory_rephrase_01",
                styling_session_id="session_memory_03",
            ),
        ).json()["candidates"][0]["candidate_id"]
        rephrased = client.post(
            f"/memory/candidates/{rephrased_candidate}/decide",
            json={
                "user_id": "u01",
                "styling_session_id": "session_memory_03",
                "decision": "rephrase",
                "idempotency_key": "memory_decision_rephrase_01",
            },
        )
        assert rephrased.status_code == 200
        assert rephrased.json()["status"] == "rejected"
        visible_again = client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(
                request_id="memory_rephrase_02",
                styling_session_id="session_memory_03",
            ),
        )
        assert len(visible_again.json()["candidates"]) == 1
        assert _repository_write_counts(path)["memory_records"] == 0
        assert _repository_write_counts(path)["memory_outbox"] == 0


def test_http_supersede_is_server_authored_atomic_and_not_browser_selectable(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "candidate_http_supersede.sqlite3"
    app = create_app(
        replace(
            offline_settings,
            database_url=f"sqlite:///{path.as_posix()}",
        )
    )
    _install_candidate_batch(app, ("color_preference", "navy", "high"))
    with TestClient(app) as client:
        first_card = client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(request_id="memory_supersede_01"),
        ).json()["candidates"][0]
        first = client.post(
            f"/memory/candidates/{first_card['candidate_id']}/decide",
            json={
                "user_id": "u01",
                "styling_session_id": None,
                "decision": "remember",
                "idempotency_key": "memory_decision_supersede_01",
            },
        )
        assert first.status_code == 200, first.text
        first_memory_id = first.json()["record_id"]

        _install_candidate_batch(
            app, ("color_avoidance", "navy", "high")
        )
        second_card = client.post(
            "/memory/candidates/extract",
            json=_candidate_extract_payload(request_id="memory_supersede_02"),
        ).json()["candidates"][0]
        assert second_card["conflict_copy"] is not None
        second = client.post(
            f"/memory/candidates/{second_card['candidate_id']}/decide",
            json={
                "user_id": "u01",
                "styling_session_id": None,
                "decision": "remember",
                "idempotency_key": "memory_decision_supersede_02",
            },
        )
        assert second.status_code == 200, second.text
        listed = client.get("/memory", params={"user_id": "u01"}).json()
        assert len(listed["records"]) == 1
        assert listed["records"][0]["content"] == "不穿藏青色"
        assert listed["records"][0]["supersedes_memory_id"] == first_memory_id
        assert _repository_write_counts(path)["memory_records"] == 2
        assert _repository_write_counts(path)["memory_outbox"] == 3


def test_sensitive_ingress_blocks_real_provider_repository_outbox_and_trace(
    tmp_path, offline_settings
) -> None:
    raw = "我体重80kg，帮我长期记住"
    path = tmp_path / "sensitive_ingress.sqlite3"
    settings = replace(
        offline_settings,
        database_url=f"sqlite:///{path.as_posix()}",
    )
    app = create_app(settings)
    services = app.state.services
    memory = services.memory
    provider = services.llm
    traces = services.traces
    ingress = services.memory_candidates
    before = _repository_write_counts(path)

    assert ingress.memory is memory
    assert ingress.provider is provider
    assert provider.memory_candidate_operation_count == 0
    assert provider._chat_attempted is False
    result = asyncio.run(ingress.extract(raw))

    assert result.code == "SENSITIVE_MEMORY_DEFAULT_NO_WRITE"
    assert result.allowed is False
    assert provider.memory_candidate_operation_count == 0
    assert provider._chat_attempted is False
    assert _repository_write_counts(path) == before == {
        "memory_proposals": 0,
        "memory_records": 0,
        "memory_outbox": 0,
    }
    assert traces._records == {}
    assert "80" not in result.model_dump_json()
    connection = sqlite3.connect(path)
    persisted = "\n".join(
        str(value)
        for table in ("memory_proposals", "memory_records", "memory_outbox")
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in row
    )
    connection.close()
    assert raw not in persisted
    assert raw not in json.dumps(traces._records, ensure_ascii=False, default=str)
    memory.close()


def test_safe_application_ingress_uses_actual_provider_and_fails_closed_offline(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "safe_application_ingress.sqlite3"
    settings = replace(
        offline_settings,
        database_url=f"sqlite:///{path.as_posix()}",
    )
    app = create_app(settings)
    services = app.state.services
    raw = "我偏爱藏青色"

    assert services.llm.memory_candidate_operation_count == 0
    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(services.memory_candidates.extract(raw))

    assert captured.value.reason_code == "CPA_PROVIDER_DISABLED"
    assert not isinstance(captured.value.__cause__, AttributeError)
    assert services.llm.memory_candidate_operation_count == 1
    assert services.llm._chat_attempted is False
    assert _repository_write_counts(path) == {
        "memory_proposals": 0,
        "memory_records": 0,
        "memory_outbox": 0,
    }
    assert services.traces._records == {}
    assert raw not in json.dumps(
        services.traces._records, ensure_ascii=False, default=str
    )
    services.memory.close()


def test_provider_success_maps_to_server_canonical_candidate_and_verified_evidence(
    monkeypatch, tmp_path, offline_settings
) -> None:
    raw = "我偏爱藏青色"
    path = tmp_path / "provider_success.sqlite3"
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        database_url=f"sqlite:///{path.as_posix()}",
    )
    captured: dict[str, object] = {}

    async def success_post(_self, url, **kwargs):
        captured["request"] = kwargs["json"]
        return _provider_response(url)

    monkeypatch.setattr(httpx.AsyncClient, "post", success_post)
    app = create_app(settings)
    services = app.state.services

    result = asyncio.run(services.memory_candidates.extract(raw))

    assert result.allowed is True
    assert result.code == "MEMORY_TEXT_READY"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.canonical_kind == "color_preference"
    assert candidate.canonical_value == "navy"
    assert candidate.content == "偏爱藏青色"
    assert candidate.confirmation_copy == "要长期记住“偏爱藏青色”吗？"
    assert candidate.confidence_band == "high"
    assert candidate.applicability_tags == ()
    assert candidate.soft_term == "color:navy"
    assert result.provider_evidence == {
        "attempted": True,
        "status": "ok",
        "requested_model": "grok4.6",
        "transport_model": "grok-4.6-high",
        "resolved_model": "grok-4.6-build",
        "model_verified": True,
        "latency_ms": result.provider_evidence["latency_ms"],
    }
    assert isinstance(result.provider_evidence["latency_ms"], float)
    request = captured["request"]
    assert isinstance(request, dict)
    assert request["model"] == "grok-4.6-high"
    assert request["messages"][1] == {"role": "user", "content": raw}
    prompt_dump = json.dumps(request, ensure_ascii=False)
    for forbidden in (
        "u01",
        "user_id",
        "garment_id",
        "memory_id",
        "trace_id",
        "hidden_profile",
        "reasoning",
    ):
        assert forbidden not in prompt_dump
    assert raw in prompt_dump
    assert _repository_write_counts(path) == {
        "memory_proposals": 0,
        "memory_records": 0,
        "memory_outbox": 0,
    }
    assert services.traces._records == {}
    assert raw not in result.model_dump_json()
    connection = sqlite3.connect(path)
    persisted = "\n".join(
        str(value)
        for table in ("memory_proposals", "memory_records", "memory_outbox")
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in row
    )
    connection.close()
    assert raw not in persisted
    assert raw not in json.dumps(
        services.traces._records, ensure_ascii=False, default=str
    )
    services.memory.close()


@pytest.mark.parametrize(
    ("case", "content"),
    (
        ("malformed", "not-json"),
        (
            "duplicate_json_key",
            '{"candidates":[{"canonical_kind":"color_preference",'
            '"canonical_kind":"color_avoidance","canonical_value":"navy",'
            '"applicability_tags":[],"confidence_band":"high"}]}',
        ),
        (
            "duplicate_candidate",
            json.dumps(
                {
                    "candidates": [
                        {
                            "canonical_kind": "color_preference",
                            "canonical_value": "navy",
                            "applicability_tags": [],
                            "confidence_band": "high",
                        },
                        {
                            "canonical_kind": "color_preference",
                            "canonical_value": "navy",
                            "applicability_tags": [],
                            "confidence_band": "medium",
                        },
                    ]
                }
            ),
        ),
        (
            "additional_field",
            json.dumps(
                {
                    "candidates": [
                        {
                            "canonical_kind": "color_preference",
                            "canonical_value": "navy",
                            "applicability_tags": [],
                            "confidence_band": "high",
                            "reasoning": "private model body",
                        }
                    ]
                }
            ),
        ),
        (
            "unknown_enum",
            json.dumps(
                {
                    "candidates": [
                        {
                            "canonical_kind": "color_preference",
                            "canonical_value": "ultraviolet",
                            "applicability_tags": [],
                            "confidence_band": "high",
                        }
                    ]
                }
            ),
        ),
        (
            "more_than_five",
            json.dumps(
                {
                    "candidates": [
                        {
                            "canonical_kind": "color_preference",
                            "canonical_value": value,
                            "applicability_tags": [],
                            "confidence_band": "low",
                        }
                        for value in COLOR_VALUES[:6]
                    ]
                }
            ),
        ),
    ),
)
def test_provider_rejects_entire_invalid_candidate_payload_before_any_write(
    case, content, monkeypatch, tmp_path, offline_settings
) -> None:
    path = tmp_path / f"provider_invalid_{case}.sqlite3"
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        database_url=f"sqlite:///{path.as_posix()}",
    )

    async def invalid_post(_self, url, **_kwargs):
        return _provider_response(url, content=content)

    monkeypatch.setattr(httpx.AsyncClient, "post", invalid_post)
    app = create_app(settings)
    services = app.state.services

    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(services.memory_candidates.extract("我喜欢藏青色"))

    assert captured.value.reason_code == "CPA_MEMORY_CANDIDATE_INVALID_RESPONSE"
    assert _repository_write_counts(path) == {
        "memory_proposals": 0,
        "memory_records": 0,
        "memory_outbox": 0,
    }
    assert services.traces._records == {}
    assert "private model body" not in str(captured.value)
    services.memory.close()


@pytest.mark.parametrize(
    "outer_body",
    (
        (
            '{"model":"grok-4.5","model":"grok-4.6-build",'
            '"choices":[{"message":{"content":'
            + json.dumps(_PROVIDER_SUCCESS_CONTENT)
            + "}}]}"
        ),
        (
            '{"model":"grok-4.6-build","choices":[],"choices":'
            '[{"message":{"content":'
            + json.dumps(_PROVIDER_SUCCESS_CONTENT)
            + "}}]}"
        ),
    ),
)
def test_provider_rejects_duplicate_keys_in_outer_cpa_body_before_any_write(
    outer_body, monkeypatch, tmp_path, offline_settings
) -> None:
    path = tmp_path / "provider_outer_duplicate.sqlite3"
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        database_url=f"sqlite:///{path.as_posix()}",
    )

    async def duplicate_outer_post(_self, url, **_kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            content=outer_body.encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", duplicate_outer_post)
    app = create_app(settings)
    services = app.state.services

    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(services.memory_candidates.extract("我偏爱藏青色"))

    assert captured.value.reason_code == "CPA_MEMORY_CANDIDATE_INVALID_RESPONSE"
    assert _repository_write_counts(path) == {
        "memory_proposals": 0,
        "memory_records": 0,
        "memory_outbox": 0,
    }
    assert services.traces._records == {}
    services.memory.close()


def test_provider_rejects_root_candidate_envelope_extra_field(
    monkeypatch, tmp_path, offline_settings
) -> None:
    path = tmp_path / "provider_root_extra.sqlite3"
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        database_url=f"sqlite:///{path.as_posix()}",
    )
    content = json.dumps(
        {
            "candidates": [],
            "reasoning": "must never be accepted or persisted",
        }
    )

    async def root_extra_post(_self, url, **_kwargs):
        return _provider_response(url, content=content)

    monkeypatch.setattr(httpx.AsyncClient, "post", root_extra_post)
    app = create_app(settings)
    services = app.state.services

    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(services.memory_candidates.extract("我偏爱藏青色"))

    assert captured.value.reason_code == "CPA_MEMORY_CANDIDATE_INVALID_RESPONSE"
    assert "must never be accepted" not in str(captured.value)
    assert _repository_write_counts(path) == {
        "memory_proposals": 0,
        "memory_records": 0,
        "memory_outbox": 0,
    }
    assert services.traces._records == {}
    services.memory.close()


def test_provider_network_failure_is_honest_and_creates_no_state(
    monkeypatch, tmp_path, offline_settings
) -> None:
    path = tmp_path / "provider_network_failure.sqlite3"
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        database_url=f"sqlite:///{path.as_posix()}",
    )

    async def network_failure_post(_self, url, **_kwargs):
        raise httpx.ConnectError(
            "private upstream network detail",
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", network_failure_post)
    app = create_app(settings)
    services = app.state.services

    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(services.memory_candidates.extract("我偏爱藏青色"))

    assert captured.value.reason_code == "CPA_PROVIDER_UNAVAILABLE"
    assert "private upstream network detail" not in str(captured.value)
    assert services.llm.resolved_model is None
    assert _repository_write_counts(path) == {
        "memory_proposals": 0,
        "memory_records": 0,
        "memory_outbox": 0,
    }
    assert services.traces._records == {}
    services.memory.close()


def test_provider_rejects_wrong_model_before_any_write(
    monkeypatch, tmp_path, offline_settings
) -> None:
    path = tmp_path / "provider_wrong_model.sqlite3"
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        database_url=f"sqlite:///{path.as_posix()}",
    )

    async def wrong_model_post(_self, url, **_kwargs):
        return _provider_response(url, model="grok-4.5")

    monkeypatch.setattr(httpx.AsyncClient, "post", wrong_model_post)
    app = create_app(settings)
    services = app.state.services

    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(services.memory_candidates.extract("我喜欢藏青色"))

    assert captured.value.reason_code == "CPA_MODEL_VERIFICATION_FAILED"
    assert services.llm.resolved_model is None
    assert _repository_write_counts(path) == {
        "memory_proposals": 0,
        "memory_records": 0,
        "memory_outbox": 0,
    }
    assert services.traces._records == {}
    services.memory.close()


def test_provider_timeout_is_fail_closed_and_creates_no_state(
    monkeypatch, tmp_path, offline_settings
) -> None:
    path = tmp_path / "provider_timeout.sqlite3"
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        database_url=f"sqlite:///{path.as_posix()}",
    )

    async def timeout_post(_self, url, **_kwargs):
        raise httpx.ReadTimeout(
            "upstream body must not escape",
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", timeout_post)
    app = create_app(settings)
    services = app.state.services

    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(services.memory_candidates.extract("我喜欢藏青色"))

    assert captured.value.reason_code == "CPA_PROVIDER_TIMEOUT"
    assert "upstream body must not escape" not in str(captured.value)
    assert _repository_write_counts(path) == {
        "memory_proposals": 0,
        "memory_records": 0,
        "memory_outbox": 0,
    }
    assert services.traces._records == {}
    services.memory.close()


def test_provider_direct_identifier_prefilter_never_calls_transport_or_writes(
    monkeypatch, tmp_path, offline_settings
) -> None:
    raw = "我的手机号是13812345678，请记住我喜欢藏青色"
    path = tmp_path / "provider_direct_identifier.sqlite3"
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        database_url=f"sqlite:///{path.as_posix()}",
    )
    transport_calls = 0

    async def forbidden_post(_self, url, **_kwargs):
        nonlocal transport_calls
        transport_calls += 1
        return _provider_response(url)

    monkeypatch.setattr(httpx.AsyncClient, "post", forbidden_post)
    app = create_app(settings)
    services = app.state.services

    result = asyncio.run(services.memory_candidates.extract(raw))

    assert result.allowed is False
    assert result.code == "SENSITIVE_MEMORY_DEFAULT_NO_WRITE"
    assert transport_calls == 0
    assert services.llm.memory_candidate_operation_count == 0
    assert _repository_write_counts(path) == {
        "memory_proposals": 0,
        "memory_records": 0,
        "memory_outbox": 0,
    }
    assert services.traces._records == {}
    assert raw not in result.model_dump_json()
    services.memory.close()


@pytest.mark.parametrize(
    "raw",
    (
        "请记住 u01 喜欢藏青色",
        "请记住 g001 适合通勤",
        "我的手机号是13812345678，请记住我喜欢藏青色",
    ),
)
def test_provider_internal_identifier_prefilter_blocks_before_prompt_and_state(
    raw, monkeypatch, tmp_path, offline_settings
) -> None:
    path = tmp_path / "provider_internal_identifier.sqlite3"
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        database_url=f"sqlite:///{path.as_posix()}",
    )
    captured_prompts: list[dict[str, object]] = []

    async def forbidden_post(_self, url, **kwargs):
        captured_prompts.append(kwargs["json"])
        return _provider_response(url)

    monkeypatch.setattr(httpx.AsyncClient, "post", forbidden_post)
    app = create_app(settings)
    services = app.state.services

    result = asyncio.run(services.memory_candidates.extract(raw))

    assert result.allowed is False
    assert result.code == "SENSITIVE_MEMORY_DEFAULT_NO_WRITE"
    assert services.llm.memory_candidate_operation_count == 0
    assert captured_prompts == []
    assert _repository_write_counts(path) == {
        "memory_proposals": 0,
        "memory_records": 0,
        "memory_outbox": 0,
    }
    assert services.traces._records == {}
    assert raw not in result.model_dump_json()
    connection = sqlite3.connect(path)
    persisted = "\n".join(
        str(value)
        for table in ("memory_proposals", "memory_records", "memory_outbox")
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in row
    )
    connection.close()
    assert raw not in persisted
    assert raw not in json.dumps(
        services.traces._records, ensure_ascii=False, default=str
    )
    services.memory.close()


def test_provider_batch_is_rejected_whole_when_canonical_mapper_rejects(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "provider_mapper_rejection.sqlite3"
    settings = replace(
        offline_settings,
        database_url=f"sqlite:///{path.as_posix()}",
    )
    app = create_app(settings)
    services = app.state.services

    async def untrusted_batch(_text, *, allowed_kinds, allowed_values):
        assert "color_preference" in allowed_kinds
        assert "navy" in allowed_values["color_preference"]
        return (
            (
                {
                    "canonical_kind": "color_preference",
                    "canonical_value": "navy",
                    "applicability_tags": [],
                    "confidence_band": "high",
                },
                {
                    "canonical_kind": "color_preference",
                    "canonical_value": "provider_invented",
                    "applicability_tags": [],
                    "confidence_band": "low",
                },
            ),
            {
                "attempted": True,
                "status": "ok",
                "requested_model": "grok4.6",
                "transport_model": "grok-4.6-high",
                "resolved_model": "grok-4.6-build",
                "model_verified": True,
                "latency_ms": 1.0,
            },
        )

    services.llm.extract_memory_candidates = untrusted_batch
    with pytest.raises(MemoryCandidateError, match="batch rejected"):
        asyncio.run(services.memory_candidates.extract("我喜欢藏青色"))

    assert _repository_write_counts(path) == {
        "memory_proposals": 0,
        "memory_records": 0,
        "memory_outbox": 0,
    }
    assert services.traces._records == {}
    services.memory.close()


def test_direct_identifier_is_blocked_without_echoing_the_value() -> None:
    result = MemoryCandidatePrefilter().prefilter(
        "我的手机号是13812345678，请把它记下来"
    )

    assert result.code == "SENSITIVE_MEMORY_DEFAULT_NO_WRITE"
    assert result.allowed is False
    assert "13812345678" not in result.model_dump_json()


def test_session_preference_store_is_owner_session_scoped_and_ttl_bound() -> None:
    store = SessionPreferenceStore(ttl_seconds=300.0)
    first = SessionPreference(
        user_id="u01",
        styling_session_id="session_a",
        canonical_kind="color_preference",
        canonical_value="navy",
        expires_at=200.0,
    )
    replacement = SessionPreference(
        user_id="u01",
        styling_session_id="session_a",
        canonical_kind="color_preference",
        canonical_value="beige",
        expires_at=300.0,
    )
    other_session = SessionPreference(
        user_id="u01",
        styling_session_id="session_b",
        canonical_kind="style_preference",
        canonical_value="simple",
        expires_at=300.0,
    )
    store.put(first)
    store.put(replacement)
    store.put(other_session)

    assert store.active("u01", "session_a", now=250.0) == (replacement,)
    assert store.active("u01", "session_a", now=300.0) == ()
    assert store.active("u02", "session_a", now=250.0) == ()
    assert store.active("u01", "session_b", now=250.0) == (other_session,)


def _memory_service(tmp_path) -> MemoryService:
    return MemoryService(
        TraceStore(),
        database_url=f"sqlite:///{(tmp_path / 'memory.sqlite3').as_posix()}",
        root_dir=tmp_path,
    )


def _commit_candidate(service: MemoryService, kind: str, value: str):
    candidate = canonicalize_candidate(kind, value)
    proposed = service.propose(
        MemoryProposeInput(
            user_id="u01",
            styling_session_id="session_s16",
            namespace="stylist",
            type=candidate.memory_type,
            content=candidate.content,
        )
    ).proposal
    committed = service.confirm(
        proposed.proposal_id,
        MemoryConfirmInput(user_id="u01", decision="confirm"),
    )
    assert committed.record is not None
    return candidate, committed.record


def test_preferred_color_commits_closed_content_and_enters_soft_rrf(tmp_path) -> None:
    service = _memory_service(tmp_path)
    candidate, record = _commit_candidate(service, "color_preference", "navy")

    assert record.content == candidate.content == "偏爱藏青色"
    assert record.memory_class == "preference_event"
    assert [signal.applied_signal for signal in service.active_signals("u01")] == [
        "prefer_color:navy"
    ]
    terms, trace = service.retrieve_soft("u01", "今晚约会想穿藏青色")
    assert terms == ("color:navy",)
    assert trace["hard_memory_in_rrf"] is False
    assert trace["k"] == 60
    assert trace["weights"] == {
        "bm25": 1.0,
        "dense": 1.0,
        "recency": 0.75,
        "importance": 1.25,
    }
    connection = sqlite3.connect(tmp_path / "memory.sqlite3")
    quarantine = connection.execute(
        "SELECT quarantined_at,quarantine_reason FROM memory_records "
        "WHERE memory_id=?",
        (record.memory_id,),
    ).fetchone()
    connection.close()
    assert quarantine == (None, None)
    service.close()


def test_repository_integrity_accepts_canonical_and_keeps_unknown_fail_closed() -> None:
    assert SqlMemoryRepository._legacy_content_is_controlled(
        "preference", "偏爱藏青色"
    )
    assert SqlMemoryRepository._legacy_content_is_controlled(
        "constraint", "不穿紫色"
    )
    assert not SqlMemoryRepository._legacy_content_is_controlled(
        "preference", "把任意模型原文当记忆"
    )
    assert not SqlMemoryRepository._legacy_content_is_controlled(
        "constraint", "不要任意东西"
    )


def test_avoided_color_is_hard_memory_and_never_enters_rrf(tmp_path) -> None:
    service = _memory_service(tmp_path)
    _candidate, record = _commit_candidate(service, "color_avoidance", "purple")

    assert record.memory_class == "hard_constraint"
    assert [signal.applied_signal for signal in service.active_signals("u01")] == [
        "avoid_color:purple"
    ]
    terms, trace = service.retrieve_soft("u01", "紫色约会穿搭")
    assert terms == ()
    assert trace["hard_memory_in_rrf"] is False
    assert record.memory_id not in json.dumps(trace)
    service.close()


def test_avoided_color_becomes_authoritative_scene_constraint(
    tmp_path, offline_settings
) -> None:
    settings = replace(
        offline_settings,
        database_url=f"sqlite:///{(tmp_path / 'scene_memory.sqlite3').as_posix()}",
    )
    app = create_app(settings)
    services = app.state.services
    _commit_candidate(services.memory, "color_avoidance", "purple")

    scene = asyncio.run(
        services.scene_parser.parse(
            SceneParseInput(
                user_id="u01",
                styling_session_id="session_s16_scene",
                query_text="下周约会，想穿得简洁一点",
                event_horizon="soon",
            )
        )
    )

    assert "purple" in scene.constraints.taboo_colors
    assert "avoid_color:purple" not in scene.constraints.comfort_notes
    eligible = services.hard_filter.apply(
        services.repository.list_garments("u01"), scene
    )
    assert all(item.color != "purple" for item in eligible.eligible)
    assert any(
        row["reason"] == "TABOO_COLOR" for row in eligible.filtered
    )
    services.memory.close()


def test_sensitive_raw_text_never_reaches_sql_outbox_or_trace(tmp_path) -> None:
    raw = "我体重80kg，身份证是110101199001011234，请长期记住"
    traces = TraceStore()
    path = tmp_path / "blocked.sqlite3"
    service = MemoryService(
        traces,
        database_url=f"sqlite:///{path.as_posix()}",
        root_dir=tmp_path,
    )
    response = service.propose(
        MemoryProposeInput(
            user_id="u01",
            namespace="stylist",
            type="preference",
            content=raw,
        )
    )

    assert response.proposal.content is None
    assert response.proposal.commit_blocked is True
    service.close()
    connection = sqlite3.connect(path)
    persisted = "\n".join(
        str(value)
        for table in ("memory_proposals", "memory_records", "memory_outbox")
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
        for value in row
    )
    connection.close()
    assert raw not in persisted
    assert "80kg" not in persisted
    assert "110101199001011234" not in persisted
    trace_dump = json.dumps(
        traces.get(response.trace_id).model_dump(mode="json"), ensure_ascii=False
    )
    assert raw not in trace_dump
    assert "80kg" not in trace_dump
    assert "110101199001011234" not in trace_dump
