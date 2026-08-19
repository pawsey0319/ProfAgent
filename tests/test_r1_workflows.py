from __future__ import annotations

import json
import struct
from dataclasses import replace
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
import re
import zlib

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from profagent.app import create_app
from profagent.providers import ProviderUnavailable


SCORE_DIMENSIONS = {
    "occasion_fit",
    "expression_match",
    "overall_harmony",
    "silhouette_layering",
    "comfort_practicality",
    "detail_finish",
}


def expect(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def start_recommendation(
    client: TestClient,
    *,
    user_id: str = "u01",
    horizon: str = "planned",
    intent: str = "recommend",
    occasion: str = "commute",
    query: str = "Plan a practical wardrobe outfit for this event.",
    session_id: str | None = None,
    constraints: dict | None = None,
) -> tuple[dict, dict]:
    payload: dict = {
        "user_id": user_id,
        "query_text": query,
        "event_horizon": horizon,
        "intent": intent,
        "occasion": occasion,
        "goals": ["comfortable"],
    }
    if session_id:
        payload["styling_session_id"] = session_id
    if constraints:
        payload["constraints"] = constraints
    scene = expect(client.post("/scene/parse", json=payload))
    recommendation = expect(
        client.post("/recommend", json={"request_id": scene["request_id"]})
    )
    return scene, recommendation


def create_initial_look(client: TestClient, scene: dict, recommendation: dict) -> dict:
    assert recommendation["outfits"], recommendation
    return expect(
        client.post(
            "/look",
            json={
                "created_from": "selected_outfit",
                "user_id": scene["user_id"],
                "styling_session_id": scene["styling_session_id"],
                "request_id": scene["request_id"],
                "outfit_id": recommendation["outfits"][0]["outfit_id"],
                "asset_ids": [],
            },
        )
    )


def png_bytes(width: int, height: int, *, informative: bool) -> bytes:
    image = Image.new(
        "RGB", (width, height), (0, 0, 0) if informative else (128, 96, 64)
    )
    if informative:
        image.putdata(
            [
                (
                    (x * 7 + y * 3) % 256,
                    (x * 2 + y * 11) % 256,
                    (x + y * 5) % 256,
                )
                for y in range(height)
                for x in range(width)
            ]
        )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def animated_png_bytes() -> bytes:
    first = Image.new("RGB", (32, 32), (255, 0, 0))
    second = Image.new("RGB", (32, 32), (0, 0, 255))
    output = BytesIO()
    first.save(
        output,
        format="PNG",
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
    )
    return output.getvalue()


def animated_webp_bytes() -> bytes:
    first = Image.new("RGB", (32, 32), (255, 0, 0))
    second = Image.new("RGB", (32, 32), (0, 0, 255))
    output = BytesIO()
    first.save(
        output,
        format="WEBP",
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
    )
    return output.getvalue()


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def invalid_crc_valid_png(width: int = 256, height: int = 256) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", b"x")
        + _png_chunk(b"IEND", b"")
    )


def upload_png(
    client: TestClient,
    scene: dict,
    body: bytes,
    *,
    user_id: str | None = None,
    angle: str = "front",
) -> dict:
    return expect(
        client.post(
            "/assets",
            data={
                "user_id": user_id or scene["user_id"],
                "styling_session_id": scene["styling_session_id"],
                "angle": angle,
                "consent": "true",
                "purpose": "styling_assessment",
            },
            files={"file": ("look.png", body, "image/png")},
        )
    )


def assert_qualitative_scorecard(card: dict, expected_status: str | None = None) -> None:
    assert card["numeric_score_available"] is False
    assert card["total_score"] is None
    assert card["priority_adjustments"] == []
    assert set(card["dimensions"]) == SCORE_DIMENSIONS
    assert all(value["score"] is None for value in card["dimensions"].values())
    if expected_status is not None:
        assert card["evidence_status"] == expected_status


def vision_payload(
    *,
    model: str = "grok-4.6-high",
    partial: bool = False,
    free_text: bool = False,
    slot_mismatch: bool = False,
    extra_field: bool = False,
    malformed_json: bool = False,
) -> dict:
    regions: list[dict] = []
    for slot in ("top", "bottom", "shoes", "overall"):
        visible = not (partial and slot == "shoes")
        observation = f"{slot}_{'visible' if visible else 'not_visible'}"
        if free_text and slot == "overall":
            observation = "the wearer looks young and attractive"
        if slot_mismatch and slot == "top":
            observation = "bottom_visible"
        issues = ["none"]
        if visible and slot == "bottom":
            issues = ["bottom_cuff_messy"]
        if visible and slot == "shoes":
            issues = ["shoe_coordination_issue"]
        region = {
            "slot": slot,
            "visible": visible,
            "observation": observation,
            "confidence": 0.94,
            "issue_codes": issues,
        }
        if extra_field and slot == "overall":
            region["person_note"] = "provider prose must fail closed"
        regions.append(region)
    content = "{" if malformed_json else json.dumps({"regions": regions})
    return {
        "model": model,
        "choices": [{"message": {"content": content}}],
    }


