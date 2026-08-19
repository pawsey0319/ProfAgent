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
from profagent.image_provider import GrokImageProvider
from profagent.providers import ProviderUnavailable


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
            "requested_model": "grok-imagine-image-quality",
            "transport_model": "grok-imagine-image-quality",
            "request_model_pinned": True,
            "cpa_trace_verified": True,
            "model_reported": True,
            "resolved_model": "grok-imagine-image-quality",
            "model_verified": True,
            "verification_basis": "reported_model_exact",
        }

    services.image_provider.generate_static_2d = generated
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
            "requested_model": "grok-imagine-image-quality",
            "transport_model": "grok-imagine-image-quality",
            "request_model_pinned": True,
            "cpa_trace_verified": True,
            "model_reported": True,
            "resolved_model": "grok-imagine-image-quality",
            "model_verified": True,
            "verification_basis": "reported_model_exact",
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

    services.image_provider.generate_static_2d = forbidden
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["capabilities"]["static_2d"] is False
        assert health["capabilities"]["video"] is False
        assert health["capabilities"]["three_d"] is False
        assert health["providers"]["static_2d"]["transport_model"] == "grok-imagine-image-quality"
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
            "offline", reason_code="CPA_IMAGE_PROVIDER_UNAVAILABLE"
        )

    services.image_provider.generate_static_2d = unavailable
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


def test_cpa_static_2d_adapter_requests_frozen_model_and_accepts_exact_image_model(
    monkeypatch, offline_settings
) -> None:
    settings = replace(offline_settings, cpa_image_enabled=True)
    provider = GrokImageProvider(settings)
    image = _png()
    captured: dict = {}
    raw_receipt = "cpa-image-reported-receipt-secret-1"

    def image_post(request: httpx.Request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"x-cpa-trace-id": raw_receipt},
            json={
                "model": "grok-imagine-image-quality",
                "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
            },
        )

    provider.set_transport(httpx.MockTransport(image_post))
    body, media_type, metadata = asyncio.run(
        provider.generate_static_2d("single static 2D outfit")
    )
    assert captured["url"].endswith("/images/generations")
    assert captured["body"] == {
        "model": "grok-imagine-image-quality",
        "prompt": "single static 2D outfit",
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
    }
    serialized_request = json.dumps(captured["body"], sort_keys=True)
    assert "grok4.6" not in serialized_request
    assert "grok-4.6-high" not in serialized_request
    assert body == image
    assert media_type == "image/png"
    assert metadata["resolved_model"] == "grok-imagine-image-quality"
    assert metadata["model_verified"] is True
    assert metadata["model_reported"] is True
    assert metadata["request_model_pinned"] is True
    assert metadata["cpa_trace_verified"] is True
    assert metadata["verification_basis"] == "reported_model_exact"
    assert provider.resolved_model == "grok-imagine-image-quality"
    image_health = provider.health()
    assert image_health["available"] is True
    assert image_health["resolved_model"] == "grok-imagine-image-quality"
    assert image_health["model_verified"] is True
    assert raw_receipt not in json.dumps(metadata)
    assert raw_receipt not in json.dumps(image_health)


def test_cpa_static_2d_accepts_unreported_model_only_with_verified_receipt(
    offline_settings,
) -> None:
    settings = replace(offline_settings, cpa_image_enabled=True)
    provider = GrokImageProvider(settings)
    image = _png()
    raw_receipt = "cpa-image-receipt-secret-7"

    def image_post(_request: httpx.Request):
        return httpx.Response(
            200,
            headers={"x-cpa-trace-id": raw_receipt},
            json={"data": [{"b64_json": base64.b64encode(image).decode("ascii")}]},
        )

    provider.set_transport(httpx.MockTransport(image_post))
    body, media_type, metadata = asyncio.run(
        provider.generate_static_2d("single static 2D outfit")
    )
    assert body == image
    assert media_type == "image/png"
    assert metadata["request_model_pinned"] is True
    assert metadata["cpa_trace_verified"] is True
    assert metadata["model_reported"] is False
    assert metadata["model_verified"] is False
    assert metadata["resolved_model"] is None
    assert metadata["verification_basis"] == "exact_request_with_cpa_trace"
    health = provider.health()
    assert health["available"] is True
    assert health["status"] == "ok"
    assert health["model_reported"] is False
    assert health["model_verified"] is False
    assert health["resolved_model"] is None
    assert health["verification_basis"] == "exact_request_with_cpa_trace"
    assert raw_receipt not in json.dumps(metadata)
    assert raw_receipt not in json.dumps(health)


