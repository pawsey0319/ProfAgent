from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from io import BytesIO
from threading import Barrier

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from profagent.app import create_app
from profagent.image_provider import GrokImageProvider
from profagent.memory_service import (
    MemoryConfirmInput,
    MemoryProposeInput,
    MemoryRecord,
    MemoryService,
    MemoryNotFound,
)
from profagent.memory_repository import SqlMemoryRepository
from profagent.providers import ProviderUnavailable
from profagent.tracing import TraceStore, utc_now


IMAGE_MODEL = "grok-imagine-image-quality"


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (256, 256), (32, 64, 96)).save(output, format="PNG")
    return output.getvalue()


def test_versioned_wardrobe_catalog_assets_are_exact_owner_bound_and_ai_labeled(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    with TestClient(app) as client:
        response = client.get("/wardrobe/catalog-assets", params={"user_id": "u01"})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["user_id"] == "u01"
        assert payload["manifest_version"] == "wardrobe_generated_v1_s12r2"
        assert {item["garment_id"] for item in payload["assets"]} == {
            item.garment_id
            for item in app.state.services.repository.list_garments("u01")
        }
        ready = next(item for item in payload["assets"] if item["status"] == "ready")
        assert ready["asset_version"] == "wardrobe_generated_v1_s12r2"
        assert ready["provider"] == {
            "status": "ok",
            "attempted": True,
            "requested_model": IMAGE_MODEL,
            "transport_model": IMAGE_MODEL,
            "request_model_pinned": True,
            "provenance_verified": True,
            "model_reported": False,
            "resolved_model": None,
            "model_verified": False,
            "verification_basis": "batch_exact_request_contract",
            "degraded": False,
            "reason_code": None,
        }
        assert ready["prompt_sha256"] is None
        assert ready["prompt_hash_status"] == "not_preserved"
        assert len(ready["content_sha256"]) == 64
        assert ready["ai_label"]["display_label"] == "AI生成目录参考"
        assert ready["ai_label"]["disclaimer"] == (
            "仅供品类、风格与配色参考，不代表真实尺码、面料或垂坠"
        )
        assert ready["image_url"].count("?") == 1
        assert set(ready["image_url"].split("?", 1)[1].split("&")) == {
            "user_id=u01",
            "asset_version=wardrobe_generated_v1_s12r2",
            f"content_sha256={ready['content_sha256']}",
        }
        image = client.get(ready["image_url"])
        assert image.status_code == 200
        assert image.headers["x-ai-generated"] == "true"
        assert image.headers["x-ai-requested-model"] == IMAGE_MODEL
        assert image.headers["x-ai-model-reported"] == "false"
        assert image.headers["x-ai-verification-basis"] == (
            "batch_exact_request_contract"
        )
        assert "x-ai-model" not in image.headers
        assert image.headers["cache-control"] == (
            "private, max-age=31536000, immutable"
        )
        assert image.headers["x-content-type-options"] == "nosniff"
        assert client.get(
            f"/wardrobe/{ready['garment_id']}/catalog-image",
            params={
                "user_id": "u02",
                "asset_version": "wardrobe_generated_v1_s12r2",
                "content_sha256": ready["content_sha256"],
            },
        ).status_code == 404
        assert client.get(
            f"/wardrobe/{ready['garment_id']}/catalog-image",
            params={
                "user_id": "u01",
                "asset_version": "wrong-version",
                "content_sha256": ready["content_sha256"],
            },
        ).status_code == 404
        assert client.get(
            f"/wardrobe/{ready['garment_id']}/catalog-image",
            params={
                "user_id": "u01",
                "asset_version": "wardrobe_generated_v1_s12r2",
                "content_sha256": "0" * 64,
            },
        ).status_code == 404
        assert client.get(
            f"/wardrobe/{ready['garment_id']}/catalog-image",
            params={
                "user_id": "u01",
                "asset_version": "wardrobe_generated_v1_s12r2",
            },
        ).status_code == 422


@pytest.mark.parametrize(
    "tamper",
    [
        "missing",
        "wrong_ai",
        "wrong_id",
        "wrong_model",
        "wrong_contract",
        "fake_prompt_hash",
        "fake_preserved_status",
        "wrong_asset_version",
        "claimed_actual_model",
        "wrong_verification_basis",
    ],
)
def test_wardrobe_catalog_rejects_missing_or_tampered_embedded_provenance(
    offline_settings, tmp_path, tamper: str
) -> None:
    app = create_app(offline_settings)
    service = app.state.services.wardrobe_assets
    service.root_dir = tmp_path.resolve()
    service.asset_root = tmp_path / "data" / "assets" / "wardrobe_generated_v1"
    service.manifest_path = (
        tmp_path / "data" / "manifests" / "wardrobe_generated_v1.json"
    )
    service.asset_root.mkdir(parents=True)
    service.manifest_path.parent.mkdir(parents=True)
    path = service.asset_root / "g001.png"
    metadata = PngImagePlugin.PngInfo()
    if tamper != "missing":
        metadata.add_text("AI-Generated", "false" if tamper == "wrong_ai" else "true")
        metadata.add_text(
            "Requested-Model", "wrong" if tamper == "wrong_model" else IMAGE_MODEL
        )
        metadata.add_text("Model-Reported", "false")
        metadata.add_text("Verification-Basis", "batch_exact_request_contract")
        metadata.add_text("Garment-ID", "g999" if tamper == "wrong_id" else "g001")
        metadata.add_text(
            "Generation-Contract",
            "wrong" if tamper == "wrong_contract" else "wardrobe_catalog_single_garment_v1",
        )
    Image.new("RGB", (256, 256), (10, 20, 30)).save(
        path, format="PNG", pnginfo=metadata
    )
    body = path.read_bytes()
    manifest = {
        "schema_version": 2,
        "asset_set": "wardrobe_generated_v1",
        "asset_version": "wardrobe_generated_v1_s12r2",
        "generation_contract": "wardrobe_catalog_single_garment_v1",
        "requested_model": IMAGE_MODEL,
        "request_model_pinned": True,
        "model_reported": False,
        "model_verified": False,
        "resolved_model": None,
        "verification_basis": "batch_exact_request_contract",
        "aspect_ratio": "1:1",
        "ai_generated": True,
        "metadata_status": "embedded_png_and_sidecar_manifest",
        "items": [
            {
                "garment_id": "g001",
                "user_id": "u01",
                "source_data_version": "fixtures_v1.0",
                "status": "succeeded",
                "requested_model": IMAGE_MODEL,
                "request_model_pinned": True,
                "model_reported": False,
                "model_verified": False,
                "resolved_model": None,
                "verification_basis": "batch_exact_request_contract",
                "aspect_ratio": "1:1",
                "prompt_sha256": None,
                "prompt_hash_status": "not_preserved",
                "relative_path": "data/assets/wardrobe_generated_v1/g001.png",
                "ai_generated": True,
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "width": 256,
                "height": 256,
                "media_type": "image/png",
            }
        ],
    }
    if tamper == "fake_prompt_hash":
        manifest["items"][0]["prompt_sha256"] = "a" * 64
    elif tamper == "fake_preserved_status":
        manifest["items"][0]["prompt_hash_status"] = "recorded_at_generation"
    elif tamper == "wrong_asset_version":
        manifest["asset_version"] = "wardrobe_generated_v1"
    elif tamper == "claimed_actual_model":
        manifest["model_reported"] = True
        manifest["model_verified"] = True
        manifest["resolved_model"] = IMAGE_MODEL
    elif tamper == "wrong_verification_basis":
        manifest["verification_basis"] = "reported_model_exact"
    service.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    asset = next(
        item
        for item in service.list_for_user("u01").assets
        if item.garment_id == "g001"
    )
    expected_status = (
        "missing"
        if tamper
        in {"wrong_asset_version", "claimed_actual_model", "wrong_verification_basis"}
        else "failed"
    )
    assert asset.status == expected_status
    assert asset.image_url is None
    assert asset.provider.status == "fallback"
    assert asset.provider.degraded is True
    app.state.services.memory.close()


def test_image_url_source_requires_allowlist_stream_limit_and_exact_content_type(
    offline_settings, monkeypatch
) -> None:
    image = _png()

    async def exercise(
        *,
        download_host: str,
        image_url: str | None = None,
        status: int = 200,
        content: bytes = image,
        content_type: str = "image/png",
    ) -> tuple[bytes, str]:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(
                    200,
                    headers={"x-cpa-trace-id": "cpa-image-url-1"},
                    json={
                        "model": IMAGE_MODEL,
                        "data": [
                            {"url": image_url or f"https://{download_host}/asset"}
                        ],
                    },
                )
            return httpx.Response(
                status,
                content=content,
                headers={"content-type": content_type},
            )

        provider = GrokImageProvider(
            replace(
                offline_settings,
                cpa_image_enabled=True,
                cpa_image_download_hosts=("cdn.example",),
            ),
            transport=httpx.MockTransport(handler),
        )
        return (await provider.generate_static_2d("static 2D outfit"))[:2]

    monkeypatch.setattr(
        GrokImageProvider,
        "_resolve_public_host",
        staticmethod(lambda _host: None),
    )
    body, media_type = asyncio.run(exercise(download_host="cdn.example"))
    assert body == image and media_type == "image/png"

    for kwargs in (
        {"download_host": "not-allowlisted.example"},
        {"download_host": "cdn.example", "status": 302},
        {"download_host": "cdn.example", "content_type": "application/octet-stream"},
        {"download_host": "cdn.example", "content_type": "image/jpeg"},
        {
            "download_host": "cdn.example",
            "content": b"\x89PNG\r\n\x1a\ncorrupt-not-decodable",
        },
        {"download_host": "cdn.example", "content": b"x" * (5 * 1024 * 1024 + 1)},
        {
            "download_host": "cdn.example",
            "image_url": "https://user:password@cdn.example/asset",
        },
        {
            "download_host": "cdn.example",
            "image_url": "https://cdn.example:444/asset",
        },
    ):
        with pytest.raises(ProviderUnavailable) as captured:
            asyncio.run(exercise(**kwargs))
        assert captured.value.reason_code == "CPA_IMAGE_OUTPUT_REJECTED"


def test_image_url_allowlisted_private_resolution_is_rejected(
    offline_settings, monkeypatch
) -> None:
    provider = GrokImageProvider(
        replace(
            offline_settings,
            cpa_image_enabled=True,
            cpa_image_download_hosts=("cdn.example",),
        ),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"x-cpa-trace-id": "cpa-image-private-1"},
                json={
                    "model": IMAGE_MODEL,
                    "data": [{"url": "https://cdn.example/asset"}],
                },
            )
        ),
    )
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))]
    )
    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(provider.generate_static_2d("static 2D outfit"))
    assert captured.value.reason_code == "CPA_IMAGE_OUTPUT_REJECTED"


