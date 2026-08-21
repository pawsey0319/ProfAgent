from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from pydantic import ValidationError

from profagent.memory_repository import SqlMemoryRepository
from profagent.memory_service import (
    MemoryConfirmInput,
    MemoryError,
    MemoryProposeInput,
    MemoryRecord,
    MemoryService,
)
from profagent.models import SceneConstraints, SceneRequest, UICapabilities
from profagent.tracing import TraceStore, utc_now


def _service(path, root) -> MemoryService:
    return MemoryService(
        TraceStore(), database_url=f"sqlite:///{path.as_posix()}", root_dir=root
    )


def _commit(
    service: MemoryService,
    content: str,
    *,
    memory_type: str = "preference",
    decision: str = "confirm",
    edited_content: str | None = None,
    ttl_days: int | None = None,
):
    proposal = service.propose(
        MemoryProposeInput(
            user_id="u01",
            namespace="stylist",
            type=memory_type,
            content=content,
            ttl_days=ttl_days,
        )
    ).proposal
    result = service.confirm(
        proposal.proposal_id,
        MemoryConfirmInput(
            user_id="u01", decision=decision, edited_content=edited_content
        ),
    )
    assert result.record is not None
    return proposal.proposal_id, result.record


def _commit_for(
    service: MemoryService,
    *,
    user_id: str,
    namespace: str,
    memory_type: str,
    content: str,
):
    proposal = service.propose(
        MemoryProposeInput(
            user_id=user_id,
            namespace=namespace,
            type=memory_type,
            content=content,
        )
    ).proposal
    result = service.confirm(
        proposal.proposal_id,
        MemoryConfirmInput(user_id=user_id, decision="confirm"),
    )
    assert result.record is not None
    return proposal, result.record


def _scene(*, long_walk: bool = False) -> SceneRequest:
    return SceneRequest(
        request_id="req_s15",
        user_id="u01",
        styling_session_id="session_s15",
        query_text="准备出门",
        intent="recommend",
        occasion="commute",
        event_horizon="soon",
        urgency="medium",
        shopping_allowed=False,
        goals=["reliable"],
        constraints=SceneConstraints(
            comfort_notes=["long_walk"] if long_walk else []
        ),
        backend="rule_fallback",
        ui_capabilities=UICapabilities(shopping_cta=False),
        trace_id="trace_s15",
    )


def test_s15_legacy_sqlite_migration_is_idempotent_and_closed(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    now = utc_now()
    payload = {
        "memory_id": "mem_legacy",
        "user_id": "u01",
        "namespace": "stylist",
        "type": "preference",
        "content": "偏爱直筒裤",
        "status": "committed",
        "sensitivity": "non_sensitive",
        "source": "user_confirmed",
        "ttl_days": None,
        "source_proposal_id": "mprop_legacy",
        "created_at": now.isoformat(),
        "expires_at": None,
    }
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE memory_proposals (
            proposal_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,namespace TEXT NOT NULL,
            memory_type TEXT NOT NULL,status TEXT NOT NULL,sensitivity TEXT NOT NULL,
            commit_blocked INTEGER NOT NULL,record_id TEXT,deleted_at TEXT,payload_json TEXT NOT NULL
        );
        CREATE TABLE memory_records (
            memory_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,namespace TEXT NOT NULL,
            memory_type TEXT NOT NULL,status TEXT NOT NULL,sensitivity TEXT NOT NULL,
            source TEXT NOT NULL,semantic_key TEXT NOT NULL,expires_at TEXT,
            deleted_at TEXT,superseded_at TEXT,payload_json TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO memory_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "mem_legacy",
            "u01",
            "stylist",
            "preference",
            "committed",
            "non_sensitive",
            "user_confirmed",
            "soft:fit:straight",
            None,
            None,
            None,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    connection.commit()
    connection.close()

    for _ in range(2):
        repository = SqlMemoryRepository(
            f"sqlite:///{path.as_posix()}", tmp_path
        )
        repository.close()

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT * FROM memory_records WHERE memory_id='mem_legacy'"
    ).fetchone()
    migrated = json.loads(row["payload_json"])
    assert migrated["memory_class"] == "preference_event"
    assert migrated["valid_from"] == now.isoformat()
    assert migrated["valid_to"] is None
    assert migrated["supersedes_memory_id"] is None
    assert migrated["source_kind"] == "legacy_migrated"
    assert migrated["provenance_version"] == "memory_legacy_v0"
    assert migrated["consent_version"] == "legacy_confirm_v0"
    assert migrated["confirmation_count"] == 1
    assert row["truth_version"] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_outbox"
    ).fetchone()[0] == 0
    connection.close()


