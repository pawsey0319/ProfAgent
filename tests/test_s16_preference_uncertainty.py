from __future__ import annotations

import sqlite3
import math
from types import SimpleNamespace

from fastapi.testclient import TestClient

from profagent.app import create_app


def _install_dialogue_provider(app) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def reply(context):
        calls.append(context)
        return (
            {
                "reply": "我已按当前场景整理好可直接试的衣橱方向。",
                "action": context["policy"]["required_action"],
                "control": context["policy"]["required_control"],
                "scene_advisory": None,
                "suggested_replies": [],
            },
            {
                "resolved_model": "grok-4.6-high",
                "model_verified": True,
            },
        )

    app.state.services.llm.stylist_dialogue = reply
    return calls


def _turn(
    client: TestClient,
    *,
    message: str,
    request_id: str,
    session_id: str | None = None,
    question_id: str | None = None,
    option_id: str | None = None,
):
    payload = {
        "user_id": "u01",
        "message": message,
        "request_id": request_id,
    }
    if session_id is not None:
        payload["styling_session_id"] = session_id
    if question_id is not None:
        payload["preference_question_id"] = question_id
    if option_id is not None:
        payload["preference_option_id"] = option_id
    return client.post("/dialogue/turn", json=payload)


def _outfit_item_sets(body: dict) -> set[frozenset[str]]:
    return {
        frozenset(outfit["items"])
        for outfit in body["recommendation"]["outfits"]
    }


def _scored_outfit(
    outfit_id: str,
    item_id: str,
    score: float | None,
    *,
    valid: bool = True,
):
    from profagent.models import OutfitValidation, RecommendedOutfit

    constructor_score = (
        score
        if score is None or (math.isfinite(score) and score >= 0)
        else 0.0
    )
    outfit = RecommendedOutfit(
        outfit_id=outfit_id,
        strategy_label=outfit_id,
        items=[item_id],
        reasons=[],
        risks=[],
        alternatives={},
        is_primary=False,
        trust_statement="grounded",
        validation=OutfitValidation(
            all_ids_grounded=valid,
            hard_constraints_passed=valid,
            required_slots_complete=valid,
        ),
        server_ranking_score=constructor_score,
    )
    # Exercise policy fail-closed behavior against corrupted internal evidence
    # that has bypassed normal Pydantic construction.
    outfit.server_ranking_score = score
    return outfit


def _color_policy():
    from profagent.preference_uncertainty import PreferenceUncertaintyPolicy

    garments = {
        "navy": SimpleNamespace(color="navy"),
        "beige": SimpleNamespace(color="beige"),
        "black": SimpleNamespace(color="black"),
        "red": SimpleNamespace(color="red"),
    }
    return PreferenceUncertaintyPolicy(SimpleNamespace(get_garment=garments.get))


def test_asks_once_when_two_legal_outfits_differ_on_unremembered_color() -> None:
    from profagent.preference_uncertainty import (
        PreferenceUncertaintyEvidence,
        PreferenceUncertaintyPolicy,
    )

    evidence = PreferenceUncertaintyEvidence(
        gap_code="color:navy_vs_beige",
        neutral_margin=0.03,
        counterfactual_winners=("outfit_navy", "outfit_beige"),
        urgency="low",
        confirmed=False,
        already_asked=False,
    )
    assert PreferenceUncertaintyPolicy().should_ask(evidence) is True


def test_does_not_ask_when_confirmed_color_preference_exists() -> None:
    from profagent.preference_uncertainty import (
        PreferenceUncertaintyEvidence,
        PreferenceUncertaintyPolicy,
    )

    evidence = PreferenceUncertaintyEvidence(
        gap_code="color:navy_vs_beige",
        neutral_margin=0.03,
        counterfactual_winners=("outfit_navy", "outfit_beige"),
        urgency="low",
        confirmed=True,
        already_asked=False,
    )
    assert PreferenceUncertaintyPolicy().should_ask(evidence) is False