def test_image_post_envelope_stream_stops_immediately_over_8mib(
    offline_settings,
) -> None:
    class CountingStream(httpx.AsyncByteStream):
        yielded = 0

        async def __aiter__(self):
            for _index in range(3):
                self.yielded += 1
                yield b"x" * (5 * 1024 * 1024)

        async def aclose(self) -> None:
            return None

    stream = CountingStream()
    provider = GrokImageProvider(
        replace(offline_settings, cpa_image_enabled=True),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=stream,
            )
        ),
    )
    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(provider.generate_static_2d("static 2D outfit"))
    assert captured.value.reason_code == "CPA_IMAGE_OUTPUT_REJECTED"
    assert stream.yielded == 2


def _memory_service(database_url: str, root_dir) -> MemoryService:
    return MemoryService(
        TraceStore(), database_url=database_url, root_dir=root_dir
    )


def _commit(
    service: MemoryService,
    *,
    content: str,
    memory_type: str = "preference",
    namespace: str = "stylist",
) -> tuple[str, str]:
    proposed = service.propose(
        MemoryProposeInput(
            user_id="u01",
            namespace=namespace,
            type=memory_type,
            content=content,
        )
    )
    committed = service.confirm(
        proposed.proposal.proposal_id,
        MemoryConfirmInput(user_id="u01", decision="confirm"),
    )
    assert committed.record is not None
    return proposed.proposal.proposal_id, committed.record.memory_id


