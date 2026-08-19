from __future__ import annotations

import hashlib
import json
from io import BytesIO
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

import httpx
from PIL import Image
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from profagent.app import create_app
from profagent.config import Settings


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _expect(response: httpx.Response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def main() -> int:
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="profagent-s12-http-") as temporary:
        database_path = (Path(temporary) / "memory.sqlite3").resolve()
        app = create_app(
            Settings(
                root_dir=ROOT,
                cpa_text_enabled=False,
                cpa_image_enabled=False,
                database_url=f"sqlite:///{database_path.as_posix()}",
            )
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="error",
                lifespan="on",
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started, "temporary HTTP server did not start"
            with httpx.Client(
                base_url=base_url, timeout=10.0, trust_env=False
            ) as client:
                health = _expect(client.get("/health"))
                assert health["ready"] is True
                assert health["capabilities"] == {
                    **health["capabilities"],
                    "video": False,
                    "three_d": False,
                }
                home = client.get("/")
                assert home.status_code == 200 and "ProfAgent" in home.text
                team = _expect(client.get("/team/home", params={"user_id": "u01"}))
                assert team["team"]["team_id"] == "personal_team"

                owner_counts: dict[str, int] = {}
                image_samples: dict[str, dict[str, object]] = {}
                for user_id in ("u01", "u02", "u03"):
                    wardrobe = _expect(
                        client.get("/wardrobe", params={"user_id": user_id})
                    )
                    assets = _expect(
                        client.get(
                            "/wardrobe/catalog-assets", params={"user_id": user_id}
                        )
                    )
                    assert assets["manifest_version"] == "wardrobe_generated_v1_s12r2"
                    garment_ids = {item["garment_id"] for item in wardrobe["items"]}
                    asset_ids = {item["garment_id"] for item in assets["assets"]}
                    assert garment_ids == asset_ids
                    assert all(item["status"] == "ready" for item in assets["assets"])
                    owner_counts[user_id] = len(asset_ids)

                    ready = assets["assets"][0]
                    assert ready["asset_version"] == "wardrobe_generated_v1_s12r2"
                    assert ready["provider"]["requested_model"] == (
                        "grok-imagine-image-quality"
                    )
                    assert ready["provider"]["request_model_pinned"] is True
                    assert ready["provider"]["model_reported"] is False
                    assert ready["provider"]["model_verified"] is False
                    assert ready["provider"]["resolved_model"] is None
                    assert ready["provider"]["verification_basis"] == (
                        "batch_exact_request_contract"
                    )
                    assert ready["prompt_sha256"] is None
                    assert ready["prompt_hash_status"] == "not_preserved"
                    assert set(ready["image_url"].split("?", 1)[1].split("&")) == {
                        f"user_id={user_id}",
                        "asset_version=wardrobe_generated_v1_s12r2",
                        f"content_sha256={ready['content_sha256']}",
                    }
                    shown = client.get(ready["image_url"])
                    assert shown.status_code == 200
                    assert shown.headers["content-type"].startswith("image/png")
                    assert shown.headers["x-ai-generated"] == "true"
                    assert shown.headers["x-ai-requested-model"] == (
                        "grok-imagine-image-quality"
                    )
                    assert shown.headers["x-ai-model-reported"] == "false"
                    assert "x-ai-model" not in shown.headers
                    assert shown.headers["x-content-type-options"] == "nosniff"
                    assert len(shown.content) > 0
                    assert hashlib.sha256(shown.content).hexdigest() == ready["content_sha256"]
                    with Image.open(BytesIO(shown.content)) as image:
                        image.load()
                        assert image.format == "PNG" and not getattr(
                            image, "is_animated", False
                        )
                    wrong_owner = "u02" if user_id != "u02" else "u01"
                    assert client.get(
                        f"/wardrobe/{ready['garment_id']}/catalog-image",
                        params={
                            "user_id": wrong_owner,
                            "asset_version": ready["asset_version"],
                            "content_sha256": ready["content_sha256"],
                        },
                    ).status_code == 404
                    assert client.get(
                        f"/wardrobe/{ready['garment_id']}/catalog-image",
                        params={
                            "user_id": user_id,
                            "asset_version": "wardrobe_generated_v1",
                            "content_sha256": ready["content_sha256"],
                        },
                    ).status_code == 404
                    assert client.get(
                        f"/wardrobe/{ready['garment_id']}/catalog-image",
                        params={
                            "user_id": user_id,
                            "asset_version": ready["asset_version"],
                        },
                    ).status_code == 422
                    assert client.get(
                        f"/wardrobe/{ready['garment_id']}/catalog-image",
                        params={
                            "user_id": user_id,
                            "asset_version": ready["asset_version"],
                            "content_sha256": "0" * 64,
                        },
                    ).status_code == 404
                    image_samples[user_id] = {
                        "garment_id": ready["garment_id"],
                        "bytes": len(shown.content),
                    }

                scene = _expect(
                    client.post(
                        "/scene/parse",
                        json={
                            "user_id": "u01",
                            "query_text": "今天面试，想显得可靠但别太老气",
                        },
                    )
                )
                assert scene["urgency"] == "high"
                assert scene["shopping_allowed"] is False
                assert scene["ui_capabilities"]["shopping_cta"] is False
                recommendation = _expect(
                    client.post("/recommend", json={"request_id": scene["request_id"]})
                )
                assert recommendation["shopping_suggestions"] == []
                trace = _expect(
                    client.get(
                        f"/trace/{recommendation['trace_id']}",
                        params={"user_id": "u01"},
                    )
                )["trace"]
                assert trace["catalog"]["attempted"] is False
                assert trace["catalog"]["call_count"] == 0

                selected = recommendation["outfits"][0]
                look = _expect(
                    client.post(
                        "/look",
                        json={
                            "user_id": "u01",
                            "styling_session_id": scene["styling_session_id"],
                            "created_from": "selected_outfit",
                            "request_id": scene["request_id"],
                            "outfit_id": selected["outfit_id"],
                            "asset_ids": [],
                        },
                    )
                )
                base_preview = {
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "look_version_id": look["look_version_id"],
                    "consent": True,
                }
                preview = _expect(
                    client.post(
                        "/preview/static-2d",
                        json={
                            **base_preview,
                            "render_mode": "static_2d",
                            "request_id": "s12-temp-http-preview",
                        },
                    )
                )
                assert preview["render_mode"] == "static_2d"
                assert preview["status"] == "degraded"
                assert preview["image_url"] is None
                assert preview["provider"]["status"] == "fallback"
                assert preview["provider"]["degraded"] is True
                assert preview["owned_garment_ids"] == look["item_ids"]
                for mode in ("3d", "360", "video", "generated_video"):
                    assert client.post(
                        "/preview/static-2d",
                        json={**base_preview, "render_mode": mode},
                    ).status_code == 422
                assert client.get("/preview/3d").status_code == 404
                assert client.get("/preview/video").status_code == 404

                browser = subprocess.run(
                    ["node", "web/catalog_image_browser_smoke.cjs"],
                    cwd=ROOT,
                    env={**os.environ, "PROFAGENT_SMOKE_URL": base_url},
                    text=True,
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=40,
                )
                assert browser.returncode == 0, (
                    "catalog image browser DOM smoke failed: "
                    + (browser.stdout + browser.stderr)[-2000:]
                )
                browser_lines = [
                    line.strip() for line in browser.stdout.splitlines() if line.strip()
                ]
                assert browser_lines and browser_lines[-1].startswith(
                    "web catalog image real-browser smoke: PASS naturalWidth="
                )

                summary = {
                    "port_is_not_8000": port != 8000,
                    "health_ready": health["ready"],
                    "home_status": home.status_code,
                    "owner_asset_counts": owner_counts,
                    "served_png_samples": image_samples,
                    "asset_version": "wardrobe_generated_v1_s12r2",
                    "catalog_provenance": {
                        "requested_model": "grok-imagine-image-quality",
                        "request_model_pinned": True,
                        "model_reported": False,
                        "model_verified": False,
                        "resolved_model": None,
                        "verification_basis": "batch_exact_request_contract",
                    },
                    "high_urgency": scene["urgency"],
                    "shopping_allowed": scene["shopping_allowed"],
                    "catalog_call_count": trace["catalog"]["call_count"],
                    "static_2d_status": preview["status"],
                    "catalog_browser_dom": {
                        "status": "pass",
                        "uses_same_non_8000_server": True,
                        "summary": browser_lines[-1],
                    },
                    "three_d_video_routes": "absent_or_rejected",
                }
                print(json.dumps(summary, ensure_ascii=False, indent=2))
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "temporary HTTP server did not stop"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