def test_s15_server_owned_record_fields_and_working_context_never_commit(
    tmp_path,
) -> None:
    path = tmp_path / "memory.sqlite3"
    service = _service(path, tmp_path)
    _proposal, confirmed = _commit(service, "偏爱直筒裤")
    assert confirmed.memory_class == "preference_event"
    assert confirmed.source_kind == "user_confirmed"
    assert confirmed.provenance_version == "memory_provenance_v1"
    assert confirmed.consent_version == "explicit_confirm_v1"
    assert confirmed.confirmation_count == 1
    assert confirmed.lifecycle_status == "active"
    assert confirmed.valid_from == confirmed.created_at
    assert confirmed.valid_to == confirmed.expires_at

    _proposal, edited = _commit(
        service,
        "偏爱直筒裤",
        decision="edit",
        edited_content="偏爱简洁风格",
    )
    assert edited.source_kind == "user_edited_confirmed"
    _proposal, hard = _commit(
        service, "不穿高跟鞋", memory_type="constraint"
    )
    assert hard.memory_class == "hard_constraint"
    _proposal, feedback = _commit(
        service,
        "偏好久走与长时间站立时选择舒适鞋履",
        memory_type="feedback",
    )
    assert feedback.source_kind == "feedback_confirmed"

    blocked = service.propose(
        MemoryProposeInput(
            user_id="u01",
            styling_session_id="session_s15",
            namespace="stylist",
            type="session_emotion",
            content="今天有点紧张",
        )
    )
    rejected = service.confirm(
        blocked.proposal.proposal_id,
        MemoryConfirmInput(user_id="u01", decision="confirm"),
    )
    assert rejected.record is None
    assert all(item.type != "session_emotion" for item in service.list("u01").records)

    with pytest.raises(ValidationError):
        MemoryProposeInput.model_validate(
            {
                "user_id": "u01",
                "namespace": "stylist",
                "type": "preference",
                "content": "偏爱直筒裤",
                "memory_class": "hard_constraint",
                "confirmation_count": 999,
                "context_match": 1.0,
            }
        )
    service.close()


def test_s15_outbox_is_minimal_atomic_and_cross_instance_idempotent(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "memory.sqlite3"
    service = _service(path, tmp_path)
    proposal_id = service.propose(
        MemoryProposeInput(
            user_id="u01",
            namespace="stylist",
            type="preference",
            content="偏爱直筒裤",
        )
    ).proposal.proposal_id

    def fail_outbox(**_kwargs):
        raise RuntimeError("injected outbox failure")

    monkeypatch.setattr(service.repository, "_write_outbox", fail_outbox)
    with pytest.raises(MemoryError):
        service.confirm(
            proposal_id, MemoryConfirmInput(user_id="u01", decision="confirm")
        )
    service.close()

    first = _service(path, tmp_path)
    second = _service(path, tmp_path)
    barrier = Barrier(2)
    for instance in (first, second):
        original = instance.repository.commit

        def synchronized(*args, _original=original, **kwargs):
            barrier.wait(timeout=5)
            return _original(*args, **kwargs)

        instance.repository.commit = synchronized  # type: ignore[method-assign]

    def confirm(instance: MemoryService):
        return instance.confirm(
            proposal_id, MemoryConfirmInput(user_id="u01", decision="confirm")
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, (first, second)))
    assert results[0].record is not None and results[1].record is not None
    assert results[0].record.memory_id == results[1].record.memory_id
    events = first.repository.outbox_events("u01")
    assert len(events) == 1 and events[0]["operation"] == "commit"
    assert set(events[0]) == {
        "event_id",
        "user_id",
        "namespace",
        "memory_id",
        "operation",
        "truth_version",
        "status",
        "occurred_at",
    }
    assert "偏爱直筒裤" not in json.dumps(events, ensure_ascii=False)
    first.close()
    second.close()


def test_s15_supersede_delete_expire_outbox_and_active_truth(tmp_path) -> None:
    path = tmp_path / "memory.sqlite3"
    service = _service(path, tmp_path)
    _proposal, old = _commit(service, "偏爱低调配色")
    _proposal, current = _commit(service, "偏爱低调配色")
    assert current.supersedes_memory_id == old.memory_id
    assert current.confirmation_count == 2
    assert [item.memory_id for item in service.list("u01").records] == [
        current.memory_id
    ]
    operations = [
        event["operation"] for event in service.repository.outbox_events("u01")
    ]
    assert operations.count("commit") == 2
    assert operations.count("supersede") == 1

    receipt = service.delete("u01", current.memory_id)
    assert receipt.deleted_id == current.memory_id
    assert receipt.deleted_kind == "record"
    assert receipt.user_id == "u01"
    assert receipt.namespace == "stylist"
    assert receipt.truth_version == 2
    assert service.list("u01").records == []
    assert service.repository.outbox_events("u01")[-1]["operation"] == "delete"

    _proposal, expiring = _commit(
        service, "偏爱简洁风格", ttl_days=1
    )
    service.repository.expire_ids(
        [expiring.memory_id], (utc_now() + timedelta(days=2)).isoformat()
    )
    service._refresh()
    assert service.list("u01").records == []
    assert service.repository.outbox_events("u01")[-1]["operation"] == "expire"
    service.close()


