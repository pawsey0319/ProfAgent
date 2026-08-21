from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta

import httpx
import uvicorn


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from profagent.app import create_app
from profagent.config import Settings
from profagent.memory_repository import SqlMemoryRepository
from profagent.tracing import utc_now


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    assert port != 8000
    return port


class TemporaryServer:
    def __init__(self, database_path: Path) -> None:
        self.port = _free_port()
        app = create_app(
            Settings(
                root_dir=ROOT,
                cpa_text_enabled=False,
                cpa_image_enabled=False,
                database_url=f"sqlite:///{database_path.as_posix()}",
            )
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port,
                log_level="error",
                lifespan="on",
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "TemporaryServer":
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.server.started, "temporary S15 HTTP server did not start"
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "temporary S15 HTTP server did not stop"


def _json(response: httpx.Response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    value = response.json()
    assert isinstance(value, dict)
    return value


def _propose(client: httpx.Client, *, namespace: str, content: str, ttl_days=None) -> dict:
    response = _json(
        client.post(
            "/memory/propose",
            json={
                "user_id": "u01",
                "styling_session_id": None,
                "namespace": namespace,
                "type": "preference",
                "content": content,
                "ttl_days": ttl_days,
            },
        )
    )
    assert response["record"] is None
    assert response["proposal"]["status"] == "proposed"
    return response["proposal"]


def _confirm(client: httpx.Client, proposal_id: str) -> dict:
    response = _json(
        client.post(
            f"/memory/{proposal_id}/confirm",
            json={"user_id": "u01", "decision": "confirm"},
        )
    )
    assert response["proposal"]["status"] == "committed"
    assert response["record"]["lifecycle_status"] == "active"
    return response["record"]


def _tamper_future_and_quarantine(
    database_path: Path,
    *,
    future_id: str,
    quarantine_id: str,
    acl_id: str,
) -> None:
    future = (utc_now() + timedelta(days=30)).isoformat()
    connection = sqlite3.connect(database_path)
    raw = connection.execute(
        "SELECT payload_json FROM memory_records WHERE memory_id=?", (future_id,)
    ).fetchone()[0]
    payload = json.loads(raw)
    payload["valid_from"] = future
    connection.execute(
        "UPDATE memory_records SET valid_from=?,payload_json=? WHERE memory_id=?",
        (future, json.dumps(payload, ensure_ascii=False), future_id),
    )

    raw = connection.execute(
        "SELECT payload_json FROM memory_records WHERE memory_id=?", (quarantine_id,)
    ).fetchone()[0]
    payload = json.loads(raw)
    payload["user_id"] = "u02"
    connection.execute(
        "UPDATE memory_records SET payload_json=? WHERE memory_id=?",
        (json.dumps(payload, ensure_ascii=False), quarantine_id),
    )
    connection.execute(
        "UPDATE memory_records SET member_id='other_member' WHERE memory_id=?",
        (acl_id,),
    )
    connection.commit()
    connection.close()


def main() -> int:
    temporary = tempfile.TemporaryDirectory(prefix="profagent-s15-http-", ignore_cleanup_errors=True)
    database_path = Path(temporary.name) / "memory.sqlite3"
    servers: list[TemporaryServer] = []
    try:
        first = TemporaryServer(database_path).start()
        servers.append(first)
        with httpx.Client(base_url=first.base_url, timeout=15.0, trust_env=False) as client:
            health = _json(client.get("/health"))
            assert health["ready"] is True
            first_record = _confirm(
                client,
                _propose(
                    client, namespace="stylist", content="偏爱直筒裤"
                )["proposal_id"],
            )
            shared_record = _confirm(
                client,
                _propose(
                    client, namespace="shared", content="偏爱简洁风格"
                )["proposal_id"],
            )
            assert len(_json(client.get("/memory", params={"user_id": "u01"}))["records"]) == 2
            assert len(
                _json(
                    client.get(
                        "/memory", params={"user_id": "u01", "namespace": "shared"}
                    )
                )["records"]
            ) == 1
            assert _json(client.get("/memory", params={"user_id": "u02"}))["records"] == []
            cross_owner = client.post(
                f"/memory/{first_record['source_proposal_id']}/confirm",
                json={"user_id": "u02", "decision": "confirm"},
            )
            assert cross_owner.status_code == 404

            second = TemporaryServer(database_path).start()
            servers.append(second)
            with httpx.Client(base_url=second.base_url, timeout=15.0, trust_env=False) as peer:
                peer_list = _json(peer.get("/memory", params={"user_id": "u01"}))
                assert {item["memory_id"] for item in peer_list["records"]} == {
                    first_record["memory_id"],
                    shared_record["memory_id"],
                }

            browser = subprocess.run(
                ["node", "scripts/tester_s15_browser_smoke.cjs"],
                cwd=ROOT,
                env={**os.environ, "PROFAGENT_SMOKE_URL": first.base_url},
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=45,
            )
            assert browser.returncode == 0, (browser.stdout + browser.stderr)[-3000:]
            browser_lines = [line.strip() for line in browser.stdout.splitlines() if line.strip()]
            browser_summary = browser_lines[-1]
            assert browser_summary.startswith("web S15 Memory real-browser smoke: PASS")

        second.stop()
        servers.remove(second)
        first.stop()
        servers.remove(first)

        restarted = TemporaryServer(database_path).start()
        servers.append(restarted)
        with httpx.Client(base_url=restarted.base_url, timeout=15.0, trust_env=False) as client:
            recovered = _json(client.get("/memory", params={"user_id": "u01"}))
            assert {item["memory_id"] for item in recovered["records"]} == {
                first_record["memory_id"],
                shared_record["memory_id"],
            }
            replacement = _confirm(
                client,
                _propose(client, namespace="stylist", content="偏爱直筒裤")["proposal_id"],
            )
            assert replacement["supersedes_memory_id"] == first_record["memory_id"]
            assert replacement["confirmation_count"] == 2
            listed = _json(client.get("/memory", params={"user_id": "u01"}))["records"]
            assert first_record["memory_id"] not in {item["memory_id"] for item in listed}
            deleted = _json(
                client.delete(
                    f"/memory/{replacement['memory_id']}", params={"user_id": "u01"}
                )
            )
            assert deleted == {
                **deleted,
                "api_version": "r1_demo_v1",
                "deleted_id": replacement["memory_id"],
                "deleted_kind": "record",
                "user_id": "u01",
                "namespace": "stylist",
                "truth_version": 2,
            }
            assert "content" not in deleted

            future = _confirm(
                client,
                _propose(client, namespace="stylist", content="偏爱低调配色")["proposal_id"],
            )
            quarantined = _confirm(
                client,
                _propose(
                    client,
                    namespace="stylist",
                    content="长时间站立时优先选择适合久走的鞋。",
                )["proposal_id"],
            )
            expiring = _confirm(
                client,
                _propose(
                    client,
                    namespace="shared",
                    content="偏爱简洁风格",
                    ttl_days=1,
                )["proposal_id"],
            )
            acl = _confirm(
                client,
                _propose(client, namespace="shared", content="偏爱直筒裤")["proposal_id"],
            )

        restarted.stop()
        servers.remove(restarted)

        repository = SqlMemoryRepository(
            f"sqlite:///{database_path.as_posix()}", ROOT
        )
        repository.expire_ids(
            [expiring["memory_id"]], (utc_now() + timedelta(days=2)).isoformat()
        )
        repository.close()
        _tamper_future_and_quarantine(
            database_path,
            future_id=future["memory_id"],
            quarantine_id=quarantined["memory_id"],
            acl_id=acl["memory_id"],
        )

        final = TemporaryServer(database_path).start()
        servers.append(final)
        with httpx.Client(base_url=final.base_url, timeout=15.0, trust_env=False) as client:
            final_list = _json(client.get("/memory", params={"user_id": "u01"}))
            assert final_list["records"] == []
            assert final_list["proposals"] == []
            assert final_list["retrieval_index_count"] == 0
            assert _json(client.get("/memory", params={"user_id": "u02"}))["records"] == []
        final.stop()
        servers.remove(final)

        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        lifecycle = {
            row["memory_id"]: (row["payload_json"], row["quarantined_at"], row["quarantine_reason"])
            for row in connection.execute(
                "SELECT memory_id,payload_json,quarantined_at,quarantine_reason FROM memory_records"
            )
        }
        assert json.loads(lifecycle[expiring["memory_id"]][0])["lifecycle_status"] == "expired"
        assert lifecycle[quarantined["memory_id"]][1] is not None
        assert lifecycle[quarantined["memory_id"]][2] == "canonical_payload_mismatch"
        assert lifecycle[acl["memory_id"]][1] is not None
        assert lifecycle[acl["memory_id"]][2] == "invalid_acl_projection"
        operations = [row[0] for row in connection.execute("SELECT operation FROM memory_outbox")]
        assert {"commit", "supersede", "delete", "expire"}.issubset(operations)
        connection.close()

        print(
            json.dumps(
                {
                    "port_is_not_8000": True,
                    "external_provider_calls": 0,
                    "http_flow": "propose-confirm-list-delete",
                    "restart_sqlite": "pass",
                    "cross_instance_sqlite": "pass",
                    "owner_namespace_acl": "pass",
                    "future_expire_supersede_quarantine": "pass",
                    "delete_receipt_content_fields": 0,
                    "browser_dom": {
                        "status": "pass",
                        "summary": browser_summary,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        for server in reversed(servers):
            try:
                server.stop()
            except Exception:
                pass
        temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
