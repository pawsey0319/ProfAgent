from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace

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
from profagent.tracing import TraceStore


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


def test_sensitive_text_is_blocked_before_provider_or_repository() -> None:
    calls = {"provider": 0, "repository": 0}
    result = MemoryCandidatePrefilter().prefilter("我体重80kg，帮我长期记住")

    if result.allowed:
        calls["provider"] += 1
        calls["repository"] += 1

    assert result.code == "SENSITIVE_MEMORY_DEFAULT_NO_WRITE"
    assert result.allowed is False
    assert calls == {"provider": 0, "repository": 0}
    assert "80" not in result.model_dump_json()


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