def test_s15_mutation_receipts_bind_owner_namespace_version_and_causality(
    tmp_path,
) -> None:
    path = tmp_path / "memory.sqlite3"
    service = _service(path, tmp_path)
    proposal = service.propose(
        MemoryProposeInput(
            user_id="u01",
            namespace="shared",
            type="preference",
            content="偏爱直筒裤",
        )
    ).proposal
    confirmed = service.confirm(
        proposal.proposal_id,
        MemoryConfirmInput(user_id="u01", decision="confirm"),
    )
    assert confirmed.proposal.proposal_id == proposal.proposal_id
    assert confirmed.record is not None
    assert confirmed.proposal.record_id == confirmed.record.memory_id
    assert confirmed.record.source_proposal_id == proposal.proposal_id

    deleted = service.delete("u01", confirmed.record.memory_id)
    assert deleted.user_id == "u01"
    assert deleted.namespace == "shared"
    assert deleted.truth_version == 2

    pending = service.propose(
        MemoryProposeInput(
            user_id="u01",
            namespace="stylist",
            type="preference",
            content="偏爱简洁风格",
        )
    ).proposal
    proposal_deleted = service.delete("u01", pending.proposal_id)
    assert proposal_deleted.deleted_kind == "proposal"
    assert proposal_deleted.user_id == "u01"
    assert proposal_deleted.namespace == "stylist"
    # A proposal-only mutation has no committed memory truth row/outbox version.
    assert proposal_deleted.truth_version == 0

    rejected_proposal = service.propose(
        MemoryProposeInput(
            user_id="u01",
            namespace="stylist",
            type="preference",
            content="偏爱低调配色",
        )
    ).proposal
    rejected = service.confirm(
        rejected_proposal.proposal_id,
        MemoryConfirmInput(user_id="u01", decision="reject"),
    )
    assert rejected.proposal.proposal_id == rejected_proposal.proposal_id
    assert rejected.proposal.status == "rejected"
    assert rejected.record is None
    service.close()


def test_s15_soft_projection_rebuild_and_stale_index_cannot_restore_truth(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "memory.sqlite3"
    service = _service(path, tmp_path)
    _proposal, soft = _commit(service, "偏爱直筒裤")
    _proposal, hard = _commit(
        service, "不穿高跟鞋", memory_type="constraint"
    )
    assert service.rebuild_soft_index("u01") == (soft.memory_id,)
    assert service.repository.soft_index_ids("u01", ("shared", "stylist")) == (
        soft.memory_id,
    )

    service.close()
    service = _service(path, tmp_path)
    assert service.repository.soft_index_ids("u01", ("shared", "stylist")) == (
        soft.memory_id,
    )
    assert service.retrieve_soft("u01", "直筒裤")[0] == ("fit:straight",)

    service.delete("u01", soft.memory_id)
    # A lagging projection may still contain the deleted ID, but retrieval
    # always revalidates SQL active truth and therefore cannot restore it.
    assert soft.memory_id in service.repository.soft_index_ids(
        "u01", ("shared", "stylist")
    )
    terms, trace = service.retrieve_soft("u01", "直筒裤")
    assert terms == () and soft.memory_id not in trace["selected_memory_ids"]
    assert hard.memory_id not in json.dumps(trace)

    assert service.rebuild_soft_index("u01") == ()
    assert service.repository.soft_index_ids("u01", ("shared", "stylist")) == ()
    monkeypatch.setattr(
        service.repository,
        "rebuild_soft_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("index down")),
    )
    with pytest.raises(RuntimeError):
        service.rebuild_soft_index("u01")
    assert service.active_signals("u01")[0].memory_id == hard.memory_id
    service.close()


def test_s15_cross_user_and_acl_projection_pollution_is_zero(tmp_path) -> None:
    path = tmp_path / "memory.sqlite3"
    service = _service(path, tmp_path)
    _proposal, soft = _commit(service, "偏爱直筒裤")
    assert service.rebuild_soft_index("u01") == (soft.memory_id,)

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE memory_records SET member_id='other_member' WHERE memory_id=?",
        (soft.memory_id,),
    )
    connection.commit()
    connection.close()

    # The disposable projection remains stale by design. It cannot grant ACL
    # eligibility because retrieval always starts from SQL active truth.
    assert service.repository.soft_index_ids("u01", ("stylist",)) == (
        soft.memory_id,
    )
    assert service.retrieve_soft("u01", "直筒裤", namespaces=("stylist",))[0] == ()
    assert service.retrieve_soft("u02", "直筒裤", namespaces=("stylist",))[0] == ()
    assert service.rebuild_soft_index("u01", namespaces=("stylist",)) == ()
    service.close()


def test_s15_structured_rerank_exact_formula_scene_and_determinism() -> None:
    now = utc_now()
    records = [
        MemoryRecord(
            memory_id="mem_long_walk",
            user_id="u01",
            namespace="stylist",
            type="preference",
            content="长时间站立时优先选择适合久走的鞋",
            ttl_days=None,
            source_proposal_id="mprop_long_walk",
            created_at=now,
            expires_at=None,
            valid_from=now,
            supersedes_memory_id="mem_long_walk_v2",
            confirmation_count=3,
        ),
        MemoryRecord(
            memory_id="mem_simple",
            user_id="u01",
            namespace="stylist",
            type="preference",
            content="偏爱简洁风格",
            ttl_days=None,
            source_proposal_id="mprop_simple",
            created_at=now,
            expires_at=None,
            valid_from=now,
        ),
        MemoryRecord(
            memory_id="mem_hard",
            user_id="u01",
            namespace="shared",
            type="constraint",
            content="不穿高跟鞋",
            ttl_days=None,
            source_proposal_id="mprop_hard",
            created_at=now,
            expires_at=None,
            valid_from=now,
            memory_class="hard_constraint",
        ),
    ]

    class TruthRepository:
        def load_state(self):
            return [], [], {}, {}

        def active_records(self, user_id, namespaces, _now):
            assert user_id == "u01"
            return [
                (item.model_dump(mode="json"), None)
                for item in records
                if item.namespace in namespaces
            ]

        def expire_ids(self, _ids, _expired_at):
            return None

        def close(self):
            return None

    service = MemoryService(TraceStore(), repository=TruthRepository())
    first_terms, first = service.retrieve_soft(
        "u01", "准备出门", scene=_scene(long_walk=True)
    )
    second_terms, second = service.retrieve_soft(
        "u01", "准备出门", scene=_scene(long_walk=True)
    )
    assert first_terms == second_terms
    assert first["selected_memory_ids"] == second["selected_memory_ids"]
    assert first["selected_memory_ids"][0] == "mem_long_walk"
    assert "mem_hard" not in json.dumps(first)
    assert first["rerank"] == "structured_rerank_v1"
    assert first["rerank_formula"] == {
        "rrf_norm": 0.60,
        "context_match": 0.15,
        "specificity": 0.10,
        "confirmation_strength": 0.10,
        "lexical_norm": 0.05,
    }
    scores = first["controlled_scores"]["mem_long_walk"]
    assert scores["context_match"] == 1.0
    assert scores["specificity"] == 1.0
    assert scores["confirmation_strength"] == 0.9
    assert scores["final"] == pytest.approx(
        0.60 * scores["rrf_norm"]
        + 0.15 * scores["context_match"]
        + 0.10 * scores["specificity"]
        + 0.10 * scores["confirmation_strength"]
        + 0.05 * scores["lexical_norm"],
        abs=2e-6,
    )

    _terms, ambiguous = service.retrieve_soft(
        "u01", "准备出门", scene=_scene(long_walk=False)
    )
    assert ambiguous["controlled_scores"]["mem_long_walk"]["context_match"] == 0.0
    assert ambiguous["content_logged"] is False
    assert ambiguous["vectors_logged"] is False
    assert "长时间站立" not in json.dumps(ambiguous, ensure_ascii=False)
    service.close()