@pytest.mark.parametrize(
    "reported_model",
    [None, "", 123, "grok-4.5-high", "grok-imagine-image-quality-preview"],
)
def test_cpa_static_2d_rejects_present_but_nonexact_model(
    offline_settings, reported_model
) -> None:
    provider = GrokImageProvider(replace(offline_settings, cpa_image_enabled=True))
    image = _png()

    def image_post(_request: httpx.Request):
        return httpx.Response(
            200,
            headers={"x-cpa-trace-id": "cpa-image-wrong-model-1"},
            json={
                "model": reported_model,
                "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
            },
        )

    provider.set_transport(httpx.MockTransport(image_post))
    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(provider.generate_static_2d("single static 2D outfit"))
    assert captured.value.reason_code == "CPA_IMAGE_MODEL_VERIFICATION_FAILED"
    health = provider.health()
    assert health["model_reported"] is True
    assert health["cpa_trace_verified"] is True
    assert health["model_verified"] is False
    assert health["resolved_model"] is None
    assert health["verification_basis"] is None


@pytest.mark.parametrize(
    "receipt_headers",
    [
        {},
        {"x-cpa-trace-id": ""},
        {"x-cpa-trace-id": "contains space"},
        {"x-cpa-trace-id": "x" * 129},
        [
            ("x-cpa-trace-id", "cpa-image-duplicate-a"),
            ("x-cpa-trace-id", "cpa-image-duplicate-b"),
        ],
    ],
)
def test_cpa_static_2d_rejects_missing_unsafe_or_duplicate_receipt_even_if_model_exact(
    offline_settings, receipt_headers
) -> None:
    provider = GrokImageProvider(replace(offline_settings, cpa_image_enabled=True))
    image = _png()

    def image_post(_request: httpx.Request):
        return httpx.Response(
            200,
            headers=receipt_headers,
            json={
                "model": "grok-imagine-image-quality",
                "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
            },
        )

    provider.set_transport(httpx.MockTransport(image_post))
    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(provider.generate_static_2d("single static 2D outfit"))
    assert captured.value.reason_code == "CPA_IMAGE_TRACE_VERIFICATION_FAILED"
    health = provider.health()
    assert health["model_reported"] is True
    assert health["cpa_trace_verified"] is False
    assert health["model_verified"] is False
    assert health["resolved_model"] is None


def test_cpa_static_2d_rejects_unreported_model_without_receipt(
    offline_settings,
) -> None:
    provider = GrokImageProvider(replace(offline_settings, cpa_image_enabled=True))
    image = _png()
    provider.set_transport(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "data": [{"b64_json": base64.b64encode(image).decode("ascii")}]
                },
            )
        )
    )
    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(provider.generate_static_2d("single static 2D outfit"))
    assert captured.value.reason_code == "CPA_IMAGE_TRACE_VERIFICATION_FAILED"
    health = provider.health()
    assert health["model_reported"] is False
    assert health["model_verified"] is False
    assert health["resolved_model"] is None


@pytest.mark.parametrize(
    "envelope_template",
    [
        '{{"model":"grok-imagine-image-quality","model":"grok-imagine-image-quality","data":[{{"b64_json":"{payload}"}}]}}',
        '{{"model":"grok-imagine-image-quality","data":[{{"b64_json":"{payload}"}}],"data":[{{"b64_json":"{payload}"}}]}}',
        '{{"model":"grok-imagine-image-quality","data":[{{"b64_json":"{payload}","b64_json":"{payload}"}}]}}',
        '{{"model":"grok-imagine-image-quality","data":[{{"url":"https://cdn.example/one","url":"https://cdn.example/two"}}]}}',
    ],
    ids=["model", "data", "b64_json", "url"],
)
def test_cpa_static_2d_rejects_duplicate_security_relevant_json_keys_without_leak(
    offline_settings, envelope_template: str
) -> None:
    provider = GrokImageProvider(replace(offline_settings, cpa_image_enabled=True))
    payload = base64.b64encode(_png()).decode("ascii")
    raw_receipt = "cpa-image-duplicate-json-secret-1"
    raw_envelope = envelope_template.format(payload=payload).encode("utf-8")

    provider.set_transport(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "x-cpa-trace-id": raw_receipt,
                },
                content=raw_envelope,
            )
        )
    )
    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(provider.generate_static_2d("single static 2D outfit"))
    assert captured.value.reason_code == "CPA_IMAGE_OUTPUT_REJECTED"
    health_text = json.dumps(provider.health(), ensure_ascii=False)
    assert raw_receipt not in health_text
    assert payload[:80] not in health_text