def test_high_urgency_second_question_is_blocked() -> None:
    from profagent.preference_uncertainty import (
        PreferenceUncertaintyEvidence,
        PreferenceUncertaintyPolicy,
    )

    evidence = PreferenceUncertaintyEvidence(
        gap_code="color:navy_vs_beige",
        neutral_margin=0.03,
        counterfactual_winners=("outfit_navy", "outfit_beige"),
        urgency="high",
        confirmed=False,
        already_asked=True,
    )
    assert PreferenceUncertaintyPolicy().should_ask(evidence) is False


def test_policy_excludes_invalid_outfit_before_margin_and_counterfactuals() -> None:
    from profagent.models import OutfitValidation, RecommendedOutfit
    from profagent.preference_uncertainty import PreferenceUncertaintyPolicy

    def outfit(
        outfit_id: str, item_id: str, score: float | None, valid: bool
    ):
        return RecommendedOutfit(
            outfit_id=outfit_id,
            strategy_label=outfit_id,
            items=[item_id],
            reasons=[],
            risks=[],
            alternatives={},
            is_primary=False,
            trust_statement="grounded",
            validation=OutfitValidation(
                all_ids_grounded=valid,
                hard_constraints_passed=valid,
                required_slots_complete=valid,
            ),
            server_ranking_score=score,
        )

    garments = {
        "invalid": SimpleNamespace(color="red"),
        "navy": SimpleNamespace(color="navy"),
        "beige": SimpleNamespace(color="beige"),
    }
    policy = PreferenceUncertaintyPolicy(
        SimpleNamespace(get_garment=garments.get)
    )
    evidence = policy.build_evidence(
        scene=SimpleNamespace(urgency="low"),
        recommendation=SimpleNamespace(
            outfits=[
                outfit("invalid_winner", "invalid", 99.0, False),
                outfit("outfit_navy", "navy", 1.0, True),
                outfit("outfit_beige", "beige", 0.97, True),
            ]
        ),
        confirmed_signals=(),
        session_preferences=(),
        asked_gap_codes=frozenset(),
    )
    assert evidence is not None
    assert evidence.counterfactual_winners == (
        "outfit_navy",
        "outfit_beige",
    )
    assert "invalid_winner" not in evidence.counterfactual_winners
    assert (
        policy.build_evidence(
            scene=SimpleNamespace(urgency="low"),
            recommendation=SimpleNamespace(
                outfits=[
                    outfit("outfit_navy", "navy", None, True),
                    outfit("outfit_beige", "beige", 0.97, True),
                ]
            ),
            confirmed_signals=(),
            session_preferences=(),
            asked_gap_codes=frozenset(),
        )
        is None
    )


def test_nonmaterial_margin_and_unchanged_counterfactual_do_not_ask() -> None:
    from profagent.preference_uncertainty import (
        PreferenceUncertaintyEvidence,
        PreferenceUncertaintyPolicy,
    )

    wide = PreferenceUncertaintyEvidence(
        gap_code="color:navy_vs_beige",
        neutral_margin=0.051,
        counterfactual_winners=("outfit_navy", "outfit_beige"),
        urgency="low",
        confirmed=False,
        already_asked=False,
    )
    unchanged = PreferenceUncertaintyEvidence(
        gap_code="color:navy_vs_beige",
        neutral_margin=0.01,
        counterfactual_winners=("outfit_navy", "outfit_navy"),
        urgency="low",
        confirmed=False,
        already_asked=False,
    )
    assert PreferenceUncertaintyPolicy.should_ask(wide) is False
    assert PreferenceUncertaintyPolicy.should_ask(unchanged) is False