def test_s15_future_valid_from_is_inactive_for_list_hard_soft_and_projection(
    tmp_path,
) -> None:
    path = tmp_path / "memory.sqlite3"
    service = _service(path, tmp_path)
    _proposal, soft = _commit(service, "偏爱直筒裤")
    _proposal, hard = _commit(
        service, "不穿高跟鞋", memory_type="constraint"
    )
    future = (utc_now() + timedelta(days=2)).isoformat()
    connection = sqlite3.connect(path)
    for memory_id in (soft.memory_id, hard.memory_id):
        raw = connection.execute(
            "SELECT payload_json FROM memory_records WHERE memory_id=?",
            (memory_id,),
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["valid_from"] = future
        connection.execute(
            "UPDATE memory_records SET valid_from=?,payload_json=? WHERE memory_id=?",
            (future, json.dumps(payload, ensure_ascii=False), memory_id),
        )
    connection.commit()
    connection.close()

    service._refresh()
    assert service.list("u01").records == []
    assert service.active_signals("u01") == ()
    assert service.retrieve_soft("u01", "直筒裤")[0] == ()
    assert service.rebuild_soft_index("u01") == ()
    assert service.consume_soft_index_outbox("u01")[0] == ()
    service.close()

    expiry_path = tmp_path / "valid_to.sqlite3"
    service = _service(expiry_path, tmp_path)
    _proposal, expiring = _commit(service, "偏爱简洁风格", ttl_days=1)
    service.close()
    earlier_dt = utc_now() - timedelta(days=3)
    earlier = earlier_dt.isoformat()
    past = (earlier_dt + timedelta(days=1)).isoformat()
    connection = sqlite3.connect(expiry_path)
    raw = connection.execute(
        "SELECT payload_json FROM memory_records WHERE memory_id=?",
        (expiring.memory_id,),
    ).fetchone()[0]
    payload = json.loads(raw)
    payload["created_at"] = earlier
    payload["valid_from"] = earlier
    payload["valid_to"] = past
    payload["expires_at"] = past
    connection.execute(
        "UPDATE memory_records SET valid_from=?,valid_to=?,expires_at=?,payload_json=? WHERE memory_id=?",
        (
            earlier,
            past,
            past,
            json.dumps(payload, ensure_ascii=False),
            expiring.memory_id,
        ),
    )
    connection.commit()
    connection.close()
    restarted = _service(expiry_path, tmp_path)
    assert restarted.list("u01").records == []
    assert [
        event["operation"] for event in restarted.repository.outbox_events("u01")
    ].count("expire") == 1
    restarted.close()


def test_s15_invalid_triplet_and_memory_class_are_quarantined(tmp_path) -> None:
    now = utc_now()
    with pytest.raises(ValidationError):
        MemoryRecord(
            memory_id="mem_bad_triplet",
            user_id="u01",
            namespace="stylist",
            type="preference",
            content="偏爱直筒裤",
            ttl_days=None,
            source_proposal_id="mprop_bad_triplet",
            created_at=now,
            expires_at=None,
            valid_from=now,
            source_kind="legacy_migrated",
            provenance_version="memory_provenance_v1",
            consent_version="legacy_confirm_v0",
        )
    with pytest.raises(ValidationError):
        MemoryRecord(
            memory_id="mem_bad_class",
            user_id="u01",
            namespace="stylist",
            type="constraint",
            content="不穿高跟鞋",
            ttl_days=None,
            source_proposal_id="mprop_bad_class",
            created_at=now,
            expires_at=None,
            valid_from=now,
            memory_class="preference_event",
        )

    path = tmp_path / "memory.sqlite3"
    service = _service(path, tmp_path)
    _proposal, triplet = _commit(service, "偏爱直筒裤")
    _proposal, wrong_class = _commit(service, "偏爱简洁风格")
    _proposal, wrong_period = _commit(service, "偏爱低调配色")
    service.close()
    connection = sqlite3.connect(path)
    for memory_id, update in (
        (
            triplet.memory_id,
            {
                "source_kind": "legacy_migrated",
                "provenance_version": "memory_provenance_v1",
                "consent_version": "legacy_confirm_v0",
            },
        ),
        (wrong_class.memory_id, {"memory_class": "hard_constraint"}),
    ):
        raw = connection.execute(
            "SELECT payload_json FROM memory_records WHERE memory_id=?",
            (memory_id,),
        ).fetchone()[0]
        payload = json.loads(raw)
        payload.update(update)
        connection.execute(
            "UPDATE memory_records SET payload_json=? WHERE memory_id=?",
            (json.dumps(payload, ensure_ascii=False), memory_id),
        )
    connection.commit()
    future = (utc_now() + timedelta(days=2)).isoformat()
    past = (utc_now() - timedelta(days=2)).isoformat()
    raw = connection.execute(
        "SELECT payload_json FROM memory_records WHERE memory_id=?",
        (wrong_period.memory_id,),
    ).fetchone()[0]
    payload = json.loads(raw)
    payload["valid_from"] = future
    payload["valid_to"] = past
    connection.execute(
        "UPDATE memory_records SET valid_from=?,valid_to=?,payload_json=? WHERE memory_id=?",
        (
            future,
            past,
            json.dumps(payload, ensure_ascii=False),
            wrong_period.memory_id,
        ),
    )
    connection.commit()
    connection.close()

    restarted = _service(path, tmp_path)
    assert restarted.list("u01").records == []
    assert restarted.retrieve_soft("u01", "直筒裤 简洁")[0] == ()
    restarted.close()
    connection = sqlite3.connect(path)
    reasons = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT memory_id,quarantine_reason FROM memory_records"
        )
    }
    assert reasons[triplet.memory_id] == "invalid_provenance_triplet"
    assert reasons[wrong_class.memory_id] == "invalid_memory_class"
    assert reasons[wrong_period.memory_id] == "invalid_validity_period"
    connection.close()


