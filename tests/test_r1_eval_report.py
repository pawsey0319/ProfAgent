from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from profagent.app import create_app
from profagent.eval import (
    REPORT_JSON,
    REPORT_MARKDOWN,
    _combine_assurances,
    independent_catalog_inventory_oracle,
    independent_query_catalog_call_required,
    independent_violation_breakdown,
    independent_outfit_violations,
    independent_slots_complete,
    run_evaluation,
)
import profagent.eval as eval_module


REQUIRED_METRICS = {
    "urgency_accuracy",
    "high_urgency_shopping_gate_accuracy",
    "high_urgency_catalog_calls",
    "item_hallucinations",
    "hard_constraint_violations",
    "slot_completeness",
}
REQUIRED_ACS = {
    "AC-01",
    "AC-03",
    "AC-04",
    "AC-05",
    "AC-06",
    "AC-07",
    "AC-10",
    "AC-11",
    "AC-13",
    "AC-14",
    "AC-16",
    "AC-17",
}


def _assert_safe_report(report: dict) -> None:
    assert report["status"] == "PASS"
    assert set(report["metrics"]) == REQUIRED_METRICS
    assert all(metric["status"] == "PASS" for metric in report["metrics"].values())
    assert {entry["ac"] for entry in report["ac_matrix"]} == REQUIRED_ACS
    assert report["versions"]["model"] == {
        "logical": "grok4.5",
        "transport": "grok-4.5-high",
        "evaluation_mode": "offline_rule_fallback",
        "external_calls": 0,
    }
    assert report["versions"]["providers"] == {
        "llm": "cpa_text_grok4.5_v1|offline_rule_fallback",
        "dense": "hashed_dense_v1|disabled_fixture_baseline",
        "catalog": "fixtures_v1.0_mock",
        "vision": "garment_visibility_codes_v2|httpx_mock_only",
    }
    assert report["supplemental_counts"]["eval_cases"] == 30
    assert (
        report["versions"]["evaluation_oracle"]
        == "independent_garment_catalog_score_oracle_v2"
    )
    assert report["evaluation_inputs"]["main_30"] == {
        "mode": "structured_scene_fields",
        "supplied_fields": [
            "user_id",
            "query_text",
            "event_horizon",
            "intent",
        ],
        "metric_definition": "horizon_to_urgency_mapping_accuracy",
        "not_claimed": "query_nlp_accuracy",
    }
    query_only = report["evaluation_inputs"]["query_only"]
    assert query_only["mode"] == "query_only"
    assert query_only["supplied_fields"] == ["user_id", "query_text"]
    assert query_only["denominator"] == 21
    assert len(query_only["case_ids"]) == 21
    assert query_only["expectations"]["e007"]["intent"] == "recommend"
    audit_no_deadline = report["evaluation_inputs"]["audit_no_deadline"]
    assert audit_no_deadline == {
        "mode": "query_only",
        "supplied_fields": ["user_id", "query_text"],
        "case_ids": ["e020", "e023", "audit_given_wardrobe"],
        "denominator": 3,
        "expected_horizon": "unknown",
        "expected_urgency": "high",
        "expected_catalog_invocations": 0,
    }
    catalog_expectation = report["evaluation_inputs"]["catalog_expectation"]
    assert catalog_expectation["call_requirement_oracle"] == (
        "independent_query_catalog_call_required_v1"
    )
    assert catalog_expectation["explicit_gap_call_required_case_ids"] == [
        "e009",
        "e010",
        "e011",
        "e012",
        "e013",
    ]
    assert catalog_expectation["explicit_gap_call_required_case_ids"] == (
        catalog_expectation["expected_explicit_gap_call_required_case_ids"]
    )
    assert catalog_expectation["non_call_required_zero_call_case_ids"] == [
        f"e{index:03d}"
        for index in range(1, 31)
        if index not in {9, 10, 11, 12, 13}
    ]
    assert "every non-call-required case" in catalog_expectation["presence_rule"]
    assert "actual/trace Catalog call" in catalog_expectation["presence_rule"]
    assert "eligible inventory empty" in catalog_expectation["presence_rule"]
    assert catalog_expectation["inventory_eligibility_oracle"] == {
        "version": "independent_fixture_query_inventory_v1",
        "inputs": [
            "query_text",
            "fixture_catalog",
            "fixture_user_budget_and_avoid_colors",
        ],
        "criteria": [
            "slot",
            "stock",
            "budget",
            "occasion_with_interview_meeting_reciprocal_rule",
            "formality",
            "season",
            "delivery_eta_plus_one_day_safety_buffer",
            "rain",
            "taboo_color",
        ],
        "uses_business_catalog_service": False,
    }
    assert catalog_expectation["legacy_generator_candidate_witness_contract"] == {
        "source_field": "allowed_catalog",
        "semantics": "legacy_generator_candidate_witness",
        "authoritative": False,
        "exhaustive": False,
        "release_gating": False,
        "basis": [
            "generate.py stores candidates[:2] after filtering",
            "legacy generator candidates may fail current SHOP-04 inventory eligibility",
        ],
    }
    assert report["supplemental_counts"]["high_urgency_catalog_invocations"] == 0
    assert report["supplemental_counts"]["high_urgency_catalog_trace_calls"] == 0
    assert report["supplemental_counts"]["reported_validation_disagreements"] == 0
    assert report["supplemental_counts"]["honest_gap_case_count"] > 0
    assert report["supplemental_counts"]["availability_cases_checked"] == 3
    assert report["supplemental_counts"]["availability_filter_failed_case_ids"] == []
    assert report["supplemental_counts"]["catalog_references"] > 0
    assert report["supplemental_counts"]["catalog_hard_violations"] == 0
    assert report["supplemental_counts"]["catalog_expectation_violations"] == 0
    assert report["supplemental_counts"]["observed_product_constraint_violations"] == 0
    assert report["supplemental_counts"]["catalog_expected_presence_violations"] == 0
    assert report["supplemental_counts"]["catalog_grounding_failed_case_ids"] == []
    assert report["supplemental_counts"]["catalog_constraint_failed_case_ids"] == []
    assert report["supplemental_counts"]["catalog_expectation_failed_case_ids"] == []
    assert report["supplemental_counts"]["catalog_call_required_case_ids"] == [
        "e009",
        "e010",
        "e011",
        "e012",
        "e013",
    ]
    assert report["supplemental_counts"]["catalog_call_mapping_status"] == "PASS"
    assert report["supplemental_counts"]["catalog_inventory_presence_failed_case_ids"] == []
    assert report["supplemental_counts"]["catalog_inventory_return_failed_case_ids"] == []
    assert report["supplemental_counts"]["catalog_inventory_trace_coverage_failed_case_ids"] == []
    assert report["supplemental_counts"]["catalog_no_gap_zero_call_failed_case_ids"] == []
    assert report["supplemental_counts"]["supportive_message_failed_case_ids"] == []
    assert report["supplemental_counts"]["direction_contract_status"] == "PASS"
    assert report["metrics"]["hard_constraint_violations"]["definition"] == (
        "observed_outfit_alternative_catalog_product_and_validation_constraint_violations"
    )
    assert report["metrics"]["hard_constraint_violations"]["excluded_release_gates"] == [
        "catalog_expected_presence"
    ]
    assert report["release_gates"]["catalog_expected_presence"] == {
        "status": "PASS",
        "violation_count": 0,
        "failed_case_ids": [],
        "included_in_hard_constraint_metric": False,
        "blocks_overall_via": "assurance_checks.CATALOG-ORACLE",
    }
    assert all(
        check["status"] == "PASS"
        for check in report["assurance_checks"].values()
    )
    assert {
        "SAFE-RAIN",
        "SAFE-CPA-PRIVACY",
        "RESPONSE-VALIDATION-ALIGNMENT",
        "QUERY-ONLY-SCENE",
        "AUDIT-NO-DEADLINE",
        "CATALOG-ORACLE",
        "OBS-05",
    }.issubset(report["assurance_checks"])
    assert report["assurance_checks"]["OBS-05"]["legs"] == {
        "llm": {"status": "PASS", "failed_steps": []},
        "dense": {"status": "PASS", "failed_steps": []},
        "catalog": {"status": "PASS", "failed_steps": []},
        "vision": {"status": "PASS", "failed_steps": []},
    }
    assert "same_universe_fixed_penalty_and_risk_rank_never_improves" in report["assurance_checks"]["AC-07"]["checks"]
    assert "end_to_end_similar_risk_scores_decrease" in report["assurance_checks"]["AC-07"]["checks"]
    assert "v2_rescore_has_nonzero_parent_comparison" in report["assurance_checks"]["AC-13"]["checks"]
    assert "canonical_trouser_cuff_single" in report["assurance_checks"]["AC-14"]["checks"]
    assert "actual_numeric_scorecard_exact_78" in report["assurance_checks"]["AC-17"]["checks"]
    assert "audit_actual_catalog_zero" in report["assurance_checks"]["AUDIT-NO-DEADLINE"]["checks"]
    assert "all_nonmandatory_cases_actual_and_trace_catalog_zero" in report["assurance_checks"]["CATALOG-ORACLE"]["checks"]
    assert "e009_e013_explicit_gap_requires_actual_and_trace_call" in report["assurance_checks"]["CATALOG-ORACLE"]["checks"]
    assert "eligible_empty_requires_safe_empty_suggestions" in report["assurance_checks"]["CATALOG-ORACLE"]["checks"]
    assert "allowed_catalog_legacy_generator_candidate_witness_non_gating" in report["assurance_checks"]["CATALOG-ORACLE"]["checks"]
    case_by_id = {case["case_id"]: case for case in report["cases"]}
    for case_id in ("e009", "e010", "e011", "e012", "e013"):
        case = case_by_id[case_id]
        assert case["checks"]["catalog_call_required_by_explicit_gap"] is True
        assert case["checks"]["catalog_explicit_gap_actual_and_trace_call"] is True
        assert case["checks"]["catalog_eligible_inventory_presence_contract"] is True
        assert case["checks"]["catalog_actual_return_subset_independent_eligible"] is True
        assert case["checks"]["catalog_inventory_trace_covers_ineligible"] is True
        assert case["catalog_invocations"] > 0
        assert case["catalog_calls"] > 0
        inventory = case["catalog_inventory_oracle"]
        assert inventory["trace_missing_or_reason_mismatch_ids"] == []
        assert len(inventory["expected_ineligible_reasons_by_id"]) + len(
            inventory["eligible_ids"]
        ) == 50
        if inventory["eligible_ids"]:
            assert case["catalog_reference_count"] > 0
        else:
            assert case["catalog_reference_count"] == 0
    for case_id in ("e011", "e012"):
        assert case_by_id[case_id]["catalog_inventory_oracle"]["eligible_ids"] == []
    for case_id in ("e020", "e021", "e023"):
        assert case_by_id[case_id]["checks"]["catalog_call_required_by_explicit_gap"] is False
        assert case_by_id[case_id]["checks"]["catalog_no_gap_actual_and_trace_zero"] is True
    for case in report["cases"]:
        if not case["checks"]["catalog_call_required_by_explicit_gap"]:
            assert case["checks"]["catalog_no_gap_actual_and_trace_zero"] is True
            assert case["catalog_invocations"] == 0
            assert case["catalog_calls"] == 0
            assert case["catalog_reference_count"] == 0
        witness = case["catalog_legacy_generator_candidate_witness"]
        assert witness["source_field"] == "allowed_catalog"
        assert witness["semantics"] == "legacy_generator_candidate_witness"
        assert witness["authoritative"] is False
        assert witness["exhaustive"] is False
        assert witness["release_gating"] is False
        assert witness["overlap_count"] == len(witness["overlap_ids"])
        assert witness["has_overlap"] is bool(witness["overlap_ids"])
        assert set(witness["overlap_ids"]).issubset(witness["witness_ids"])
        assert 0.0 <= witness["overlap_ratio"] <= 1.0
    witness_diagnostics = report["catalog_legacy_witness_diagnostics"]
    assert witness_diagnostics["release_gating"] is False
    assert witness_diagnostics["authoritative"] is False
    assert witness_diagnostics["exhaustive"] is False
    for scope in ("all_cases", "explicit_gap_cases"):
        diagnostics = witness_diagnostics[scope]
        assert diagnostics["overlap_count"] <= diagnostics["witness_entry_count"]
        assert 0.0 <= diagnostics["overlap_ratio"] <= 1.0
    assert report["privacy"] == {
        "raw_queries_included": False,
        "images_or_base64_included": False,
        "credentials_included": False,
    }
    serialized = json.dumps(report, ensure_ascii=False).lower()
    assert "data:image" not in serialized
    assert "bearer " not in serialized
    assert "sk-" not in serialized
    assert all("query" not in case for case in report["cases"])


