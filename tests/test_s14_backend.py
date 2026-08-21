from __future__ import annotations

import asyncio
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from profagent.app import create_app
from profagent.providers import ProviderUnavailable


def _png(color: str = "white") -> bytes:
    output = BytesIO()
    Image.new("RGB", (512, 512), color=color).save(output, format="PNG")
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


def _dialogue_chain(client: TestClient) -> tuple[dict, dict]:
    first = client.post(
        "/dialogue/turn",
        json={
            "user_id": "u01",
            "message": "今晚要进行一场辩论赛，有点紧张，我应该怎么穿才行",
            "request_id": "s14-dialogue-1",
        },
    )
    assert first.status_code == 200
    first_body = first.json()
    second = client.post(
        "/dialogue/turn",
        json={
            "user_id": "u01",
            "styling_session_id": first_body["styling_session_id"],
            "message": "你帮我搭配两套吧",
            "request_id": "s14-dialogue-2",
        },
    )
    assert second.status_code == 200
    return first_body, second.json()


def test_screenshot_chain_inherits_scene_and_returns_exactly_two_owned_outfits(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    with TestClient(app) as client:
        first, second = _dialogue_chain(client)

    assert first["scene"]["occasion"] == "meeting"
    assert second["scene"]["occasion"] == "meeting"
    assert second["scene"]["event_horizon"] == "today"
    assert second["scene"]["shopping_allowed"] is False
    assert second["recommendation"]["requested_outfit_count"] == 2
    assert len(second["recommendation"]["outfits"]) == 2
    assert second["turn_index"] == 2
    assert second["history_version"] == 2
    if second["preference_clarification"] is not None:
        assert second["action"] == "recommend"
        assert second["recommendation_paused"] is False
        assert second["recommendation"] is not None
    assert len(second["memory_candidates"]) <= 1
    assert "读完你的衣橱" in second["assistant_message"]
    assert "手头有哪些" not in second["assistant_message"]
    owned = app.state.services.repository.garment_ids("u01")
    for outfit in second["recommendation"]["outfits"]:
        assert set(outfit["items"]).issubset(owned)
        assert outfit["validation"]["all_ids_grounded"] is True
        assert outfit["validation"]["hard_constraints_passed"] is True
    trace = app.state.services.traces.get(second["trace_id"])
    assert trace is not None
    assert trace.catalog["call_count"] == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("请给我一套", 1),
        ("帮我搭二套", 2),
        ("我想看两套方案", 2),
        ("给我3个方向", 3),
        ("第二套换上衣", None),
        ("不要三套，给我两套", 2),
        ("三套不要，两套就行", 2),
        ("给我一套或两套", None),
        ("一套还是两套都可以", None),
        ("不要两套", None),
        ("给我三套，改成两套", 2),
        ("先给三套，最后要两套", 2),
        ("给我三套，那就两套", 2),
        ("给我三套，给我两套", None),
    ],
)
def test_current_turn_outfit_count_synonyms(text, expected) -> None:
    from profagent.dialogue import DialogueService

    assert DialogueService._requested_outfit_count(text) == expected