def test_policy_sorts_real_top_two_by_score_not_display_order() -> None:
    policy = _color_policy()
    evidence = policy.build_evidence(
        scene=SimpleNamespace(urgency="low"),
        recommendation=SimpleNamespace(
            outfits=[
                _scored_outfit("outfit_low", "black", 0.10),
                _scored_outfit("outfit_high", "navy", 0.90),
                _scored_outfit("outfit_mid", "beige", 0.87),
            ]
        ),
        confirmed_signals=(),
        session_preferences=(),
        asked_gap_codes=frozenset(),
    )
    assert evidence is not None
    assert evidence.gap_code == "color:navy_vs_beige"
    assert evidence.counterfactual_winners == (
        "outfit_high",
        "outfit_mid",
    )


def test_margin_boundary_ties_zero_less_than_two_and_invalid_scores_fail_closed() -> None:
    policy = _color_policy()

    def evidence(scores: list[tuple[str, str, float | None]]):
        return policy.build_evidence(
            scene=SimpleNamespace(urgency="low"),
            recommendation=SimpleNamespace(
                outfits=[
                    _scored_outfit(outfit_id, color, score)
                    for outfit_id, color, score in scores
                ]
            ),
            confirmed_signals=(),
            session_preferences=(),
            asked_gap_codes=frozenset(),
        )

    at_boundary = evidence(
        [("outfit_navy", "navy", 0.75), ("outfit_beige", "beige", 0.70)]
    )
    over_boundary = evidence(
        [("outfit_navy", "navy", 0.751), ("outfit_beige", "beige", 0.70)]
    )
    assert at_boundary is not None
    assert policy.should_ask(at_boundary) is True
    assert over_boundary is not None
    assert policy.should_ask(over_boundary) is False

    tie = evidence(
        [("z_navy", "navy", 0.0), ("a_beige", "beige", 0.0)]
    )
    assert tie is not None
    assert tie.gap_code == "color:beige_vs_navy"
    assert evidence([("only", "navy", 0.5)]) is None
    for invalid in (None, math.nan, math.inf, -0.01, 1.01):
        assert (
            evidence(
                [("outfit_navy", "navy", invalid), ("outfit_beige", "beige", 0.5)]
            )
            is None
        )
    corrupt_recommendation = SimpleNamespace(
        outfits=[
            _scored_outfit("outfit_navy", "navy", math.nan),
            _scored_outfit("outfit_beige", "beige", 0.5),
        ]
    )
    decision = policy.evaluate(
        scene=SimpleNamespace(urgency="low"),
        recommendation=corrupt_recommendation,
        confirmed_signals=(),
        session_preferences=(),
        asked_gap_codes=frozenset(),
    )
    assert decision.clarification is None
    assert decision.reason_code == "INVALID_SERVER_RANKING_EVIDENCE"


def test_assembler_source_normalization_is_pair_independent_and_bounded() -> None:
    from profagent.retrieval import OutfitAssembler

    upper_three = OutfitAssembler._combo_score_upper_bound(3)
    upper_four = OutfitAssembler._combo_score_upper_bound(4)
    assert upper_three == 3 * (1.0 + 0.03) + 9 * 0.02
    assert upper_four == 4 * (1.0 + 0.03) + 9 * 0.02
    assert OutfitAssembler._normalize_combo_score(upper_three * 0.8, 3) == 0.8
    assert OutfitAssembler._normalize_combo_score(upper_four * 0.8, 4) == 0.8
    assert OutfitAssembler._normalize_combo_score(0.0, 3) == 0.0
    assert OutfitAssembler._normalize_combo_score(upper_three, 3) == 1.0