def test_frozen_eval_writes_stable_json_and_markdown(project_root: Path) -> None:
    report = asyncio.run(run_evaluation(project_root))
    _assert_safe_report(report)

    json_path = project_root / REPORT_JSON
    markdown_path = project_root / REPORT_MARKDOWN
    assert json_path.is_file()
    assert markdown_path.is_file()
    persisted = json.loads(json_path.read_text(encoding="utf-8"))
    _assert_safe_report(persisted)
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "# ProfAgent R1 Demo Evaluation" in markdown
    assert "structured_scene_fields" in markdown
    assert "horizon_to_urgency_mapping_accuracy" in markdown
    assert "llm_failure_attempted" in markdown
    assert "non-exhaustive" in markdown
    assert "legacy_generator_candidate_witness" in markdown
    assert "diagnostic only" in markdown

    report_files = sorted(path.name for path in json_path.parent.iterdir() if path.is_file())
    assert report_files == ["r1_demo_v1.json", "r1_demo_v1.md"]


def test_eval_run_endpoint_returns_same_stable_contract(offline_settings) -> None:
    app = create_app(offline_settings)
    with TestClient(app) as client:
        response = client.post("/eval/run")
    assert response.status_code == 200, response.text
    report = response.json()
    _assert_safe_report(report)
    assert report["report_paths"] == {
        "json": "reports/eval/r1_demo_v1.json",
        "markdown": "reports/eval/r1_demo_v1.md",
    }


