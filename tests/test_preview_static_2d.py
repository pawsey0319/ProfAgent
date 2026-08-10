from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from profagent.app import create_app
from profagent.providers import GrokLLMProvider, ProviderUnavailable


def _png() -> bytes:
    image = Image.new("RGB", (256, 256))
    pixels = image.load()
    for y in range(256):
        for x in range(256):
            pixels[x, y] = (
                (x * 5 + y * 3) % 256,
                (x * 2 + y * 7) % 256,
                (x * 11 + y) % 256,
            )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _look(client: TestClient) -> tuple[dict, dict]:
    scene_response = client.post(
        "/scene/parse",
        json={
            "user_id": "u01",
            "query_text": "今天面试，想显得可靠但别太老气",
        },
    )
    assert scene_response.status_code == 200, scene_response.text
    scene = scene_response.json()
    recommendation_response = client.post(
        "/recommend", json={"request_id": scene["request_id"]}
    )
    assert recommendation_response.status_code == 200, recommendation_response.text
    recommendation = recommendation_response.json()
    selected = recommendation["outfits"][0]
    look_response = client.post(
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
    assert look_response.status_code == 200, look_response.text
    return scene, look_response.json()


def test_static_2d_uses_server_look_ids_and_owner_bound_image_lifecycle(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    calls = 0
    image = _png()

    async def generated(prompt: str):
        nonlocal calls
        calls += 1
        assert "禁止3D模型、360度展示、动画、视频" in prompt
        assert "场合=interview" in prompt
        return image, "image/png", {
            "attempted": True,
            "status": "ok",
            "requested_model": "grok4.5",
            "transport_model": "grok-4.5-high",
            "resolved_model": "grok-4.5-build",
            "model_verified": True,
        }

    services.llm.generate_static_2d = generated
    with TestClient(app) as client:
        scene, look = _look(client)
        payload = {
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": look["look_version_id"],
            "render_mode": "static_2d",
            "consent": True,
            "request_id": "preview-idempotent-1",
        }
        first = client.post("/preview/static-2d", json=payload)
        assert first.status_code == 200, first.text
        preview = first.json()
        assert calls == 1
        assert preview["status"] == "succeeded"
        assert preview["owned_garment_ids"] == look["item_ids"]
        assert preview["external_item_ids"] == []
        assert preview["identity_asset_id"] is None
        assert preview["fidelity"]["identity"] == "not_assessed"
        assert preview["provider"] == {
            "status": "ok",
            "attempted": True,
            "requested_model": "grok4.5",
            "transport_model": "grok-4.5-high",
            "resolved_model": "grok-4.5-build",
            "model_verified": True,
            "degraded": False,
            "reason_code": None,
        }
        assert "base64" not in json.dumps(preview).lower()

        retry = client.post("/preview/static-2d", json=payload)
        assert retry.status_code == 200
        assert retry.json() == preview
        assert calls == 1

        wrong_owner = client.get(
            f"/preview/static-2d/{preview['preview_id']}/image",
            params={
                "user_id": "u02",
                "styling_session_id": scene["styling_session_id"],
            },
        )
        assert wrong_owner.status_code == 404
        shown = client.get(preview["image_url"])
        assert shown.status_code == 200
        assert shown.content == image
        assert shown.headers["content-type"].startswith("image/png")
        assert shown.headers["x-ai-generated"] == "true"
        assert shown.headers["cache-control"] == "no-store"

        trace = client.get(
            f"/trace/{preview['trace_id']}", params={"user_id": "u01"}
        ).json()["trace"]
        trace_text = json.dumps(trace, ensure_ascii=False)
        assert "data:image" not in trace_text.lower()
        assert base64.b64encode(image).decode("ascii")[:80] not in trace_text
        assert trace["provider"]["base64_logged"] is False
        assert trace["validator"]["all_ids_grounded"] is True
        assert trace["validator"]["video_provider_calls"] == 0
        assert trace["validator"]["three_d_provider_calls"] == 0

        deleted = client.delete(
            f"/preview/static-2d/{preview['preview_id']}",
            params={
                "user_id": "u01",
                "styling_session_id": scene["styling_session_id"],
            },
        )
        assert deleted.status_code == 200
        assert deleted.json()["image_deleted"] is True
        assert client.get(preview["image_url"]).status_code == 404
        assert client.post("/preview/static-2d", json=payload).status_code == 409


def test_static_2d_rejects_prohibited_modes_and_client_item_injection_before_provider(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    calls = 0

    async def forbidden(_prompt: str):
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not be called")

    services.llm.generate_static_2d = forbidden
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["capabilities"]["static_2d"] is False
        assert health["capabilities"]["video"] is False
        assert health["capabilities"]["three_d"] is False
        assert health["providers"]["static_2d"]["transport_model"] == "grok-4.5-high"
        scene, look = _look(client)
        base = {
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": look["look_version_id"],
            "consent": True,
        }
        for mode in ("3d", "360", "video", "generated_video"):
            response = client.post(
                "/preview/static-2d", json={**base, "render_mode": mode}
            )
            assert response.status_code == 422
        injected = client.post(
            "/preview/static-2d",
            json={
                **base,
                "render_mode": "static_2d",
                "owned_garment_ids": ["g999"],
            },
        )
        assert injected.status_code == 422
        assert calls == 0


def test_static_2d_provider_failure_is_explicit_degradation(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    services = app.state.services

    async def unavailable(_prompt: str):
        raise ProviderUnavailable(
            "offline", reason_code="CPA_PROVIDER_UNAVAILABLE"
        )

    services.llm.generate_static_2d = unavailable
    with TestClient(app) as client:
        scene, look = _look(client)
        response = client.post(
            "/preview/static-2d",
            json={
                "user_id": "u01",
                "styling_session_id": scene["styling_session_id"],
                "look_version_id": look["look_version_id"],
                "render_mode": "static_2d",
                "consent": True,
            },
        )
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["status"] == "degraded"
        assert preview["asset_id"] is None
        assert preview["image_url"] is None
        assert preview["provider"]["status"] == "fallback"
        assert preview["provider"]["degraded"] is True
        assert preview["fallback"]["type"] == "flat_lay_and_text"


def test_cpa_static_2d_adapter_requests_frozen_model_and_accepts_allowlisted_build(
    monkeypatch, offline_settings
) -> None:
    settings = replace(offline_settings, cpa_text_enabled=True)
    provider = GrokLLMProvider(settings)
    image = _png()
    captured: dict = {}

    async def image_post(_self, url, **kwargs):
        captured["url"] = url
        captured["body"] = kwargs["json"]
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "grok-4.5-build",
                "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", image_post)
    body, media_type, metadata = asyncio.run(
        provider.generate_static_2d("single static 2D outfit")
    )
    assert captured["url"].endswith("/images/generations")
    assert captured["body"] == {
        "model": "grok-4.5-high",
        "prompt": "single static 2D outfit",
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
    }
    assert body == image
    assert media_type == "image/png"
    assert metadata["resolved_model"] == "grok-4.5-build"
    assert metadata["model_verified"] is True
    assert provider.resolved_model is None
    image_health = provider.image_health()
    assert image_health["available"] is True
    assert image_health["resolved_model"] == "grok-4.5-build"
    assert image_health["model_verified"] is True


def test_static_2d_success_does_not_authenticate_text_health(
    monkeypatch, offline_settings
) -> None:
    settings = replace(offline_settings, cpa_text_enabled=True)
    provider = GrokLLMProvider(settings)
    image = _png()

    async def image_post(_self, url, **_kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "grok-4.5-build",
                "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
            },
        )

    async def models_get(_self, url, **_kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={"data": [{"id": "grok-4.5-high"}]},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", image_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", models_get)
    asyncio.run(provider.generate_static_2d("single static 2D outfit"))
    text_health = asyncio.run(provider.health())
    assert text_health["chat_model_verified"] is False
    assert text_health["resolved_model"] is None
    assert text_health["available"] is False
    assert provider.image_health()["resolved_model"] == "grok-4.5-build"


def test_cpa_static_2d_adapter_rejects_response_envelope_over_7_5mib(
    monkeypatch, offline_settings
) -> None:
    settings = replace(offline_settings, cpa_text_enabled=True)
    provider = GrokLLMProvider(settings)

    async def oversized_post(_self, url, **_kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            content=b'{' + b'"padding":"' + (b"x" * (15 * 1024 * 1024 // 2)) + b'"}',
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", oversized_post)
    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(provider.generate_static_2d("single static 2D outfit"))
    assert captured.value.reason_code == "CPA_PREVIEW_OUTPUT_REJECTED"


def test_static_2d_endpoint_calls_real_cpa_transport_once_without_identity_bytes(
    monkeypatch, offline_settings
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    generated_image = _png()
    identity_image = _png()
    captured: dict = {}
    calls = 0

    async def image_post(_self, url, **kwargs):
        nonlocal calls
        calls += 1
        captured["url"] = url
        captured["body"] = kwargs["json"]
        captured["headers"] = kwargs["headers"]
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": "grok-4.5-build",
                "data": [
                    {"b64_json": base64.b64encode(generated_image).decode("ascii")}
                ],
            },
        )

    with TestClient(app) as client:
        scene, look = _look(client)
        identity = client.post(
            "/assets",
            data={
                "user_id": "u01",
                "styling_session_id": scene["styling_session_id"],
                "angle": "front",
                "consent": "true",
                "purpose": "styling_assessment",
            },
            files={"file": ("identity.png", identity_image, "image/png")},
        )
        assert identity.status_code == 200, identity.text
        identity_asset_id = identity.json()["asset_id"]

        services.llm.settings = replace(
            offline_settings,
            cpa_text_enabled=True,
            cpa_api_key="s6-secret-do-not-log",
        )
        monkeypatch.setattr(httpx.AsyncClient, "post", image_post)
        response = client.post(
            "/preview/static-2d",
            json={
                "user_id": "u01",
                "styling_session_id": scene["styling_session_id"],
                "look_version_id": look["look_version_id"],
                "render_mode": "static_2d",
                "identity_asset_id": identity_asset_id,
                "consent": True,
                "request_id": "s6-real-transport-preview",
            },
        )
        assert response.status_code == 200, response.text
        preview = response.json()
        assert calls == 1
        assert captured["url"].endswith("/images/generations")
        assert captured["body"]["model"] == "grok-4.5-high"
        assert captured["body"]["n"] == 1
        assert captured["body"]["response_format"] == "b64_json"
        serialized_request = json.dumps(captured["body"], ensure_ascii=False)
        assert identity_asset_id not in serialized_request
        assert base64.b64encode(identity_image).decode("ascii")[:80] not in serialized_request
        assert preview["status"] == "succeeded"
        assert preview["owned_garment_ids"] == look["item_ids"]
        assert preview["external_item_ids"] == []
        assert preview["identity_asset_id"] == identity_asset_id
        assert preview["fidelity"]["identity"] == "not_assessed"
        assert preview["provider"]["status"] == "ok"
        assert preview["provider"]["attempted"] is True
        assert preview["provider"]["requested_model"] == "grok4.5"
        assert preview["provider"]["transport_model"] == "grok-4.5-high"
        assert preview["provider"]["resolved_model"] == "grok-4.5-build"
        assert preview["provider"]["model_verified"] is True

        shown = client.get(preview["image_url"])
        assert shown.status_code == 200
        assert shown.content == generated_image
        assert shown.headers["x-ai-generated"] == "true"

        trace_response = client.get(
            f"/trace/{preview['trace_id']}", params={"user_id": "u01"}
        )
        assert trace_response.status_code == 200
        trace = trace_response.json()["trace"]
        assert trace["provider"]["status"] == "ok"
        assert trace["provider"]["requested_model"] == "grok4.5"
        assert trace["provider"]["transport_model"] == "grok-4.5-high"
        assert trace["provider"]["resolved_model"] == "grok-4.5-build"
        assert trace["provider"]["model_verified"] is True
        assert trace["provider"]["prompt_logged"] is False
        assert trace["provider"]["base64_logged"] is False
        assert trace["validator"]["identity_asset_sent_to_provider"] is False
        trace_text = json.dumps(trace, ensure_ascii=False)
        forbidden = [
            "s6-secret-do-not-log",
            base64.b64encode(identity_image).decode("ascii")[:80],
            base64.b64encode(generated_image).decode("ascii")[:80],
            captured["body"]["prompt"],
            "林然",
            "professional_fashion_stylist",
        ]
        assert all(value not in trace_text for value in forbidden)

        deleted = client.delete(
            f"/preview/static-2d/{preview['preview_id']}",
            params={
                "user_id": "u01",
                "styling_session_id": scene["styling_session_id"],
            },
        )
        assert deleted.status_code == 200
        assert client.get(preview["image_url"]).status_code == 404


def test_static_2d_unknown_cross_owner_stale_and_tampered_requests_are_preprovider(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    calls = 0

    async def forbidden(_prompt: str):
        nonlocal calls
        calls += 1
        raise AssertionError("invalid preview input must not call provider")

    services.llm.generate_static_2d = forbidden
    with TestClient(app) as client:
        scene, look = _look(client)
        base = {
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": look["look_version_id"],
            "render_mode": "static_2d",
            "consent": True,
        }
        unknown = client.post(
            "/preview/static-2d",
            json={**base, "look_version_id": "look_unknown"},
        )
        assert unknown.status_code == 404
        cross_owner = client.post(
            "/preview/static-2d",
            json={**base, "user_id": "u02"},
        )
        assert cross_owner.status_code == 404
        wrong_session = client.post(
            "/preview/static-2d",
            json={**base, "styling_session_id": "session_unknown"},
        )
        assert wrong_session.status_code == 404
        tampered = client.post(
            "/preview/static-2d",
            json={**base, "owned_garment_ids": ["g999"]},
        )
        assert tampered.status_code == 422
        assert calls == 0

        stale_item_id = look["item_ids"][0]
        stale_patch = client.patch(
            f"/wardrobe/{stale_item_id}",
            params={"user_id": "u01"},
            json={"status": "unavailable"},
        )
        assert stale_patch.status_code == 200
        stale = client.post("/preview/static-2d", json=base)
        assert stale.status_code in {404, 409, 422}
        assert calls == 0


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        ("wrong_model_old_alias", "CPA_MODEL_VERIFICATION_FAILED"),
        ("wrong_model_prefix", "CPA_MODEL_VERIFICATION_FAILED"),
        ("bad_mime", "CPA_PREVIEW_OUTPUT_REJECTED"),
        ("oversize", "CPA_PREVIEW_OUTPUT_REJECTED"),
    ],
)
def test_static_2d_wrong_model_bad_mime_and_oversize_degrade_explicitly(
    monkeypatch, offline_settings, failure: str, expected_reason: str
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    calls = 0
    valid = _png()

    async def image_post(_self, url, **kwargs):
        nonlocal calls
        calls += 1
        if failure in {"wrong_model_old_alias", "wrong_model_prefix"}:
            model = (
                "grok-4.5"
                if failure == "wrong_model_old_alias"
                else "grok-4.5-high-preview"
            )
            body = valid
        elif failure == "bad_mime":
            model = "grok-4.5-high"
            body = b"not-a-supported-image"
        else:
            model = "grok-4.5-high"
            body = b"x" * (5 * 1024 * 1024 + 1)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "model": model,
                "data": [{"b64_json": base64.b64encode(body).decode("ascii")}],
            },
        )

    with TestClient(app) as client:
        scene, look = _look(client)
        services.llm.settings = replace(
            offline_settings,
            cpa_text_enabled=True,
            cpa_api_key="s6-preview-failure-secret",
        )
        monkeypatch.setattr(httpx.AsyncClient, "post", image_post)
        response = client.post(
            "/preview/static-2d",
            json={
                "user_id": "u01",
                "styling_session_id": scene["styling_session_id"],
                "look_version_id": look["look_version_id"],
                "render_mode": "static_2d",
                "consent": True,
            },
        )
        assert response.status_code == 200, response.text
        preview = response.json()
        assert calls == 1
        assert preview["status"] == "degraded"
        assert preview["asset_id"] is None
        assert preview["image_url"] is None
        assert preview["provider"]["status"] == "fallback"
        assert preview["provider"]["attempted"] is True
        assert preview["provider"]["degraded"] is True
        assert preview["provider"]["reason_code"] == expected_reason
        assert preview["provider"]["resolved_model"] is None
        assert preview["fallback"]["type"] == "flat_lay_and_text"
        trace = client.get(
            f"/trace/{preview['trace_id']}", params={"user_id": "u01"}
        ).json()["trace"]
        trace_text = json.dumps(trace, ensure_ascii=False)
        assert "s6-preview-failure-secret" not in trace_text
        assert "b64_json" not in trace_text
        assert "data:image" not in trace_text
