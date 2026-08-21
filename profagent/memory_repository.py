from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Literal, Protocol
from urllib.parse import unquote, urlsplit


class MemoryRepositoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemoryTombstoneResult:
    deleted_kind: Literal["proposal", "record"]
    user_id: str
    namespace: str
    truth_version: int
    payload: dict[str, Any]


class MemoryRepository(Protocol):
    def load_state(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]: ...
    def put_proposal(self, payload: dict[str, Any], context: dict[str, Any] | None = None) -> None: ...
    def commit(self, proposal: dict[str, Any], record: dict[str, Any], *, semantic_key: str, proposal_context: dict[str, Any] | None, record_context: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any], bool]: ...
    def put_rejected_proposal(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def tombstone(self, *, user_id: str, object_id: str, deleted_at: str) -> MemoryTombstoneResult | None: ...
    def expire_ids(self, ids: list[str], expired_at: str) -> None: ...
    def active_records(self, user_id: str, namespaces: tuple[str, ...], now: str) -> list[tuple[dict[str, Any], dict[str, Any] | None]]: ...
    def rebuild_soft_index(self, user_id: str, namespaces: tuple[str, ...], memory_ids: tuple[str, ...], *, index_version: str, rebuilt_at: str) -> tuple[str, ...]: ...
    def consume_soft_index_outbox(self, user_id: str, namespaces: tuple[str, ...], memory_ids: tuple[str, ...], *, consumer_name: str, index_version: str, consumed_at: str) -> tuple[tuple[str, ...], tuple[str, ...]]: ...
    def soft_index_ids(self, user_id: str, namespaces: tuple[str, ...]) -> tuple[str, ...]: ...
    def outbox_events(self, user_id: str | None = None) -> list[dict[str, Any]]: ...
    def put_candidate_batch(self, request: dict[str, Any], candidates: list[dict[str, Any]]) -> None: ...
    def candidate_request(self, request_id: str) -> dict[str, Any] | None: ...
    def candidate_state(self, candidate_id: str) -> dict[str, Any] | None: ...
    def candidate_decision(self, candidate_id: str, idempotency_key: str) -> dict[str, Any] | None: ...
    def record_candidate_decision(self, candidate_id: str, idempotency_key: str, fingerprint: str, status: str, response: dict[str, Any]) -> dict[str, Any]: ...
    def candidate_proposal_managed(self, proposal_id: str) -> bool: ...
    def candidate_rejected(self, *, user_id: str, styling_session_id: str, namespace: str, canonical_kind: str, canonical_value: str) -> bool: ...
    def close(self) -> None: ...


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SqlMemoryRepository:
    """Normalized durable store with transaction-level ACL/status prefilters.

    SQLite is the offline/test equivalent. PostgreSQL uses the same schema and
    operations through an optional psycopg DB-API connection when the production
    `PROFAGENT_DATABASE_URL` is configured.
    """

    def __init__(self, database_url: str | None, root_dir: Path) -> None:
        self.database_url = database_url or "sqlite:///:memory:"
        self.root_dir = root_dir.resolve()
        self._lock = RLock()
        self._dialect = "sqlite"
        self._connection: Any
        if self.database_url.startswith(("postgresql://", "postgres://")):
            self._dialect = "postgresql"
            try:
                import psycopg  # type: ignore[import-not-found]
                from psycopg.rows import dict_row  # type: ignore[import-not-found]
            except ImportError as exc:
                raise MemoryRepositoryError(
                    "PostgreSQL memory storage requires the psycopg driver"
                ) from exc
            self._connection = psycopg.connect(self.database_url, row_factory=dict_row)
            self._connection.autocommit = False
        elif self.database_url.startswith("sqlite:///"):
            raw = unquote(self.database_url[len("sqlite:///") :])
            if raw == ":memory:":
                path = ":memory:"
            else:
                candidate = Path(raw)
                if not candidate.is_absolute():
                    candidate = self.root_dir / candidate
                candidate = candidate.resolve()
                candidate.parent.mkdir(parents=True, exist_ok=True)
                path = str(candidate)
            self._connection = sqlite3.connect(
                path, check_same_thread=False, timeout=10.0
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
        else:
            scheme = urlsplit(self.database_url).scheme or "unknown"
            raise MemoryRepositoryError(f"unsupported memory database scheme: {scheme}")
        self._create_schema()

    def _sql(self, statement: str) -> str:
        return statement.replace("?", "%s") if self._dialect == "postgresql" else statement

    def _execute(self, statement: str, params: tuple[Any, ...] = ()) -> Any:
        if self._dialect == "postgresql":
            cursor = self._connection.cursor()
            cursor.execute(self._sql(statement), params)
            return cursor
        return self._connection.execute(statement, params)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            try:
                if self._dialect == "sqlite":
                    self._connection.execute("BEGIN IMMEDIATE")
                yield
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _create_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS memory_proposals (
                proposal_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                team_id TEXT NOT NULL DEFAULT 'personal_team',
                member_id TEXT,
                acl_visibility TEXT NOT NULL DEFAULT 'member',
                namespace TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                status TEXT NOT NULL,
                sensitivity TEXT NOT NULL,
                commit_blocked INTEGER NOT NULL,
                record_id TEXT,
                deleted_at TEXT,
                quarantined_at TEXT,
                quarantine_reason TEXT,
                payload_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_records (
                memory_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                team_id TEXT NOT NULL DEFAULT 'personal_team',
                member_id TEXT,
                acl_visibility TEXT NOT NULL DEFAULT 'member',
                namespace TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                status TEXT NOT NULL,
                sensitivity TEXT NOT NULL,
                source TEXT NOT NULL,
                semantic_key TEXT NOT NULL,
                memory_class TEXT NOT NULL DEFAULT 'preference_event',
                valid_from TEXT,
                valid_to TEXT,
                supersedes_memory_id TEXT,
                source_kind TEXT NOT NULL DEFAULT 'legacy_migrated',
                provenance_version TEXT NOT NULL DEFAULT 'memory_provenance_v1',
                consent_version TEXT NOT NULL DEFAULT 'explicit_confirm_v1',
                confirmation_count INTEGER NOT NULL DEFAULT 1,
                truth_version INTEGER NOT NULL DEFAULT 1,
                quarantined_at TEXT,
                quarantine_reason TEXT,
                expires_at TEXT,
                deleted_at TEXT,
                superseded_at TEXT,
                payload_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_contexts (
                object_kind TEXT NOT NULL,
                object_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (object_kind, object_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_audit (
                event_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                object_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_outbox (
                event_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                truth_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_soft_index (
                user_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                index_version TEXT NOT NULL,
                rebuilt_at TEXT NOT NULL,
                PRIMARY KEY (user_id, namespace, memory_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_outbox_delivery (
                consumer_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (consumer_name, event_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_semantic_heads (
                user_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                semantic_key TEXT NOT NULL,
                head_memory_id TEXT,
                truth_version INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, namespace, semantic_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_candidate_requests (
                request_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_candidates (
                candidate_id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL UNIQUE,
                request_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                styling_session_id TEXT,
                namespace TEXT NOT NULL,
                canonical_kind TEXT NOT NULL,
                canonical_value TEXT NOT NULL,
                status TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(request_id) REFERENCES memory_candidate_requests(request_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_candidate_decisions (
                candidate_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (candidate_id, idempotency_key),
                FOREIGN KEY(candidate_id) REFERENCES memory_candidates(candidate_id)
            )
            """,
        )
        with self._transaction():
            for statement in statements:
                self._execute(statement)
        self._ensure_acl_columns()
        self._ensure_proposal_integrity()
        self._ensure_s15_columns()
        with self._transaction():
            self._execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_active_acl ON memory_records (user_id, team_id, member_id, acl_visibility, namespace, status, sensitivity, source, deleted_at, superseded_at, expires_at)"
            )
            self._execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_outbox_user ON memory_outbox (user_id, namespace, memory_id, truth_version)"
            )
            self._execute(
                """
                DELETE FROM memory_outbox
                WHERE event_id NOT IN (
                    SELECT MIN(event_id) FROM memory_outbox
                    GROUP BY memory_id,operation,truth_version
                )
                """
            )
            self._execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_outbox_fact ON memory_outbox (memory_id, operation, truth_version)"
            )
            self._execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_candidate_owner ON memory_candidates (user_id, styling_session_id, namespace, status, expires_at)"
            )

    def _ensure_acl_columns(self) -> None:
        """Migrate pre-S12 stores and freeze the one-member R1 ACL metadata."""
        with self._transaction():
            for table in ("memory_proposals", "memory_records"):
                if self._dialect == "sqlite":
                    present = {
                        row["name"]
                        for row in self._fetchall(f"PRAGMA table_info({table})")
                    }
                    additions = {
                        "team_id": "TEXT NOT NULL DEFAULT 'personal_team'",
                        "member_id": "TEXT",
                        "acl_visibility": "TEXT NOT NULL DEFAULT 'member'",
                    }
                    migrated = False
                    for column, definition in additions.items():
                        if column not in present:
                            self._execute(
                                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                            )
                            migrated = True
                else:
                    present = {
                        row["column_name"]
                        for row in self._fetchall(
                            "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?",
                            (table,),
                        )
                    }
                    additions = {
                        "team_id": "TEXT NOT NULL DEFAULT 'personal_team'",
                        "member_id": "TEXT",
                        "acl_visibility": "TEXT NOT NULL DEFAULT 'member'",
                    }
                    migrated = False
                    for column, definition in additions.items():
                        if column not in present:
                            self._execute(
                                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                            )
                            migrated = True
                if migrated:
                    self._execute(
                        f"""
                        UPDATE {table}
                        SET team_id='personal_team',
                            member_id=CASE WHEN namespace='stylist' THEN 'stylist' ELSE NULL END,
                            acl_visibility=CASE WHEN namespace='stylist' THEN 'member' ELSE 'team' END
                        """
                    )

    @staticmethod
    def _legacy_memory_class(memory_type: str, payload: dict[str, Any]) -> str:
        normalized = "".join(str(payload.get("content") or "").split())
        if memory_type in {"constraint", "comfort_constraint"} or normalized in {
            "不穿高跟鞋",
            "不穿裙装",
            "久走或长时间站立时需要舒适鞋履",
        }:
            return "hard_constraint"
        if memory_type == "profile_stable":
            return "profile_current"
        return "preference_event"

    @staticmethod
    def _legacy_content_is_controlled(memory_type: str, content: Any) -> bool:
        approved = {
            "constraint": {
                "不穿高跟鞋",
                "不穿裙装",
                "久走或长时间站立时需要舒适鞋履",
            },
            "comfort_constraint": {"久走或长时间站立时需要舒适鞋履"},
            "preference": {
                "偏爱直筒裤",
                "偏爱简洁风格",
                "偏爱低调配色",
                "长时间站立时优先选择适合久走的鞋。",
                "长时间站立时优先选择适合久走的鞋",
            },
            "profile_stable": {"不穿高跟鞋", "不穿裙装", "偏爱直筒裤"},
            "feedback": {"偏好久走与长时间站立时选择舒适鞋履"},
        }
        if not isinstance(content, str):
            return False
        if content in approved.get(memory_type, set()):
            return True
        # S16 extends the same fail-closed vocabulary through the canonical
        # validator. Import lazily to avoid weakening the repository boundary or
        # creating an import cycle during MemoryService construction.
        from .memory_candidates import canonical_candidate_for_content

        return canonical_candidate_for_content(memory_type, content) is not None

    @staticmethod
    def _acl_is_canonical(row: dict[str, Any], namespace: str) -> bool:
        if namespace == "shared":
            return (
                row.get("team_id") == "personal_team"
                and row.get("member_id") is None
                and row.get("acl_visibility") == "team"
            )
        if namespace == "stylist":
            return (
                row.get("team_id") == "personal_team"
                and row.get("member_id") == "stylist"
                and row.get("acl_visibility") == "member"
            )
        return False

    def _ensure_proposal_integrity(self) -> None:
        """Sticky-quarantine proposal payload/column or ACL divergence."""
        additions = {
            "quarantined_at": "TEXT",
            "quarantine_reason": "TEXT",
        }
        with self._transaction():
            if self._dialect == "sqlite":
                present = {
                    row["name"]
                    for row in self._fetchall("PRAGMA table_info(memory_proposals)")
                }
            else:
                present = {
                    row["column_name"]
                    for row in self._fetchall(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='memory_proposals'"
                    )
                }
            for column, definition in additions.items():
                if column not in present:
                    self._execute(
                        f"ALTER TABLE memory_proposals ADD COLUMN {column} {definition}"
                    )
            rows = self._fetchall(
                """
                SELECT proposal_id,user_id,team_id,member_id,acl_visibility,
                       namespace,memory_type,status,sensitivity,commit_blocked,
                       record_id,quarantined_at,quarantine_reason,payload_json
                FROM memory_proposals
                """
            )
            for row in rows:
                if row.get("quarantined_at") is not None:
                    continue
                reason: str | None = None
                try:
                    payload = json.loads(row["payload_json"])
                    if not isinstance(payload, dict):
                        raise ValueError("proposal payload must be an object")
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = None
                    reason = "invalid_payload_json"
                if payload is not None:
                    forbidden_acl = {
                        "team_id", "member_id", "acl_visibility"
                    } & set(payload)
                    canonical = {
                        "proposal_id": row["proposal_id"],
                        "user_id": row["user_id"],
                        "namespace": row["namespace"],
                        "type": row["memory_type"],
                        "status": row["status"],
                        "sensitivity": row["sensitivity"],
                        "commit_blocked": bool(row["commit_blocked"]),
                        "record_id": row["record_id"],
                    }
                    if forbidden_acl:
                        reason = "payload_acl_forbidden"
                    elif not self._acl_is_canonical(row, row["namespace"]):
                        reason = "invalid_acl_projection"
                    elif any(payload.get(key) != value for key, value in canonical.items()):
                        reason = "canonical_payload_mismatch"
                    elif row["status"] not in {"proposed", "committed", "rejected"}:
                        reason = "invalid_proposal_status"
                    elif row["sensitivity"] not in {"non_sensitive", "sensitive"}:
                        reason = "invalid_proposal_sensitivity"
                    elif bool(row["commit_blocked"]) != (
                        row["sensitivity"] == "sensitive"
                    ):
                        reason = "invalid_proposal_gate"
                    elif row["status"] == "committed" and row["record_id"] is None:
                        reason = "missing_committed_record"
                    elif row["status"] != "committed" and row["record_id"] is not None:
                        reason = "unexpected_record_binding"
                if reason is not None:
                    self._execute(
                        """
                        UPDATE memory_proposals
                        SET quarantined_at=?,quarantine_reason=?
                        WHERE proposal_id=? AND quarantined_at IS NULL
                        """,
                        (
                            datetime.now(timezone.utc).isoformat(),
                            reason,
                            row["proposal_id"],
                        ),
                    )

    def _ensure_s15_columns(self) -> None:
        """Idempotently enrich pre-S15 truth rows without changing identity/content."""
        additions = {
            "memory_class": "TEXT NOT NULL DEFAULT 'preference_event'",
            "valid_from": "TEXT",
            "valid_to": "TEXT",
            "supersedes_memory_id": "TEXT",
            "source_kind": "TEXT NOT NULL DEFAULT 'legacy_migrated'",
            "provenance_version": "TEXT NOT NULL DEFAULT 'memory_provenance_v1'",
            "consent_version": "TEXT NOT NULL DEFAULT 'explicit_confirm_v1'",
            "confirmation_count": "INTEGER NOT NULL DEFAULT 1",
            "truth_version": "INTEGER NOT NULL DEFAULT 1",
            "quarantined_at": "TEXT",
            "quarantine_reason": "TEXT",
        }
        with self._transaction():
            if self._dialect == "sqlite":
                present = {
                    row["name"]
                    for row in self._fetchall("PRAGMA table_info(memory_records)")
                }
            else:
                present = {
                    row["column_name"]
                    for row in self._fetchall(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='memory_records'"
                    )
                }
            for column, definition in additions.items():
                if column not in present:
                    self._execute(
                        f"ALTER TABLE memory_records ADD COLUMN {column} {definition}"
                    )

            rows = self._fetchall(
                """
                SELECT memory_id,user_id,team_id,member_id,acl_visibility,
                       namespace,memory_type,status,sensitivity,source,semantic_key,
                       memory_class,valid_from,valid_to,supersedes_memory_id,
                       source_kind,provenance_version,consent_version,
                       confirmation_count,expires_at,deleted_at,superseded_at,quarantined_at,
                       payload_json
                FROM memory_records
                """
            )
            for row in rows:
                if row.get("quarantined_at") is not None:
                    continue
                try:
                    payload = json.loads(row["payload_json"])
                    if not isinstance(payload, dict):
                        raise ValueError("record payload must be an object")
                except (TypeError, ValueError, json.JSONDecodeError):
                    self._execute(
                        "UPDATE memory_records SET quarantined_at=?,quarantine_reason=? WHERE memory_id=? AND quarantined_at IS NULL",
                        (
                            datetime.now(timezone.utc).isoformat(),
                            "invalid_payload_json",
                            row["memory_id"],
                        ),
                    )
                    continue
                created_at = payload.get("created_at")
                expires_at = payload.get("expires_at")
                expected_class = self._legacy_memory_class(
                    row["memory_type"], payload
                )
                had_s15_fields = any(
                    key in payload
                    for key in (
                        "memory_class",
                        "source_kind",
                        "provenance_version",
                        "consent_version",
                        "confirmation_count",
                    )
                )
                memory_class = payload.get("memory_class") or expected_class
                payload_lifecycle = payload.get("lifecycle_status")
                if row["deleted_at"] is not None:
                    lifecycle_status = (
                        payload_lifecycle
                        if payload_lifecycle in {"deleted", "expired"}
                        else "deleted"
                    )
                elif row["superseded_at"] is not None:
                    lifecycle_status = "superseded"
                else:
                    lifecycle_status = payload_lifecycle or "active"
                if had_s15_fields:
                    source_kind = payload.get("source_kind")
                    provenance_version = payload.get("provenance_version")
                    consent_version = payload.get("consent_version")
                else:
                    source_kind = "legacy_migrated"
                    provenance_version = "memory_legacy_v0"
                    consent_version = "legacy_confirm_v0"
                triplet_valid = (
                    (
                        source_kind == "legacy_migrated"
                        and provenance_version == "memory_legacy_v0"
                        and consent_version == "legacy_confirm_v0"
                    )
                    or (
                        source_kind
                        in {
                            "user_confirmed",
                            "user_edited_confirmed",
                            "feedback_confirmed",
                        }
                        and provenance_version == "memory_provenance_v1"
                        and consent_version == "explicit_confirm_v1"
                    )
                )
                historical_redaction = (
                    lifecycle_status in {"deleted", "expired"}
                    and payload.get("content") in {"[deleted]", "[expired]"}
                )
                valid_class = historical_redaction or memory_class == expected_class
                controlled_content = historical_redaction or self._legacy_content_is_controlled(
                    row["memory_type"], payload.get("content")
                )
                try:
                    raw_confirmation_count = int(
                        payload.get("confirmation_count") or 1
                    )
                    confirmation_count_valid = raw_confirmation_count >= 1
                except (TypeError, ValueError):
                    raw_confirmation_count = 1
                    confirmation_count_valid = False
                payload_column_mismatch = False
                effective_valid_from = payload.get("valid_from") or created_at
                effective_valid_to = payload.get("valid_to", expires_at)
                parsed_valid_to: datetime | None = None
                try:
                    parsed_valid_from = datetime.fromisoformat(effective_valid_from)
                    parsed_valid_to = (
                        datetime.fromisoformat(effective_valid_to)
                        if effective_valid_to is not None
                        else None
                    )
                    validity_period_valid = (
                        parsed_valid_to is None
                        or parsed_valid_to >= parsed_valid_from
                    )
                except (TypeError, ValueError):
                    validity_period_valid = False
                if had_s15_fields:
                    comparisons = {
                        "memory_class": memory_class,
                        "valid_from": effective_valid_from,
                        "valid_to": effective_valid_to,
                        "supersedes_memory_id": payload.get("supersedes_memory_id"),
                        "source_kind": source_kind,
                        "provenance_version": provenance_version,
                        "consent_version": consent_version,
                        "confirmation_count": raw_confirmation_count,
                    }
                    payload_column_mismatch = any(
                        row.get(column) != value
                        for column, value in comparisons.items()
                    )
                forbidden_acl = {
                    "team_id", "member_id", "acl_visibility"
                } & set(payload)
                canonical_core = {
                    "memory_id": row["memory_id"],
                    "user_id": row["user_id"],
                    "namespace": row["namespace"],
                    "type": row["memory_type"],
                    "status": row["status"],
                    "sensitivity": row["sensitivity"],
                    "source": row["source"],
                }
                canonical_payload_mismatch = any(
                    payload.get(key) != value
                    for key, value in canonical_core.items()
                )
                lifecycle_columns_valid = (
                    (
                        row["deleted_at"] is not None
                        and lifecycle_status in {"deleted", "expired"}
                    )
                    or (
                        row["deleted_at"] is None
                        and row["superseded_at"] is not None
                        and lifecycle_status == "superseded"
                    )
                    or (
                        row["deleted_at"] is None
                        and row["superseded_at"] is None
                        and lifecycle_status == "active"
                    )
                )
                source_kind_type_valid = (
                    source_kind == "legacy_migrated"
                    or (
                        row["memory_type"] == "feedback"
                        and source_kind
                        in {"feedback_confirmed", "user_edited_confirmed"}
                    )
                    or (
                        row["memory_type"] != "feedback"
                        and source_kind
                        in {"user_confirmed", "user_edited_confirmed"}
                    )
                )
                ttl_days = payload.get("ttl_days")
                sql_expires_at = row.get("expires_at")
                payload_expires_at = payload.get("expires_at")
                ttl_valid = sql_expires_at == payload_expires_at
                try:
                    created_datetime = datetime.fromisoformat(created_at)
                    if ttl_days is None:
                        ttl_valid = ttl_valid and payload_expires_at is None
                    else:
                        ttl_int = int(ttl_days)
                        parsed_expiry = datetime.fromisoformat(payload_expires_at)
                        ttl_valid = (
                            ttl_valid
                            and 1 <= ttl_int <= 365
                            and abs(
                                (
                                    parsed_expiry
                                    - (created_datetime + timedelta(days=ttl_int))
                                ).total_seconds()
                            )
                            < 0.001
                        )
                    if (
                        lifecycle_status == "active"
                        and parsed_valid_to is not None
                        and payload_expires_at is not None
                    ):
                        ttl_valid = ttl_valid and parsed_valid_to <= datetime.fromisoformat(
                            payload_expires_at
                        )
                except (TypeError, ValueError):
                    ttl_valid = False
                supersedes_id = payload.get("supersedes_memory_id")
                supersedes_shape_valid = (
                    (
                        supersedes_id is None
                        and raw_confirmation_count == 1
                    )
                    or (
                        isinstance(supersedes_id, str)
                        and supersedes_id != row["memory_id"]
                        and raw_confirmation_count >= 2
                    )
                )
                quarantine_reason = None
                if forbidden_acl:
                    quarantine_reason = "payload_acl_forbidden"
                elif not self._acl_is_canonical(row, row["namespace"]):
                    quarantine_reason = "invalid_acl_projection"
                elif canonical_payload_mismatch:
                    quarantine_reason = "canonical_payload_mismatch"
                elif not created_at:
                    quarantine_reason = "missing_valid_from"
                elif not validity_period_valid:
                    quarantine_reason = "invalid_validity_period"
                elif row["status"] != "committed" or row["sensitivity"] != "non_sensitive" or row["source"] != "user_confirmed":
                    quarantine_reason = "invalid_record_authority"
                elif not lifecycle_columns_valid:
                    quarantine_reason = "invalid_lifecycle_columns"
                elif not controlled_content:
                    quarantine_reason = "invalid_controlled_content"
                elif not triplet_valid:
                    quarantine_reason = "invalid_provenance_triplet"
                elif not source_kind_type_valid:
                    quarantine_reason = "invalid_source_kind_type"
                elif not valid_class:
                    quarantine_reason = "invalid_memory_class"
                elif not confirmation_count_valid:
                    quarantine_reason = "invalid_confirmation_count"
                elif not supersedes_shape_valid:
                    quarantine_reason = "invalid_supersedes_shape"
                elif not ttl_valid:
                    quarantine_reason = "invalid_ttl_validity"
                elif payload_column_mismatch:
                    quarantine_reason = "column_payload_mismatch"
                if quarantine_reason is not None:
                    self._execute(
                        """
                        UPDATE memory_records
                        SET quarantined_at=?,quarantine_reason=?
                        WHERE memory_id=? AND quarantined_at IS NULL
                        """,
                        (
                            datetime.now(timezone.utc).isoformat(),
                            quarantine_reason,
                            row["memory_id"],
                        ),
                    )
                    continue
                if had_s15_fields:
                    continue
                stored_source_kind = "legacy_migrated"
                stored_provenance_version = "memory_legacy_v0"
                stored_consent_version = "legacy_confirm_v0"
                payload.update(
                    {
                        "memory_class": memory_class,
                        "valid_from": payload.get("valid_from") or created_at,
                        "valid_to": payload.get("valid_to", expires_at),
                        "supersedes_memory_id": payload.get("supersedes_memory_id"),
                        "source_kind": stored_source_kind,
                        "provenance_version": stored_provenance_version,
                        "consent_version": stored_consent_version,
                        "confirmation_count": max(
                            1, raw_confirmation_count
                        ),
                        "lifecycle_status": lifecycle_status,
                    }
                )
                self._execute(
                    """
                    UPDATE memory_records
                    SET memory_class=?,valid_from=?,valid_to=?,supersedes_memory_id=?,
                        source_kind=?,provenance_version=?,consent_version=?,
                        confirmation_count=?,truth_version=COALESCE(truth_version,1),
                        quarantined_at=?,quarantine_reason=?,payload_json=?
                    WHERE memory_id=?
                    """,
                    (
                        memory_class,
                        payload["valid_from"],
                        payload["valid_to"],
                        payload["supersedes_memory_id"],
                        payload["source_kind"],
                        payload["provenance_version"],
                        payload["consent_version"],
                        payload["confirmation_count"],
                        None,
                        None,
                        _canonical(payload),
                        row["memory_id"],
                    ),
                )

            # Second pass validates relational invariants only after every row
            # has either migrated cleanly or entered sticky quarantine.
            chain_rows = self._fetchall(
                """
                SELECT memory_id,user_id,namespace,semantic_key,
                       supersedes_memory_id,confirmation_count,source_kind,
                       payload_json
                FROM memory_records
                WHERE quarantined_at IS NULL
                ORDER BY confirmation_count,memory_id
                """
            )
            for row in chain_rows:
                reason: str | None = None
                payload = json.loads(row["payload_json"])
                predecessor_id = row.get("supersedes_memory_id")
                if predecessor_id is not None:
                    predecessors = self._fetchall(
                        """
                        SELECT memory_id,user_id,namespace,semantic_key,
                               confirmation_count,quarantined_at,payload_json
                        FROM memory_records WHERE memory_id=?
                        """,
                        (predecessor_id,),
                    )
                    if len(predecessors) != 1:
                        reason = "missing_supersedes_record"
                    else:
                        predecessor = predecessors[0]
                        try:
                            predecessor_payload = json.loads(
                                predecessor["payload_json"]
                            )
                        except (TypeError, ValueError, json.JSONDecodeError):
                            predecessor_payload = {}
                        if (
                            predecessor.get("quarantined_at") is not None
                            or predecessor["memory_id"] == row["memory_id"]
                            or predecessor["user_id"] != row["user_id"]
                            or predecessor["namespace"] != row["namespace"]
                            or predecessor["semantic_key"] != row["semantic_key"]
                            or predecessor_payload.get("memory_id")
                            != predecessor["memory_id"]
                            or int(predecessor["confirmation_count"] or 0) + 1
                            != int(row["confirmation_count"] or 0)
                        ):
                            reason = "invalid_supersedes_chain"
                if row["source_kind"] != "legacy_migrated":
                    proposal_id = payload.get("source_proposal_id")
                    proposals = self._fetchall(
                        """
                        SELECT proposal_id,user_id,namespace,memory_type,status,
                               record_id,quarantined_at
                        FROM memory_proposals WHERE proposal_id=?
                        """,
                        (proposal_id,),
                    )
                    if (
                        len(proposals) != 1
                        or proposals[0].get("quarantined_at") is not None
                        or proposals[0]["user_id"] != row["user_id"]
                        or proposals[0]["namespace"] != row["namespace"]
                        or proposals[0]["memory_type"] != payload.get("type")
                        or proposals[0]["status"] != "committed"
                        or proposals[0]["record_id"] != row["memory_id"]
                    ):
                        reason = reason or "invalid_source_proposal"
                if reason is not None:
                    self._execute(
                        """
                        UPDATE memory_records
                        SET quarantined_at=?,quarantine_reason=?
                        WHERE memory_id=? AND quarantined_at IS NULL
                        """,
                        (
                            datetime.now(timezone.utc).isoformat(),
                            reason,
                            row["memory_id"],
                        ),
                    )

    def _write_outbox(
        self,
        *,
        user_id: str,
        namespace: str,
        memory_id: str,
        operation: str,
        truth_version: int,
        status: str,
        occurred_at: str,
    ) -> None:
        """Write a content-free lifecycle fact inside the caller's transaction."""
        self._execute(
            "INSERT INTO memory_outbox VALUES (?,?,?,?,?,?,?,?)",
            (
                f"mout_{uuid.uuid4().hex}",
                user_id,
                namespace,
                memory_id,
                operation,
                truth_version,
                status,
                occurred_at,
            ),
        )

    def _lock_semantic_head(
        self,
        *,
        user_id: str,
        namespace: str,
        semantic_key: str,
        occurred_at: str,
    ) -> None:
        """Create then lock the semantic-key head, closing PostgreSQL gap races."""
        self._execute(
            """
            INSERT INTO memory_semantic_heads (
                user_id,namespace,semantic_key,head_memory_id,truth_version,updated_at
            ) VALUES (?,?,?,NULL,0,?)
            ON CONFLICT(user_id,namespace,semantic_key) DO NOTHING
            """,
            (user_id, namespace, semantic_key, occurred_at),
        )
        lock_clause = " FOR UPDATE" if self._dialect == "postgresql" else ""
        rows = self._fetchall(
            "SELECT head_memory_id FROM memory_semantic_heads "
            "WHERE user_id=? AND namespace=? AND semantic_key=?" + lock_clause,
            (user_id, namespace, semantic_key),
        )
        if len(rows) != 1:
            raise MemoryRepositoryError("semantic memory head is unavailable")

    @staticmethod
    def _row_dict(row: Any) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _canonical_proposal_projection(row: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        payload.update(
            {
                "proposal_id": row["proposal_id"],
                "user_id": row["user_id"],
                "namespace": row["namespace"],
                "type": row["memory_type"],
                "status": row["status"],
                "sensitivity": row["sensitivity"],
                "commit_blocked": bool(row["commit_blocked"]),
                "record_id": row["record_id"],
            }
        )
        for key in ("team_id", "member_id", "acl_visibility"):
            payload.pop(key, None)
        return payload

    @staticmethod
    def _canonical_record_projection(row: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        payload.update(
            {
                "memory_id": row["memory_id"],
                "user_id": row["user_id"],
                "namespace": row["namespace"],
                "type": row["memory_type"],
                "status": row["status"],
                "sensitivity": row["sensitivity"],
                "source": row["source"],
                "memory_class": row["memory_class"],
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "supersedes_memory_id": row["supersedes_memory_id"],
                "source_kind": row["source_kind"],
                "provenance_version": row["provenance_version"],
                "consent_version": row["consent_version"],
                "confirmation_count": row["confirmation_count"],
                "expires_at": row["expires_at"],
            }
        )
        for key in ("team_id", "member_id", "acl_visibility"):
            payload.pop(key, None)
        return payload

    def _fetchall(self, statement: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cursor = self._execute(statement, params)
        rows = cursor.fetchall()
        return [self._row_dict(row) for row in rows]

    @staticmethod
    def _proposal_columns(payload: dict[str, Any]) -> tuple[Any, ...]:
        member_id = "stylist" if payload["namespace"] == "stylist" else None
        visibility = "member" if member_id else "team"
        return (
            payload["proposal_id"],
            payload["user_id"],
            "personal_team",
            member_id,
            visibility,
            payload["namespace"],
            payload["type"],
            payload["status"],
            payload["sensitivity"],
            int(bool(payload["commit_blocked"])),
            payload.get("record_id"),
            _canonical(payload),
        )

    def _upsert_proposal(self, payload: dict[str, Any]) -> None:
        self._execute(
            """
            INSERT INTO memory_proposals (
                proposal_id,user_id,team_id,member_id,acl_visibility,
                namespace,memory_type,status,sensitivity,
                commit_blocked,record_id,deleted_at,payload_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?)
            ON CONFLICT(proposal_id) DO UPDATE SET
                user_id=excluded.user_id, team_id=excluded.team_id,
                member_id=excluded.member_id, acl_visibility=excluded.acl_visibility,
                namespace=excluded.namespace,
                memory_type=excluded.memory_type, status=excluded.status,
                sensitivity=excluded.sensitivity,
                commit_blocked=excluded.commit_blocked, record_id=excluded.record_id,
                deleted_at=NULL, payload_json=excluded.payload_json
            """,
            self._proposal_columns(payload),
        )

    def _put_context(self, kind: str, object_id: str, payload: dict[str, Any]) -> None:
        self._execute(
            """
            INSERT INTO memory_contexts (object_kind,object_id,payload_json)
            VALUES (?,?,?)
            ON CONFLICT(object_kind,object_id) DO UPDATE SET payload_json=excluded.payload_json
            """,
            (kind, object_id, _canonical(payload)),
        )

    def put_proposal(
        self, payload: dict[str, Any], context: dict[str, Any] | None = None
    ) -> None:
        with self._transaction():
            self._upsert_proposal(payload)
            if context is not None:
                self._put_context("proposal", payload["proposal_id"], context)

    def put_rejected_proposal(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._transaction():
            lock_clause = " FOR UPDATE" if self._dialect == "postgresql" else ""
            current_rows = self._fetchall(
                "SELECT payload_json,status FROM memory_proposals "
                "WHERE proposal_id=? AND user_id=? AND deleted_at IS NULL AND quarantined_at IS NULL "
                "AND team_id='personal_team' "
                "AND ((namespace='shared' AND acl_visibility='team' AND member_id IS NULL) "
                "OR (namespace='stylist' AND acl_visibility='member' AND member_id='stylist'))"
                + lock_clause,
                (payload["proposal_id"], payload["user_id"]),
            )
            if not current_rows:
                raise MemoryRepositoryError("memory proposal is unavailable")
            current = current_rows[0]
            if current["status"] == "rejected":
                return json.loads(current["payload_json"])
            if current["status"] != "proposed":
                raise MemoryRepositoryError("memory proposal decision conflicts")
            self._upsert_proposal(payload)
            self._execute(
                "DELETE FROM memory_contexts WHERE object_kind='proposal' AND object_id=?",
                (payload["proposal_id"],),
            )
            return payload

    def commit(
        self,
        proposal: dict[str, Any],
        record: dict[str, Any],
        *,
        semantic_key: str,
        proposal_context: dict[str, Any] | None,
        record_context: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        now = record["created_at"]
        with self._transaction():
            lock_clause = " FOR UPDATE" if self._dialect == "postgresql" else ""
            current_rows = self._fetchall(
                "SELECT payload_json,status,record_id FROM memory_proposals "
                "WHERE proposal_id=? AND user_id=? AND deleted_at IS NULL AND quarantined_at IS NULL "
                "AND team_id='personal_team' "
                "AND ((namespace='shared' AND acl_visibility='team' AND member_id IS NULL) "
                "OR (namespace='stylist' AND acl_visibility='member' AND member_id='stylist'))"
                + lock_clause,
                (proposal["proposal_id"], proposal["user_id"]),
            )
            if not current_rows:
                raise MemoryRepositoryError("memory proposal is unavailable")
            current = current_rows[0]
            if current["status"] == "committed" and current["record_id"]:
                authoritative_rows = self._fetchall(
                    "SELECT payload_json FROM memory_records WHERE memory_id=? AND user_id=? AND deleted_at IS NULL",
                    (current["record_id"], proposal["user_id"]),
                )
                if not authoritative_rows:
                    raise MemoryRepositoryError("committed memory record is unavailable")
                return (
                    json.loads(current["payload_json"]),
                    json.loads(authoritative_rows[0]["payload_json"]),
                    False,
                )
            if current["status"] != "proposed" or current["record_id"] is not None:
                raise MemoryRepositoryError("memory proposal cannot be committed")
            self._lock_semantic_head(
                user_id=record["user_id"],
                namespace=record["namespace"],
                semantic_key=semantic_key,
                occurred_at=now,
            )
            # Same semantic key is immutable history, not duplicate active
            # influence. Keep the older row recoverable but mark superseded.
            superseded_rows = self._fetchall(
                """
                SELECT memory_id,valid_from,payload_json,truth_version FROM memory_records
                WHERE user_id=? AND namespace=? AND semantic_key=?
                  AND status='committed' AND deleted_at IS NULL
                  AND superseded_at IS NULL AND quarantined_at IS NULL
                ORDER BY valid_from DESC,memory_id
                """ + lock_clause,
                (record["user_id"], record["namespace"], semantic_key),
            )
            record["supersedes_memory_id"] = (
                superseded_rows[0]["memory_id"] if superseded_rows else None
            )
            if superseded_rows:
                # Concurrent callers can enter with a request timestamp older
                # than the head they eventually lock.  Advance the new validity
                # boundary to the authoritative head boundary so history never
                # closes before it began.
                predecessor_start = max(
                    datetime.fromisoformat(old["valid_from"])
                    for old in superseded_rows
                )
                requested_start = datetime.fromisoformat(record["valid_from"])
                if predecessor_start > requested_start:
                    record["valid_from"] = predecessor_start.isoformat()
                record["confirmation_count"] = min(
                    2_147_483_647,
                    max(
                        int(
                            json.loads(old["payload_json"]).get(
                                "confirmation_count", 1
                            )
                        )
                        for old in superseded_rows
                    )
                    + 1,
                )
            for old in superseded_rows:
                old_payload = json.loads(old["payload_json"])
                close_at = record["valid_from"]
                old_payload["valid_to"] = close_at
                old_payload["lifecycle_status"] = "superseded"
                next_version = int(old.get("truth_version") or 1) + 1
                self._execute(
                    """
                    UPDATE memory_records
                    SET superseded_at=?,valid_to=?,truth_version=?,payload_json=?
                    WHERE memory_id=?
                    """,
                    (
                        close_at,
                        close_at,
                        next_version,
                        _canonical(old_payload),
                        old["memory_id"],
                    ),
                )
                self._write_outbox(
                    user_id=record["user_id"],
                    namespace=record["namespace"],
                    memory_id=old["memory_id"],
                    operation="supersede",
                    truth_version=next_version,
                    status="superseded",
                    occurred_at=now,
                )
            self._upsert_proposal(proposal)
            self._execute(
                """
                INSERT INTO memory_records (
                    memory_id,user_id,team_id,member_id,acl_visibility,
                    namespace,memory_type,status,sensitivity,
                    source,semantic_key,memory_class,valid_from,valid_to,
                    supersedes_memory_id,source_kind,provenance_version,
                    consent_version,confirmation_count,truth_version,
                    expires_at,deleted_at,superseded_at,payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?)
                """,
                (
                    record["memory_id"],
                    record["user_id"],
                    "personal_team",
                    "stylist" if record["namespace"] == "stylist" else None,
                    "member" if record["namespace"] == "stylist" else "team",
                    record["namespace"],
                    record["type"],
                    record["status"],
                    record["sensitivity"],
                    record["source"],
                    semantic_key,
                    record["memory_class"],
                    record["valid_from"],
                    record.get("valid_to"),
                    record.get("supersedes_memory_id"),
                    record["source_kind"],
                    record["provenance_version"],
                    record["consent_version"],
                    record["confirmation_count"],
                    1,
                    record.get("expires_at"),
                    _canonical(record),
                ),
            )
            self._execute(
                "DELETE FROM memory_contexts WHERE object_kind='proposal' AND object_id=?",
                (proposal["proposal_id"],),
            )
            if record_context is not None:
                self._put_context("record", record["memory_id"], record_context)
            self._execute(
                "INSERT INTO memory_audit VALUES (?,?,?,?,?)",
                (
                    f"maudit_{uuid.uuid4().hex}",
                    record["user_id"],
                    record["memory_id"],
                    "commit",
                    now,
                ),
            )
            self._write_outbox(
                user_id=record["user_id"],
                namespace=record["namespace"],
                memory_id=record["memory_id"],
                operation="commit",
                truth_version=1,
                status="active",
                occurred_at=now,
            )
            self._execute(
                """
                UPDATE memory_semantic_heads
                SET head_memory_id=?,truth_version=truth_version+1,updated_at=?
                WHERE user_id=? AND namespace=? AND semantic_key=?
                """,
                (
                    record["memory_id"],
                    now,
                    record["user_id"],
                    record["namespace"],
                    semantic_key,
                ),
            )
            return proposal, record, True

    def put_candidate_batch(
        self,
        request: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> None:
        """Persist only server-canonical candidate state, never source text."""

        with self._transaction():
            existing = self._fetchall(
                "SELECT user_id,fingerprint,payload_json FROM memory_candidate_requests WHERE request_id=?",
                (request["request_id"],),
            )
            if existing:
                row = existing[0]
                if (
                    row["user_id"] == request["user_id"]
                    and row["fingerprint"] == request["fingerprint"]
                    and json.loads(row["payload_json"]) == request["response"]
                ):
                    return
                raise MemoryRepositoryError("memory candidate request conflicts")
            self._execute(
                """
                INSERT INTO memory_candidate_requests (
                    request_id,user_id,fingerprint,expires_at,payload_json
                ) VALUES (?,?,?,?,?)
                """,
                (
                    request["request_id"],
                    request["user_id"],
                    request["fingerprint"],
                    str(request["expires_at"]),
                    _canonical(request["response"]),
                ),
            )
            for candidate in candidates:
                self._execute(
                    """
                    INSERT INTO memory_candidates (
                        candidate_id,proposal_id,request_id,user_id,
                        styling_session_id,namespace,canonical_kind,
                        canonical_value,status,expires_at,payload_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        candidate["candidate_id"],
                        candidate["proposal_id"],
                        request["request_id"],
                        candidate["user_id"],
                        candidate.get("styling_session_id"),
                        candidate["namespace"],
                        candidate["canonical_kind"],
                        candidate["canonical_value"],
                        candidate["status"],
                        str(candidate["expires_at"]),
                        _canonical(candidate),
                    ),
                )

    def candidate_request(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            rows = self._fetchall(
                "SELECT user_id,fingerprint,expires_at,payload_json FROM memory_candidate_requests WHERE request_id=?",
                (request_id,),
            )
        if not rows:
            return None
        row = rows[0]
        return {
            "user_id": row["user_id"],
            "fingerprint": row["fingerprint"],
            "expires_at": float(row["expires_at"]),
            "response": json.loads(row["payload_json"]),
        }

    def candidate_state(self, candidate_id: str) -> dict[str, Any] | None:
        with self._lock:
            rows = self._fetchall(
                "SELECT payload_json,status FROM memory_candidates WHERE candidate_id=?",
                (candidate_id,),
            )
        if not rows:
            return None
        payload = json.loads(rows[0]["payload_json"])
        payload["status"] = rows[0]["status"]
        return payload

    def candidate_decision(
        self, candidate_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self._lock:
            rows = self._fetchall(
                "SELECT fingerprint,payload_json FROM memory_candidate_decisions WHERE candidate_id=? AND idempotency_key=?",
                (candidate_id, idempotency_key),
            )
        if not rows:
            return None
        return {
            "fingerprint": rows[0]["fingerprint"],
            "response": json.loads(rows[0]["payload_json"]),
        }

    def record_candidate_decision(
        self,
        candidate_id: str,
        idempotency_key: str,
        fingerprint: str,
        status: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        with self._transaction():
            existing = self._fetchall(
                "SELECT fingerprint,payload_json FROM memory_candidate_decisions WHERE candidate_id=? AND idempotency_key=?",
                (candidate_id, idempotency_key),
            )
            if existing:
                if existing[0]["fingerprint"] != fingerprint:
                    raise MemoryRepositoryError(
                        "memory candidate idempotency key conflicts"
                    )
                return json.loads(existing[0]["payload_json"])
            rows = self._fetchall(
                "SELECT status,payload_json FROM memory_candidates WHERE candidate_id=?",
                (candidate_id,),
            )
            if not rows or rows[0]["status"] != "active":
                raise MemoryRepositoryError("memory candidate is not active")
            state = json.loads(rows[0]["payload_json"])
            state["status"] = status
            self._execute(
                "UPDATE memory_candidates SET status=?,payload_json=? WHERE candidate_id=?",
                (status, _canonical(state), candidate_id),
            )
            self._execute(
                """
                INSERT INTO memory_candidate_decisions (
                    candidate_id,idempotency_key,fingerprint,payload_json
                ) VALUES (?,?,?,?)
                """,
                (
                    candidate_id,
                    idempotency_key,
                    fingerprint,
                    _canonical(response),
                ),
            )
            return response

    def candidate_proposal_managed(self, proposal_id: str) -> bool:
        with self._lock:
            return bool(
                self._fetchall(
                    "SELECT candidate_id FROM memory_candidates WHERE proposal_id=?",
                    (proposal_id,),
                )
            )

    def candidate_rejected(
        self,
        *,
        user_id: str,
        styling_session_id: str,
        namespace: str,
        canonical_kind: str,
        canonical_value: str,
    ) -> bool:
        with self._lock:
            return bool(
                self._fetchall(
                    """
                    SELECT candidate_id FROM memory_candidates
                    WHERE user_id=? AND styling_session_id=? AND namespace=?
                      AND canonical_kind=? AND canonical_value=?
                      AND status='rejected'
                    """,
                    (
                        user_id,
                        styling_session_id,
                        namespace,
                        canonical_kind,
                        canonical_value,
                    ),
                )
            )

    def load_state(
        self,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        self._ensure_proposal_integrity()
        self._ensure_s15_columns()
        with self._lock:
            proposals = self._fetchall(
                """
                SELECT proposal_id,user_id,namespace,memory_type,status,
                       sensitivity,commit_blocked,record_id,payload_json
                FROM memory_proposals
                WHERE deleted_at IS NULL AND quarantined_at IS NULL AND team_id='personal_team'
                  AND ((namespace='shared' AND acl_visibility='team' AND member_id IS NULL)
                    OR (namespace='stylist' AND acl_visibility='member' AND member_id='stylist'))
                """
            )
            records = self._fetchall(
                """
                SELECT memory_id,user_id,namespace,memory_type,status,sensitivity,
                       source,memory_class,valid_from,valid_to,supersedes_memory_id,
                       source_kind,provenance_version,consent_version,
                       confirmation_count,expires_at,payload_json
                FROM memory_records
                WHERE deleted_at IS NULL AND superseded_at IS NULL
                  AND quarantined_at IS NULL
                  AND team_id='personal_team'
                  AND ((namespace='shared' AND acl_visibility='team' AND member_id IS NULL)
                    OR (namespace='stylist' AND acl_visibility='member' AND member_id='stylist'))
                """
            )
            contexts = self._fetchall(
                """
                SELECT c.object_kind,c.object_id,c.payload_json FROM memory_contexts c
                WHERE (
                    c.object_kind='proposal' AND EXISTS (
                        SELECT 1 FROM memory_proposals p
                        WHERE p.proposal_id=c.object_id AND p.deleted_at IS NULL
                          AND p.quarantined_at IS NULL
                          AND p.team_id='personal_team'
                          AND ((p.namespace='shared' AND p.acl_visibility='team' AND p.member_id IS NULL)
                            OR (p.namespace='stylist' AND p.acl_visibility='member' AND p.member_id='stylist'))
                    )
                ) OR (
                    c.object_kind='record' AND EXISTS (
                        SELECT 1 FROM memory_records r
                        WHERE r.memory_id=c.object_id AND r.deleted_at IS NULL
                          AND r.quarantined_at IS NULL
                          AND r.team_id='personal_team'
                          AND ((r.namespace='shared' AND r.acl_visibility='team' AND r.member_id IS NULL)
                            OR (r.namespace='stylist' AND r.acl_visibility='member' AND r.member_id='stylist'))
                    )
                )
                """
            )
        proposal_context: dict[str, dict[str, Any]] = {}
        record_context: dict[str, dict[str, Any]] = {}
        for row in contexts:
            target = proposal_context if row["object_kind"] == "proposal" else record_context
            target[row["object_id"]] = json.loads(row["payload_json"])
        return (
            [self._canonical_proposal_projection(row) for row in proposals],
            [self._canonical_record_projection(row) for row in records],
            proposal_context,
            record_context,
        )

    def active_records(
        self, user_id: str, namespaces: tuple[str, ...], now: str
    ) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        if not namespaces:
            return []
        # Re-run the sticky integrity audit before every retrieval boundary.  This
        # catches an out-of-band SQL/payload mutation even when the repository
        # instance itself has not restarted.
        self._ensure_proposal_integrity()
        self._ensure_s15_columns()
        placeholders = ",".join("?" for _ in namespaces)
        with self._lock:
            rows = self._fetchall(
                f"""
                SELECT r.memory_id,r.user_id,r.namespace,r.memory_type,r.status,
                       r.sensitivity,r.source,r.memory_class,r.valid_from,r.valid_to,
                       r.supersedes_memory_id,r.source_kind,r.provenance_version,
                       r.consent_version,r.confirmation_count,r.expires_at,
                       r.payload_json,c.payload_json AS context_json
                FROM memory_records r
                LEFT JOIN memory_contexts c
                  ON c.object_kind='record' AND c.object_id=r.memory_id
                WHERE r.user_id=? AND r.namespace IN ({placeholders})
                  AND r.team_id='personal_team'
                  AND (
                    (r.namespace='shared' AND r.acl_visibility='team' AND r.member_id IS NULL)
                    OR
                    (r.namespace='stylist' AND r.acl_visibility='member' AND r.member_id='stylist')
                  )
                  AND r.status='committed' AND r.sensitivity='non_sensitive'
                  AND r.source='user_confirmed' AND r.deleted_at IS NULL
                  AND r.superseded_at IS NULL
                  AND r.quarantined_at IS NULL
                  AND r.valid_from IS NOT NULL AND r.valid_from<=?
                  AND (r.valid_to IS NULL OR r.valid_to>?)
                  AND (r.expires_at IS NULL OR r.expires_at>?)
                """,
                (user_id, *namespaces, now, now, now),
            )
        return [
            (
                self._canonical_record_projection(row),
                json.loads(row["context_json"]) if row["context_json"] else None,
            )
            for row in rows
        ]

    def expire_ids(self, ids: list[str], expired_at: str) -> None:
        if not ids:
            return
        with self._transaction():
            lock_clause = " FOR UPDATE" if self._dialect == "postgresql" else ""
            for memory_id in ids:
                rows = self._fetchall(
                    "SELECT user_id,namespace,truth_version,payload_json FROM memory_records WHERE memory_id=? AND deleted_at IS NULL AND superseded_at IS NULL AND quarantined_at IS NULL"
                    + lock_clause,
                    (memory_id,),
                )
                if not rows:
                    continue
                payload = json.loads(rows[0]["payload_json"])
                proposal_id = payload["source_proposal_id"]
                payload["content"] = "[expired]"
                payload["valid_to"] = expired_at
                payload["lifecycle_status"] = "expired"
                next_version = int(rows[0].get("truth_version") or 1) + 1
                updated = self._execute(
                    "UPDATE memory_records SET deleted_at=?,valid_to=?,truth_version=?,payload_json=? WHERE memory_id=? AND deleted_at IS NULL AND truth_version=?",
                    (
                        expired_at,
                        expired_at,
                        next_version,
                        _canonical(payload),
                        memory_id,
                        next_version - 1,
                    ),
                )
                if updated.rowcount != 1:
                    raise MemoryRepositoryError("memory expiry CAS conflict")
                self._execute(
                    "DELETE FROM memory_contexts WHERE object_kind='record' AND object_id=?",
                    (memory_id,),
                )
                self._execute(
                    "UPDATE memory_proposals SET deleted_at=? WHERE proposal_id=? AND user_id=?",
                    (expired_at, proposal_id, rows[0]["user_id"]),
                )
                self._execute(
                    "DELETE FROM memory_contexts WHERE object_kind='proposal' AND object_id=?",
                    (proposal_id,),
                )
                self._execute(
                    "INSERT INTO memory_audit VALUES (?,?,?,?,?)",
                    (
                        f"maudit_{uuid.uuid4().hex}",
                        rows[0]["user_id"],
                        memory_id,
                        "expire",
                        expired_at,
                    ),
                )
                self._write_outbox(
                    user_id=rows[0]["user_id"],
                    namespace=rows[0]["namespace"],
                    memory_id=memory_id,
                    operation="expire",
                    truth_version=next_version,
                    status="expired",
                    occurred_at=expired_at,
                )

    def tombstone(
        self, *, user_id: str, object_id: str, deleted_at: str
    ) -> MemoryTombstoneResult | None:
        with self._transaction():
            lock_clause = " FOR UPDATE" if self._dialect == "postgresql" else ""
            proposal_rows = self._fetchall(
                """
                SELECT payload_json FROM memory_proposals
                WHERE proposal_id=? AND user_id=? AND deleted_at IS NULL
                  AND quarantined_at IS NULL
                  AND team_id='personal_team'
                  AND ((namespace='shared' AND acl_visibility='team' AND member_id IS NULL)
                    OR (namespace='stylist' AND acl_visibility='member' AND member_id='stylist'))
                """ + lock_clause,
                (object_id, user_id),
            )
            if proposal_rows:
                proposal = json.loads(proposal_rows[0]["payload_json"])
                proposal["content"] = None
                self._execute(
                    "UPDATE memory_proposals SET deleted_at=?,payload_json=? WHERE proposal_id=?",
                    (deleted_at, _canonical(proposal), object_id),
                )
                linked = proposal.get("record_id")
                if linked:
                    linked_rows = self._fetchall(
                        "SELECT namespace,truth_version,payload_json FROM memory_records WHERE memory_id=? AND user_id=? AND deleted_at IS NULL"
                        + lock_clause,
                        (linked, user_id),
                    )
                    if not linked_rows:
                        raise MemoryRepositoryError(
                            "committed proposal record is unavailable"
                        )
                    linked_payload = (
                        json.loads(linked_rows[0]["payload_json"])
                        if linked_rows
                        else None
                    )
                    if linked_payload is not None:
                        linked_payload["content"] = "[deleted]"
                        linked_payload["valid_to"] = deleted_at
                        linked_payload["lifecycle_status"] = "deleted"
                    next_version = (
                        int(linked_rows[0].get("truth_version") or 1) + 1
                        if linked_rows
                        else 1
                    )
                    updated = self._execute(
                        "UPDATE memory_records SET deleted_at=?,valid_to=?,truth_version=?,payload_json=? WHERE memory_id=? AND user_id=? AND deleted_at IS NULL AND truth_version=?",
                        (
                            deleted_at,
                            deleted_at,
                            next_version,
                            _canonical(linked_payload or {}),
                            linked,
                            user_id,
                            next_version - 1,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise MemoryRepositoryError("memory deletion CAS conflict")
                    if linked_rows:
                        self._write_outbox(
                            user_id=user_id,
                            namespace=linked_rows[0]["namespace"],
                            memory_id=linked,
                            operation="delete",
                            truth_version=next_version,
                            status="deleted",
                            occurred_at=deleted_at,
                        )
                self._execute(
                    "DELETE FROM memory_contexts WHERE object_id IN (?,?)",
                    (object_id, linked or ""),
                )
                kind = "record" if linked else "proposal"
                payload = linked_payload if linked else proposal
                receipt_namespace = proposal["namespace"]
                receipt_truth_version = next_version if linked else 0
            else:
                record_rows = self._fetchall(
                    """
                    SELECT namespace,truth_version,payload_json FROM memory_records
                    WHERE memory_id=? AND user_id=? AND deleted_at IS NULL
                      AND team_id='personal_team'
                      AND ((namespace='shared' AND acl_visibility='team' AND member_id IS NULL)
                        OR (namespace='stylist' AND acl_visibility='member' AND member_id='stylist'))
                    """ + lock_clause,
                    (object_id, user_id),
                )
                if not record_rows:
                    return None
                payload = json.loads(record_rows[0]["payload_json"])
                payload["content"] = "[deleted]"
                payload["valid_to"] = deleted_at
                payload["lifecycle_status"] = "deleted"
                next_version = int(record_rows[0].get("truth_version") or 1) + 1
                updated = self._execute(
                    "UPDATE memory_records SET deleted_at=?,valid_to=?,truth_version=?,payload_json=? WHERE memory_id=? AND deleted_at IS NULL AND truth_version=?",
                    (
                        deleted_at,
                        deleted_at,
                        next_version,
                        _canonical(payload),
                        object_id,
                        next_version - 1,
                    ),
                )
                if updated.rowcount != 1:
                    raise MemoryRepositoryError("memory deletion CAS conflict")
                proposal_id = payload["source_proposal_id"]
                self._execute(
                    "UPDATE memory_proposals SET deleted_at=? WHERE proposal_id=? AND user_id=?",
                    (deleted_at, proposal_id, user_id),
                )
                self._execute(
                    "DELETE FROM memory_contexts WHERE object_id IN (?,?)",
                    (object_id, proposal_id),
                )
                kind = "record"
                receipt_namespace = record_rows[0]["namespace"]
                receipt_truth_version = next_version
                self._write_outbox(
                    user_id=user_id,
                    namespace=record_rows[0]["namespace"],
                    memory_id=object_id,
                    operation="delete",
                    truth_version=next_version,
                    status="deleted",
                    occurred_at=deleted_at,
                )
            self._execute(
                "INSERT INTO memory_audit VALUES (?,?,?,?,?)",
                (
                    f"maudit_{uuid.uuid4().hex}",
                    user_id,
                    object_id,
                    "delete",
                    deleted_at,
                ),
            )
            return MemoryTombstoneResult(
                deleted_kind=kind,
                user_id=user_id,
                namespace=receipt_namespace,
                truth_version=receipt_truth_version,
                payload=payload,
            )

    def rebuild_soft_index(
        self,
        user_id: str,
        namespaces: tuple[str, ...],
        memory_ids: tuple[str, ...],
        *,
        index_version: str,
        rebuilt_at: str,
    ) -> tuple[str, ...]:
        """Replace a derived projection using only IDs revalidated by SQL truth."""
        if not namespaces:
            return ()
        requested = tuple(sorted(set(memory_ids)))
        placeholders = ",".join("?" for _ in namespaces)
        with self._transaction():
            allowed = self._replace_soft_index_locked(
                user_id=user_id,
                namespaces=namespaces,
                requested=requested,
                index_version=index_version,
                occurred_at=rebuilt_at,
            )
        return tuple(sorted(allowed))

    def _replace_soft_index_locked(
        self,
        *,
        user_id: str,
        namespaces: tuple[str, ...],
        requested: tuple[str, ...],
        index_version: str,
        occurred_at: str,
    ) -> dict[str, str]:
        placeholders = ",".join("?" for _ in namespaces)
        active = self._fetchall(
            f"""
            SELECT memory_id,namespace FROM memory_records
            WHERE user_id=? AND namespace IN ({placeholders})
              AND team_id='personal_team'
              AND ((namespace='shared' AND acl_visibility='team' AND member_id IS NULL)
                OR (namespace='stylist' AND acl_visibility='member' AND member_id='stylist'))
              AND status='committed' AND sensitivity='non_sensitive'
              AND source='user_confirmed' AND deleted_at IS NULL
              AND superseded_at IS NULL AND quarantined_at IS NULL
              AND memory_class!='hard_constraint'
              AND valid_from IS NOT NULL AND valid_from<=?
              AND (valid_to IS NULL OR valid_to>?)
              AND (expires_at IS NULL OR expires_at>?)
            """,
            (user_id, *namespaces, occurred_at, occurred_at, occurred_at),
        )
        allowed = {
            row["memory_id"]: row["namespace"]
            for row in active
            if row["memory_id"] in requested
        }
        self._execute(
            f"DELETE FROM memory_soft_index WHERE user_id=? AND namespace IN ({placeholders})",
            (user_id, *namespaces),
        )
        for memory_id in sorted(allowed):
            self._execute(
                "INSERT INTO memory_soft_index VALUES (?,?,?,?,?)",
                (
                    user_id,
                    allowed[memory_id],
                    memory_id,
                    index_version,
                    occurred_at,
                ),
            )
        return allowed

    def consume_soft_index_outbox(
        self,
        user_id: str,
        namespaces: tuple[str, ...],
        memory_ids: tuple[str, ...],
        *,
        consumer_name: str,
        index_version: str,
        consumed_at: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Atomically rebuild the ID projection and advance outbox delivery."""
        if not namespaces:
            return (), ()
        requested = tuple(sorted(set(memory_ids)))
        placeholders = ",".join("?" for _ in namespaces)
        with self._transaction():
            pending = self._fetchall(
                f"""
                SELECT o.event_id FROM memory_outbox o
                LEFT JOIN memory_outbox_delivery d
                  ON d.consumer_name=? AND d.event_id=o.event_id
                WHERE o.user_id=? AND o.namespace IN ({placeholders})
                  AND d.event_id IS NULL
                ORDER BY o.occurred_at,o.event_id
                """,
                (consumer_name, user_id, *namespaces),
            )
            # Delivery state is not truth.  Rebuild from the caller's eligible
            # IDs and revalidate them against canonical SQL on every consume,
            # including when there are no new outbox rows.  This removes stale
            # projection entries after ACL/quarantine/validity changes.
            allowed = self._replace_soft_index_locked(
                user_id=user_id,
                namespaces=namespaces,
                requested=requested,
                index_version=index_version,
                occurred_at=consumed_at,
            )
            for row in pending:
                self._execute(
                    """
                    INSERT INTO memory_outbox_delivery (
                        consumer_name,event_id,processed_at
                    ) VALUES (?,?,?)
                    ON CONFLICT(consumer_name,event_id) DO NOTHING
                    """,
                    (consumer_name, row["event_id"], consumed_at),
                )
        return tuple(sorted(allowed)), tuple(row["event_id"] for row in pending)

    def soft_index_ids(
        self, user_id: str, namespaces: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not namespaces:
            return ()
        placeholders = ",".join("?" for _ in namespaces)
        with self._lock:
            rows = self._fetchall(
                f"SELECT memory_id FROM memory_soft_index WHERE user_id=? AND namespace IN ({placeholders}) ORDER BY memory_id",
                (user_id, *namespaces),
            )
        return tuple(row["memory_id"] for row in rows)

    def outbox_events(self, user_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if user_id is None:
                return self._fetchall(
                    "SELECT * FROM memory_outbox ORDER BY occurred_at,event_id"
                )
            return self._fetchall(
                "SELECT * FROM memory_outbox WHERE user_id=? ORDER BY occurred_at,event_id",
                (user_id,),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def create_memory_repository(database_url: str | None, root_dir: Path) -> SqlMemoryRepository:
    return SqlMemoryRepository(database_url, root_dir)