def test_eval_cli_returns_nonzero_when_any_gate_fails(monkeypatch) -> None:
    async def failed_evaluation(*_args, **_kwargs):
        return {
            "status": "FAIL",
            "metrics": {
                "urgency_accuracy": {
                    "value": 0.0,
                    "unit": "ratio",
                    "numerator": 0,
                    "denominator": 30,
                    "status": "FAIL",
                }
            },
            "report_paths": {
                "json": "reports/eval/r1_demo_v1.json",
                "markdown": "reports/eval/r1_demo_v1.md",
            },
        }

    monkeypatch.setattr(eval_module, "run_evaluation", failed_evaluation)
    assert eval_module.main(["--no-write"]) == 1


def test_combine_assurances_fails_closed_on_empty_failed_steps() -> None:
    combined = _combine_assurances(
        "fail-closed regression",
        {
            "status": "FAIL",
            "evidence": "malformed check",
            "checks": [],
            "failed_steps": [],
        },
    )
    assert combined["status"] == "FAIL"
    assert combined["failed_steps"] == ["assurance_not_passed"]


def test_catalog_call_oracle_uses_query_local_slot_semantics(
    project_root: Path,
) -> None:
    rows = {
        row["case_id"]: row
        for row in (
            json.loads(line)
            for line in (project_root / "data/eval/eval.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
    }
    required = {
        case_id
        for case_id, row in rows.items()
        if independent_query_catalog_call_required(row["query"])
        and row["allow_catalog"]
    }
    assert required == {"e009", "e010", "e011", "e012", "e013"}
    assert not independent_query_catalog_call_required(rows["e020"]["query"])
    assert not independent_query_catalog_call_required(rows["e021"]["query"])
    assert not independent_query_catalog_call_required(rows["e023"]["query"])
    assert not independent_query_catalog_call_required(
        "下周面试缺正式鞋，但不要打开商品目录。"
    )
    assert not independent_query_catalog_call_required(
        "下月出差想补外套，先别推荐商品。"
    )


def test_independent_inventory_oracle_allows_safe_empty_catalog(
    project_root: Path,
) -> None:
    from profagent.repository import FixtureRepository

    repository = FixtureRepository(project_root)
    rows = {
        row["case_id"]: row
        for row in (
            json.loads(line)
            for line in (project_root / "data/eval/eval.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
    }
    for case_id in ("e009", "e010", "e011", "e012", "e013"):
        row = rows[case_id]
        eligible, rejected, context = independent_catalog_inventory_oracle(
            repository.list_catalog(),
            row["query"],
            repository.get_user(row["user_id"]),
        )
        assert len(eligible) + len(rejected) == 50
        assert context["required_slots"]
        if case_id in {"e011", "e012"}:
            assert eligible == set()
        else:
            assert eligible
    e011_eligible, e011_rejected, _context = independent_catalog_inventory_oracle(
        repository.list_catalog(),
        rows["e011"]["query"],
        repository.get_user(rows["e011"]["user_id"]),
    )
    assert e011_eligible == set()
    assert "DELIVERY_BUFFER_MISSED" in e011_rejected["c047"]
    assert {
        "c019",
        "c033",
    }.issubset(e011_rejected)
    e012_eligible, e012_rejected, _context = independent_catalog_inventory_oracle(
        repository.list_catalog(),
        rows["e012"]["query"],
        repository.get_user(rows["e012"]["user_id"]),
    )
    assert e012_eligible == set()
    assert all(
        "OCCASION_MISMATCH" in e012_rejected[item_id]
        for item_id in ("c006", "c027")
    )


def test_catalog_presence_failure_is_not_mislabeled_as_hard_constraint() -> None:
    breakdown = independent_violation_breakdown(
        outfit_hard_violations=0,
        invalid_alternative_references=0,
        catalog_product_constraint_violations=0,
        reported_validation_disagreements=0,
        catalog_expected_presence_violations=2,
    )
    assert breakdown == {
        "observed_hard_constraint_violations": 0,
        "catalog_expected_presence_violations": 2,
    }


def test_independent_oracle_rejects_tampered_true_validation(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    services = app.state.services
    with TestClient(app) as client:
        scene_response = client.post(
            "/scene/parse",
            json={
                "user_id": "u03",
                "query_text": "下周日常活动，只用现有衣橱搭一套。",
                "event_horizon": "soon",
                "intent": "recommend",
                "occasion": "daily",
                "goals": ["comfortable"],
            },
        )
        assert scene_response.status_code == 200, scene_response.text
        scene_json = scene_response.json()
        recommendation_response = client.post(
            "/recommend", json={"request_id": scene_json["request_id"]}
        )
        assert recommendation_response.status_code == 200, recommendation_response.text
        recommendation = recommendation_response.json()
        assert recommendation["outfits"]

        scene = services.state.by_request(scene_json["request_id"])
        assert scene is not None
        garment_by_id = {
            item.garment_id: item
            for user in services.repository.list_users()
            for item in services.repository.list_garments(user.user_id)
        }
        outfit = recommendation["outfits"][0]
        assert not independent_outfit_violations(
            outfit["items"], scene, garment_by_id
        )

        shoe_id = next(
            item_id
            for item_id in outfit["items"]
            if garment_by_id[item_id].slot == "shoes"
        )
        unavailable = next(
            item
            for item in services.repository.list_garments("u03")
            if item.status == "unavailable"
        )
        claimed_validation = {
            "all_ids_grounded": True,
            "hard_constraints_passed": True,
            "required_slots_complete": True,
        }
        assert all(claimed_validation.values())

        without_shoes = [
            item_id for item_id in outfit["items"] if item_id != shoe_id
        ]
        without_shoe_items = [garment_by_id[item_id] for item_id in without_shoes]
        assert not independent_slots_complete(
            without_shoe_items, list(scene.constraints.required_slots)
        )
        assert "REQUIRED_SLOTS_INCOMPLETE" in independent_outfit_violations(
            without_shoes, scene, garment_by_id
        )

        injected = [*outfit["items"], unavailable.garment_id]
        assert any(
            reason.startswith("STATUS_UNAVAILABLE:")
            for reason in independent_outfit_violations(
                injected, scene, garment_by_id
            )
        )


def test_recommend_validation_and_private_request_id_never_echo(
    offline_settings,
) -> None:
    app = create_app(offline_settings)
    private_text = "medical diagnosis diabetes 13800138000 private@example.com"
    with TestClient(app) as client:
        invalid = client.post(
            "/recommend",
            json={"request_id": {"private_value": private_text}},
        )
        assert invalid.status_code == 422
        assert private_text not in invalid.text
        assert "13800138000" not in invalid.text
        assert "private@example.com" not in invalid.text
        assert "input" not in invalid.json()["error"].get("details", {})

        private_request_id = "request_private_13800138000_private@example.com"
        missing = client.post(
            "/recommend", json={"request_id": private_request_id}
        )
        assert missing.status_code == 404
        assert private_request_id not in missing.text
        assert "13800138000" not in missing.text
        assert "private@example.com" not in missing.text