@pytest.mark.parametrize(
    ("unsafe_reply", "diagnostic"),
    [
        ("先告诉我你手头有哪些上衣、裤子和鞋子。", "WARDROBE_REASK"),
        ("好的，我给你整理三套。", "OUTFIT_COUNT_CONFLICT"),
    ],
)
def test_cpa_cannot_reask_wardrobe_or_change_authoritative_count(
    offline_settings, unsafe_reply, diagnostic
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    calls = []

    async def cpa(context):
        calls.append(context)
        summary = context["session"]["recommendation_summary"]
        if len(calls) == 1:
            reply = "今晚是辩论场景，我先给你三个可直接试的衣橱方向。"
        else:
            reply = unsafe_reply
        return (
            {
                "reply": reply,
                "action": context["policy"]["required_action"],
                "control": context["policy"]["required_control"],
                "scene_advisory": None,
                "suggested_replies": [],
            },
            {
                "resolved_model": "grok-4.6-high",
                "model_verified": True,
                "summary_seen": summary is not None,
            },
        )

    services.llm.stylist_dialogue = cpa
    with TestClient(app) as client:
        _first, second = _dialogue_chain(client)

    assert second["provider"]["generation_source"] == "local_fallback"
    assert second["provider"]["reason_code"] == "CPA_DIALOGUE_OUTPUT_REJECTED"
    assert "读完你的衣橱" in second["assistant_message"]
    summary = calls[1]["session"]["recommendation_summary"]
    assert summary["actual_outfit_count"] == 2
    serialized = str(summary)
    assert "g001" not in serialized and "g005" not in serialized
    trace = services.traces.get(second["trace_id"])
    assert diagnostic in trace.provider["diagnostic_reason_code"]


@pytest.mark.parametrize(
    "unsafe_reply",
    [
        "你目前可以拿来搭配的单品分别是什么？",
        "请先把可选单品发我，我再搭。",
        "麻烦先把你现有衣服列出来，然后我再推荐。",
    ],
)
def test_cpa_inventory_request_semantics_are_rejected(
    offline_settings, unsafe_reply
) -> None:
    from profagent.dialogue import DialogueService

    assert DialogueService._recommendation_reply_rejection(
        unsafe_reply, None, 2
    ) == "CPA_DIALOGUE_WARDROBE_REASK_REJECTED"


def test_normal_statement_about_items_is_not_misclassified_as_inventory_reask() -> None:
    from profagent.dialogue import DialogueService

    assert (
        DialogueService._recommendation_reply_rejection(
            "这两套里有哪些单品，我会在下面的方向卡逐项说明。", None, 2
        )
        is None
    )


def test_cpa_high_repetition_is_rejected_for_recommendation(offline_settings) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    repeated = "我会围绕今晚的辩论场景，保持专业、稳妥并照顾活动舒适度。"

    async def cpa(context):
        return (
            {
                "reply": repeated,
                "action": context["policy"]["required_action"],
                "control": context["policy"]["required_control"],
                "scene_advisory": None,
                "suggested_replies": [],
            },
            {"resolved_model": "grok-4.6-high", "model_verified": True},
        )

    services.llm.stylist_dialogue = cpa
    with TestClient(app) as client:
        first = client.post(
            "/dialogue/turn",
            json={
                "user_id": "u01",
                "message": "今晚辩论赛怎么穿",
                "request_id": "repeat-1",
            },
        ).json()
        second = client.post(
            "/dialogue/turn",
            json={
                "user_id": "u01",
                "styling_session_id": first["styling_session_id"],
                "message": "再具体一点",
                "request_id": "repeat-2",
            },
        ).json()
    assert second["provider"]["generation_source"] == "local_fallback"
    trace = services.traces.get(second["trace_id"])
    assert trace.provider["diagnostic_reason_code"] == (
        "CPA_DIALOGUE_HIGH_REPETITION_REJECTED"
    )


def test_batch_recommendation_previews_are_owner_bound_idempotent_and_no_prompt_ids(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    prompts = []

    async def generated(prompt):
        prompts.append(prompt)
        return _png("navy"), "image/png", _image_meta()

    services.image_provider.generate_static_2d = generated
    with TestClient(app) as client:
        _first, second = _dialogue_chain(client)
        outfits = second["recommendation"]["outfits"]
        payload = {
            "user_id": "u01",
            "styling_session_id": second["styling_session_id"],
            "request_id": second["request_id"],
            "outfit_ids": [item["outfit_id"] for item in outfits],
            "preview_request_id": "s14-preview-batch-1",
        }
        response = client.post("/recommend/previews/static-2d", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert len(body["previews"]) == 2
        assert len(prompts) == 2
        assert all(item["status"] == "succeeded" for item in body["previews"])
        assert all(item["fidelity"]["identity"] == "not_assessed" for item in body["previews"])
        assert all("g0" not in prompt for prompt in prompts)
        assert all("辩论赛" not in prompt for prompt in prompts)

        retry = client.post("/recommend/previews/static-2d", json=payload)
        assert retry.status_code == 200
        assert retry.json() == body
        assert len(prompts) == 2

        first_preview = body["previews"][0]
        image = client.get(first_preview["image_url"])
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/png")
        assert client.get(
            first_preview["image_url"].replace("user_id=u01", "user_id=u02")
        ).status_code == 404


def test_batch_validation_rejects_unknown_cross_owner_and_forbidden_fields_before_provider(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    calls = 0

    async def forbidden(_prompt):
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not be called")

    services.image_provider.generate_static_2d = forbidden
    with TestClient(app) as client:
        _first, second = _dialogue_chain(client)
        base = {
            "user_id": "u01",
            "styling_session_id": second["styling_session_id"],
            "request_id": second["request_id"],
            "outfit_ids": ["unknown-outfit"],
            "preview_request_id": "invalid-preview",
        }
        assert client.post("/recommend/previews/static-2d", json=base).status_code == 404
        cross = {**base, "user_id": "u02", "preview_request_id": "cross-owner"}
        assert client.post("/recommend/previews/static-2d", json=cross).status_code == 404
        duplicate = {
            **base,
            "outfit_ids": [
                second["recommendation"]["outfits"][0]["outfit_id"],
                second["recommendation"]["outfits"][0]["outfit_id"],
            ],
            "preview_request_id": "duplicate-outfit",
        }
        assert client.post("/recommend/previews/static-2d", json=duplicate).status_code == 422
        for forbidden_field, value in (
            ("render_mode", "3d"),
            ("model", "grok-4.6-high"),
            ("prompt", "video"),
            ("garment_ids", ["g001"]),
        ):
            attack = {
                **base,
                "outfit_ids": [second["recommendation"]["outfits"][0]["outfit_id"]],
                "preview_request_id": f"attack-{forbidden_field}",
                forbidden_field: value,
            }
            assert client.post("/recommend/previews/static-2d", json=attack).status_code == 422
    assert calls == 0


def test_batch_single_image_failure_does_not_cancel_other_preview(offline_settings) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    calls = 0

    async def partly_available(_prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderUnavailable(
                "temporary image failure", reason_code="CPA_IMAGE_PROVIDER_UNAVAILABLE"
            )
        return _png("beige"), "image/png", _image_meta()

    services.image_provider.generate_static_2d = partly_available
    with TestClient(app) as client:
        _first, second = _dialogue_chain(client)
        payload = {
            "user_id": "u01",
            "styling_session_id": second["styling_session_id"],
            "request_id": second["request_id"],
            "outfit_ids": [
                item["outfit_id"] for item in second["recommendation"]["outfits"]
            ],
            "preview_request_id": "partial-batch",
        }
        response = client.post("/recommend/previews/static-2d", json=payload)
    assert response.status_code == 200
    previews = response.json()["previews"]
    assert [item["status"] for item in previews] == ["degraded", "succeeded"]
    assert previews[0]["image_url"] is None
    assert previews[0]["fallback"]["type"] == "wardrobe_cards_and_text"
    assert previews[1]["image_url"] is not None
    assert calls == 2


def test_batch_failed_item_does_not_inherit_concurrent_sibling_provider_evidence(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    calls = 0
    sibling_succeeded = asyncio.Event()

    async def interleaved(_prompt):
        nonlocal calls
        calls += 1
        current = calls
        if current == 1:
            await sibling_succeeded.wait()
            raise ProviderUnavailable(
                "first item failed", reason_code="CPA_IMAGE_PROVIDER_UNAVAILABLE"
            )
        # Simulate the shared provider health being updated by the successful
        # sibling before the failed task constructs its response.
        provider = services.image_provider
        provider._attempted = True
        provider._available = True
        provider._request_model_pinned = True
        provider._cpa_trace_verified = True
        provider._model_reported = True
        provider._model_verified = True
        provider._verification_basis = "reported_model_exact"
        provider.resolved_model = "grok-imagine-image-quality"
        sibling_succeeded.set()
        return _png("green"), "image/png", {
            **_image_meta(),
            "model_reported": True,
            "model_verified": True,
            "resolved_model": "grok-imagine-image-quality",
            "verification_basis": "reported_model_exact",
        }

    services.image_provider.generate_static_2d = interleaved
    with TestClient(app) as client:
        _first, second = _dialogue_chain(client)
        payload = {
            "user_id": "u01",
            "styling_session_id": second["styling_session_id"],
            "request_id": second["request_id"],
            "outfit_ids": [
                item["outfit_id"] for item in second["recommendation"]["outfits"]
            ],
            "preview_request_id": "interleaved-provenance",
        }
        response = client.post("/recommend/previews/static-2d", json=payload)
    assert response.status_code == 200
    failed, succeeded = response.json()["previews"]
    assert failed["status"] == "degraded"
    assert failed["provider"]["attempted"] is True
    assert failed["provider"]["request_model_pinned"] is True
    assert failed["provider"]["cpa_trace_verified"] is False
    assert failed["provider"]["model_reported"] is False
    assert failed["provider"]["model_verified"] is False
    assert failed["provider"]["resolved_model"] is None
    assert failed["provider"]["verification_basis"] is None
    assert succeeded["status"] == "succeeded"
    assert succeeded["provider"]["cpa_trace_verified"] is True
    assert succeeded["provider"]["model_verified"] is True
