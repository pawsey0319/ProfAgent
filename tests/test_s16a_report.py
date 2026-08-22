from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import tester_s16a_report as report_module


REQUIRED_MEMORY_CHECKS = {
    "candidate_extraction",
    "sensitive_no_write",
    "raw_text_leakage",
    "supersede",
    "one_question_budget",
    "high_urgency_continuation",
}


def _redirect_reports(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    json_path = tmp_path / "s16a.json"
    markdown_path = tmp_path / "s16a.md"
    monkeypatch.setattr(report_module, "REPORT_JSON", json_path)
    monkeypatch.setattr(report_module, "REPORT_MD", markdown_path)
    return json_path, markdown_path


def _passing_report() -> dict:
    memory_uncertainty = {
        name: {"status": "PASS"} for name in REQUIRED_MEMORY_CHECKS
    }
    memory_uncertainty["candidate_extraction"]["verified_cases"] = 1
    memory_uncertainty["sensitive_no_write"]["writes"] = 0
    memory_uncertainty["raw_text_leakage"]["count"] = 0
    memory_uncertainty["supersede"]["atomic"] = True
    memory_uncertainty["one_question_budget"]["maximum_questions"] = 1
    memory_uncertainty["high_urgency_continuation"].update(
        {
            "recommendation_paused": False,
            "shopping_allowed": False,
            "catalog_actual_calls": 0,
        }
    )
    return {
        "schema_version": 1,
        "report_id": "s16a_memory_uncertainty_v1",
        "overall_status": "PASS",
        "memory_uncertainty": memory_uncertainty,
        "provider_determinism": {
            "status": "PASS",
            "external_calls": 0,
        },
        "pytest": {
            "focused": {"status": "PASS", "passed": 1},
            "memory": {"status": "PASS", "passed": 1},
            "dialogue": {"status": "PASS", "passed": 1},
            "full": {"status": "PASS", "passed": 1},
        },
        "node": {"status": "PASS", "passed": 1},
        "fixed_r1": {
            "status": "PASS",
            "metrics": {
                "urgency_accuracy": {
                    "value": 1.0,
                    "status": "PASS",
                    "threshold": {"operator": ">=", "value": 0.95},
                },
                "high_urgency_shopping_gate_accuracy": {
                    "value": 1.0,
                    "status": "PASS",
                    "threshold": {"operator": "==", "value": 1.0},
                },
                "high_urgency_catalog_calls": {
                    "value": 0,
                    "status": "PASS",
                    "threshold": {"operator": "==", "value": 0},
                },
                "item_hallucinations": {
                    "value": 0,
                    "status": "PASS",
                    "threshold": {"operator": "==", "value": 0},
                },
                "hard_constraint_violations": {
                    "value": 0,
                    "status": "PASS",
                    "threshold": {"operator": "==", "value": 0},
                },
                "slot_completeness": {
                    "value": 1.0,
                    "status": "PASS",
                    "threshold": {"operator": ">=", "value": 0.95},
                },
            },
        },
        "reviewer_gate": {
            "status": "PENDING",
            "findings": {"P0": None, "P1": None, "P2": None},
        },
        "commands": [],
    }


def test_s16a_report_contract_covers_required_gates() -> None:
    report = _passing_report()
    report_module._validate_report_contract(report)

    assert set(report["memory_uncertainty"]) == REQUIRED_MEMORY_CHECKS
    assert report["memory_uncertainty"]["raw_text_leakage"] == {
        "status": "PASS",
        "count": 0,
    }
    assert report["memory_uncertainty"]["high_urgency_continuation"] == {
        "status": "PASS",
        "recommendation_paused": False,
        "shopping_allowed": False,
        "catalog_actual_calls": 0,
    }
    assert report["provider_determinism"]["external_calls"] == 0
    assert report["pytest"]["full"]["status"] == "PASS"
    assert report["node"]["status"] == "PASS"
    assert report["reviewer_gate"] == {
        "status": "PENDING",
        "findings": {"P0": None, "P1": None, "P2": None},
    }


def test_s16a_report_contract_rejects_threshold_drift() -> None:
    report = _passing_report()
    report["fixed_r1"]["metrics"]["urgency_accuracy"]["value"] = 0.949
    with pytest.raises(report_module.GateFailure, match="urgency_accuracy"):
        report_module._validate_report_contract(report)


def test_s16a_subprocess_gate_is_checked_captured_and_runtime_locked(
    monkeypatch,
) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="1 passed\n", stderr="")

    monkeypatch.setattr(report_module.subprocess, "run", fake_run)
    result = report_module._run(
        [report_module.PYTHON, "-c", "print('ok')"], "runtime probe"
    )
    assert result.returncode == 0
    assert calls == [
        (
            [report_module.PYTHON, "-c", "print('ok')"],
            {
                "cwd": report_module.ROOT,
                "check": True,
                "text": True,
                "capture_output": True,
                "encoding": "utf-8",
                "errors": "replace",
            },
        )
    ]
    assert Path(report_module.PYTHON).resolve() == Path(sys.executable).resolve()
    assert Path(report_module.NODE).is_absolute()


def test_s16a_failure_does_not_overwrite_existing_pass_or_leave_temp_files(
    monkeypatch, tmp_path: Path
) -> None:
    json_path, markdown_path = _redirect_reports(monkeypatch, tmp_path)
    previous_json = json.dumps(_passing_report(), ensure_ascii=False, indent=2) + "\n"
    previous_markdown = "# previous verified PASS\n"
    json_path.write_text(previous_json, encoding="utf-8")
    markdown_path.write_text(previous_markdown, encoding="utf-8")

    def fail() -> dict:
        raise report_module.GateFailure("injected gate failure")

    monkeypatch.setattr(report_module, "_execute", fail)
    assert report_module.main() == 1
    assert json_path.read_text(encoding="utf-8") == previous_json
    assert markdown_path.read_text(encoding="utf-8") == previous_markdown
    assert list(tmp_path.glob("*.tmp")) == []


def test_s16a_pair_replace_rolls_back_if_second_replace_fails(
    monkeypatch, tmp_path: Path
) -> None:
    json_path, markdown_path = _redirect_reports(monkeypatch, tmp_path)
    previous_json = "{\"overall_status\":\"PASS\"}\n"
    previous_markdown = "# previous PASS\n"
    json_path.write_text(previous_json, encoding="utf-8")
    markdown_path.write_text(previous_markdown, encoding="utf-8")

    real_replace = report_module.os.replace
    replacements = 0

    def fail_second_replace(source, destination):
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("injected second replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(report_module.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="injected second replace failure"):
        report_module._atomic_replace_pair(
            json_path,
            "{\"overall_status\":\"PASS\",\"new\":true}\n",
            markdown_path,
            "# new PASS\n",
        )
    assert json_path.read_text(encoding="utf-8") == previous_json
    assert markdown_path.read_text(encoding="utf-8") == previous_markdown
    assert list(tmp_path.glob("*.tmp")) == []
