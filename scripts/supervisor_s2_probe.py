from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import struct
import sys
import zlib

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from profagent.app import create_app
from profagent.config import Settings


def chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def png(width: int, height: int, *, informative: bool) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = []
    for y in range(height):
        pixels = bytearray()
        for x in range(width):
            if informative:
                pixels.extend(((x * 7 + y * 3) % 256, (x * 2 + y * 11) % 256, (x + y * 5) % 256))
            else:
                pixels.extend((128, 96, 64))
        rows.append(b"\x00" + bytes(pixels))
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def fake_png(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", b"x") + chunk(b"IEND", b"")


def vision_payload(*, model: str = "grok-4.5-build", unsafe: bool = False, partial: bool = False) -> dict[str, object]:
    regions = []
    for slot in ("top", "bottom", "shoes", "overall"):
        visible = not (partial and slot == "shoes")
        observation = f"{slot}_{'visible' if visible else 'not_visible'}"
        if unsafe and slot == "overall":
            # Any Provider prose, including a previously missed human-attribute
            # wording, must fail the closed observation-code contract.
            observation = "中年气质"
        regions.append(
            {
                "slot": slot,
                "visible": visible,
                "observation": observation,
                "confidence": 0.94,
                "issue_codes": (
                    ["bottom_cuff_messy"]
                    if slot == "bottom" and visible
                    else ["none"]
                ),
            }
        )
    return {
        "model": model,
        "choices": [{"message": {"content": json.dumps({"regions": regions}, ensure_ascii=False)}}],
    }


def mock_transport(payload: dict[str, object] | None = None, *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "grok-4.5-high"
        assert body["response_format"] == {"type": "json_object"}
        content = body["messages"][1]["content"]
        assert any(part.get("type") == "image_url" and part["image_url"]["url"].startswith("data:image/") for part in content)
        if status != 200:
            return httpx.Response(status, json={"error": "injected"})
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def expect(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def upload(client: TestClient, session_id: str, body: bytes, name: str = "look.png") -> dict:
    return expect(
        client.post(
            "/assets",
            data={
                "user_id": "u01",
                "styling_session_id": session_id,
                "angle": "front",
                "consent": "true",
                "purpose": "styling_assessment",
            },
            files={"file": (name, body, "image/png")},
        )
    )


def assert_qualitative(card: dict) -> None:
    assert card["numeric_score_available"] is False
    assert card["total_score"] is None
    assert card["priority_adjustments"] == []
    assert len(card["dimensions"]) == 6
    assert all(value["score"] is None for value in card["dimensions"].values())


def main() -> None:
    settings = replace(
        Settings.from_env(),
        cpa_text_enabled=False,
        dense_enabled=False,
        catalog_enabled=True,
        cpa_api_key=None,
    )
    app = create_app(settings)
    services = app.state.services
    catalog_invocations = 0
    original_catalog_search = services.catalog.search

    def counted_catalog_search(scene):
        nonlocal catalog_invocations
        catalog_invocations += 1
        return original_catalog_search(scene)

    services.catalog.search = counted_catalog_search
    summary: dict[str, object] = {}
    with TestClient(app) as client:
        # Rejected values must not be reflected by either request-level or
        # internal Pydantic validation handlers.
        validation_sentinel = "PRIVATE_VALIDATION_SENTINEL_20260804"
        invalid_request_id = client.post(
            "/recommend",
            json={"request_id": {"private": validation_sentinel}},
        )
        assert invalid_request_id.status_code == 422
        assert validation_sentinel not in invalid_request_id.text
        invalid_scene = client.post(
            "/recommend",
            json={"scene": {"private": validation_sentinel}},
        )
        assert invalid_scene.status_code == 422
        assert validation_sentinel not in invalid_scene.text
        missing_private_id = client.post(
            "/recommend", json={"request_id": validation_sentinel}
        )
        assert missing_private_id.status_code == 404
        assert validation_sentinel not in missing_private_id.text

        # High-urgency recommendation remains wardrobe-only and grounded.
        scene = expect(client.post("/scene/parse", json={
            "user_id": "u01",
            "query_text": "十分钟后开会，不穿高跟鞋，直接用衣橱给我一套稳妥搭配",
        }))
        assert scene["urgency"] == "high" and scene["shopping_allowed"] is False
        recommendation = expect(client.post("/recommend", json={"request_id": scene["request_id"]}))
        assert recommendation["shopping_suggestions"] == []
        high_catalog_invocations = catalog_invocations
        assert high_catalog_invocations == 0
        wardrobe = expect(client.get("/wardrobe", params={"user_id": "u01"}))
        garment_by_id = {item["garment_id"]: item for item in wardrobe["items"]}
        grounded = set(garment_by_id)
        for outfit in recommendation["outfits"]:
            assert set(outfit["items"]).issubset(grounded)
            slots = {garment_by_id[item_id]["slot"] for item_id in outfit["items"]}
            assert "shoes" in slots and ("dress" in slots or {"top", "bottom"}.issubset(slots))
            assert "g021" not in outfit["items"] and "g022" not in outfit["items"]
        high_trace = expect(client.get(f"/trace/{scene['trace_id']}", params={"user_id": "u01"}))["trace"]
        assert high_trace["catalog"]["call_count"] == 0

        # Rain protection is a hard evidence requirement. fixtures_v1.0 has no
        # structured waterproof field, so ordinary outerwear must be removed
        # before all recall lists and the Mock Catalog must not fill the gap.
        rain_scene = expect(client.post("/scene/parse", json={
            "user_id": "u01",
            "styling_session_id": "session_rain_fail_closed",
            "query_text": "两周后雨天通勤，缺一件防雨外套，可以补购",
            "event_horizon": "planned",
        }))
        assert rain_scene["constraints"]["weather_requirement"] == "rain"
        assert "outer" in rain_scene["constraints"]["required_slots"]
        assert "outer" in rain_scene["constraints"]["gap_slots"]
        assert rain_scene["shopping_allowed"] is True
        rain_recommendation = expect(client.post(
            "/recommend", json={"request_id": rain_scene["request_id"]}
        ))
        assert rain_recommendation["outfits"] == []
        assert rain_recommendation["shopping_suggestions"] == []
        assert rain_recommendation["gap_explanation"]
        rain_trace = expect(client.get(
            f"/trace/{rain_scene['trace_id']}", params={"user_id": "u01"}
        ))["trace"]
        outer_ids = {
            item_id
            for item_id, item in garment_by_id.items()
            if item["slot"] == "outer"
        }
        rain_filtered_outer = {
            row["item_id"]
            for row in rain_trace["filters"]
            if row.get("reason") == "RAIN_PROOF_EVIDENCE_MISSING"
        }
        assert rain_filtered_outer == outer_ids
        for ranker in ("rule", "bm25", "dense", "rrf"):
            assert not {
                row["item_id"] for row in rain_trace["retrieval"].get(ranker, [])
            } & outer_ids
        assert rain_trace["catalog"]["returned_count"] == 0
        assert rain_trace["catalog"]["blocked_reason"] == "RAIN_PROOF_EVIDENCE_MISSING"

        rain_other_gap = expect(client.post("/scene/parse", json={
            "user_id": "u01",
            "styling_session_id": "session_rain_other_gap",
            "query_text": "两周后雨天通勤，缺一件上衣，可以补购",
            "event_horizon": "planned",
        }))
        assert rain_other_gap["constraints"]["gap_slots"] == ["top"]
        rain_other_recommendation = expect(client.post(
            "/recommend", json={"request_id": rain_other_gap["request_id"]}
        ))
        assert rain_other_recommendation["outfits"] == []
        assert rain_other_recommendation["shopping_suggestions"] == []
        assert rain_other_recommendation["gap_explanation"]
        rain_other_trace = expect(client.get(
            f"/trace/{rain_other_gap['trace_id']}", params={"user_id": "u01"}
        ))["trace"]
        assert rain_other_trace["catalog"]["returned_count"] == 0
        assert rain_other_trace["catalog"]["blocked_reason"] == (
            "RAIN_PROOF_EVIDENCE_MISSING"
        )

        selected = next(
            outfit for outfit in recommendation["outfits"]
            if any("裤" in garment_by_id[item_id]["name"] for item_id in outfit["items"])
        )
        look_v1 = expect(client.post("/look", json={
            "created_from": "selected_outfit",
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "request_id": scene["request_id"],
            "outfit_id": selected["outfit_id"],
            "asset_ids": [],
        }))
        no_asset = expect(client.post("/scorecard", json={
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": look_v1["look_version_id"],
        }))
        assert no_asset["evidence_status"] == "no_asset"
        assert no_asset["keep_point"]["label"] == "值得保留"
        assert_qualitative(no_asset)

        common_form = {
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "angle": "front",
            "purpose": "styling_assessment",
        }
        assert client.post("/assets", data=common_form, files={"file": ("x.png", png(1, 1, informative=False), "image/png")}).status_code == 422
        assert client.post("/assets", data={**common_form, "consent": "false"}, files={"file": ("x.png", png(1, 1, informative=False), "image/png")}).status_code == 422
        assert client.post("/assets", data={**common_form, "consent": "true", "purpose": "profile"}, files={"file": ("x.png", png(1, 1, informative=False), "image/png")}).status_code == 422
        assert client.post("/assets", data={**common_form, "consent": "true"}, files={"file": ("fake.png", fake_png(256, 256), "image/png")}).status_code == 422
        assert client.post("/assets", data={**common_form, "consent": "true", "angle": "video"}, files={"file": ("x.mp4", b"video", "video/mp4")}).status_code == 422

        tiny = upload(client, scene["styling_session_id"], png(1, 1, informative=False), "tiny.png")
        assert tiny["visual_quality"] == "limited"
        assert tiny["storage"] == "memory_ephemeral"
        assert tiny["consent_obtained"] is True
        assert tiny["purpose"] == "styling_assessment"
        assert tiny["retention"] == "process_lifetime"

        informative = upload(client, scene["styling_session_id"], png(256, 256, informative=True), "informative.png")
        assert informative["visual_quality"] == "usable"
        look_v2 = expect(client.post("/look", json={
            "created_from": "user_revision",
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "parent_version_id": look_v1["look_version_id"],
            "item_ids": look_v1["item_ids"],
            "asset_ids": [informative["asset_id"]],
        }))
        assert look_v2["parent_version_id"] == look_v1["look_version_id"]
        degraded = expect(client.post("/scorecard", json={
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": look_v2["look_version_id"],
        }))
        assert degraded["evidence_status"] == "vision_degraded"
        assert_qualitative(degraded)

        services = app.state.services
        services.vision.settings = replace(
            settings,
            cpa_text_enabled=True,
            cpa_base_url="http://cpa.invalid/v1",
            cpa_api_key=None,
        )
        services.vision.set_transport(mock_transport(vision_payload()))
        score = expect(client.post("/scorecard", json={
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": look_v2["look_version_id"],
            "asset_ids": [informative["asset_id"]],
        }))
        assert score["numeric_score_available"] is True and score["total_score"] is not None
        assert score["target"] == "当前穿搭×当前目标"
        assert len(score["dimensions"]) == 6
        assert all(value is False for value in score["prohibited_subject_checks"].values())
        assert 1 <= len(score["priority_adjustments"]) <= 2
        proposal = score["priority_adjustments"][0]
        assert proposal["evidence_source"] == "verified_visual"
        assert proposal["evidence_regions"]
        assert proposal["scorecard_id"] == score["scorecard_id"]

        score_trace = expect(client.get(f"/trace/{score['trace_id']}", params={"user_id": "u01"}))["trace"]
        trace_text = json.dumps(score_trace, ensure_ascii=False)
        assert score["scorecard_id"] in trace_text
        assert informative["asset_id"] in trace_text
        assert "data:image" not in trace_text and "base64" not in trace_text.lower()

        unbound = upload(client, scene["styling_session_id"], png(256, 256, informative=True), "unbound.png")
        assert client.post("/scorecard", json={
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": look_v2["look_version_id"],
            "asset_ids": [unbound["asset_id"]],
        }).status_code == 422
        assert client.post("/scorecard", json={
            "user_id": "u02",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": look_v2["look_version_id"],
        }).status_code == 404

        for label, payload, status in (
            ("model_mismatch", vision_payload(model="grok-4.5"), 200),
            ("unsafe", vision_payload(unsafe=True), 200),
            ("partial", vision_payload(partial=True), 200),
            ("http_error", None, 503),
        ):
            services.vision.set_transport(mock_transport(payload, status=status))
            card = expect(client.post("/scorecard", json={
                "user_id": "u01",
                "styling_session_id": scene["styling_session_id"],
                "look_version_id": look_v2["look_version_id"],
            }))
            assert_qualitative(card)
            summary[label] = card["evidence_status"]

        services.vision.set_transport(mock_transport(vision_payload()))
        score_again = expect(client.post("/scorecard", json={
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": look_v2["look_version_id"],
        }))
        same_canonical = next(item for item in score_again["priority_adjustments"] if item["canonical_action"] == proposal["canonical_action"])
        rejection = expect(client.post("/adjust", json={
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": look_v2["look_version_id"],
            "adjustment_id": same_canonical["adjustment_id"],
            "decision": "reject",
            "reason": "不喜欢露脚踝",
        }))
        assert rejection["look_version"] is None
        decision_id = rejection["decision_record"]["decision_record_id"]
        decision_trace = expect(client.get(f"/trace/{rejection['decision_record']['trace_id']}", params={"user_id": "u01"}))["trace"]
        assert decision_id in json.dumps(decision_trace, ensure_ascii=False)
        assert client.post("/adjust", json={
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": look_v2["look_version_id"],
            "adjustment_id": same_canonical["adjustment_id"],
            "decision": "reject",
        }).status_code == 422
        after_reject = expect(client.post("/scorecard", json={
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": look_v2["look_version_id"],
        }))
        assert proposal["canonical_action"] not in {item["canonical_action"] for item in after_reject["priority_adjustments"]}
        chain = expect(client.get("/look", params={"user_id": "u01", "styling_session_id": scene["styling_session_id"]}))
        assert any(item["decision_record_id"] == decision_id for item in chain["adjustment_decisions"])
        assert client.get("/look", params={"user_id": "u02", "styling_session_id": scene["styling_session_id"]}).status_code == 404

        look_v3 = expect(client.post("/look", json={
            "created_from": "user_revision",
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "parent_version_id": look_v2["look_version_id"],
            "item_ids": look_v2["item_ids"],
            "asset_ids": [unbound["asset_id"]],
        }))
        assert proposal["canonical_action"] in look_v3["rejected_adjustments"]
        rollback = expect(client.post("/look", json={
            "created_from": "rollback",
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "parent_version_id": look_v3["look_version_id"],
            "rollback_target_version_id": look_v1["look_version_id"],
        }))
        assert rollback["version_index"] == look_v3["version_index"] + 1
        assert rollback["parent_version_id"] == look_v3["look_version_id"]
        assert rollback["rollback_target_version_id"] == look_v1["look_version_id"]
        assert look_v1["asset_ids"] == []

        deleted = expect(client.delete(f"/assets/{informative['asset_id']}", params={"user_id": "u01", "styling_session_id": scene["styling_session_id"]}))
        assert deleted["deleted_id"] == informative["asset_id"] and deleted["storage_deleted"] is True
        assert client.post("/scorecard", json={
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": look_v2["look_version_id"],
        }).status_code == 422
        assert client.post("/look", json={
            "created_from": "rollback",
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "parent_version_id": rollback["look_version_id"],
            "rollback_target_version_id": look_v2["look_version_id"],
        }).status_code == 422
        assert client.delete(f"/assets/{unbound['asset_id']}", params={"user_id": "u02", "styling_session_id": scene["styling_session_id"]}).status_code == 404

        final = expect(client.post("/finalize", json={
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": rollback["look_version_id"],
            "satisfied": True,
            "satisfaction": 1,
            "reason": "这就是我想要的",
        }))
        assert final["satisfaction"] == 1 and final["advice_stopped"] is True
        assert client.post("/adjust", json={
            "user_id": "u01",
            "styling_session_id": scene["styling_session_id"],
            "look_version_id": rollback["look_version_id"],
            "decision": "user_modified",
            "item_ids": rollback["item_ids"],
        }).status_code == 409

        # AC-07 causal memory: server derives a grounded shoe target.
        seed_scene = expect(client.post("/scene/parse", json={
            "user_id": "u02",
            "styling_session_id": "session_feedback_seed",
            "query_text": "两周后参加轻松的校园活动，先用现有衣橱给我方向",
        }))
        seed_rec = expect(client.post("/recommend", json={"request_id": seed_scene["request_id"]}))
        u02_wardrobe = expect(client.get("/wardrobe", params={"user_id": "u02"}))["items"]
        u02_map = {item["garment_id"]: item for item in u02_wardrobe}
        seed_outfit = seed_rec["outfits"][0]
        target_shoe = next(item_id for item_id in seed_outfit["items"] if u02_map[item_id]["slot"] == "shoes")
        feedback = expect(client.post("/recommend/feedback", json={
            "user_id": "u02",
            "styling_session_id": seed_scene["styling_session_id"],
            "request_id": seed_scene["request_id"],
            "outfit_id": seed_outfit["outfit_id"],
            "decision": "dislike",
            "reason_code": "long_walk_shoes",
            "memory_scope": "propose",
        }))
        proposal_id = feedback["memory_proposal"]["proposal_id"]
        assert feedback["record"] is None and feedback["memory_proposal"]["status"] == "proposed"

        related_query = "下周要走很多路并长时间站立，想穿得轻松舒服"
        before = expect(client.post("/scene/parse", json={
            "user_id": "u02", "styling_session_id": "session_feedback_before", "query_text": related_query,
        }))
        assert target_shoe not in before["constraints"]["excluded_items"]
        before_rec = expect(client.post("/recommend", json={"request_id": before["request_id"]}))
        before_ids = {item_id for outfit in before_rec["outfits"] for item_id in outfit["items"]}

        confirmed = expect(client.post(f"/memory/{proposal_id}/confirm", json={"user_id": "u02", "decision": "confirm"}))
        memory_id = confirmed["record"]["memory_id"]
        assert confirmed["proposal"]["content"] is None
        memory_view = expect(client.get("/memory", params={"user_id": "u02"}))
        memory_text = json.dumps(memory_view, ensure_ascii=False)
        assert target_shoe not in memory_text and memory_view["retrieval_index_count"] == 1

        after = expect(client.post("/scene/parse", json={
            "user_id": "u02", "styling_session_id": "session_feedback_after", "query_text": related_query,
        }))
        assert target_shoe in after["constraints"]["excluded_items"]
        after_rec = expect(client.post("/recommend", json={"request_id": after["request_id"]}))
        after_ids = {item_id for outfit in after_rec["outfits"] for item_id in outfit["items"]}
        assert target_shoe not in after_ids
        assert target_shoe in before_ids or before_ids != after_ids
        after_trace = expect(client.get(f"/trace/{after['trace_id']}", params={"user_id": "u02"}))["trace"]
        after_trace_text = json.dumps(after_trace, ensure_ascii=False)
        assert target_shoe not in after_trace_text
        assert "偏好久走" not in after_trace_text
        assert client.get(f"/trace/{after['trace_id']}", params={"user_id": "u01"}).status_code == 404

        unrelated = expect(client.post("/scene/parse", json={
            "user_id": "u02", "styling_session_id": "session_feedback_unrelated", "query_text": "今天在家休息，想穿得轻松",
        }))
        assert "long_walk" not in unrelated["constraints"]["comfort_notes"]
        assert target_shoe not in unrelated["constraints"]["excluded_items"]

        expect(client.delete(f"/memory/{memory_id}", params={"user_id": "u02"}))
        memory_after_delete = expect(client.get("/memory", params={"user_id": "u02"}))
        assert memory_after_delete["records"] == [] and memory_after_delete["proposals"] == []
        restored = expect(client.post("/scene/parse", json={
            "user_id": "u02", "styling_session_id": "session_feedback_restored", "query_text": related_query,
        }))
        assert target_shoe not in restored["constraints"]["excluded_items"]
        restored_rec = expect(client.post("/recommend", json={"request_id": restored["request_id"]}))
        restored_ids = {item_id for outfit in restored_rec["outfits"] for item_id in outfit["items"]}
        assert target_shoe in restored_ids or restored_ids != after_ids

        sensitive = expect(client.post("/memory/propose", json={
            "user_id": "u01",
            "namespace": "stylist",
            "type": "profile_stable",
            "content": "我患有糖尿病和焦虑症，电话是13800138000",
        }))
        assert sensitive["proposal"]["content"] is None
        assert sensitive["proposal"]["commit_blocked"] is True
        blocked = expect(client.post(f"/memory/{sensitive['proposal']['proposal_id']}/confirm", json={"user_id": "u01", "decision": "confirm"}))
        assert blocked["record"] is None and blocked["proposal"]["status"] == "rejected"
        sensitive_trace = expect(client.get(f"/trace/{blocked['trace_id']}", params={"user_id": "u01"}))["trace"]
        sensitive_trace_text = json.dumps(sensitive_trace, ensure_ascii=False)
        assert "糖尿病" not in sensitive_trace_text and "13800138000" not in sensitive_trace_text

        # SAFE-04 is fail-closed by type and controlled semantics, not by an
        # endlessly growing sensitive-word denylist.
        for memory_type, content in (
            ("sensitive", "隐私内容"),
            ("preference", "我有肾病"),
            ("preference", "我是维吾尔族"),
            ("preference", "隐私内容"),
        ):
            guarded = expect(client.post("/memory/propose", json={
                "user_id": "u01",
                "namespace": "stylist",
                "type": memory_type,
                "content": content,
            }))
            guarded_text = json.dumps(guarded, ensure_ascii=False)
            assert content not in guarded_text
            assert guarded["proposal"]["content"] is None
            assert guarded["proposal"]["commit_blocked"] is True
            guarded_confirm = expect(client.post(
                f"/memory/{guarded['proposal']['proposal_id']}/confirm",
                json={"user_id": "u01", "decision": "confirm"},
            ))
            assert guarded_confirm["record"] is None
            assert guarded_confirm["proposal"]["status"] == "rejected"

        unknown_type = client.post("/memory/propose", json={
            "user_id": "u01",
            "namespace": "stylist",
            "type": "arbitrary_private_bucket",
            "content": "不应在校验错误中回显的原文",
        })
        assert unknown_type.status_code == 422
        assert "不应在校验错误中回显的原文" not in unknown_type.text

        safe_to_edit = expect(client.post("/memory/propose", json={
            "user_id": "u01",
            "namespace": "stylist",
            "type": "constraint",
            "content": "不穿高跟鞋",
        }))
        edited_block = expect(client.post(
            f"/memory/{safe_to_edit['proposal']['proposal_id']}/confirm",
            json={
                "user_id": "u01",
                "decision": "edit",
                "edited_content": "隐私内容",
            },
        ))
        assert edited_block["record"] is None
        assert edited_block["proposal"]["content"] is None
        assert edited_block["proposal"]["commit_blocked"] is True
        edited_trace = expect(client.get(
            f"/trace/{edited_block['trace_id']}", params={"user_id": "u01"}
        ))["trace"]
        assert "隐私内容" not in json.dumps(edited_trace, ensure_ascii=False)

        all_traces = expect(client.get("/trace", params={"user_id": "u01", "styling_session_id": scene["styling_session_id"]}))
        all_trace_text = json.dumps(all_traces, ensure_ascii=False).lower()
        assert "data:image" not in all_trace_text
        assert "bearer " not in all_trace_text
        assert "sk-" not in all_trace_text

        # Clean remaining ephemeral assets through the owner-bound API.
        expect(client.delete(f"/assets/{tiny['asset_id']}", params={"user_id": "u01", "styling_session_id": scene["styling_session_id"]}))
        expect(client.delete(f"/assets/{unbound['asset_id']}", params={"user_id": "u01", "styling_session_id": scene["styling_session_id"]}))

        summary.update({
            "high_catalog_calls": high_trace["catalog"]["call_count"],
            "high_catalog_invocations": high_catalog_invocations,
            "grounded_outfits": len(recommendation["outfits"]),
            "score_dimensions": len(score["dimensions"]),
            "numeric_success": score["numeric_score_available"],
            "reject_replayed": True,
            "look_versions": len(expect(client.get("/look", params={"user_id": "u01", "styling_session_id": scene["styling_session_id"]}))["versions"]),
            "satisfaction_one_finalized": True,
            "memory_target": target_shoe,
            "memory_before_present": target_shoe in before_ids,
            "memory_after_absent": target_shoe not in after_ids,
            "memory_delete_restored": target_shoe in restored_ids,
            "trace_redaction": True,
        })

    # Sensitive/support text is classified locally before any CPA advisory.
    privacy_settings = replace(
        settings,
        cpa_text_enabled=True,
        cpa_base_url="http://cpa.invalid/v1",
    )
    privacy_app = create_app(privacy_settings)
    privacy_calls: list[str] = []

    async def privacy_advisory_spy(query_text: str):
        privacy_calls.append(query_text)
        return (
            {"intent": "recommend", "occasion": "commute", "goals": ["comfortable"]},
            {
                "attempted": True,
                "status": "ok",
                "requested_model": "grok4.5",
                "resolved_model": "grok-4.5-build",
                "transport_model": "grok-4.5-high",
                "model_verified": True,
            },
        )

    privacy_app.state.services.llm.parse_scene_advisory = privacy_advisory_spy
    blocked_queries = (
        "我对明天的汇报有点紧张，穿什么都觉得不对",
        "请评价我是不是太胖，并用衣服把所有身材缺点遮住",
        "我最近胸闷，应该穿什么衣服治疗",
        "我的邮箱是private@example.com，帮我搭配",
        "手机号13800138000，今天先聊聊心情",
    )
    with TestClient(privacy_app) as privacy_client:
        for index, query in enumerate(blocked_queries):
            blocked_scene = expect(privacy_client.post("/scene/parse", json={
                "user_id": "u01",
                "styling_session_id": f"session_privacy_{index}",
                "query_text": query,
            }))
            assert blocked_scene["backend"] == "rule_fallback"
            blocked_trace = expect(privacy_client.get(
                f"/trace/{blocked_scene['trace_id']}", params={"user_id": "u01"}
            ))["trace"]
            assert blocked_trace["provider"]["attempted"] is False
            assert blocked_trace["provider"]["status"] == "blocked"
            assert blocked_trace["provider"]["reason_code"] == (
                "LOCAL_SAFETY_OR_PRIVACY_SHORT_CIRCUIT"
            )
        assert privacy_calls == []
        ordinary_scene = expect(privacy_client.post("/scene/parse", json={
            "user_id": "u01",
            "styling_session_id": "session_privacy_ordinary",
            "query_text": "下周通勤想穿得简洁舒适",
        }))
        assert ordinary_scene["backend"] == "grok4.5"
        assert privacy_calls == ["下周通勤想穿得简洁舒适"]
    summary["privacy_blocked_cpa_calls"] = 0
    summary["ordinary_cpa_calls"] = len(privacy_calls)

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
