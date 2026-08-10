from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from profagent.app import create_app


def _body(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def test_no_deadline_wardrobe_audit_fails_closed_before_catalog(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    actual_catalog_invocations: list[str] = []
    original_search = services.catalog.search

    def search_spy(scene):
        actual_catalog_invocations.append(scene.request_id)
        return original_search(scene)

    services.catalog.search = search_spy
    with TestClient(app) as client:
        scene = _body(
            client.post(
                "/scene/parse",
                json={
                    "user_id": "u01",
                    "query_text": (
                        "过滤衣橱库存并说明推荐依据：缺一双鞋，需要补购。"
                    ),
                },
            )
        )
        recommendation = _body(
            client.post("/recommend", json={"request_id": scene["request_id"]})
        )

        assert scene["event_horizon"] == "unknown"
        assert scene["urgency"] == "high"
        assert scene["shopping_allowed"] is False
        assert scene["ui_capabilities"]["shopping_cta"] is False
        assert recommendation["shopping_suggestions"] == []
        assert recommendation["ui_capabilities"]["shopping_cta"] is False
        assert actual_catalog_invocations == []

        trace = _body(
            client.get(
                f"/trace/{scene['trace_id']}", params={"user_id": scene["user_id"]}
            )
        )["trace"]
        assert trace["catalog"]["attempted"] is False
        assert trace["catalog"]["call_count"] == 0

        # The provider itself is the second independent gate if orchestration is
        # ever bypassed accidentally.
        authoritative = services.state.by_request(scene["request_id"])
        assert authoritative is not None
        forced = original_search(authoritative)
        assert forced.items == ()
        assert forced.call_count == 0
        assert forced.blocked_reason == "HIGH_URGENCY"


def test_structured_planned_horizon_is_preserved_without_text_deadline(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    with TestClient(app) as client:
        scene = _body(
            client.post(
                "/scene/parse",
                json={
                    "user_id": "u02",
                    "query_text": "过滤黄色和已预留衣物，再看可买候选",
                    "event_horizon": "planned",
                    "intent": "fill_gap",
                },
            )
        )

        assert scene["event_horizon"] == "planned"
        assert scene["urgency"] == "low"


def test_text_and_event_time_cannot_be_lowered_by_structured_horizon(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    soon = (datetime.now().astimezone() + timedelta(minutes=30)).isoformat()
    cases = (
        ("现在缺一双鞋，需要补购。", None, "now"),
        ("今天缺一双鞋，需要补购。", None, "today"),
        ("时间不确定，缺一双鞋，需要补购。", None, "unknown"),
        ("过滤衣橱库存，缺一双鞋，需要补购。", soon, "now"),
    )

    with TestClient(app) as client:
        for query, event_time, expected_horizon in cases:
            payload = {
                "user_id": "u01",
                "query_text": query,
                "event_horizon": "planned",
                "intent": "fill_gap",
            }
            if event_time is not None:
                payload["event_time"] = event_time
            scene = _body(client.post("/scene/parse", json=payload))

            assert scene["event_horizon"] == expected_horizon
            assert scene["urgency"] == "high"
            assert scene["shopping_allowed"] is False
            assert scene["ui_capabilities"]["shopping_cta"] is False