def vision_transport(payload: dict | None, *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        request_body = json.loads(request.content)
        assert request_body["model"] == "grok-4.6-high"
        assert request_body["response_format"] == {"type": "json_object"}
        assert request_body["temperature"] == 0
        user_content = request_body["messages"][1]["content"]
        prompt = user_content[0]["text"]
        assert "controlled code, never prose" in prompt
        assert any(
            part.get("type") == "image_url"
            and part["image_url"]["url"].startswith("data:image/png;base64,")
            for part in user_content
        )
        if status != 200:
            return httpx.Response(status, json={"error": "injected"})
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def test_high_urgency_dual_gate_grounding_and_hard_constraints(offline_settings) -> None:
    app = create_app(offline_settings)
    with TestClient(app) as client:
        scene, recommendation = start_recommendation(
            client,
            horizon="today",
            intent="buy",
            occasion="meeting",
            query="I need to leave today; use my wardrobe and do not shop.",
            constraints={
                "taboo_colors": ["purple"],
                "excluded_items": [],
                "comfort_notes": [],
                "required_slots": ["top", "bottom", "shoes"],
                "gap_slots": ["shoes"],
                "season": None,
                "weather_requirement": "none",
            },
        )
        assert scene["urgency"] == "high"
        assert scene["shopping_allowed"] is False
        assert scene["ui_capabilities"]["shopping_cta"] is False
        assert recommendation["shopping_suggestions"] == []
        assert recommendation["ui_capabilities"]["shopping_cta"] is False

        # Client tampering cannot loosen the authoritative server scene.
        tampered = dict(scene)
        tampered["urgency"] = "low"
        tampered["shopping_allowed"] = True
        tampered["ui_capabilities"] = {"shopping_cta": True}
        replay = expect(client.post("/recommend", json={"scene": tampered}))
        assert replay["shopping_suggestions"] == []
        assert replay["ui_capabilities"]["shopping_cta"] is False

        trace = expect(
            client.get(f"/trace/{scene['trace_id']}", params={"user_id": "u01"})
        )["trace"]
        assert trace["catalog"]["attempted"] is False
        assert trace["catalog"]["call_count"] == 0

        owned = app.state.services.repository.garment_ids("u01")
        authoritative = app.state.services.state.by_request(scene["request_id"])
        eligible = app.state.services.hard_filter.apply(
            app.state.services.repository.list_garments("u01"), authoritative
        ).eligible_ids
        assert 0 < len(recommendation["outfits"]) <= 3
        for outfit in recommendation["outfits"]:
            assert set(outfit["items"]).issubset(owned)
            assert set(outfit["items"]).issubset(eligible)
            assert outfit["validation"] == {
                "all_ids_grounded": True,
                "hard_constraints_passed": True,
                "required_slots_complete": True,
            }
            assert outfit["reasons"] and outfit["risks"]
            for alternatives in outfit["alternatives"].values():
                assert set(alternatives).issubset(eligible)


def test_look_asset_version_rollback_final_and_owner_contracts(offline_settings) -> None:
    app = create_app(offline_settings)
    with TestClient(app) as client:
        scene, recommendation = start_recommendation(client)
        v1 = create_initial_look(client, scene, recommendation)

        no_asset = expect(
            client.post(
                "/scorecard",
                json={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "look_version_id": v1["look_version_id"],
                },
            )
        )
        assert_qualitative_scorecard(no_asset, "no_asset")
        assert no_asset["keep_point"]["statement"]
        assert no_asset["prohibited_subject_checks"] == {
            "appearance": False,
            "body": False,
            "age": False,
            "sexual_attractiveness": False,
        }
        assert client.post(
            "/look",
            json={
                "created_from": "user_revision",
                "user_id": "u01",
                "styling_session_id": scene["styling_session_id"],
                "parent_version_id": v1["look_version_id"],
                "item_ids": [*v1["item_ids"], "g999"],
                "asset_ids": [],
            },
        ).status_code == 422

        form = {
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "angle": "front",
            "purpose": "styling_assessment",
        }
        vision_call_count = 0

        def should_not_call_vision(_request: httpx.Request) -> httpx.Response:
            nonlocal vision_call_count
            vision_call_count += 1
            return httpx.Response(500, json={"error": "must not be called"})

        services = app.state.services
        services.vision.settings = replace(
            offline_settings,
            cpa_text_enabled=True,
            cpa_base_url="http://vision.invalid/v1",
        )
        services.vision.set_transport(httpx.MockTransport(should_not_call_vision))
        asset_records_before = set(services.assets._records)
        asset_content_before = dict(services.assets._content)
        assert client.post(
            "/assets",
            data=form,
            files={"file": ("x.png", png_bytes(1, 1, informative=False), "image/png")},
        ).status_code == 422
        assert client.post(
            "/assets",
            data={**form, "consent": "false"},
            files={"file": ("x.png", png_bytes(1, 1, informative=False), "image/png")},
        ).status_code == 422
        assert client.post(
            "/assets",
            data={**form, "consent": "true", "purpose": "profile"},
            files={"file": ("x.png", png_bytes(1, 1, informative=False), "image/png")},
        ).status_code == 422
        assert client.post(
            "/assets",
            data={**form, "consent": "true"},
            files={"file": ("fake.png", invalid_crc_valid_png(), "image/png")},
        ).status_code == 422
        assert client.post(
            "/assets",
            data={**form, "consent": "true"},
            files={"file": ("animated.png", animated_png_bytes(), "image/png")},
        ).status_code == 422
        assert client.post(
            "/assets",
            data={**form, "consent": "true"},
            files={
                "file": ("animated.webp", animated_webp_bytes(), "image/webp")
            },
        ).status_code == 422
        assert set(services.assets._records) == asset_records_before
        assert services.assets._content == asset_content_before
        assert vision_call_count == 0
        services.vision.settings = offline_settings
        services.vision.set_transport(None)
        assert client.post(
            "/assets",
            data={**form, "consent": "true", "angle": "video"},
            files={"file": ("x.mp4", b"not-video", "video/mp4")},
        ).status_code == 422

        limited = upload_png(client, scene, png_bytes(1, 1, informative=False))
        assert limited["visual_quality"] == "limited"
        assert limited["storage"] == "memory_ephemeral"
        assert limited["consent_obtained"] is True
        assert limited["purpose"] == "styling_assessment"
        assert limited["retention"] == "process_lifetime"

        usable = upload_png(client, scene, png_bytes(256, 256, informative=True))
        assert usable["visual_quality"] == "usable"
        v2 = expect(
            client.post(
                "/look",
                json={
                    "created_from": "user_revision",
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "parent_version_id": v1["look_version_id"],
                    "item_ids": v1["item_ids"],
                    "asset_ids": [limited["asset_id"]],
                },
            )
        )
        assert v2["version_index"] == 2
        assert v2["parent_version_id"] == v1["look_version_id"]
        assert v2["created_from"] == "user_revision"
        assert v2["partially_accepted_adjustments"] == []

        limited_score = expect(
            client.post(
                "/scorecard",
                json={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "look_version_id": v2["look_version_id"],
                },
            )
        )
        assert_qualitative_scorecard(limited_score, "limited_asset")

        v3 = expect(
            client.post(
                "/look",
                json={
                    "created_from": "user_revision",
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "parent_version_id": v2["look_version_id"],
                    "item_ids": v2["item_ids"],
                    "asset_ids": [usable["asset_id"]],
                },
            )
        )
        assert v3["version_index"] == 3

        disabled_vision = expect(
            client.post(
                "/scorecard",
                json={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "look_version_id": v3["look_version_id"],
                },
            )
        )
        assert_qualitative_scorecard(disabled_vision, "vision_degraded")

        # Score evidence must exactly match the immutable LookVersion binding.
        assert client.post(
            "/scorecard",
            json={
                "user_id": "u01",
                "styling_session_id": scene["styling_session_id"],
                "look_version_id": v3["look_version_id"],
                "asset_ids": [limited["asset_id"]],
            },
        ).status_code == 422

        rollback = expect(
            client.post(
                "/look",
                json={
                    "created_from": "rollback",
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "parent_version_id": v3["look_version_id"],
                    "rollback_target_version_id": v1["look_version_id"],
                },
            )
        )
        assert rollback["version_index"] == 4
        assert rollback["parent_version_id"] == v3["look_version_id"]
        assert rollback["rollback_target_version_id"] == v1["look_version_id"]
        assert rollback["item_ids"] == v1["item_ids"]
        assert rollback["asset_ids"] == v1["asset_ids"] == []

        chain = expect(
            client.get(
                "/look",
                params={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                },
            )
        )
        assert [item["version_index"] for item in chain["versions"]] == [1, 2, 3, 4]
        assert chain["comparison"]["from_version_id"] == v3["look_version_id"]
        assert chain["comparison"]["to_version_id"] == rollback["look_version_id"]
        assert client.get(
            "/look",
            params={
                "user_id": "u02",
                "styling_session_id": scene["styling_session_id"],
            },
        ).status_code == 404

        assert client.delete(
            f"/assets/{usable['asset_id']}",
            params={
                "user_id": "u02",
                "styling_session_id": scene["styling_session_id"],
            },
        ).status_code == 404
        deleted = expect(
            client.delete(
                f"/assets/{usable['asset_id']}",
                params={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                },
            )
        )
        assert deleted["storage_deleted"] is True
        assert client.post(
            "/scorecard",
            json={
                "user_id": "u01",
                "styling_session_id": scene["styling_session_id"],
                "look_version_id": v3["look_version_id"],
            },
        ).status_code == 422

        final = expect(
            client.post(
                "/finalize",
                json={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "look_version_id": rollback["look_version_id"],
                    "satisfied": True,
                    "satisfaction": 1,
                    "reason": "Save this decision now.",
                },
            )
        )
        assert final["satisfaction"] == 1
        assert final["advice_stopped"] is True
        assert client.post(
            "/adjust",
            json={
                "user_id": "u01",
                "styling_session_id": scene["styling_session_id"],
                "look_version_id": rollback["look_version_id"],
                "decision": "user_modified",
                "item_ids": rollback["item_ids"],
            },
        ).status_code == 409
        assert client.post(
            "/scorecard",
            json={
                "user_id": "u01",
                "styling_session_id": scene["styling_session_id"],
                "look_version_id": rollback["look_version_id"],
            },
        ).status_code == 409
        assert client.post(
            "/look",
            json={
                "created_from": "user_revision",
                "user_id": "u01",
                "styling_session_id": scene["styling_session_id"],
                "parent_version_id": rollback["look_version_id"],
                "item_ids": rollback["item_ids"],
                "asset_ids": [],
            },
        ).status_code == 409

        expect(
            client.delete(
                f"/assets/{limited['asset_id']}",
                params={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                },
            )
        )


def test_strict_vision_matrix_reject_replay_and_decision_trace(offline_settings) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    with TestClient(app) as client:
        scene, recommendation = start_recommendation(client)
        wardrobe = {
            item.garment_id: item for item in services.repository.list_garments("u01")
        }
        selected = next(
            outfit
            for outfit in recommendation["outfits"]
            if any(wardrobe[item_id].slot == "shoes" for item_id in outfit["items"])
        )
        v1 = expect(
            client.post(
                "/look",
                json={
                    "created_from": "selected_outfit",
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "request_id": scene["request_id"],
                    "outfit_id": selected["outfit_id"],
                    "asset_ids": [],
                },
            )
        )
        usable = upload_png(client, scene, png_bytes(256, 256, informative=True))
        v2 = expect(
            client.post(
                "/look",
                json={
                    "created_from": "user_revision",
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "parent_version_id": v1["look_version_id"],
                    "item_ids": v1["item_ids"],
                    "asset_ids": [usable["asset_id"]],
                },
            )
        )
        services.vision.settings = replace(
            offline_settings,
            cpa_text_enabled=True,
            cpa_base_url="http://vision.invalid/v1",
            cpa_api_key=None,
        )

        services.vision.set_transport(vision_transport(vision_payload()))
        score = expect(
            client.post(
                "/scorecard",
                json={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "look_version_id": v2["look_version_id"],
                    "asset_ids": [usable["asset_id"]],
                },
            )
        )
        assert score["numeric_score_available"] is True
        assert score["total_score"] is not None
        assert score["evidence_status"] == "sufficient"
        assert set(score["dimensions"]) == SCORE_DIMENSIONS
        assert all(value["score"] is not None for value in score["dimensions"].values())
        assert score["keep_point"]["statement"]
        assert 1 <= len(score["priority_adjustments"]) <= 2
        assert all(
            proposal["evidence_source"] == "verified_visual"
            and proposal["evidence_regions"]
            and proposal["scorecard_id"] == score["scorecard_id"]
            for proposal in score["priority_adjustments"]
        )
        # Provider codes are mapped to server-owned evidence text.
        observations = [item["observation"] for item in score["visual_evidence"]]
        assert observations
        assert all(not value.endswith(("_visible", "_not_visible")) for value in observations)
        assert all("wearer" not in value.lower() for value in observations)

        score_trace = expect(
            client.get(f"/trace/{score['trace_id']}", params={"user_id": "u01"})
        )["trace"]
        score_trace_text = json.dumps(score_trace, ensure_ascii=False)
        assert score["scorecard_id"] in score_trace_text
        assert v2["look_version_id"] in score_trace_text
        assert all(
            proposal["adjustment_id"] in score_trace_text
            for proposal in score["priority_adjustments"]
        )
        assert "data:image" not in score_trace_text
        assert "base64," not in score_trace_text

        services.vision.set_transport(
            vision_transport(vision_payload(model="grok-4.6-build"))
        )
        concrete_build_score = expect(
            client.post(
                "/scorecard",
                json={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "look_version_id": v2["look_version_id"],
                },
            )
        )
        assert concrete_build_score["numeric_score_available"] is True
        concrete_build_trace = expect(
            client.get(
                f"/trace/{concrete_build_score['trace_id']}",
                params={"user_id": "u01"},
            )
        )["trace"]
        assert concrete_build_trace["provider"]["resolved_model"] == "grok-4.6-build"
        assert concrete_build_trace["provider"]["model_verified"] is True

        degraded_payloads = {
            "model_mismatch": (vision_payload(model="grok-4.5"), 200),
            "model_prefix_mismatch": (
                vision_payload(model="grok-4.6-high-preview"),
                200,
            ),
            "malformed": (vision_payload(malformed_json=True), 200),
            "unsafe_free_text": (vision_payload(free_text=True), 200),
            "slot_observation_mismatch": (vision_payload(slot_mismatch=True), 200),
            "extra_provider_field": (vision_payload(extra_field=True), 200),
            "partial": (vision_payload(partial=True), 200),
            "http_error": (None, 503),
        }
        for label, (payload, status) in degraded_payloads.items():
            services.vision.set_transport(vision_transport(payload, status=status))
            card = expect(
                client.post(
                    "/scorecard",
                    json={
                        "user_id": "u01",
                        "styling_session_id": scene["styling_session_id"],
                        "look_version_id": v2["look_version_id"],
                    },
                )
            )
            assert_qualitative_scorecard(card, "vision_degraded")
            trace = expect(
                client.get(f"/trace/{card['trace_id']}", params={"user_id": "u01"})
            )["trace"]
            assert trace["fallback_events"], label

        services.vision.set_transport(vision_transport(vision_payload()))
        proposal = score["priority_adjustments"][0]
        rejection = expect(
            client.post(
                "/adjust",
                json={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "look_version_id": v2["look_version_id"],
                    "adjustment_id": proposal["adjustment_id"],
                    "decision": "reject",
                    "reason": "not_for_this_look",
                },
            )
        )
        assert rejection["look_version"] is None
        assert rejection["decision_record"]["canonical_key"] == proposal["canonical_key"]
        decision_id = rejection["decision_record"]["decision_record_id"]
        decision_trace = expect(
            client.get(
                f"/trace/{rejection['decision_record']['trace_id']}",
                params={"user_id": "u01"},
            )
        )["trace"]
        assert decision_id in json.dumps(decision_trace, ensure_ascii=False)
        assert client.post(
            "/adjust",
            json={
                "user_id": "u01",
                "styling_session_id": scene["styling_session_id"],
                "look_version_id": v2["look_version_id"],
                "adjustment_id": proposal["adjustment_id"],
                "decision": "reject",
            },
        ).status_code == 422

        after_reject = expect(
            client.post(
                "/scorecard",
                json={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "look_version_id": v2["look_version_id"],
                },
            )
        )
        assert proposal["canonical_key"] not in {
            item["canonical_key"] for item in after_reject["priority_adjustments"]
        }

        user_revision = expect(
            client.post(
                "/adjust",
                json={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "look_version_id": v2["look_version_id"],
                    "decision": "user_modified",
                    "asset_ids": [],
                },
            )
        )
        v3 = user_revision["look_version"]
        assert v3["created_from"] == "user_revision"
        assert v3["partially_accepted_adjustments"] == []
        assert proposal["canonical_key"] in v3["rejected_adjustments"]
        chain = expect(
            client.get(
                "/look",
                params={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                },
            )
        )
        assert any(
            decision["decision_record_id"] == decision_id
            for decision in chain["adjustment_decisions"]
        )
        assert any(
            decision["decision"] == "user_modified"
            for decision in chain["adjustment_decisions"]
        )
        expect(
            client.delete(
                f"/assets/{usable['asset_id']}",
                params={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                },
            )
        )


def test_feedback_memory_causal_chain_and_memory_safety(offline_settings) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    with TestClient(app) as client:
        seed_scene, seed_recommendation = start_recommendation(
            client,
            user_id="u02",
            horizon="planned",
            occasion="commute",
            query="下周通勤活动，先用现有衣橱给出舒适方向。",
        )
        wardrobe = {
            item.garment_id: item for item in services.repository.list_garments("u02")
        }
        seed_outfit = next(
            outfit
            for outfit in seed_recommendation["outfits"]
            if sum(wardrobe[item_id].slot == "shoes" for item_id in outfit["items"])
            == 1
        )
        target_shoe = next(
            item_id
            for item_id in seed_outfit["items"]
            if wardrobe[item_id].slot == "shoes"
        )

        liked = expect(
            client.post(
                "/recommend/feedback",
                json={
                    "user_id": "u02",
                    "styling_session_id": seed_scene["styling_session_id"],
                    "request_id": seed_scene["request_id"],
                    "outfit_id": seed_outfit["outfit_id"],
                    "decision": "like",
                    "memory_scope": "session",
                },
            )
        )
        assert liked["memory_proposal"] is None and liked["record"] is None

        feedback = expect(
            client.post(
                "/recommend/feedback",
                json={
                    "user_id": "u02",
                    "styling_session_id": seed_scene["styling_session_id"],
                    "request_id": seed_scene["request_id"],
                    "outfit_id": seed_outfit["outfit_id"],
                    "decision": "dislike",
                    "reason_code": "long_walk_shoes",
                    "memory_scope": "propose",
                },
            )
        )
        proposal = feedback["memory_proposal"]
        assert proposal["status"] == "proposed"
        assert proposal["commit_blocked"] is False
        assert feedback["record"] is None
        assert target_shoe not in json.dumps(feedback, ensure_ascii=False)
        assert client.post(
            f"/memory/{proposal['proposal_id']}/confirm",
            json={"user_id": "u01", "decision": "confirm"},
        ).status_code == 404

        related_query = "下周要久走并长时间站立，想穿得轻松舒适。"
        before, _ = start_recommendation(
            client,
            user_id="u02",
            horizon="planned",
            occasion="commute",
            query=related_query,
        )
        assert target_shoe not in before["constraints"]["excluded_items"]
        before_scene = services.state.by_request(before["request_id"])
        assert target_shoe in services.hard_filter.apply(
            services.repository.list_garments("u02"), before_scene
        ).eligible_ids

        committed = expect(
            client.post(
                f"/memory/{proposal['proposal_id']}/confirm",
                json={"user_id": "u02", "decision": "confirm"},
            )
        )
        memory_id = committed["record"]["memory_id"]
        assert committed["proposal"]["status"] == "committed"
        assert committed["proposal"]["content"] is None
        public_memory = expect(client.get("/memory", params={"user_id": "u02"}))
        assert public_memory["retrieval_index_count"] == 1
        assert target_shoe not in json.dumps(public_memory, ensure_ascii=False)

        after, after_recommendation = start_recommendation(
            client,
            user_id="u02",
            horizon="planned",
            occasion="commute",
            query=related_query,
        )
        assert target_shoe in after["constraints"]["excluded_items"]
        assert all(
            target_shoe not in outfit["items"]
            for outfit in after_recommendation["outfits"]
        )
        after_trace = expect(
            client.get(f"/trace/{after['trace_id']}", params={"user_id": "u02"})
        )["trace"]
        after_trace_text = json.dumps(after_trace, ensure_ascii=False)
        assert target_shoe not in after_trace_text
        assert proposal["content"] not in after_trace_text

        unrelated, _ = start_recommendation(
            client,
            user_id="u02",
            horizon="today",
            occasion="home",
            query="今天在家休息，想穿得轻松。",
        )
        assert target_shoe not in unrelated["constraints"]["excluded_items"]
        assert "long_walk" not in unrelated["constraints"]["comfort_notes"]

        assert client.delete(
            f"/memory/{memory_id}", params={"user_id": "u01"}
        ).status_code == 404
        expect(client.delete(f"/memory/{memory_id}", params={"user_id": "u02"}))
        empty = expect(client.get("/memory", params={"user_id": "u02"}))
        assert empty["records"] == []
        assert empty["proposals"] == []
        assert empty["retrieval_index_count"] == 0
        restored, _ = start_recommendation(
            client,
            user_id="u02",
            horizon="planned",
            occasion="commute",
            query=related_query,
        )
        assert target_shoe not in restored["constraints"]["excluded_items"]

        # Unknown free text and an explicitly sensitive type are both
        # content-free, non-committable proposals.
        unknown = expect(
            client.post(
                "/memory/propose",
                json={
                    "user_id": "u01",
                    "namespace": "stylist",
                    "type": "preference",
                    "content": "I prefer arbitrary unapproved free text.",
                },
            )
        )
        assert unknown["proposal"]["content"] is None
        assert unknown["proposal"]["commit_blocked"] is True
        unknown_confirm = expect(
            client.post(
                f"/memory/{unknown['proposal']['proposal_id']}/confirm",
                json={"user_id": "u01", "decision": "confirm"},
            )
        )
        assert unknown_confirm["record"] is None
        assert unknown_confirm["proposal"]["status"] == "rejected"

        sensitive_type = expect(
            client.post(
                "/memory/propose",
                json={
                    "user_id": "u01",
                    "namespace": "shared",
                    "type": "sensitive",
                    "content": "不穿高跟鞋",
                },
            )
        )
        assert sensitive_type["proposal"]["content"] is None
        assert sensitive_type["proposal"]["commit_blocked"] is True

        detected_sensitive = expect(
            client.post(
                "/memory/propose",
                json={
                    "user_id": "u01",
                    "namespace": "stylist",
                    "type": "profile_stable",
                    "content": "medical diagnosis diabetes phone 13800138000",
                },
            )
        )
        assert detected_sensitive["proposal"]["content"] is None
        assert detected_sensitive["proposal"]["commit_blocked"] is True

        for memory_type, private_text in (
            ("profile_stable", "我有肾病"),
            ("profile_stable", "我是维吾尔族"),
            ("sensitive", "隐私内容"),
        ):
            blocked_private = expect(
                client.post(
                    "/memory/propose",
                    json={
                        "user_id": "u01",
                        "namespace": "stylist",
                        "type": memory_type,
                        "content": private_text,
                    },
                )
            )
            assert blocked_private["proposal"]["content"] is None
            assert blocked_private["proposal"]["commit_blocked"] is True
            assert private_text not in json.dumps(blocked_private, ensure_ascii=False)

        unknown_type_text = "我有肾病且这是不应回显的原文"
        unknown_type = client.post(
            "/memory/propose",
            json={
                "user_id": "u01",
                "namespace": "stylist",
                "type": "unknown_memory_type",
                "content": unknown_type_text,
            },
        )
        assert unknown_type.status_code == 422
        assert unknown_type_text not in unknown_type.text

        approved = expect(
            client.post(
                "/memory/propose",
                json={
                    "user_id": "u01",
                    "namespace": "stylist",
                    "type": "constraint",
                    "content": "不穿高跟鞋",
                },
            )
        )
        assert approved["proposal"]["status"] == "proposed"
        assert approved["proposal"]["commit_blocked"] is False
        assert client.post(
            f"/memory/{approved['proposal']['proposal_id']}/confirm",
            json={"user_id": "u02", "decision": "confirm"},
        ).status_code == 404
        approved_commit = expect(
            client.post(
                f"/memory/{approved['proposal']['proposal_id']}/confirm",
                json={"user_id": "u01", "decision": "confirm"},
            )
        )
        approved_memory_id = approved_commit["record"]["memory_id"]
        assert approved_commit["proposal"]["content"] is None
        assert client.delete(
            f"/memory/{approved_memory_id}", params={"user_id": "u02"}
        ).status_code == 404
        expect(
            client.delete(
                f"/memory/{approved_memory_id}", params={"user_id": "u01"}
            )
        )

        edited = expect(
            client.post(
                "/memory/propose",
                json={
                    "user_id": "u01",
                    "namespace": "stylist",
                    "type": "constraint",
                    "content": "不穿高跟鞋",
                },
            )
        )
        edited_blocked = expect(
            client.post(
                f"/memory/{edited['proposal']['proposal_id']}/confirm",
                json={
                    "user_id": "u01",
                    "decision": "edit",
                    "edited_content": "medical diagnosis diabetes",
                },
            )
        )
        assert edited_blocked["record"] is None
        assert edited_blocked["proposal"]["status"] == "rejected"
        assert edited_blocked["proposal"]["content"] is None

        ttl = expect(
            client.post(
                "/memory/propose",
                json={
                    "user_id": "u01",
                    "namespace": "stylist",
                    "type": "constraint",
                    "content": "不穿裙装",
                    "ttl_days": 1,
                },
            )
        )
        ttl_commit = expect(
            client.post(
                f"/memory/{ttl['proposal']['proposal_id']}/confirm",
                json={"user_id": "u01", "decision": "confirm"},
            )
        )
        ttl_memory_id = ttl_commit["record"]["memory_id"]
        record = services.memory._records[ttl_memory_id]
        services.memory._records[ttl_memory_id] = record.model_copy(
            update={"expires_at": datetime.now().astimezone() - timedelta(seconds=1)}
        )
        after_ttl = expect(client.get("/memory", params={"user_id": "u01"}))
        assert all(item["memory_id"] != ttl_memory_id for item in after_ttl["records"])
        assert all(
            item["proposal_id"] != ttl["proposal"]["proposal_id"]
            for item in after_ttl["proposals"]
        )

        all_memory_traces = services.traces.list_for_session(
            detected_sensitive["proposal"].get("styling_session_id")
            or "memory_u01"
        )
        memory_trace_text = json.dumps(
            [item.model_dump(mode="json") for item in all_memory_traces],
            ensure_ascii=False,
        ).lower()
        assert "13800138000" not in memory_trace_text
        assert "medical diagnosis" not in memory_trace_text
        assert "data:image" not in memory_trace_text


def test_llm_dense_and_catalog_failures_degrade_without_external_calls(offline_settings) -> None:
    # LLM failure is injected before parsing, so no network transport can run.
    llm_settings = replace(offline_settings, cpa_text_enabled=True)
    llm_app = create_app(llm_settings)

    async def injected_llm_failure(_query: str):
        raise ProviderUnavailable("injected llm failure")

    llm_app.state.services.llm.parse_scene_advisory = injected_llm_failure
    with TestClient(llm_app) as client:
        scene, recommendation = start_recommendation(client)
        assert scene["backend"] == "rule_fallback"
        assert recommendation["outfits"]
        trace = expect(
            client.get(f"/trace/{scene['trace_id']}", params={"user_id": "u01"})
        )["trace"]
        assert any(event["component"] == "llm" for event in trace["fallback_events"])

    dense_app = create_app(
        replace(offline_settings, dense_enabled=True, dense_force_failure=True)
    )
    with TestClient(dense_app) as client:
        scene, recommendation = start_recommendation(client)
        assert recommendation["outfits"]
        trace = expect(
            client.get(f"/trace/{scene['trace_id']}", params={"user_id": "u01"})
        )["trace"]
        assert any(event["component"] == "dense" for event in trace["fallback_events"])

    catalog_app = create_app(replace(offline_settings, catalog_force_failure=True))
    with TestClient(catalog_app) as client:
        event_time = (datetime.now().astimezone() + timedelta(days=14)).isoformat()
        scene = expect(
            client.post(
                "/scene/parse",
                json={
                    "user_id": "u01",
                    "query_text": "两周后通勤缺一双鞋，可以补购。",
                    "event_horizon": "planned",
                    "event_time": event_time,
                    "intent": "fill_gap",
                    "occasion": "commute",
                    "goals": ["comfortable"],
                    "constraints": {
                        "taboo_colors": [],
                        "excluded_items": [],
                        "comfort_notes": [],
                        "required_slots": ["top", "bottom", "shoes"],
                        "gap_slots": ["shoes"],
                        "season": None,
                        "weather_requirement": "none",
                    },
                },
            )
        )
        assert scene["shopping_allowed"] is True
        recommendation = expect(
            client.post("/recommend", json={"request_id": scene["request_id"]})
        )
        assert recommendation["outfits"]
        assert recommendation["shopping_suggestions"] == []
        trace = expect(
            client.get(f"/trace/{scene['trace_id']}", params={"user_id": "u01"})
        )["trace"]
        assert trace["catalog"]["call_count"] == 0
        assert trace["catalog"]["blocked_reason"] == "PROVIDER_UNAVAILABLE"
        assert any(event["component"] == "catalog" for event in trace["fallback_events"])


def test_trace_redaction_object_ids_and_cross_owner_access(offline_settings) -> None:
    app = create_app(offline_settings)
    with TestClient(app) as client:
        scene, recommendation = start_recommendation(
            client,
            query="Do not log sk-test-secret or Bearer test-token; plan my outfit.",
        )
        look = create_initial_look(client, scene, recommendation)
        card = expect(
            client.post(
                "/scorecard",
                json={
                    "user_id": "u01",
                    "styling_session_id": scene["styling_session_id"],
                    "look_version_id": look["look_version_id"],
                },
            )
        )
        trace = expect(
            client.get(f"/trace/{card['trace_id']}", params={"user_id": "u01"})
        )["trace"]
        text = json.dumps(trace, ensure_ascii=False).lower()
        assert look["look_version_id"] in text
        assert card["scorecard_id"] in text
        assert "sk-test-secret" not in text
        assert "bearer test-token" not in text
        assert "data:image" not in text
        assert "base64," not in text
        assert trace["query"].keys() == {"sha256_prefix", "character_count"}
        assert client.get(
            f"/trace/{card['trace_id']}", params={"user_id": "u02"}
        ).status_code == 404
        assert client.get(
            "/trace",
            params={
                "user_id": "u02",
                "styling_session_id": scene["styling_session_id"],
            },
        ).status_code == 404


def test_rain_recall_and_catalog_fail_closed_for_every_gap(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    actual_catalog_calls: list[str] = []
    original_search = services.catalog.search

    def search_spy(scene):
        actual_catalog_calls.append(scene.request_id)
        return original_search(scene)

    services.catalog.search = search_spy
    with TestClient(app) as client:
        outer_ids = {
            item.garment_id
            for item in services.repository.list_garments("u01")
            if item.slot == "outer"
        }
        assert outer_ids
        low_scenes: list[dict] = []
        for slot, query in (
            ("outer", "两周后下雨，缺外套，需要补购。"),
            ("top", "两周后下雨，缺上衣，需要补购。"),
            ("bag", "两周后下雨，缺包，需要补购。"),
        ):
            before_calls = len(actual_catalog_calls)
            scene = expect(
                client.post(
                    "/scene/parse",
                    json={
                        "user_id": "u01",
                        "query_text": query,
                        "event_horizon": "planned",
                        "intent": "fill_gap",
                        "occasion": "daily",
                        "goals": ["comfortable"],
                    },
                )
            )
            low_scenes.append(scene)
            assert scene["urgency"] == "low"
            assert scene["shopping_allowed"] is True
            assert scene["constraints"]["weather_requirement"] == "rain"
            assert "outer" in scene["constraints"]["required_slots"]
            assert slot in scene["constraints"]["gap_slots"]
            recommendation = expect(
                client.post(
                    "/recommend", json={"request_id": scene["request_id"]}
                )
            )
            assert len(actual_catalog_calls) - before_calls == 1
            assert recommendation["outfits"] == []
            assert recommendation["gap_explanation"]
            assert recommendation["shopping_suggestions"] == []
            trace = expect(
                client.get(
                    f"/trace/{scene['trace_id']}", params={"user_id": "u01"}
                )
            )["trace"]
            filtered_outer_ids = {
                row["item_id"]
                for row in trace["filters"]
                if row["reason"] == "RAIN_PROOF_EVIDENCE_MISSING"
            }
            assert filtered_outer_ids == outer_ids
            for ranker in ("rule", "bm25", "dense", "rrf"):
                assert not (
                    outer_ids
                    & {
                        row["item_id"]
                        for row in trace["retrieval"].get(ranker, [])
                    }
                )
            assert trace["catalog"]["attempted"] is True
            assert trace["catalog"]["call_count"] == 1
            assert trace["catalog"]["returned_count"] == 0
            assert (
                trace["catalog"]["blocked_reason"]
                == "RAIN_PROOF_EVIDENCE_MISSING"
            )

        authoritative = services.state.by_request(low_scenes[0]["request_id"])
        assert authoritative is not None and authoritative.shopping_allowed
        empty_gap_scene = authoritative.model_copy(deep=True)
        empty_gap_scene.constraints = empty_gap_scene.constraints.model_copy(
            update={"gap_slots": []}
        )
        empty_gap_result = services.catalog.search(empty_gap_scene)
        assert empty_gap_result.items == ()
        assert empty_gap_result.attempted is True
        assert empty_gap_result.call_count == 1
        assert empty_gap_result.blocked_reason == "RAIN_PROOF_EVIDENCE_MISSING"
        assert services.catalog.suggestions(empty_gap_result, empty_gap_scene) == []

        calls_before_high = len(actual_catalog_calls)
        high_scene = expect(
            client.post(
                "/scene/parse",
                json={
                    "user_id": "u01",
                    "query_text": "现在下雨，缺外套，需要补购后出门。",
                    "event_horizon": "now",
                    "intent": "fill_gap",
                    "occasion": "daily",
                    "goals": ["comfortable"],
                },
            )
        )
        high_recommendation = expect(
            client.post(
                "/recommend", json={"request_id": high_scene["request_id"]}
            )
        )
        assert len(actual_catalog_calls) == calls_before_high
        assert high_scene["urgency"] == "high"
        assert high_scene["shopping_allowed"] is False
        assert high_recommendation["outfits"] == []
        assert high_recommendation["gap_explanation"]
        assert high_recommendation["shopping_suggestions"] == []
        high_trace = expect(
            client.get(
                f"/trace/{high_scene['trace_id']}", params={"user_id": "u01"}
            )
        )["trace"]
        assert high_trace["catalog"]["attempted"] is False
        assert high_trace["catalog"]["call_count"] == 0


def test_cpa_privacy_short_circuit_spy_and_ordinary_call(
    offline_settings, project_root: Path
) -> None:
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    services = app.state.services
    provider_calls: list[str] = []

    async def provider_spy(query_text: str):
        provider_calls.append(query_text)
        return (
            {
                "intent": "recommend",
                "occasion": "commute",
                "goals": ["reliable"],
            },
            {
                "attempted": True,
                "status": "ok",
                "requested_model": "grok4.6",
                "resolved_model": "grok-4.6-high",
                "model_verified": True,
            },
        )

    services.llm.parse_scene_advisory = provider_spy
    safety_rows = {}
    for line in (project_root / "data" / "eval" / "eval.jsonl").read_text(
        encoding="utf-8"
    ).splitlines():
        row = json.loads(line)
        if row["case_id"] in {"e028", "e029", "e030"}:
            safety_rows[row["case_id"]] = row
    assert set(safety_rows) == {"e028", "e029", "e030"}
    blocked_inputs = [
        {
            "user_id": row["user_id"],
            "query_text": row["query"],
            "event_horizon": row["horizon"],
            "intent": row["intent"],
        }
        for _case_id, row in sorted(safety_rows.items())
    ]
    blocked_inputs.extend(
        [
            {
                "user_id": "u01",
                "query_text": "下周通勤搭配，联系邮箱 private@example.com。",
                "event_horizon": "soon",
                "intent": "recommend",
            },
            {
                "user_id": "u01",
                "query_text": "下周通勤搭配，手机号 13800138000。",
                "event_horizon": "soon",
                "intent": "recommend",
            },
            {
                "user_id": "u01",
                "query_text": "我只是想说说今天很难过。",
                "event_horizon": "today",
                "intent": "vent",
            },
        ]
    )

    with TestClient(app) as client:
        for payload in blocked_inputs:
            before_calls = len(provider_calls)
            scene = expect(client.post("/scene/parse", json=payload))
            assert len(provider_calls) == before_calls
            assert scene["backend"] == "rule_fallback"
            trace = expect(
                client.get(
                    f"/trace/{scene['trace_id']}",
                    params={"user_id": payload["user_id"]},
                )
            )["trace"]
            assert trace["provider"]["attempted"] is False
            assert trace["provider"]["status"] == "blocked"
            assert (
                trace["provider"]["reason_code"]
                == "LOCAL_SAFETY_OR_PRIVACY_SHORT_CIRCUIT"
            )
            assert payload["query_text"] not in json.dumps(
                trace, ensure_ascii=False
            )

        ordinary = expect(
            client.post(
                "/scene/parse",
                json={
                    "user_id": "u01",
                    "query_text": "下周通勤，请用现有衣橱搭一套可靠穿搭。",
                    "event_horizon": "soon",
                    "intent": "recommend",
                    "occasion": "commute",
                    "goals": ["reliable"],
                },
            )
        )
        assert len(provider_calls) == 1
        assert ordinary["backend"] == "grok4.6"
        ordinary_trace = expect(
            client.get(
                f"/trace/{ordinary['trace_id']}", params={"user_id": "u01"}
            )
        )["trace"]
        assert ordinary_trace["provider"]["attempted"] is True
        assert ordinary_trace["provider"]["status"] == "ok"


def test_high_urgency_ui_contract_and_no_3d_video_entry(project_root: Path) -> None:
    html = (project_root / "web" / "index.html").read_text(encoding="utf-8")
    script = (project_root / "web" / "app.js").read_text(encoding="utf-8")
    lowered_html = html.lower()
    lowered_script = script.lower()

    assert 'accept="image/png,image/jpeg,image/webp"' in lowered_html
    assert "<video" not in lowered_html
    assert "<model-viewer" not in lowered_html
    assert not re.search(r'(?:id|href|src|action)="[^"]*(?:3d|360|video)', lowered_html)
    assert not re.search(r"function\s+\w*(?:3d|360|video)\w*\s*\(", lowered_script)

    assert 'scene?.urgency === "high"' in script
    assert '["now", "today", "unknown"]' in script
    assert "shopping_allowed: urgencyGuard ? false" in script
    assert "shopping_cta: urgencyGuard ? false" in script
    assert 'const shopping = byId("allowed-shopping-zone")' in script
    assert "if (!highGuard && recommendation.ui_capabilities?.shopping_cta" in script