@pytest.mark.parametrize(
    "duplicate_case",
    ["wrong_then_exact", "exact_then_wrong", "data", "b64_body", "url_body"],
)
def test_cpa_static_2d_rejects_duplicate_json_keys_recursively(
    offline_settings, duplicate_case: str
) -> None:
    provider = GrokImageProvider(replace(offline_settings, cpa_image_enabled=True))
    encoded = base64.b64encode(_png()).decode("ascii")
    item = f'{{"b64_json":"{encoded}"}}'
    if duplicate_case == "wrong_then_exact":
        raw = (
            '{"model":"grok-4.5-high",'
            '"model":"grok-imagine-image-quality",'
            f'"data":[{item}]}}'
        )
    elif duplicate_case == "exact_then_wrong":
        raw = (
            '{"model":"grok-imagine-image-quality",'
            '"model":"grok-4.5-high",'
            f'"data":[{item}]}}'
        )
    elif duplicate_case == "data":
        raw = (
            '{"model":"grok-imagine-image-quality",'
            f'"data":[{item}],"data":[{item}]}}'
        )
    elif duplicate_case == "b64_body":
        raw = (
            '{"model":"grok-imagine-image-quality","data":['
            f'{{"b64_json":"{encoded}","b64_json":"{encoded}"}}]}}'
        )
    else:
        raw = (
            '{"model":"grok-imagine-image-quality","data":['
            '{"url":"https://cdn.example/a","url":"https://cdn.example/b"}]}'
        )

    provider.set_transport(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=raw.encode("utf-8"),
                headers={
                    "content-type": "application/json",
                    "x-cpa-trace-id": "cpa-image-duplicate-json-1",
                },
            )
        )
    )
    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(provider.generate_static_2d("single static 2D outfit"))
    assert captured.value.reason_code == "CPA_IMAGE_OUTPUT_REJECTED"
    health_text = json.dumps(provider.health())
    assert "grok-4.5-high" not in health_text
    assert encoded[:80] not in health_text


def test_static_2d_success_does_not_authenticate_text_health(
    monkeypatch, offline_settings
) -> None:
    app = create_app(replace(offline_settings, cpa_image_enabled=True))
    provider = app.state.services.image_provider
    image = _png()

    def image_post(_request: httpx.Request):
        return httpx.Response(
            200,
            headers={"x-cpa-trace-id": "cpa-image-text-isolation-1"},
            json={
                "model": "grok-imagine-image-quality",
                "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
            },
        )

    provider.set_transport(httpx.MockTransport(image_post))
    asyncio.run(provider.generate_static_2d("single static 2D outfit"))
    text_health = asyncio.run(app.state.services.llm.health())
    assert text_health["chat_model_verified"] is False
    assert text_health["resolved_model"] is None
    assert text_health["available"] is False
    assert provider.health()["resolved_model"] == "grok-imagine-image-quality"


def test_static_2d_unreported_model_api_health_and_trace_are_honest_and_redacted(
    offline_settings,
) -> None:
    enabled_settings = replace(offline_settings, cpa_image_enabled=True)
    app = create_app(enabled_settings)
    services = app.state.services
    image = _png()
    raw_receipt = "cpa-image-private-receipt-123"

    def image_post(_request: httpx.Request):
        return httpx.Response(
            200,
            headers={"x-cpa-trace-id": raw_receipt},
            json={"data": [{"b64_json": base64.b64encode(image).decode("ascii")}]},
        )

    services.image_provider.set_transport(httpx.MockTransport(image_post))
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
                "request_id": "s12r-unreported-image-model",
            },
        )
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["status"] == "succeeded"
        assert preview["provider"]["request_model_pinned"] is True
        assert preview["provider"]["cpa_trace_verified"] is True
        assert preview["provider"]["model_reported"] is False
        assert preview["provider"]["model_verified"] is False
        assert preview["provider"]["resolved_model"] is None
        assert preview["provider"]["verification_basis"] == (
            "exact_request_with_cpa_trace"
        )

        health = client.get("/health").json()
        image_health = health["providers"]["static_2d"]
        assert health["capabilities"]["static_2d"] is True
        assert image_health["available"] is True
        assert image_health["request_model_pinned"] is True
        assert image_health["cpa_trace_verified"] is True
        assert image_health["model_reported"] is False
        assert image_health["model_verified"] is False
        assert image_health["resolved_model"] is None
        assert image_health["verification_basis"] == "exact_request_with_cpa_trace"
        assert health["providers"]["llm"]["chat_model_verified"] is False

        trace = client.get(
            f"/trace/{preview['trace_id']}", params={"user_id": "u01"}
        ).json()["trace"]
        assert trace["provider"]["cpa_trace_verified"] is True
        assert trace["provider"]["model_reported"] is False
        assert trace["provider"]["model_verified"] is False
        assert trace["provider"]["resolved_model"] is None
        assert trace["provider"]["verification_basis"] == (
            "exact_request_with_cpa_trace"
        )
        combined = json.dumps(
            {"preview": preview, "health": health, "trace": trace},
            ensure_ascii=False,
        )
        assert raw_receipt not in combined
        assert base64.b64encode(image).decode("ascii")[:80] not in combined