def test_memory_sql_repository_survives_instances_and_delete(tmp_path) -> None:
    path = tmp_path / "memory.sqlite3"
    database_url = f"sqlite:///{path.as_posix()}"
    first = _memory_service(database_url, tmp_path)
    proposed = first.propose(
        MemoryProposeInput(
            user_id="u01",
            namespace="stylist",
            type="preference",
            content="偏爱直筒裤",
        )
    )
    proposal_id = proposed.proposal.proposal_id
    first.close()

    second = _memory_service(database_url, tmp_path)
    committed = second.confirm(
        proposal_id, MemoryConfirmInput(user_id="u01", decision="confirm")
    )
    memory_id = committed.record.memory_id
    second.close()

    third = _memory_service(database_url, tmp_path)
    assert [item.memory_id for item in third.list("u01").records] == [memory_id]
    terms, trace = third.retrieve_soft("u01", "通勤想穿直筒裤，风格简洁")
    assert "fit:straight" in terms
    trace_text = json.dumps(trace, ensure_ascii=False)
    assert "偏爱直筒裤" not in trace_text
    assert "vector" not in trace_text.lower() or '"vectors_logged": false' in trace_text.lower()
    third.delete("u01", memory_id)
    third.close()

    fourth = _memory_service(database_url, tmp_path)
    assert fourth.list("u01").records == []
    assert fourth.list("u01").proposals == []
    fourth.close()


