from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Protocol
from urllib.parse import unquote, urlsplit


class MemoryRepositoryError(RuntimeError):
    pass


class MemoryRepository(Protocol):
    def load_state(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]: ...
    def put_proposal(self, payload: dict[str, Any], context: dict[str, Any] | None = None) -> None: ...
    def commit(self, proposal: dict[str, Any], record: dict[str, Any], *, semantic_key: str, proposal_context: dict[str, Any] | None, record_context: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any], bool]: ...
    def put_rejected_proposal(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def tombstone(self, *, user_id: str, object_id: str, deleted_at: str) -> tuple[str, dict[str, Any]] | None: ...
    def expire_ids(self, ids: list[str], expired_at: str) -> None: ...
    def active_records(self, user_id: str, namespaces: tuple[str, ...], now: str) -> list[tuple[dict[str, Any], dict[str, Any] | None]]: ...
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
        )
        with self._transaction():
            for statement in statements:
                self._execute(statement)
        self._ensure_acl_columns()
        with self._transaction():
            self._execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_active_acl ON memory_records (user_id, team_id, member_id, acl_visibility, namespace, status, sensitivity, source, deleted_at, superseded_at, expires_at)"
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
    def _row_dict(row: Any) -> dict[str, Any]:
        return dict(row)

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
                "WHERE proposal_id=? AND user_id=? AND deleted_at IS NULL "
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
                "WHERE proposal_id=? AND user_id=? AND deleted_at IS NULL "
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
            # Same semantic key is immutable history, not duplicate active
            # influence. Keep the older row recoverable but mark superseded.
            self._execute(
                """
                UPDATE memory_records SET superseded_at=?
                WHERE user_id=? AND namespace=? AND semantic_key=?
                  AND deleted_at IS NULL AND superseded_at IS NULL
                """,
                (now, record["user_id"], record["namespace"], semantic_key),
            )
            self._upsert_proposal(proposal)
            self._execute(
                """
                INSERT INTO memory_records (
                    memory_id,user_id,team_id,member_id,acl_visibility,
                    namespace,memory_type,status,sensitivity,
                    source,semantic_key,expires_at,deleted_at,superseded_at,payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?)
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
            return proposal, record, True

    def load_state(
        self,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        with self._lock:
            proposals = self._fetchall(
                """
                SELECT payload_json FROM memory_proposals
                WHERE deleted_at IS NULL AND team_id='personal_team'
                  AND ((namespace='shared' AND acl_visibility='team' AND member_id IS NULL)
                    OR (namespace='stylist' AND acl_visibility='member' AND member_id='stylist'))
                """
            )
            records = self._fetchall(
                """
                SELECT payload_json FROM memory_records
                WHERE deleted_at IS NULL AND superseded_at IS NULL
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
                          AND p.team_id='personal_team'
                          AND ((p.namespace='shared' AND p.acl_visibility='team' AND p.member_id IS NULL)
                            OR (p.namespace='stylist' AND p.acl_visibility='member' AND p.member_id='stylist'))
                    )
                ) OR (
                    c.object_kind='record' AND EXISTS (
                        SELECT 1 FROM memory_records r
                        WHERE r.memory_id=c.object_id AND r.deleted_at IS NULL
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
            [json.loads(row["payload_json"]) for row in proposals],
            [json.loads(row["payload_json"]) for row in records],
            proposal_context,
            record_context,
        )

    def active_records(
        self, user_id: str, namespaces: tuple[str, ...], now: str
    ) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        if not namespaces:
            return []
        placeholders = ",".join("?" for _ in namespaces)
        with self._lock:
            rows = self._fetchall(
                f"""
                SELECT r.payload_json, c.payload_json AS context_json
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
                  AND (r.expires_at IS NULL OR r.expires_at>?)
                """,
                (user_id, *namespaces, now),
            )
        return [
            (
                json.loads(row["payload_json"]),
                json.loads(row["context_json"]) if row["context_json"] else None,
            )
            for row in rows
        ]

    def expire_ids(self, ids: list[str], expired_at: str) -> None:
        if not ids:
            return
        with self._transaction():
            for memory_id in ids:
                rows = self._fetchall(
                    "SELECT user_id,payload_json FROM memory_records WHERE memory_id=? AND deleted_at IS NULL",
                    (memory_id,),
                )
                if not rows:
                    continue
                payload = json.loads(rows[0]["payload_json"])
                proposal_id = payload["source_proposal_id"]
                payload["content"] = "[expired]"
                self._execute(
                    "UPDATE memory_records SET deleted_at=?,payload_json=? WHERE memory_id=?",
                    (expired_at, _canonical(payload), memory_id),
                )
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

    def tombstone(
        self, *, user_id: str, object_id: str, deleted_at: str
    ) -> tuple[str, dict[str, Any]] | None:
        with self._transaction():
            proposal_rows = self._fetchall(
                """
                SELECT payload_json FROM memory_proposals
                WHERE proposal_id=? AND user_id=? AND deleted_at IS NULL
                  AND team_id='personal_team'
                  AND ((namespace='shared' AND acl_visibility='team' AND member_id IS NULL)
                    OR (namespace='stylist' AND acl_visibility='member' AND member_id='stylist'))
                """,
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
                        "SELECT payload_json FROM memory_records WHERE memory_id=? AND user_id=? AND deleted_at IS NULL",
                        (linked, user_id),
                    )
                    linked_payload = (
                        json.loads(linked_rows[0]["payload_json"])
                        if linked_rows
                        else None
                    )
                    if linked_payload is not None:
                        linked_payload["content"] = "[deleted]"
                    self._execute(
                        "UPDATE memory_records SET deleted_at=?,payload_json=? WHERE memory_id=? AND user_id=?",
                        (
                            deleted_at,
                            _canonical(linked_payload or {}),
                            linked,
                            user_id,
                        ),
                    )
                self._execute(
                    "DELETE FROM memory_contexts WHERE object_id IN (?,?)",
                    (object_id, linked or ""),
                )
                kind = "proposal"
                payload = proposal
            else:
                record_rows = self._fetchall(
                    """
                    SELECT payload_json FROM memory_records
                    WHERE memory_id=? AND user_id=? AND deleted_at IS NULL
                      AND team_id='personal_team'
                      AND ((namespace='shared' AND acl_visibility='team' AND member_id IS NULL)
                        OR (namespace='stylist' AND acl_visibility='member' AND member_id='stylist'))
                    """,
                    (object_id, user_id),
                )
                if not record_rows:
                    return None
                payload = json.loads(record_rows[0]["payload_json"])
                payload["content"] = "[deleted]"
                self._execute(
                    "UPDATE memory_records SET deleted_at=?,payload_json=? WHERE memory_id=?",
                    (deleted_at, _canonical(payload), object_id),
                )
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
            return kind, payload

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def create_memory_repository(database_url: str | None, root_dir: Path) -> SqlMemoryRepository:
    return SqlMemoryRepository(database_url, root_dir)
