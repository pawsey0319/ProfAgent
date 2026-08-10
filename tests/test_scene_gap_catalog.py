from __future__ import annotations

from fastapi.testclient import TestClient

from profagent.app import create_app


def _body(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def test_comparative_slot_need_is_bound_and_returns_grounded_catalog_items(
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
                    "user_id": "u02",
                    "query_text": "两周后会议需要更正式的上衣，可以购物",
                    "event_horizon": "planned",
                    "intent": "buy",
                },
            )
        )
        recommendation = _body(
            client.post("/recommend", json={"request_id": scene["request_id"]})
        )

        assert scene["constraints"]["gap_slots"] == ["top"]
        assert scene["urgency"] == "low"
        assert scene["shopping_allowed"] is True
        assert actual_catalog_invocations == [scene["request_id"]]
        assert recommendation["shopping_suggestions"]

        catalog = {item.item_id: item for item in services.repository.list_catalog()}
        returned_ids = {
            suggestion["item_id"]
            for suggestion in recommendation["shopping_suggestions"]
        }
        assert returned_ids <= set(catalog)
        assert all(catalog[item_id].slot == "top" for item_id in returned_ids)
        assert all(catalog[item_id].stock > 0 for item_id in returned_ids)
        assert all(catalog[item_id].formal >= 3 for item_id in returned_ids)
        assert all(
            catalog[item_id].synthetic is True
            and catalog[item_id].declaration == "合成模拟商品，非真实在售商品"
            for item_id in returned_ids
        )

        trace = _body(
            client.get(
                f"/trace/{scene['trace_id']}", params={"user_id": scene["user_id"]}
            )
        )["trace"]
        assert trace["catalog"]["attempted"] is True
        assert trace["catalog"]["call_count"] == 1
        assert trace["catalog"]["returned_count"] == len(returned_ids)