def test_s15_outbox_consumer_is_idempotent_and_rebuilds_each_lifecycle(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "memory.sqlite3"
    service = _service(path, tmp_path)
    _proposal, first = _commit(service, "偏爱低调配色")
    _proposal, hard = _commit(
        service, "不穿高跟鞋", memory_type="constraint"
    )
    projected, event_ids = service.consume_soft_index_outbox("u01")
    assert projected == (first.memory_id,)
    assert len(event_ids) == 2
    assert hard.memory_id not in projected
    assert service.consume_soft_index_outbox("u01") == (projected, ())

    _proposal, current = _commit(service, "偏爱低调配色")
    projected, event_ids = service.consume_soft_index_outbox("u01")
    assert projected == (current.memory_id,)
    assert len(event_ids) == 2  # supersede old + commit current

    deleted = service.delete("u01", current.memory_id)
    assert deleted.truth_version == 2
    assert service.consume_soft_index_outbox("u01")[0] == ()

    _proposal, expiring = _commit(service, "偏爱简洁风格", ttl_days=1)
    service.repository.expire_ids(
        [expiring.memory_id], (utc_now() + timedelta(days=2)).isoformat()
    )
    projected, event_ids = service.consume_soft_index_outbox("u01")
    assert projected == () and len(event_ids) == 2  # commit + expire

    _proposal, recoverable = _commit(service, "偏爱直筒裤")
    original = service.repository._replace_soft_index_locked

    def fail_projection(**_kwargs):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(
        service.repository, "_replace_soft_index_locked", fail_projection
    )
    with pytest.raises(RuntimeError):
        service.consume_soft_index_outbox("u01")
    monkeypatch.setattr(service.repository, "_replace_soft_index_locked", original)
    projected, event_ids = service.consume_soft_index_outbox("u01")
    assert projected == (recoverable.memory_id,) and len(event_ids) == 1
    service.close()


def test_s15_different_proposals_same_semantic_are_serialized(tmp_path) -> None:
    path = tmp_path / "memory.sqlite3"
    seed = _service(path, tmp_path)
    proposal_ids = [
        seed.propose(
            MemoryProposeInput(
                user_id="u01",
                namespace="stylist",
                type="preference",
                content="偏爱低调配色",
            )
        ).proposal.proposal_id
        for _ in range(2)
    ]
    seed.close()
    first = _service(path, tmp_path)
    second = _service(path, tmp_path)
    barrier = Barrier(2)
    for instance in (first, second):
        original = instance.repository.commit

        def synchronized(*args, _original=original, **kwargs):
            barrier.wait(timeout=5)
            return _original(*args, **kwargs)

        instance.repository.commit = synchronized  # type: ignore[method-assign]

    def confirm(pair):
        instance, proposal_id = pair
        return instance.confirm(
            proposal_id, MemoryConfirmInput(user_id="u01", decision="confirm")
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, zip((first, second), proposal_ids)))
    assert results[0].record is not None and results[1].record is not None
    assert results[0].record.memory_id != results[1].record.memory_id
    first.close()
    second.close()

    final = _service(path, tmp_path)
    active = final.list("u01").records
    assert len(active) == 1
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_records WHERE superseded_at IS NULL AND deleted_at IS NULL"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_records WHERE superseded_at IS NOT NULL"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT head_memory_id FROM memory_semantic_heads"
    ).fetchone()[0] == active[0].memory_id
    facts = connection.execute(
        "SELECT operation,COUNT(*) FROM memory_outbox GROUP BY operation"
    ).fetchall()
    assert dict(facts) == {"commit": 2, "supersede": 1}
    connection.close()
    final.close()


