from __future__ import annotations

import json
from pathlib import Path
import subprocess

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


def _redirect_reports(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    json_path = tmp_path / "s16a.json"
    markdown_path = tmp_path / "s16a.md"
    bundle_path = tmp_path / "s16a.bundle.json"
    monkeypatch.setattr(report_module, "REPORT_JSON", json_path)
    monkeypatch.setattr(report_module, "REPORT_MD", markdown_path)
    monkeypatch.setattr(report_module, "REPORT_BUNDLE", bundle_path)
    return json_path, markdown_path, bundle_path


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
    command = report_module._conda_python_argv("-c", "print('ok')")
    result = report_module._run(command, "runtime probe")
    assert result.returncode == 0
    assert calls == [
        (
            command,
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
    assert command[:6] == [
        report_module.CONDA,
        "run",
        "--no-capture-output",
        "-n",
        "torch128",
        "python",
    ]
    assert report_module._conda_node_argv("-p", "process.execPath")[:6] == [
        report_module.CONDA,
        "run",
        "--no-capture-output",
        "-n",
        "torch128",
        "node",
    ]


def test_s16a_failure_does_not_overwrite_existing_pass_or_leave_temp_files(
    monkeypatch, tmp_path: Path
) -> None:
    json_path, markdown_path, bundle_path = _redirect_reports(monkeypatch, tmp_path)
    previous_report = _passing_report()
    previous_markdown = "# previous verified PASS\n"
    report_module._publish_authoritative_bundle(previous_report, previous_markdown)
    previous_json = json_path.read_bytes()
    previous_md = markdown_path.read_bytes()
    previous_bundle = bundle_path.read_bytes()

    def fail() -> dict:
        raise report_module.GateFailure("injected gate failure")

    monkeypatch.setattr(report_module, "_execute", fail)
    assert report_module.main() == 1
    assert json_path.read_bytes() == previous_json
    assert markdown_path.read_bytes() == previous_md
    assert bundle_path.read_bytes() == previous_bundle
    assert report_module._load_authoritative_report() == previous_report
    assert list(tmp_path.glob("*.tmp")) == []


def test_s16a_bundle_is_only_authority_when_both_projection_hashes_match(
    monkeypatch, tmp_path: Path
) -> None:
    json_path, markdown_path, bundle_path = _redirect_reports(monkeypatch, tmp_path)
    report = _passing_report()
    report_module._publish_authoritative_bundle(report, "# verified PASS\n")
    loaded = report_module._load_authoritative_report()
    assert loaded == report
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert len(bundle["bundle_sha256"]) == 64
    assert len(bundle["projections"]["json"]["sha256"]) == 64
    assert len(bundle["projections"]["markdown"]["sha256"]) == 64

    json_path.write_text("{\"overall_status\":\"PASS\",\"mixed\":true}\n", encoding="utf-8")
    with pytest.raises(report_module.GateFailure, match="projection"):
        report_module._load_authoritative_report()
    assert bundle_path.exists()
    assert markdown_path.exists()


def test_s16a_persistent_second_projection_failure_is_not_authoritative_and_repairs(
    monkeypatch, tmp_path: Path
) -> None:
    _json_path, markdown_path, _bundle_path = _redirect_reports(monkeypatch, tmp_path)
    old_report = _passing_report()
    report_module._publish_authoritative_bundle(old_report, "# old PASS\n")
    new_report = _passing_report()
    new_report["scope"] = "new report generation"

    real_replace = report_module.os.replace

    def persistently_fail_markdown(source, destination):
        if Path(destination) == markdown_path:
            raise OSError("persistent markdown projection failure")
        return real_replace(source, destination)

    monkeypatch.setattr(report_module.os, "replace", persistently_fail_markdown)
    with pytest.raises(OSError, match="persistent markdown projection failure"):
        report_module._publish_authoritative_bundle(new_report, "# new PASS\n")
    with pytest.raises(report_module.GateFailure, match="projection"):
        report_module._load_authoritative_report()
    assert list(tmp_path.glob("*.tmp")) == []

    monkeypatch.setattr(report_module.os, "replace", real_replace)
    report_module._publish_authoritative_bundle(new_report, "# new PASS\n")
    assert report_module._load_authoritative_report() == new_report


def test_s16a_bundle_commit_interruption_never_exposes_mixed_pass_and_repairs(
    monkeypatch, tmp_path: Path
) -> None:
    _json_path, _markdown_path, bundle_path = _redirect_reports(monkeypatch, tmp_path)
    report_module._publish_authoritative_bundle(_passing_report(), "# old PASS\n")
    replacement = _passing_report()
    replacement["scope"] = "interrupted replacement"
    real_replace = report_module.os.replace

    def interrupt_bundle_commit(source, destination):
        if Path(destination) == bundle_path:
            raise KeyboardInterrupt("injected process interruption")
        return real_replace(source, destination)

    monkeypatch.setattr(report_module.os, "replace", interrupt_bundle_commit)
    with pytest.raises(KeyboardInterrupt, match="injected process interruption"):
        report_module._publish_authoritative_bundle(replacement, "# replacement\n")
    with pytest.raises(report_module.GateFailure, match="projection"):
        report_module._load_authoritative_report()
    assert list(tmp_path.glob("*.tmp")) == []

    monkeypatch.setattr(report_module.os, "replace", real_replace)
    report_module._publish_authoritative_bundle(replacement, "# replacement\n")
    assert report_module._load_authoritative_report() == replacement


def test_s16a_review_evidence_is_hash_and_source_bound() -> None:
    source = "a" * 40
    evidence = {
        "schema_version": 1,
        "source_revision": source,
        "fixed_package_sha256": "b" * 64,
        "review_output_sha256": "c" * 64,
        "status": "PASS",
        "findings": {"P0": 0, "P1": 0, "P2": 0},
    }
    accepted = report_module._select_reviewer_gate(source, evidence)
    assert accepted["status"] == "PASS"
    assert accepted["source_revision"] == source
    assert accepted["fixed_package_sha256"] == "b" * 64
    assert accepted["review_output_sha256"] == "c" * 64

    changed = report_module._select_reviewer_gate("d" * 40, evidence)
    assert changed == {
        "status": "PENDING",
        "findings": {"P0": None, "P1": None, "P2": None},
        "note": "no valid hash-bound reviewer evidence for current source revision",
    }
    invalid = dict(evidence, review_output_sha256="not-a-sha")
    assert report_module._select_reviewer_gate(source, invalid)["status"] == "PENDING"


def test_s16a_clean_source_provenance_and_command_plan_hash(monkeypatch) -> None:
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="a" * 40 + "\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="b" * 40 + "\n", stderr=""),
        ]
    )
    monkeypatch.setattr(report_module, "_run", lambda *_args, **_kwargs: next(responses))
    provenance = report_module._clean_source_provenance()
    assert provenance == {"source_revision": "a" * 40, "git_tree": "b" * 40}

    first = report_module._command_plan_sha256(
        [{"label": "one", "command": "python[torch128] -m pytest"}]
    )
    second = report_module._command_plan_sha256(
        [{"label": "two", "command": "python[torch128] -m pytest"}]
    )
    assert len(first) == 64
    assert first != second

    dirty = iter(
        [subprocess.CompletedProcess([], 0, stdout=" M BUILD_LOG.md\n", stderr="")]
    )
    monkeypatch.setattr(report_module, "_run", lambda *_args, **_kwargs: next(dirty))
    with pytest.raises(report_module.GateFailure, match="clean tracked worktree"):
        report_module._clean_source_provenance()


def test_s16a_mutating_gate_restores_tracked_reports_even_when_gate_fails(
    tmp_path: Path,
) -> None:
    tracked = tmp_path / "r1.json"
    tracked.write_bytes(b"previous tracked report\n")

    def mutate_then_fail():
        tracked.write_bytes(b"new generated report\n")
        raise report_module.GateFailure("injected mutating gate failure")

    with pytest.raises(report_module.GateFailure, match="injected mutating"):
        report_module._run_preserving_files([tracked], mutate_then_fail)
    assert tracked.read_bytes() == b"previous tracked report\n"
    assert list(tmp_path.glob("*.tmp")) == []
