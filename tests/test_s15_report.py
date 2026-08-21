from __future__ import annotations

import json

from scripts import tester_s15_report as report_module


def _redirect_reports(monkeypatch, tmp_path) -> tuple:
    json_path = tmp_path / "s15.json"
    markdown_path = tmp_path / "s15.md"
    monkeypatch.setattr(report_module, "REPORT_JSON", json_path)
    monkeypatch.setattr(report_module, "REPORT_MD", markdown_path)
    return json_path, markdown_path


def test_s15_report_failure_is_atomic_and_nonzero(monkeypatch, tmp_path) -> None:
    json_path, markdown_path = _redirect_reports(monkeypatch, tmp_path)

    def fail():
        raise report_module.GateFailure("controlled gate failure")

    monkeypatch.setattr(report_module, "_execute", fail)
    assert report_module.main() == 1
    assert json.loads(json_path.read_text(encoding="utf-8"))["overall_status"] == "FAIL"
    assert "结论：FAIL" in markdown_path.read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.tmp")) == []


def test_s15_report_pending_result_never_exits_zero(monkeypatch, tmp_path) -> None:
    json_path, _markdown_path = _redirect_reports(monkeypatch, tmp_path)
    monkeypatch.setattr(
        report_module,
        "_execute",
        lambda: {"overall_status": "PENDING"},
    )
    assert report_module.main() == 1
    persisted = json.loads(json_path.read_text(encoding="utf-8"))
    assert persisted["overall_status"] == "FAIL"
    assert persisted["error_type"] == "GateFailure"


def test_s15_synthetic_rerank_benchmark_is_bounded_and_honest() -> None:
    benchmark = report_module._synthetic_rerank_benchmark()
    assert benchmark["status"] == "PASS"
    assert benchmark["dataset"] == {
        "soft_records": 24,
        "hard_exclusion_records": 1,
        "scenarios": 4,
        "contains_real_user_data": False,
    }
    assert benchmark["algorithm"]["branch_candidate_limit"] == 20
    assert benchmark["algorithm"]["external_model_calls"] == 0
    assert benchmark["pollution"]["hard_memory_in_rrf"] == 0
    assert benchmark["claim_boundary"] == {
        "real_user_quality_improvement": "not_assessed",
        "production_semantic_embedding": "not_implemented",
        "cross_encoder": "not_implemented",
        "allowed_interpretation": "deterministic contract and synthetic diagnostic only",
    }
    assert all(
        scenario["branch_top_n"]
        == {"bm25": 20, "dense": 20, "recency": 20, "importance": 20}
        and scenario["hard_memory_in_rrf"] is False
        and scenario["deterministic_repeat"] is True
        and scenario["context_source"] == "authoritative_scene_v1"
        for scenario in benchmark["per_scenario"]
    )