def test_s15_history_payloads_parse_and_committed_proposal_deletes_record(
    tmp_path,
) -> None:
    path = tmp_path / "memory.sqlite3"
    service = _service(path, tmp_path)
    first_proposal, old = _commit(service, "偏爱低调配色")
    current_proposal, current = _commit(service, "偏爱低调配色")
    receipt = service.delete("u01", current_proposal)
    assert receipt.deleted_kind == "record"
    assert receipt.deleted_id == current.memory_id
    assert receipt.truth_version == 2
    service.close()

    connection = sqlite3.connect(path)
    payloads = {
        row[0]: json.loads(row[1])
        for row in connection.execute(
            "SELECT memory_id,payload_json FROM memory_records"
        )
    }
    superseded = MemoryRecord.model_validate(payloads[old.memory_id])
    deleted = MemoryRecord.model_validate(payloads[current.memory_id])
    assert superseded.lifecycle_status == "superseded"
    assert superseded.valid_to is not None
    assert deleted.lifecycle_status == "deleted"
    assert deleted.valid_to is not None
    assert deleted.content == "[deleted]"
    connection.close()


def test_s15_concurrent_delete_and_expire_emit_one_cas_fact(tmp_path) -> None:
    delete_path = tmp_path / "delete.sqlite3"
    seed = _service(delete_path, tmp_path)
    _proposal, record = _commit(seed, "偏爱直筒裤")
    seed.close()
    first = _service(delete_path, tmp_path)
    second = _service(delete_path, tmp_path)
    barrier = Barrier(2)
    for instance in (first, second):
        original = instance.repository.tombstone

        def synchronized(*args, _original=original, **kwargs):
            barrier.wait(timeout=5)
            return _original(*args, **kwargs)

        instance.repository.tombstone = synchronized  # type: ignore[method-assign]

    def remove(instance: MemoryService):
        try:
            return instance.delete("u01", record.memory_id)
        except (MemoryError, KeyError):
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(remove, (first, second)))
    assert sum(result is not None for result in results) == 1
    first.close()
    second.close()
    connection = sqlite3.connect(delete_path)
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_outbox WHERE operation='delete'"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_outbox GROUP BY memory_id,operation,truth_version HAVING COUNT(*)>1"
    ).fetchall() == []
    connection.close()

    expire_path = tmp_path / "expire.sqlite3"
    seed = _service(expire_path, tmp_path)
    _proposal, expiring = _commit(seed, "偏爱简洁风格", ttl_days=1)
    seed.close()
    first_repo = SqlMemoryRepository(
        f"sqlite:///{expire_path.as_posix()}", tmp_path
    )
    second_repo = SqlMemoryRepository(
        f"sqlite:///{expire_path.as_posix()}", tmp_path
    )
    barrier = Barrier(2)

    def expire(repository: SqlMemoryRepository):
        barrier.wait(timeout=5)
        repository.expire_ids(
            [expiring.memory_id], (utc_now() + timedelta(days=2)).isoformat()
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(expire, (first_repo, second_repo)))
    first_repo.close()
    second_repo.close()
    connection = sqlite3.connect(expire_path)
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_outbox WHERE operation='expire'"
    ).fetchone()[0] == 1
    indexes = {
        row[1]: row[2]
        for row in connection.execute("PRAGMA index_list(memory_outbox)")
    }
    assert indexes["uq_memory_outbox_fact"] == 1
    connection.close()


def test_s15_postgresql_paths_declare_row_and_semantic_gap_locks() -> None:
    # CI uses the contract-equivalent SQLite backend. Keep a direct guard that
    # the production dialect cannot silently lose its FOR UPDATE locks.
    import inspect

    expire_source = inspect.getsource(SqlMemoryRepository.expire_ids)
    delete_source = inspect.getsource(SqlMemoryRepository.tombstone)
    head_source = inspect.getsource(SqlMemoryRepository._lock_semantic_head)
    assert 'self._dialect == "postgresql"' in expire_source
    assert "FOR UPDATE" in expire_source and "truth_version=?" in expire_source
    assert 'self._dialect == "postgresql"' in delete_source
    assert "FOR UPDATE" in delete_source and "truth_version=?" in delete_source
    assert "memory_semantic_heads" in head_source and "FOR UPDATE" in head_source


