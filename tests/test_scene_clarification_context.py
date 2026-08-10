from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from profagent.app import create_app
from profagent.scene import SceneParser


def _expect(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("有点儿紧张", "vent"),
        ("第一次和女朋友约会，有点儿紧张", "recommend"),
        ("我对明天的汇报有点紧张，穿什么都觉得不对", "vent"),
        ("明天汇报有点紧张，帮我搭一套", "recommend"),
    ],
)
def test_nervousness_intent_boundary(query: str, expected: str) -> None:
    assert SceneParser._detect_intent(query)[0] == expected


def test_global_styling_helplessness_stays_local_support(
    offline_settings,
) -> None:
    app = create_app(replace(offline_settings, cpa_text_enabled=True))
    calls = 0

    async def forbidden_provider(_query_text: str):
        nonlocal calls
        calls += 1
        raise AssertionError("support-only query must not reach CPA")

    app.state.services.llm.parse_scene_advisory = forbidden_provider
    with TestClient(app) as client:
        scene = _expect(
            client.post(
                "/scene/parse",
                json={
                    "user_id": "u01",
                    "query_text": "我对明天的汇报有点紧张，穿什么都觉得不对",
                },
            )
        )
        assert calls == 0
        assert scene["intent"] == "vent"
        assert scene["occasion"] == "meeting"
        assert scene["event_horizon"] == "soon"
        recommendation = _expect(
            client.post("/recommend", json={"request_id": scene["request_id"]})
        )
        assert recommendation["outfits"] == []
        assert recommendation["shopping_suggestions"] == []
        trace = _expect(
            client.get(
                f"/trace/{scene['trace_id']}", params={"user_id": scene["user_id"]}
            )
        )["trace"]
        assert trace["provider"]["attempted"] is False
        assert (
            trace["provider"]["reason_code"]
            == "LOCAL_SAFETY_OR_PRIVACY_SHORT_CIRCUIT"
        )


def test_two_turn_deadline_clarification_inherits_styling_context(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    with TestClient(app) as client:
        first = _expect(
            client.post(
                "/scene/parse",
                json={
                    "user_id": "u01",
                    "query_text": "第一次和女朋友约会，有点儿紧张",
                },
            )
        )
        assert first["intent"] == "recommend"
        assert first["occasion"] == "date"
        assert first["event_horizon"] == "unknown"
        assert first["urgency"] == "high"
        assert first["shopping_allowed"] is False
        assert first["clarification_required"] is True
        assert first["clarification_question"]
        assert first["clarification_question"].count("？") <= 1

        second = _expect(
            client.post(
                "/scene/parse",
                json={
                    "user_id": "u01",
                    "styling_session_id": first["styling_session_id"],
                    "query_text": "明天",
                },
            )
        )
        assert second["intent"] == first["intent"] == "recommend"
        assert second["occasion"] == first["occasion"] == "date"
        assert second["goals"] == first["goals"]
        assert second["constraints"] == first["constraints"]
        assert second["event_horizon"] == "soon"
        assert second["urgency"] == "medium"
        assert second["clarification_required"] is False
        assert second["clarification_question"] is None

        recommendation = _expect(
            client.post("/recommend", json={"request_id": second["request_id"]})
        )
        assert recommendation["outfits"]
        assert "今天" not in recommendation["assistant_message"]
        assert recommendation["shopping_suggestions"] == []
        wardrobe_ids = {
            item["garment_id"]
            for item in _expect(client.get("/wardrobe", params={"user_id": "u01"}))[
                "items"
            ]
        }
        assert all(
            set(outfit["items"]).issubset(wardrobe_ids)
            for outfit in recommendation["outfits"]
        )