def test_memory_cross_instance_confirm_is_transactionally_idempotent(tmp_path) -> None:
    path = tmp_path / "memory.sqlite3"
    database_url = f"sqlite:///{path.as_posix()}"
    seed = _memory_service(database_url, tmp_path)
    proposal_id = seed.propose(
        MemoryProposeInput(
            user_id="u01",
            namespace="stylist",
            type="preference",
            content="偏爱简洁风格",
        )
    ).proposal.proposal_id
    seed.close()
    first = _memory_service(database_url, tmp_path)
    second = _memory_service(database_url, tmp_path)
    barrier = Barrier(2)
    for service in (first, second):
        original = service.repository.commit

        def synchronized_commit(*args, _original=original, **kwargs):
            barrier.wait(timeout=5)
            return _original(*args, **kwargs)

        service.repository.commit = synchronized_commit  # type: ignore[method-assign]

    def confirm(service: MemoryService):
        return service.confirm(
            proposal_id, MemoryConfirmInput(user_id="u01", decision="confirm")
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, (first, second)))
    assert results[0].record is not None and results[1].record is not None
    assert results[0].record.memory_id == results[1].record.memory_id
    assert results[0].proposal.record_id == results[1].proposal.record_id
    for service, result in zip((first, second), results):
        assert result.proposal.trace_id == result.trace_id
        local_trace = service.traces.get(result.trace_id)
        assert local_trace is not None
        assert "偏爱简洁风格" not in json.dumps(
            local_trace.model_dump(mode="json"), ensure_ascii=False
        )
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 1
    finally:
        connection.close()
    first.close()
    second.close()
    third = _memory_service(database_url, tmp_path)
    repeated = third.confirm(
        proposal_id, MemoryConfirmInput(user_id="u01", decision="confirm")
    )
    assert repeated.record is not None
    assert repeated.record.memory_id == results[0].record.memory_id
    assert repeated.proposal.trace_id == repeated.trace_id
    assert third.traces.get(repeated.trace_id) is not None
    assert "偏爱简洁风格" not in json.dumps(
        third.traces.get(repeated.trace_id).model_dump(mode="json"),
        ensure_ascii=False,
    )
    third.close()