def test_cpa_static_2d_adapter_rejects_response_envelope_over_8mib(
    monkeypatch, offline_settings
) -> None:
    settings = replace(offline_settings, cpa_image_enabled=True)
    provider = GrokImageProvider(settings)

    def oversized_post(_request: httpx.Request):
        return httpx.Response(
            200,
            content=b'{' + b'"padding":"' + (b"x" * (9 * 1024 * 1024)) + b'"}',
            headers={"content-type": "application/json"},
        )

    provider.set_transport(httpx.MockTransport(oversized_post))
    with pytest.raises(ProviderUnavailable) as captured:
        asyncio.run(provider.generate_static_2d("single static 2D outfit"))
    assert captured.value.reason_code == "CPA_IMAGE_OUTPUT_REJECTED"


def test_static_2d_endpoint_calls_real_cpa_transport_once_without_identity_bytes(
    monkeypatch, offline_settings
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    generated_image = _png()
    identity_image = _png()
    captured: dict = {}
    calls = 0
    raw_receipt = "cpa-image-endpoint-receipt-secret-1"

    def image_post(request: httpx.Request):
        nonlocal calls
        calls += 1
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            headers={"x-cpa-trace-id": raw_receipt},
            json={
                "model": "grok-imagine-image-quality",
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

        services.image_provider.settings = replace(
            offline_settings,
            cpa_image_enabled=True,
            cpa_api_key="s6-secret-do-not-log",
        )
        services.image_provider.set_transport(httpx.MockTransport(image_post))
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
        assert captured["body"]["model"] == "grok-imagine-image-quality"
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
        assert preview["provider"]["requested_model"] == "grok-imagine-image-quality"
        assert preview["provider"]["transport_model"] == "grok-imagine-image-quality"
        assert preview["provider"]["resolved_model"] == "grok-imagine-image-quality"
        assert preview["provider"]["model_verified"] is True
        assert preview["provider"]["model_reported"] is True
        assert preview["provider"]["request_model_pinned"] is True
        assert preview["provider"]["cpa_trace_verified"] is True
        assert preview["provider"]["verification_basis"] == "reported_model_exact"

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
        assert trace["provider"]["requested_model"] == "grok-imagine-image-quality"
        assert trace["provider"]["transport_model"] == "grok-imagine-image-quality"
        assert trace["provider"]["resolved_model"] == "grok-imagine-image-quality"
        assert trace["provider"]["model_verified"] is True
        assert trace["provider"]["model_reported"] is True
        assert trace["provider"]["request_model_pinned"] is True
        assert trace["provider"]["cpa_trace_verified"] is True
        assert trace["provider"]["verification_basis"] == "reported_model_exact"
        assert trace["provider"]["prompt_logged"] is False
        assert trace["provider"]["base64_logged"] is False
        assert trace["validator"]["identity_asset_sent_to_provider"] is False
        trace_text = json.dumps(trace, ensure_ascii=False)
        forbidden = [
            "s6-secret-do-not-log",
            raw_receipt,
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

    services.image_provider.generate_static_2d = forbidden
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
        ("wrong_model_old_alias", "CPA_IMAGE_MODEL_VERIFICATION_FAILED"),
        ("wrong_model_prefix", "CPA_IMAGE_MODEL_VERIFICATION_FAILED"),
        ("bad_mime", "CPA_IMAGE_OUTPUT_REJECTED"),
        ("oversize", "CPA_IMAGE_OUTPUT_REJECTED"),
    ],
)
def test_static_2d_wrong_model_bad_mime_and_oversize_degrade_explicitly(
    monkeypatch, offline_settings, failure: str, expected_reason: str
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    calls = 0
    valid = _png()

    def image_post(request: httpx.Request):
        nonlocal calls
        calls += 1
        if failure in {"wrong_model_old_alias", "wrong_model_prefix"}:
            model = "grok-4.5-high" if failure == "wrong_model_old_alias" else "grok-imagine-image-quality-preview"
            body = valid
        elif failure == "bad_mime":
            model = "grok-imagine-image-quality"
            body = b"not-a-supported-image"
        else:
            model = "grok-imagine-image-quality"
            body = b"x" * (5 * 1024 * 1024 + 1)
        return httpx.Response(
            200,
            headers={"x-cpa-trace-id": "cpa-image-negative-1"},
            json={
                "model": model,
                "data": [{"b64_json": base64.b64encode(body).decode("ascii")}],
            },
        )

    with TestClient(app) as client:
        scene, look = _look(client)
        services.image_provider.settings = replace(
            offline_settings,
            cpa_image_enabled=True,
            cpa_api_key="s6-preview-failure-secret",
        )
        services.image_provider.set_transport(httpx.MockTransport(image_post))
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
