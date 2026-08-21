from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace

import httpx
import pytest
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
    assert _repository_write_counts(path) == {
        "memory_proposals": 0,
        "memory_records": 0,
        "memory_outbox": 0,
    }
    assert services.traces._records == {}
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