def test_policy_consumes_real_validator_output_not_self_declared_flags(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "preference_real_validator.sqlite3"
    app = create_app(
        offline_settings.__class__(
            **{
                **offline_settings.__dict__,
                "database_url": f"sqlite:///{path.as_posix()}",
            }
        )
    )
    _install_dialogue_provider(app)
    with TestClient(app) as client:
        body = _turn(
            client,
            message="下周通勤帮我搭两套",
            request_id="pref_validator_01",
        ).json()
    scene = app.state.services.scene_parser.state.by_request(body["request_id"])
    assert scene is not None
    _, recommendation = app.state.services.recommendations.get_saved_recommendation(
        "u01", body["styling_session_id"], body["request_id"]
    )
    foreign_id = next(
        iter(
            app.state.services.repository.garment_ids("u02")
            - app.state.services.repository.garment_ids("u01")
        )
    )
    forged = recommendation.outfits[0].model_copy(deep=True)
    forged.outfit_id = "forged_self_declared_valid"
    forged.items = [foreign_id]
    accepted, validator_trace = app.state.services.recommendations.validator.validate(
        scene,
        [forged, *[outfit.model_copy(deep=True) for outfit in recommendation.outfits]],
        app.state.services.repository.garment_ids("u01"),
    )
    assert validator_trace["rejected_reason_counts"]["NON_WHITELIST_ID"] == 1
    assert all(outfit.outfit_id != forged.outfit_id for outfit in accepted)
    decision = app.state.services.dialogue.preference_policy.evaluate(
        scene=scene,
        recommendation=recommendation.model_copy(update={"outfits": accepted}),
        confirmed_signals=(),
        session_preferences=(),
        asked_gap_codes=frozenset(),
    )
    assert "forged_self_declared_valid" not in decision.reordered_outfit_ids


def test_applicable_confirmed_or_session_preference_suppresses_question() -> None:
    from profagent.memory_candidates import SessionPreference
    from profagent.memory_service import MemorySignal
    from profagent.models import OutfitValidation, RecommendedOutfit
    from profagent.preference_uncertainty import PreferenceUncertaintyPolicy

    def outfit(outfit_id: str, item_id: str, score: float):
        return RecommendedOutfit(
            outfit_id=outfit_id,
            strategy_label=outfit_id,
            items=[item_id],
            reasons=[],
            risks=[],
            alternatives={},
            is_primary=False,
            trust_statement="grounded",
            validation=OutfitValidation(
                all_ids_grounded=True,
                hard_constraints_passed=True,
                required_slots_complete=True,
            ),
            server_ranking_score=score,
        )

    garments = {
        "navy": SimpleNamespace(color="navy"),
        "beige": SimpleNamespace(color="beige"),
    }
    policy = PreferenceUncertaintyPolicy(
        SimpleNamespace(get_garment=garments.get)
    )
    recommendation = SimpleNamespace(
        outfits=[
            outfit("outfit_navy", "navy", 1.0),
            outfit("outfit_beige", "beige", 0.97),
        ]
    )
    scene = SimpleNamespace(urgency="low")
    confirmed = policy.evaluate(
        scene=scene,
        recommendation=recommendation,
        confirmed_signals=(
            MemorySignal(
                memory_id="mem_confirmed",
                namespace="stylist",
                type="preference",
                applied_signal="prefer_color:navy",
            ),
        ),
        session_preferences=(),
        asked_gap_codes=frozenset(),
    )
    session = policy.evaluate(
        scene=scene,
        recommendation=recommendation,
        confirmed_signals=(),
        session_preferences=(
            SessionPreference(
                user_id="u01",
                styling_session_id="session_pref",
                canonical_kind="color_preference",
                canonical_value="beige",
                expires_at=9999999999.0,
            ),
        ),
        asked_gap_codes=frozenset(),
    )
    assert confirmed.clarification is None
    assert confirmed.reason_code == "APPLICABLE_PREFERENCE_PRESENT"
    assert session.clarification is None
    assert session.reason_code == "APPLICABLE_PREFERENCE_PRESENT"


def test_dialogue_material_question_is_advisory_and_keeps_valid_recommendation(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "preference_question.sqlite3"
    app = create_app(
        offline_settings.__class__(
            **{
                **offline_settings.__dict__,
                "database_url": f"sqlite:///{path.as_posix()}",
            }
        )
    )
    calls = _install_dialogue_provider(app)
    with TestClient(app) as client:
        response = _turn(
            client,
            message="下周通勤帮我搭两套",
            request_id="pref_question_01",
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["action"] == "recommend"
        assert body["recommendation_paused"] is False
        assert len(body["recommendation"]["outfits"]) == 2
        assert body["preference_clarification"] is not None
        assert len(body["preference_clarification"]["options"]) == 3
        assert body["memory_candidates"] == []
        assert len(calls) == 1
        owned = app.state.services.repository.garment_ids("u01")
        for outfit in body["recommendation"]["outfits"]:
            assert "server_ranking_score" not in outfit
            assert set(outfit["items"]).issubset(owned)
            assert outfit["validation"] == {
                "all_ids_grounded": True,
                "hard_constraints_passed": True,
                "required_slots_complete": True,
            }
        trace = app.state.services.traces.get(body["trace_id"])
        assert trace is not None
        assert trace.catalog["call_count"] == 0
        preference_trace = trace.dialogue["preference_uncertainty"]
        serialized_preference_trace = str(preference_trace)
        assert "neutral_margin" not in serialized_preference_trace
        assert "weights" not in serialized_preference_trace


def test_explicit_answer_reorders_same_grounded_sets_and_emits_one_decidable_card(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "preference_answer.sqlite3"
    settings = offline_settings.__class__(
        **{
            **offline_settings.__dict__,
            "database_url": f"sqlite:///{path.as_posix()}",
        }
    )
    app = create_app(settings)
    calls = _install_dialogue_provider(app)
    with TestClient(app) as client:
        first = _turn(
            client,
            message="下周通勤帮我搭两套",
            request_id="pref_answer_01",
        ).json()
        question = first["preference_clarification"]
        option = question["options"][1]
        before_sets = _outfit_item_sets(first)
        before_primary = first["recommendation"]["outfits"][0]["outfit_id"]
        cross_owner = client.post(
            "/dialogue/turn",
            json={
                "user_id": "u02",
                "message": option["label"],
                "request_id": "pref_answer_cross_owner",
                "styling_session_id": first["styling_session_id"],
                "preference_question_id": question["question_id"],
                "preference_option_id": option["option_id"],
            },
        )
        assert cross_owner.status_code == 404
        app.state.services.recommendations.recommend = lambda _scene: (_ for _ in ()).throw(
            AssertionError("preference answer must not rerun recommendation tools")
        )
        answer = _turn(
            client,
            message=option["label"],
            request_id="pref_answer_02",
            session_id=first["styling_session_id"],
            question_id=question["question_id"],
            option_id=option["option_id"],
        )
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["action"] == "recommend"
        assert body["recommendation_paused"] is False
        assert _outfit_item_sets(body) == before_sets
        assert sum(
            outfit["is_primary"] for outfit in body["recommendation"]["outfits"]
        ) == 1
        assert body["recommendation"]["outfits"][0]["outfit_id"] != before_primary
        assert len(body["memory_candidates"]) == 1
        assert body["preference_clarification"] is None
        assert len(calls) == 2
        answered_envelope = app.state.services.dialogue.state.session(
            first["styling_session_id"], "u01"
        )
        assert answered_envelope.asked_preference_gap_codes
        assert answered_envelope.pending_preference_question_id is None
        answer_trace = app.state.services.traces.get(body["trace_id"])
        assert answer_trace is not None
        preference_trace = answer_trace.dialogue["preference_uncertainty"]
        assert option["label"] not in str(preference_trace)
        assert "neutral_margin" not in str(preference_trace)
        connection = sqlite3.connect(path)
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_records"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_outbox"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_candidates"
        ).fetchone()[0] == 1
        connection.close()

        card = body["memory_candidates"][0]
        decided = client.post(
            f"/memory/candidates/{card['candidate_id']}/decide",
            json={
                "user_id": "u01",
                "styling_session_id": first["styling_session_id"],
                "decision": "remember",
                "idempotency_key": "pref_answer_commit_01",
            },
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "committed"
        connection = sqlite3.connect(path)
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_records"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_outbox"
        ).fetchone()[0] == 1
        connection.close()


def test_neutral_answer_and_unanswered_followup_do_not_ask_or_write_memory(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "preference_neutral.sqlite3"
    settings = offline_settings.__class__(
        **{
            **offline_settings.__dict__,
            "database_url": f"sqlite:///{path.as_posix()}",
        }
    )
    app = create_app(settings)
    _install_dialogue_provider(app)
    with TestClient(app) as client:
        first = _turn(
            client,
            message="下周通勤帮我搭两套",
            request_id="pref_neutral_01",
        ).json()
        question = first["preference_clarification"]
        neutral = question["options"][2]
        answer = _turn(
            client,
            message="你决定",
            request_id="pref_neutral_02",
            session_id=first["styling_session_id"],
            question_id=question["question_id"],
            option_id=neutral["option_id"],
        )
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["memory_candidates"] == []
        assert body["preference_clarification"] is None
        assert _outfit_item_sets(body) == _outfit_item_sets(first)
        neutral_envelope = app.state.services.dialogue.state.session(
            first["styling_session_id"], "u01"
        )
        assert neutral_envelope.asked_preference_gap_codes
        assert neutral_envelope.pending_preference_question_id is None
        first_sets = [
            frozenset(outfit["items"])
            for outfit in first["recommendation"]["outfits"][:2]
        ]
        policy = app.state.services.dialogue.preference_policy
        original_colors = policy._colors

        def changed_gap_colors(item_ids):
            item_set = frozenset(item_ids)
            if item_set == first_sets[0]:
                return frozenset({"black"})
            if item_set == first_sets[1]:
                return frozenset({"red"})
            return original_colors(item_ids)

        policy._colors = changed_gap_colors
        followup = _turn(
            client,
            message="还是按刚才的场景继续",
            request_id="pref_neutral_03",
            session_id=first["styling_session_id"],
        )
        assert followup.status_code == 200, followup.text
        assert followup.json()["preference_clarification"] is None
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_candidates"
    ).fetchone()[0] == 0
    connection.close()


def test_high_urgency_question_budget_and_retry_do_not_duplicate_cpa_or_candidate(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "preference_high_retry.sqlite3"
    settings = offline_settings.__class__(
        **{
            **offline_settings.__dict__,
            "database_url": f"sqlite:///{path.as_posix()}",
        }
    )
    app = create_app(settings)
    calls = _install_dialogue_provider(app)
    catalog_calls: list[str] = []

    def forbidden_catalog_search(_scene):
        catalog_calls.append("search")
        raise AssertionError("high urgency must not enter Catalog.search")

    app.state.services.catalog.search = forbidden_catalog_search
    memory_candidate_operations_before = (
        app.state.services.llm.memory_candidate_operation_count
    )
    with TestClient(app) as client:
        first_response = _turn(
            client,
            message="今晚通勤，帮我搭两套",
            request_id="pref_high_01",
        )
        assert first_response.status_code == 200, first_response.text
        first = first_response.json()
        assert first["scene"]["urgency"] == "high"
        assert first["scene"]["shopping_allowed"] is False
        question = first["preference_clarification"]
        assert question is not None
        option = question["options"][0]
        payload = dict(
            message=option["label"],
            request_id="pref_high_02",
            session_id=first["styling_session_id"],
            question_id=question["question_id"],
            option_id=option["option_id"],
        )
        answer = _turn(client, **payload)
        replay = _turn(client, **payload)
        assert answer.status_code == replay.status_code == 200
        assert answer.json() == replay.json()
        assert answer.json()["preference_clarification"] is None
        assert len(answer.json()["memory_candidates"]) == 1
        assert len(calls) == 2
        assert catalog_calls == []
        assert (
            app.state.services.llm.memory_candidate_operation_count
            == memory_candidate_operations_before
        )
        answer_trace = app.state.services.traces.get(answer.json()["trace_id"])
        assert answer_trace is not None
        assert answer_trace.catalog["attempted"] is False
        assert answer_trace.catalog["call_count"] == 0
        connection = sqlite3.connect(path)
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_candidates"
        ).fetchone()[0] == 1
        connection.close()
        trace = app.state.services.traces.get(first["trace_id"])
        assert trace is not None
        assert trace.catalog["attempted"] is False
        assert trace.catalog["call_count"] == 0


def test_normal_task_budget_blocks_every_later_gap_and_new_task_can_ask() -> None:
    policy = _color_policy()
    recommendation = SimpleNamespace(
        outfits=[
            _scored_outfit("outfit_navy", "navy", 0.75),
            _scored_outfit("outfit_beige", "beige", 0.70),
        ]
    )
    # Once any question was shown, every terminal disposition of that question
    # (skip, neutral, refuse, or no answer) retains the same task-level budget.
    for urgency in ("low", "medium", "high"):
        scene = SimpleNamespace(urgency=urgency)
        for disposition in (
            "answered",
            "skip",
            "neutral",
            "refuse",
            "no_answer",
        ):
            decision = policy.evaluate(
                scene=scene,
                recommendation=recommendation,
                confirmed_signals=(),
                session_preferences=(),
                # A different prior gap proves the budget is task-wide rather
                # than a same-gap suppression trick. The disposition is server
                # state metadata here; none may reopen the task budget.
                asked_gap_codes=frozenset({"color:black_vs_red"}),
            )
            assert decision.clarification is None, disposition
            assert decision.reason_code == "QUESTION_BUDGET_SPENT", disposition

        new_task = policy.evaluate(
            scene=scene,
            recommendation=recommendation,
            confirmed_signals=(),
            session_preferences=(),
            asked_gap_codes=frozenset(),
        )
        assert new_task.clarification is not None
        assert new_task.reason_code == "MATERIAL_PREFERENCE_GAP"


def test_team_home_uses_private_stylist_display_name_without_changing_ids(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    with TestClient(app) as client:
        response = client.get("/team/home", params={"user_id": "u01"})
    assert response.status_code == 200
    member = response.json()["members"][0]
    assert member["name"] == "私人 Stylist"
    assert member["member_id"] == "stylist"
    assert member["persona_id"] == "stylist"


def test_unanswered_normal_task_cannot_switch_gap_but_new_session_can_ask(
    tmp_path, offline_settings
) -> None:
    path = tmp_path / "preference_task_budget.sqlite3"
    app = create_app(
        offline_settings.__class__(
            **{
                **offline_settings.__dict__,
                "database_url": f"sqlite:///{path.as_posix()}",
            }
        )
    )
    _install_dialogue_provider(app)
    policy = app.state.services.dialogue.preference_policy
    with TestClient(app) as client:
        first = _turn(
            client,
            message="下周通勤帮我搭两套",
            request_id="pref_task_budget_01",
        ).json()
        assert first["preference_clarification"] is not None
        first_sets = [
            frozenset(outfit["items"])
            for outfit in first["recommendation"]["outfits"][:2]
        ]
        original_colors = policy._colors

        def changed_gap_colors(item_ids):
            item_set = frozenset(item_ids)
            if item_set == first_sets[0]:
                return frozenset({"black"})
            if item_set == first_sets[1]:
                return frozenset({"red"})
            return original_colors(item_ids)

        policy._colors = changed_gap_colors
        skipped = _turn(
            client,
            message="不想回答，直接按这个场景再给我两套",
            request_id="pref_task_budget_02",
            session_id=first["styling_session_id"],
        )
        assert skipped.status_code == 200, skipped.text
        skipped_body = skipped.json()
        assert skipped_body["action"] == "recommend"
        assert skipped_body["recommendation_paused"] is False
        assert skipped_body["recommendation"] is not None
        assert skipped_body["preference_clarification"] is None

        new_task = _turn(
            client,
            message="下周通勤帮我搭两套",
            request_id="pref_task_budget_03",
        )
        assert new_task.status_code == 200, new_task.text
        assert new_task.json()["styling_session_id"] != first["styling_session_id"]
        assert new_task.json()["preference_clarification"] is not None