def test_memory_sql_acl_metadata_is_authoritative_not_caller_namespace(tmp_path) -> None:
    path = tmp_path / "memory.sqlite3"
    database_url = f"sqlite:///{path.as_posix()}"
    service = _memory_service(database_url, tmp_path)
    proposal_id, memory_id = _commit(service, content="偏爱直筒裤")
    connection = sqlite3.connect(path)
    try:
        stored = connection.execute(
            "SELECT team_id,member_id,acl_visibility FROM memory_records WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        assert stored == ("personal_team", "stylist", "member")
        proposal_acl = connection.execute(
            "SELECT team_id,member_id,acl_visibility FROM memory_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        assert proposal_acl == ("personal_team", "stylist", "member")
        connection.execute(
            "UPDATE memory_records SET member_id='other_member' WHERE memory_id=?",
            (memory_id,),
        )
        connection.commit()
    finally:
        connection.close()
    assert service.retrieve_soft("u01", "直筒裤", namespaces=("stylist",))[0] == ()
    pending_id = service.propose(
        MemoryProposeInput(
            user_id="u01",
            namespace="stylist",
            type="preference",
            content="偏爱简洁风格",
        )
    ).proposal.proposal_id
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE memory_proposals SET acl_visibility='team' WHERE proposal_id=?",
            (pending_id,),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(MemoryNotFound):
        service.confirm(
            pending_id, MemoryConfirmInput(user_id="u01", decision="confirm")
        )
    service.close()


def test_memory_repository_migrates_legacy_sqlite_acl_schema(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
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
        INSERT INTO memory_records VALUES (
            'mem_legacy','u01','shared','preference','committed','non_sensitive',
            'user_confirmed','soft:legacy',NULL,NULL,NULL,'{}'
        );
        """
    )
    connection.commit()
    connection.close()
    repository = SqlMemoryRepository(f"sqlite:///{path.as_posix()}", tmp_path)
    repository.close()
    connection = sqlite3.connect(path)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(memory_records)")
        }
        assert {"team_id", "member_id", "acl_visibility"}.issubset(columns)
        acl = connection.execute(
            "SELECT team_id,member_id,acl_visibility FROM memory_records WHERE memory_id='mem_legacy'"
        ).fetchone()
        assert acl == ("personal_team", None, "team")
    finally:
        connection.close()


def test_memory_hard_signals_bypass_rrf_and_soft_prefilter_is_acl_safe(
    tmp_path,
) -> None:
    path = tmp_path / "memory.sqlite3"
    database_url = f"sqlite:///{path.as_posix()}"
    service = _memory_service(database_url, tmp_path)
    _proposal, hard_id = _commit(
        service, content="不穿高跟鞋", memory_type="constraint", namespace="shared"
    )
    _commit(service, content="偏爱简洁风格")
    terms, trace = service.retrieve_soft("u01", "明天通勤想简洁一些")
    assert "style:simple" in terms
    assert hard_id not in json.dumps(trace)
    assert trace["hard_memory_in_rrf"] is False
    assert trace["k"] == 60
    assert trace["candidate_limit"] == 20
    assert trace["weights"] == {
        "bm25": 1.0,
        "dense": 1.0,
        "recency": 0.75,
        "importance": 1.25,
    }
    assert trace["top_k"] == 5
    assert {item.applied_signal for item in service.active_signals("u01")} == {
        "no_high_heels"
    }
    assert service.retrieve_soft("u02", "简洁风格")[0] == ()
    assert service.retrieve_soft("u01", "简洁风格", namespaces=("shared",))[0] == ()
    service.close()


def test_memory_rrf_branches_rank_all_prefiltered_rows_before_top20_cutoff() -> None:
    now = utc_now()
    rows: list[tuple[dict, None]] = []
    for index in range(24):
        record = MemoryRecord(
            memory_id=f"mem_{index:03d}",
            user_id="u01",
            namespace="stylist",
            type="preference",
            content="偏爱低调配色",
            ttl_days=None,
            source_proposal_id=f"mprop_{index:03d}",
            created_at=now - timedelta(days=30),
            expires_at=None,
        )
        rows.append((record.model_dump(mode="json"), None))
    relevant = MemoryRecord(
        memory_id="mem_999",
        user_id="u01",
        namespace="stylist",
        type="preference",
        content="偏爱直筒裤",
        ttl_days=None,
        source_proposal_id="mprop_999",
        created_at=now,
        expires_at=None,
    )
    rows.append((relevant.model_dump(mode="json"), None))

    class PrefilteredRepository:
        def load_state(self):
            return [], [], {}, {}

        def active_records(self, user_id, namespaces, _now):
            assert user_id == "u01" and namespaces == ("shared", "stylist")
            return rows

        def expire_ids(self, _ids, _expired_at):
            return None

        def close(self):
            return None

    service = MemoryService(TraceStore(), repository=PrefilteredRepository())
    terms, trace = service.retrieve_soft("u01", "想穿直筒裤去通勤")
    assert trace["eligible_count"] == 25
    assert trace["fused_candidate_count"] >= 20
    assert trace["dense_backend"] == "deterministic_hashed_surrogate_v1"
    assert "mem_999" in trace["branches"]["bm25"]
    assert "mem_999" in trace["selected_memory_ids"]
    assert "fit:straight" in terms
    service.close()


def test_memory_superseded_and_ttl_state_persist_without_sensitive_trace(
    tmp_path,
) -> None:
    path = tmp_path / "memory.sqlite3"
    database_url = f"sqlite:///{path.as_posix()}"
    service = _memory_service(database_url, tmp_path)
    _commit(service, content="偏爱低调配色")
    _proposal, current_id = _commit(service, content="偏爱低调配色")
    current = service._records[current_id]
    service._records[current_id] = current.model_copy(
        update={"expires_at": utc_now() - timedelta(seconds=1)}
    )
    assert service.list("u01").records == []
    blocked_text = "我的电话是13800138000"
    blocked = service.propose(
        MemoryProposeInput(
            user_id="u01",
            namespace="stylist",
            type="preference",
            content=blocked_text,
        )
    )
    assert blocked.proposal.content is None and blocked.proposal.commit_blocked
    service.close()

    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT superseded_at,deleted_at,payload_json FROM memory_records ORDER BY rowid"
        ).fetchall()
        stored = json.dumps(rows, ensure_ascii=False)
        assert rows[0][0] is not None
        assert rows[1][1] is not None
        assert blocked_text not in stored
    finally:
        connection.close()