def test_inventory_filter_mentions_do_not_create_slot_gaps_or_call_catalog(
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
    cases = (
        (
            "u02",
            "过滤黄色和已预留衣物，再看可买候选",
            "fill_gap",
        ),
        ("u03", "橙色不适合我，无库存商品也不要", "buy"),
        ("u02", "商品没库存就不要推荐，衣橱也要可用", "buy"),
    )

    with TestClient(app) as client:
        for user_id, query, intent in cases:
            scene = _body(
                client.post(
                    "/scene/parse",
                    json={
                        "user_id": user_id,
                        "query_text": query,
                        "event_horizon": "planned",
                        "intent": intent,
                    },
                )
            )
            recommendation = _body(
                client.post("/recommend", json={"request_id": scene["request_id"]})
            )

            assert scene["constraints"]["gap_slots"] == []
            assert recommendation["shopping_suggestions"] == []
            trace = _body(
                client.get(
                    f"/trace/{scene['trace_id']}",
                    params={"user_id": scene["user_id"]},
                )
            )["trace"]
            assert trace["catalog"]["attempted"] is False
            assert trace["catalog"]["call_count"] == 0

        assert actual_catalog_invocations == []


def test_query_only_intent_is_slot_bound_for_e008_and_e013(offline_settings) -> None:
    app = create_app(offline_settings)
    with TestClient(app) as client:
        no_shop = _body(
            client.post(
                "/scene/parse",
                json={
                    "user_id": "u02",
                    "query_text": "行程还没定但需要快速预案，不要打开商品",
                },
            )
        )
        assert no_shop["event_horizon"] == "unknown"
        assert no_shop["intent"] == "recommend"
        assert no_shop["constraints"]["gap_slots"] == []
        assert no_shop["shopping_allowed"] is False

        comparative_gap = _body(
            client.post(
                "/scene/parse",
                json={
                    "user_id": "u02",
                    "query_text": "两周后会议需要更正式的上衣，可以购物",
                },
            )
        )
        assert comparative_gap["event_horizon"] == "planned"
        assert comparative_gap["intent"] == "fill_gap"
        assert comparative_gap["constraints"]["gap_slots"] == ["top"]
        assert comparative_gap["shopping_allowed"] is True

        generic_catalog = _body(
            client.post(
                "/scene/parse",
                json={
                    "user_id": "u02",
                    "query_text": "下月只检查商品库存，可以购物",
                    "event_horizon": "planned",
                    "intent": "buy",
                },
            )
        )
        assert generic_catalog["intent"] == "recommend"
        assert generic_catalog["constraints"]["gap_slots"] == []
        assert generic_catalog["shopping_allowed"] is False


def test_explicit_e011_e012_gaps_call_catalog_but_fail_closed_without_legal_fixture(
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
    cases = (
        (
            "u03",
            "下周运动课想看看低预算鞋子",
            "soon",
            "browse",
            "shoes",
            {
                "c005": "BUDGET_EXCEEDED",
                "c026": "BUDGET_EXCEEDED",
                "c047": "DELIVERY_BUFFER_MISSED",
                "c019": "OCCASION_MISMATCH",
                "c033": "OCCASION_MISMATCH",
            },
        ),
        (
            "u01",
            "月底聚会可能缺一个小包，先给候选",
            "planned",
            "fill_gap",
            "bag",
            {
                "c020": "OUT_OF_STOCK",
                "c006": "OCCASION_MISMATCH",
                "c027": "OCCASION_MISMATCH",
            },
        ),
    )

    with TestClient(app) as client:
        for user_id, query, horizon, intent, slot, expected_reasons in cases:
            scene = _body(
                client.post(
                    "/scene/parse",
                    json={
                        "user_id": user_id,
                        "query_text": query,
                        "event_horizon": horizon,
                        "intent": intent,
                    },
                )
            )
            recommendation = _body(
                client.post("/recommend", json={"request_id": scene["request_id"]})
            )
            assert scene["constraints"]["gap_slots"] == [slot]
            assert scene["shopping_allowed"] is True
            assert recommendation["shopping_suggestions"] == []

            trace = _body(
                client.get(
                    f"/trace/{scene['trace_id']}",
                    params={"user_id": scene["user_id"]},
                )
            )["trace"]
            assert trace["catalog"]["attempted"] is True
            assert trace["catalog"]["call_count"] == 1
            assert trace["catalog"]["returned_count"] == 0
            filtered = {
                row["item_id"]: row["reason"]
                for row in trace["catalog"]["filtered"]
            }
            assert all(
                filtered.get(item_id) == reason
                for item_id, reason in expected_reasons.items()
            )

        assert len(actual_catalog_invocations) == len(cases)


def test_global_no_catalog_phrase_overrides_explicit_gap_at_low_and_medium_urgency(
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
    cases = (
        (
            "下月会议缺一双鞋，但不要打开商品",
            "planned",
            "buy",
            "shoes",
            "low",
        ),
        (
            "下周通勤缺一个包，不要打开目录",
            "soon",
            "fill_gap",
            "bag",
            "medium",
        ),
    )

    with TestClient(app) as client:
        for query, horizon, supplied_intent, gap_slot, urgency in cases:
            scene = _body(
                client.post(
                    "/scene/parse",
                    json={
                        "user_id": "u01",
                        "query_text": query,
                        "event_horizon": horizon,
                        "intent": supplied_intent,
                    },
                )
            )
            assert scene["constraints"]["gap_slots"] == [gap_slot]
            assert scene["urgency"] == urgency
            assert scene["intent"] == "recommend"
            assert scene["shopping_allowed"] is False
            assert scene["ui_capabilities"]["shopping_cta"] is False

            recommendation = _body(
                client.post("/recommend", json={"request_id": scene["request_id"]})
            )
            assert recommendation["shopping_suggestions"] == []
            assert recommendation["ui_capabilities"]["shopping_cta"] is False
            assert actual_catalog_invocations == []

            trace = _body(
                client.get(
                    f"/trace/{scene['trace_id']}",
                    params={"user_id": scene["user_id"]},
                )
            )["trace"]
            assert trace["catalog"]["attempted"] is False
            assert trace["catalog"]["call_count"] == 0

            authoritative = services.state.by_request(scene["request_id"])
            assert authoritative is not None
            forced = original_search(authoritative)
            assert forced.items == ()
            assert forced.call_count == 0
            assert forced.blocked_reason == "SHOPPING_NOT_ALLOWED"