def test_s15_payload_identity_swaps_are_sticky_quarantined_for_both_owners(
    tmp_path,
) -> None:
    path = tmp_path / "memory.sqlite3"
    service = _service(path, tmp_path)
    _u01_source, u01_record = _commit_for(
        service,
        user_id="u01",
        namespace="stylist",
        memory_type="preference",
        content="偏爱直筒裤",
    )
    _u02_source, u02_record = _commit_for(
        service,
        user_id="u02",
        namespace="shared",
        memory_type="constraint",
        content="不穿高跟鞋",
    )
    u01_pending = service.propose(
        MemoryProposeInput(
            user_id="u01",
            namespace="stylist",
            type="preference",
            content="偏爱简洁风格",
        )
    ).proposal
    u02_pending = service.propose(
        MemoryProposeInput(
            user_id="u02",
            namespace="shared",
            type="constraint",
            content="不穿裙装",
        )
    ).proposal
    service.close()

    connection = sqlite3.connect(path)
    record_originals = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT memory_id,payload_json FROM memory_records WHERE memory_id IN (?,?)",
            (u01_record.memory_id, u02_record.memory_id),
        )
    }
    proposal_originals = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT proposal_id,payload_json FROM memory_proposals WHERE proposal_id IN (?,?)",
            (u01_pending.proposal_id, u02_pending.proposal_id),
        )
    }
    connection.execute(
        "UPDATE memory_records SET payload_json=? WHERE memory_id=?",
        (record_originals[u02_record.memory_id], u01_record.memory_id),
    )
    connection.execute(
        "UPDATE memory_records SET payload_json=? WHERE memory_id=?",
        (record_originals[u01_record.memory_id], u02_record.memory_id),
    )
    connection.execute(
        "UPDATE memory_proposals SET payload_json=? WHERE proposal_id=?",
        (proposal_originals[u02_pending.proposal_id], u01_pending.proposal_id),
    )
    connection.execute(
        "UPDATE memory_proposals SET payload_json=? WHERE proposal_id=?",
        (proposal_originals[u01_pending.proposal_id], u02_pending.proposal_id),
    )
    connection.commit()
    connection.close()

    restarted = _service(path, tmp_path)
    for user_id in ("u01", "u02"):
        public = restarted.list(user_id)
        assert public.proposals == []
        assert public.records == []
        assert restarted.active_signals(user_id) == ()
        assert restarted.retrieve_soft(user_id, "直筒裤 简洁")[0] == ()
        assert restarted.rebuild_soft_index(user_id) == ()
    loaded_proposals, loaded_records, _proposal_context, _record_context = (
        restarted.repository.load_state()
    )
    attacked_pending = {u01_pending.proposal_id, u02_pending.proposal_id}
    assert attacked_pending.isdisjoint(
        {item["proposal_id"] for item in loaded_proposals}
    )
    assert {u01_record.memory_id, u02_record.memory_id}.isdisjoint(
        {item["memory_id"] for item in loaded_records}
    )
    restarted.close()

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    record_quarantine = {
        row["memory_id"]: (row["quarantined_at"], row["quarantine_reason"])
        for row in connection.execute(
            "SELECT memory_id,quarantined_at,quarantine_reason FROM memory_records WHERE memory_id IN (?,?)",
            (u01_record.memory_id, u02_record.memory_id),
        )
    }
    proposal_quarantine = {
        row["proposal_id"]: (row["quarantined_at"], row["quarantine_reason"])
        for row in connection.execute(
            "SELECT proposal_id,quarantined_at,quarantine_reason FROM memory_proposals WHERE proposal_id IN (?,?)",
            (u01_pending.proposal_id, u02_pending.proposal_id),
        )
    }
    assert all(value[0] is not None for value in record_quarantine.values())
    assert all(
        value[1] == "canonical_payload_mismatch"
        for value in record_quarantine.values()
    )
    assert all(value[0] is not None for value in proposal_quarantine.values())
    assert all(
        value[1] == "canonical_payload_mismatch"
        for value in proposal_quarantine.values()
    )

    # Even restoring the bytes out of band is not an administrative repair:
    # ordinary startup migration must preserve the quarantine evidence.
    for object_id, raw in record_originals.items():
        connection.execute(
            "UPDATE memory_records SET payload_json=? WHERE memory_id=?",
            (raw, object_id),
        )
    for object_id, raw in proposal_originals.items():
        connection.execute(
            "UPDATE memory_proposals SET payload_json=? WHERE proposal_id=?",
            (raw, object_id),
        )
    connection.commit()
    connection.close()

    second_restart = _service(path, tmp_path)
    assert second_restart.list("u01").records == []
    assert second_restart.list("u02").records == []
    second_restart.close()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    assert record_quarantine == {
        row["memory_id"]: (row["quarantined_at"], row["quarantine_reason"])
        for row in connection.execute(
            "SELECT memory_id,quarantined_at,quarantine_reason FROM memory_records WHERE memory_id IN (?,?)",
            (u01_record.memory_id, u02_record.memory_id),
        )
    }
    assert proposal_quarantine == {
        row["proposal_id"]: (row["quarantined_at"], row["quarantine_reason"])
        for row in connection.execute(
            "SELECT proposal_id,quarantined_at,quarantine_reason FROM memory_proposals WHERE proposal_id IN (?,?)",
            (u01_pending.proposal_id, u02_pending.proposal_id),
        )
    }
    connection.close()


