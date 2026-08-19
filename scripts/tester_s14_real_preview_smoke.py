from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from profagent.app import create_app
from profagent.config import Settings


REPORT_JSON = ROOT / "reports" / "demo" / "s14_real_recommendation_previews.json"
REPORT_MD = ROOT / "reports" / "demo" / "s14_real_recommendation_previews.md"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _safe_reason(value: object) -> str:
    text = str(value or "UNSPECIFIED_FAILURE")
    return text if re.fullmatch(r"[A-Z0-9_]{1,96}", text) else "CONTROLLED_PROVIDER_FAILURE"


def main() -> int:
    base = Settings.from_env()
    if not (base.cpa_image_enabled and base.cpa_api_key and base.cpa_base_url):
        result = {
            "schema_version": 1,
            "report_id": "s14_real_recommendation_previews",
            "evidence_kind": "real_cpa_image_provider",
            "status": "NOT_RUN",
            "reason_code": "CPA_IMAGE_CONFIGURATION_UNAVAILABLE",
            "port_8000_touched": False,
            "text_cpa_calls": 0,
            "image_cpa_attempts": 0,
        }
        _atomic_write(REPORT_JSON, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(REPORT_MD, "# S14 真实 CPA 推荐两图\n\n结论：NOT_RUN（图片 CPA 配置不可用）。\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    with tempfile.TemporaryDirectory(prefix="profagent-s14-real-") as temporary:
        settings = replace(
            base,
            cpa_text_enabled=False,
            database_url=f"sqlite:///{(Path(temporary) / 'memory.sqlite3').as_posix()}",
            port=0,
        )
        app = create_app(settings)
        provider = app.state.services.image_provider
        original = provider.generate_static_2d
        measurements: list[dict[str, Any]] = []
        attempt_number = 0

        async def measured(prompt: str):
            nonlocal attempt_number
            attempt_number += 1
            current = attempt_number
            started = time.perf_counter()
            try:
                body, media_type, metadata = await original(prompt)
            except Exception as exc:
                measurements.append(
                    {
                        "attempt": current,
                        "wall_clock_ms": round((time.perf_counter() - started) * 1000, 1),
                        "status": "failed",
                        "reason_code": _safe_reason(getattr(exc, "reason_code", None)),
                    }
                )
                raise
            measurements.append(
                {
                    "attempt": current,
                    "wall_clock_ms": round((time.perf_counter() - started) * 1000, 1),
                    "status": "succeeded",
                    "media_type": media_type,
                    "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "provider": {
                        "request_model_pinned": metadata.get("request_model_pinned") is True,
                        "cpa_trace_verified": metadata.get("cpa_trace_verified") is True,
                        "model_reported": metadata.get("model_reported") is True,
                        "model_verified": metadata.get("model_verified") is True,
                        "resolved_model": metadata.get("resolved_model"),
                        "verification_basis": metadata.get("verification_basis"),
                    },
                }
            )
            return body, media_type, metadata

        provider.generate_static_2d = measured
        started_batch = time.perf_counter()
        with TestClient(app) as client:
            first = client.post(
                "/dialogue/turn",
                json={
                    "user_id": "u01",
                    "message": "今晚要进行一场辩论赛，有点紧张，我应该怎么穿才行",
                    "request_id": "s14-real-dialogue-1",
                },
            ).json()
            second_response = client.post(
                "/dialogue/turn",
                json={
                    "user_id": "u01",
                    "styling_session_id": first["styling_session_id"],
                    "message": "你帮我搭配两套吧",
                    "request_id": "s14-real-dialogue-2",
                },
            )
            assert second_response.status_code == 200
            second = second_response.json()
            recommendation = second["recommendation"]
            assert recommendation["requested_outfit_count"] == 2
            assert len(recommendation["outfits"]) == 2
            payload = {
                "user_id": "u01",
                "styling_session_id": second["styling_session_id"],
                "request_id": second["request_id"],
                "outfit_ids": [item["outfit_id"] for item in recommendation["outfits"]],
                "preview_request_id": "s14-real-preview-batch-1",
            }
            preview_response = client.post("/recommend/previews/static-2d", json=payload)
            batch_wall_clock_ms = round((time.perf_counter() - started_batch) * 1000, 1)
            assert preview_response.status_code == 200
            response = preview_response.json()
            response_items: list[dict[str, Any]] = []
            for index, preview in enumerate(response["previews"], 1):
                entry: dict[str, Any] = {
                    "index": index,
                    "status": preview["status"],
                    "provider": {
                        key: preview["provider"].get(key)
                        for key in (
                            "status",
                            "attempted",
                            "requested_model",
                            "transport_model",
                            "resolved_model",
                            "model_reported",
                            "model_verified",
                            "request_model_pinned",
                            "cpa_trace_verified",
                            "verification_basis",
                            "degraded",
                            "reason_code",
                        )
                    },
                    "owned_garment_count": len(preview["owned_garment_ids"]),
                    "identity_fidelity": preview["fidelity"]["identity"],
                }
                if preview["status"] == "succeeded":
                    image = client.get(preview["image_url"])
                    assert image.status_code == 200
                    entry["image"] = {
                        "media_type": image.headers["content-type"].split(";", 1)[0],
                        "bytes": len(image.content),
                        "sha256": hashlib.sha256(image.content).hexdigest(),
                    }
                else:
                    entry["image"] = None
                response_items.append(entry)

        measurements.sort(key=lambda item: item["attempt"])
        statuses = [item["status"] for item in response_items]
        result = {
            "schema_version": 1,
            "report_id": "s14_real_recommendation_previews",
            "evidence_kind": "real_cpa_image_provider",
            "status": "PASS" if statuses == ["succeeded", "succeeded"] else "DEGRADED",
            "port_8000_touched": False,
            "transport": "in_process_TestClient_no_listening_port",
            "text_cpa_calls": 0,
            "image_cpa_attempts": len(measurements),
            "requested_outfit_count": recommendation["requested_outfit_count"],
            "actual_outfit_count": len(recommendation["outfits"]),
            "batch_wall_clock_ms_including_local_setup": batch_wall_clock_ms,
            "per_provider_attempt": measurements,
            "response_items": response_items,
            "privacy": {
                "prompt_recorded": False,
                "raw_receipt_recorded": False,
                "base64_recorded": False,
                "api_key_recorded": False,
            },
        }
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    assert "prompt\"" not in serialized
    assert "data:image/" not in serialized.lower()
    assert str(ROOT) not in serialized
    _atomic_write(REPORT_JSON, serialized)
    lines = [
        "# S14 真实 CPA 推荐两图",
        "",
        f"结论：{result['status']}。这是项目真实 `grok-imagine-image-quality` Provider 证据，不是 mock；使用进程内 TestClient，不监听或触碰 8000。",
        "",
        f"- 图片 CPA 尝试：{result['image_cpa_attempts']}；响应状态：{statuses}。",
        f"- 批量墙钟（含本地 Scene/Recommendation 设置）：{result['batch_wall_clock_ms_including_local_setup']} ms。",
    ]
    for item in measurements:
        lines.append(
            f"- 尝试 {item['attempt']}：{item['status']}，{item['wall_clock_ms']} ms"
            + (f"，{item.get('media_type')}，{item.get('bytes')} bytes，SHA-256 `{item.get('sha256')}`" if item["status"] == "succeeded" else f"，reason `{item['reason_code']}`")
        )
    lines.extend(
        [
            "",
            "报告不保存 prompt、raw CPA receipt、base64、API key 或图片正文。",
            "",
        ]
    )
    _atomic_write(REPORT_MD, "\n".join(lines))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
