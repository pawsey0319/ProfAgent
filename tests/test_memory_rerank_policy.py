from __future__ import annotations

from profagent.app import create_app
from profagent.memory_service import MemorySignal, ShoeSimilaritySignature
from profagent.models import SceneConstraints, SceneRequest, UICapabilities


def test_confirmed_similarity_policy_applies_exact_post_rrf_score_penalty(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    garments = {
        item.garment_id: item for item in services.repository.list_garments("u01")
    }
    target = garments["g020"]
    eligible = {item_id: garments[item_id] for item_id in ("g019", "g021", "g022")}
    scene = SceneRequest(
        request_id="req_policy_unit",
        user_id="u01",
        styling_session_id="session_policy_unit",
        query_text="下周通勤要久走并长时间站立",
        intent="recommend",
        occasion="commute",
        event_horizon="soon",
        urgency="medium",
        shopping_allowed=False,
        goals=["comfortable"],
        constraints=SceneConstraints(comfort_notes=["long_walk"]),
        backend="rule_fallback",
        ui_capabilities=UICapabilities(shopping_cta=False),
        trace_id="trace_policy_unit",
    )
    signal = MemorySignal(
        memory_id="mem_policy_unit",
        namespace="stylist",
        type="feedback",
        applied_signal="long_walk",
        target_item_id=target.garment_id,
        similarity_policy="demote_structured_similar_shoes_v1",
        shoe_signature=ShoeSimilaritySignature(
            styles=tuple(sorted(target.styles)),
            fit=target.fit,
            material=target.material,
            formal=target.formal,
            warmth=target.warmth,
        ),
    )
    # g022 exercises the zero clamp: its actual deduction is 0.005, not the
    # policy maximum of 0.01.
    fused = [("g021", 0.05), ("g019", 0.044), ("g022", 0.005)]

    adjusted, policy_trace = services.retriever._apply_confirmed_memory_rerank(
        fused, eligible, scene, (signal,)
    )
    before_scores = dict(fused)
    after_scores = dict(adjusted)

    assert services.retriever._is_structurally_similar_shoe(
        garments["g021"], signal.shoe_signature
    )
    assert services.retriever._is_structurally_similar_shoe(
        garments["g022"], signal.shoe_signature
    )
    assert not services.retriever._is_structurally_similar_shoe(
        garments["g019"], signal.shoe_signature
    )
    assert after_scores["g021"] == before_scores["g021"] - 0.01
    assert after_scores["g022"] == 0.0
    assert after_scores["g019"] == before_scores["g019"]
    assert [item_id for item_id, _score in adjusted] == ["g019", "g021", "g022"]
    assert policy_trace["applied"] is True
    assert policy_trace["demoted_count"] == 2
    assert policy_trace["score_penalty_applied_count"] == 2
    assert policy_trace["total_score_penalty"] == 0.015
    assert policy_trace["same_universe_rank_lowered_count"] == 1
    assert policy_trace["same_universe_rank_unchanged_count"] == 1
    assert policy_trace["same_universe_rank_improved_count"] == 0
    assert policy_trace["target_ids_logged"] is False
    assert policy_trace["signatures_logged"] is False
    assert policy_trace["demoted_ids_logged"] is False