def test_s15_pending_zero_consume_revalidates_acl_and_quarantine_atomically(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "memory.sqlite3"
    service = _service(path, tmp_path)
    _proposal, first = _commit(service, "偏爱直筒裤")
    _proposal, second = _commit(service, "偏爱简洁风格")
    projected, initial_events = service.consume_soft_index_outbox("u01")
    assert projected == tuple(sorted((first.memory_id, second.memory_id)))
    assert len(initial_events) == 2
    assert service.consume_soft_index_outbox("u01")[1] == ()

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE memory_records SET acl_visibility='team' WHERE memory_id=?",
        (first.memory_id,),
    )
    connection.commit()
    connection.close()
    projected, events = service.consume_soft_index_outbox("u01")
    assert projected == (second.memory_id,)
    assert events == ()

    connection = sqlite3.connect(path)
    raw = connection.execute(
        "SELECT payload_json FROM memory_records WHERE memory_id=?",
        (second.memory_id,),
    ).fetchone()[0]
    payload = json.loads(raw)
    payload["user_id"] = "u02"
    connection.execute(
        "UPDATE memory_records SET payload_json=? WHERE memory_id=?",
        (json.dumps(payload, ensure_ascii=False), second.memory_id),
    )
    connection.commit()
    connection.close()

    original = service.repository._replace_soft_index_locked

    def fail_projection(**_kwargs):
        raise RuntimeError("injected pending-zero projection failure")

    monkeypatch.setattr(
        service.repository, "_replace_soft_index_locked", fail_projection
    )
    with pytest.raises(RuntimeError):
        service.consume_soft_index_outbox("u01")
    # The projection update is transactional: a failed rebuild leaves the last
    # completed version in place, while every retrieval still checks SQL truth.
    assert service.repository.soft_index_ids("u01", ("stylist",)) == (
        second.memory_id,
    )
    assert service.retrieve_soft("u01", "直筒裤 简洁")[0] == ()
    monkeypatch.setattr(service.repository, "_replace_soft_index_locked", original)
    assert service.consume_soft_index_outbox("u01") == ((), ())
    assert service.repository.soft_index_ids("u01", ("stylist",)) == ()
    service.close()


def test_s15_record_invariant_divergence_is_quarantined(tmp_path) -> None:
    path = tmp_path / "memory.sqlite3"
    service = _service(path, tmp_path)
    _proposal, wrong_source = _commit(service, "偏爱直筒裤")
    _proposal, self_supersede = _commit(service, "偏爱简洁风格")
    _proposal, wrong_ttl = _commit(service, "偏爱低调配色")
    service.close()

    connection = sqlite3.connect(path)
    raw = connection.execute(
        "SELECT payload_json FROM memory_records WHERE memory_id=?",
        (wrong_source.memory_id,),
    ).fetchone()[0]
    payload = json.loads(raw)
    payload["source_kind"] = "feedback_confirmed"
    connection.execute(
        "UPDATE memory_records SET source_kind=?,payload_json=? WHERE memory_id=?",
        ("feedback_confirmed", json.dumps(payload, ensure_ascii=False), wrong_source.memory_id),
    )

    raw = connection.execute(
        "SELECT payload_json FROM memory_records WHERE memory_id=?",
        (self_supersede.memory_id,),
    ).fetchone()[0]
    payload = json.loads(raw)
    payload["supersedes_memory_id"] = self_supersede.memory_id
    payload["confirmation_count"] = 2
    connection.execute(
        "UPDATE memory_records SET supersedes_memory_id=?,confirmation_count=2,payload_json=? WHERE memory_id=?",
        (
            self_supersede.memory_id,
            json.dumps(payload, ensure_ascii=False),
            self_supersede.memory_id,
        ),
    )

    raw = connection.execute(
        "SELECT payload_json FROM memory_records WHERE memory_id=?",
        (wrong_ttl.memory_id,),
    ).fetchone()[0]
    payload = json.loads(raw)
    wrong_expiry = (
        utc_now() + timedelta(days=2)
    ).isoformat()
    payload["ttl_days"] = 1
    payload["expires_at"] = wrong_expiry
    payload["valid_to"] = wrong_expiry
    connection.execute(
        "UPDATE memory_records SET expires_at=?,valid_to=?,payload_json=? WHERE memory_id=?",
        (
            wrong_expiry,
            wrong_expiry,
            json.dumps(payload, ensure_ascii=False),
            wrong_ttl.memory_id,
        ),
    )
    connection.commit()
    connection.close()

    restarted = _service(path, tmp_path)
    assert restarted.list("u01").records == []
    assert restarted.active_signals("u01") == ()
    assert restarted.retrieve_soft("u01", "直筒裤 简洁 低调")[0] == ()
    restarted.close()
    connection = sqlite3.connect(path)
    reasons = dict(
        connection.execute(
            "SELECT memory_id,quarantine_reason FROM memory_records"
        ).fetchall()
    )
    assert reasons[wrong_source.memory_id] == "invalid_source_kind_type"
    assert reasons[self_supersede.memory_id] == "invalid_supersedes_shape"
    assert reasons[wrong_ttl.memory_id] == "invalid_ttl_validity"
    connection.close()


def test_s15_list_exposes_only_pending_proposals_and_active_records(tmp_path) -> None:
    path = tmp_path / "memory.sqlite3"
    service = _service(path, tmp_path)
    pending = service.propose(
        MemoryProposeInput(
            user_id="u01",
            namespace="stylist",
            type="preference",
            content="偏爱简洁风格",
        )
    ).proposal
    rejected = service.propose(
        MemoryProposeInput(
            user_id="u01",
            namespace="stylist",
            type="preference",
            content="偏爱低调配色",
        )
    ).proposal
    service.confirm(
        rejected.proposal_id,
        MemoryConfirmInput(user_id="u01", decision="reject"),
    )
    committed_proposal, active = _commit(service, "偏爱直筒裤")

    response = service.list("u01")
    assert [item.proposal_id for item in response.proposals] == [pending.proposal_id]
    assert [item.status for item in response.proposals] == ["proposed"]
    assert [item.memory_id for item in response.records] == [active.memory_id]
    assert response.records[0].lifecycle_status == "active"
    assert rejected.proposal_id not in {
        item.proposal_id for item in response.proposals
    }
    assert committed_proposal not in {
        item.proposal_id for item in response.proposals
    }
    service.close()
