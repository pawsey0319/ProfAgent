from __future__ import annotations

from io import BytesIO
import json
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
from profagent.providers import ProviderUnavailable


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _png(color: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (256, 256), color=color).save(output, format="PNG")
    return output.getvalue()


def _image_meta() -> dict[str, object]:
    return {
        "request_model_pinned": True,
        "cpa_trace_verified": True,
        "model_reported": False,
        "model_verified": False,
        "resolved_model": None,
        "verification_basis": "exact_request_with_cpa_trace",
    }


def _expect(response: httpx.Response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def main() -> int:
    port = _free_port()
    assert port != 8000
    with tempfile.TemporaryDirectory(prefix="profagent-s14-http-") as temporary:
        database_path = (Path(temporary) / "memory.sqlite3").resolve()
        app = create_app(
            Settings(
                root_dir=ROOT,
                cpa_text_enabled=False,
                cpa_image_enabled=False,
                database_url=f"sqlite:///{database_path.as_posix()}",
            )
        )
        provider_calls = 0

        async def mock_success(_prompt: str):
            nonlocal provider_calls
            provider_calls += 1
            color = "navy" if provider_calls % 2 else "beige"
            return _png(color), "image/png", _image_meta()

        app.state.services.image_provider.generate_static_2d = mock_success
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
            assert server.started
            with httpx.Client(base_url=base_url, timeout=15, trust_env=False) as client:
                first = _expect(
                    client.post(
                        "/dialogue/turn",
                        json={
                            "user_id": "u01",
                            "message": "今晚要进行一场辩论赛，有点紧张，我应该怎么穿才行",
                            "request_id": "s14-http-dialogue-1",
                        },
                    )
                )
                second = _expect(
                    client.post(
                        "/dialogue/turn",
                        json={
                            "user_id": "u01",
                            "styling_session_id": first["styling_session_id"],
                            "message": "你帮我搭配两套吧",
                            "request_id": "s14-http-dialogue-2",
                        },
                    )
                )
                recommendation = second["recommendation"]
                assert second["turn_index"] == 2 and second["history_version"] == 2
                assert second["scene"]["occasion"] == first["scene"]["occasion"] == "meeting"
                assert second["scene"]["event_horizon"] == "today"
                assert second["scene"]["shopping_allowed"] is False
                assert recommendation["requested_outfit_count"] == 2
                assert len(recommendation["outfits"]) == 2
                assert "手头有哪些" not in second["assistant_message"]
                assert "读完你的衣橱" in second["assistant_message"]
                wardrobe = _expect(client.get("/wardrobe", params={"user_id": "u01"}))
                owned = {item["garment_id"] for item in wardrobe["items"]}
                assert all(
                    set(outfit["items"]).issubset(owned)
                    and outfit["validation"]["all_ids_grounded"] is True
                    and outfit["validation"]["hard_constraints_passed"] is True
                    for outfit in recommendation["outfits"]
                )
                trace = _expect(
                    client.get(f"/trace/{second['trace_id']}", params={"user_id": "u01"})
                )["trace"]
                assert trace["catalog"]["call_count"] == 0

                batch_payload = {
                    "user_id": "u01",
                    "styling_session_id": second["styling_session_id"],
                    "request_id": second["request_id"],
                    "outfit_ids": [item["outfit_id"] for item in recommendation["outfits"]],
                    "preview_request_id": "s14-http-preview-success",
                }
                batch = _expect(client.post("/recommend/previews/static-2d", json=batch_payload))
                assert [item["status"] for item in batch["previews"]] == ["succeeded", "succeeded"]
                assert provider_calls == 2
                assert _expect(client.post("/recommend/previews/static-2d", json=batch_payload)) == batch
                assert provider_calls == 2
                for preview in batch["previews"]:
                    assert preview["owned_garment_ids"] in [
                        outfit["items"] for outfit in recommendation["outfits"]
                    ]
                    image = client.get(preview["image_url"])
                    assert image.status_code == 200
                    assert image.headers["content-type"].startswith("image/png")
                    assert client.get(
                        preview["image_url"].replace("user_id=u01", "user_id=u02")
                    ).status_code == 404

                partial_calls = 0

                async def mock_partial(_prompt: str):
                    nonlocal partial_calls
                    partial_calls += 1
                    if partial_calls == 1:
                        raise ProviderUnavailable(
                            "synthetic tester failure",
                            reason_code="CPA_IMAGE_PROVIDER_UNAVAILABLE",
                        )
                    return _png("green"), "image/png", _image_meta()

                app.state.services.image_provider.generate_static_2d = mock_partial
                partial = _expect(
                    client.post(
                        "/recommend/previews/static-2d",
                        json={**batch_payload, "preview_request_id": "s14-http-preview-partial"},
                    )
                )
                assert [item["status"] for item in partial["previews"]] == [
                    "degraded",
                    "succeeded",
                ]
                assert partial_calls == 2

                invalid_base = {
                    **batch_payload,
                    "outfit_ids": ["unknown-outfit"],
                    "preview_request_id": "s14-http-invalid",
                }
                assert client.post(
                    "/recommend/previews/static-2d", json=invalid_base
                ).status_code == 404
                assert client.post(
                    "/recommend/previews/static-2d",
                    json={**invalid_base, "user_id": "u02"},
                ).status_code == 404
                for field, value in (
                    ("model", "grok-4.6-high"),
                    ("render_mode", "3d"),
                    ("prompt", "video"),
                    ("garment_ids", ["g001"]),
                ):
                    payload = {
                        **batch_payload,
                        "preview_request_id": f"s14-http-forbidden-{field}",
                        field: value,
                    }
                    assert client.post(
                        "/recommend/previews/static-2d", json=payload
                    ).status_code == 422

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
                assert browser.returncode == 0, (browser.stdout + browser.stderr)[-2000:]
                browser_lines = [line.strip() for line in browser.stdout.splitlines() if line.strip()]
                browser_summary = browser_lines[-1]
                assert browser_summary.startswith(
                    "web wardrobe accordion real-browser smoke: PASS collapsedCards=0 naturalWidth="
                )

                print(
                    json.dumps(
                        {
                            "port_is_not_8000": True,
                            "provider_kind": "mock_image_provider_no_external_calls",
                            "dialogue_scene_inherited": True,
                            "requested_outfit_count": recommendation["requested_outfit_count"],
                            "actual_outfit_count": len(recommendation["outfits"]),
                            "all_items_owner_bound": True,
                            "shopping_allowed": False,
                            "catalog_call_count": trace["catalog"]["call_count"],
                            "mock_batch_statuses": [
                                item["status"] for item in batch["previews"]
                            ],
                            "mock_batch_provider_calls": provider_calls,
                            "idempotent_retry_additional_calls": 0,
                            "mock_partial_statuses": [
                                item["status"] for item in partial["previews"]
                            ],
                            "negative_http": {
                                "unknown_outfit": "404",
                                "cross_owner": "404",
                                "client_model_3d_video_ids": "422",
                            },
                            "browser_dom": {
                                "status": "pass",
                                "summary": browser_summary,
                            },
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
