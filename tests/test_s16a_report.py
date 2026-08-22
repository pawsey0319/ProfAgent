from __future__ import annotations

import json
import os
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
_FIXED_PACKAGE_RELATIVE = "reports/eval/evidence/s16a_fixed_package_v1.json"
_REVIEW_OUTPUT_RELATIVE = "reports/eval/evidence/s16a_broad_review_v1.txt"
_FINAL_PUBLICATION_PATHS = {
    "BUILD_LOG.md",
    "reports/eval/s16a_memory_uncertainty_v1.bundle.json",
    "reports/eval/s16a_memory_uncertainty_v1.json",
    "reports/eval/s16a_memory_uncertainty_v1.md",
    _FIXED_PACKAGE_RELATIVE,
    _REVIEW_OUTPUT_RELATIVE,
}

_CPA_ROUTE_CASES = (
    (
        "test_scene_cpa_closed_response_contract_degrades_without_raw_leak",
        (
            "outer_duplicate",
            "outer_extra",
            "choice_duplicate",
            "choice_extra",
            "message_duplicate",
            "message_extra",
            "outer_missing",
            "choices_wrong_type",
            "choices_multiple",
            "inner_duplicate",
            "inner_extra",
            "inner_missing",
            "inner_wrong_type",
        ),
    ),
    (
        "test_dialogue_cpa_closed_response_contract_falls_back_without_raw_leak",
        (
            "outer_duplicate",
            "outer_extra",
            "choice_duplicate",
            "choice_extra",
            "message_duplicate",
            "message_extra",
            "outer_missing",
            "choices_wrong_type",
            "choices_multiple",
            "inner_duplicate",
            "inner_extra",
            "inner_missing",
            "inner_wrong_type",
            "nested_duplicate",
            "nested_extra",
        ),
    ),
    (
        "test_memory_cpa_closed_response_contract_writes_nothing_and_leaks_nothing",
        (
            "outer_duplicate",
            "outer_extra",
            "choice_duplicate",
            "choice_extra",
            "message_duplicate",
            "message_extra",
            "outer_missing",
            "choices_wrong_type",
            "choices_multiple",
            "inner_duplicate",
            "inner_extra",
            "inner_missing",
            "inner_wrong_type",
            "nested_duplicate",
            "nested_extra",
        ),
    ),
)


def _expected_cpa_manifest() -> tuple[str, ...]:
    return tuple(
        f"tests/test_cpa_response_contracts.py::{test_name}[{case}]"
        for test_name, cases in _CPA_ROUTE_CASES
        for case in cases
    )


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
            "closed_envelope_contracts": {
                "status": "PASS",
                "collected": 43,
                "passed": 43,
                "failed": 0,
                "skipped": 0,
                "xfailed": 0,
                "errors": 0,
                "manifest_nodeids": list(_expected_cpa_manifest()),
                "manifest_sha256": report_module._sha256(
                    report_module._canonical_json_bytes(
                        list(_expected_cpa_manifest())
                    )
                ),
                "paths": {
                    "scene": {"invalid_envelope_degraded": True, "raw_leakage": 0},
                    "dialogue": {"invalid_envelope_degraded": True, "raw_leakage": 0},
                    "memory": {
                        "invalid_envelope_degraded": True,
                        "raw_leakage": 0,
                        "writes": 0,
                    },
                },
            },
        },
        "pytest": {
            "focused": {"status": "PASS", "passed": 1},
            "memory": {"status": "PASS", "passed": 1},
            "dialogue": {"status": "PASS", "passed": 1},
            "cpa_closed_envelopes": {"status": "PASS", "passed": 43},
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
        "provenance": {
            "source_revision": "a" * 40,
            "git_tree": "b" * 40,
            "reviewed_head": "a" * 40,
            "reviewed_git_tree": "b" * 40,
            "command_plan_sha256": "c" * 64,
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
    assert report["pytest"]["cpa_closed_envelopes"]["passed"] == 43
    assert report["pytest"]["full"]["status"] == "PASS"
    assert report["node"]["status"] == "PASS"
    assert report["reviewer_gate"] == {
        "status": "PENDING",
        "findings": {"P0": None, "P1": None, "P2": None},
    }


def test_s16a_report_requires_explicit_cpa_closed_envelope_evidence() -> None:
    expected = _expected_cpa_manifest()
    assert len(expected) == 43
    assert report_module.CPA_CLOSED_ENVELOPE_MANIFEST == expected

    report = _passing_report()
    report_module._validate_report_contract(report)
    del report["provider_determinism"]["closed_envelope_contracts"]
    with pytest.raises(report_module.GateFailure, match="CPA closed-envelope"):
        report_module._validate_report_contract(report)


@pytest.mark.parametrize("passed", [3, 42, 44])
def test_s16a_report_rejects_nonexact_cpa_closed_envelope_pass_count(
    passed: int,
) -> None:
    report = _passing_report()
    report["provider_determinism"]["closed_envelope_contracts"]["passed"] = passed
    report["pytest"]["cpa_closed_envelopes"]["passed"] = passed
    with pytest.raises(report_module.GateFailure, match="CPA closed-envelope"):
        report_module._validate_report_contract(report)


def test_s16a_cpa_collection_manifest_rejects_missing_extra_and_duplicate() -> None:
    expected = _expected_cpa_manifest()

    def collected(nodeids: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        output = "\n".join(nodeids) + f"\n\n{len(nodeids)} tests collected in 0.01s\n"
        return subprocess.CompletedProcess([], 0, stdout=output, stderr="")

    assert report_module._cpa_collected_nodeids(collected(expected)) == expected
    for drifted in (
        expected[:-1],
        (*expected, "tests/test_cpa_response_contracts.py::test_unknown[extra]"),
        (*expected[:-1], expected[-2]),
        (expected[1], expected[0], *expected[2:]),
    ):
        with pytest.raises(report_module.GateFailure, match="CPA collection manifest"):
            report_module._cpa_collected_nodeids(collected(drifted))


@pytest.mark.parametrize(
    "summary",
    [
        "3 passed in 0.01s",
        "42 passed in 0.01s",
        "44 passed in 0.01s",
        "42 passed, 1 skipped in 0.01s",
        "42 passed, 1 xfailed in 0.01s",
        "42 passed, 1 error in 0.01s",
    ],
)
def test_s16a_cpa_result_requires_exact_43_clean_passes(summary: str) -> None:
    completed = subprocess.CompletedProcess([], 0, stdout=summary + "\n", stderr="")
    with pytest.raises(report_module.GateFailure, match="CPA closed-envelope outcomes"):
        report_module._cpa_exact_outcomes(completed)


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


def test_s16a_node_runtime_inventory_is_explicit_closed_and_includes_identity(
    tmp_path: Path,
) -> None:
    expected = (
        "dialogue_runtime_test.cjs",
        "image_asset_runtime_test.cjs",
        "memory_candidate_flow_test.cjs",
        "memory_candidate_runtime_test.cjs",
        "memory_runtime_test.cjs",
        "preference_clarification_runtime_test.cjs",
        "recommendation_preview_runtime_test.cjs",
        "s14_frontend_static_test.cjs",
        "s15_frontend_static_test.cjs",
        "s16_frontend_static_test.cjs",
        "static_contract_test.cjs",
        "visible_member_identity_test.cjs",
    )
    assert report_module.NODE_RUNTIME_CONTRACTS == expected
    web_root = tmp_path / "web"
    web_root.mkdir()
    for name in expected:
        (web_root / name).write_text("// fixture\n", encoding="utf-8")
    assert tuple(
        path.name for path in report_module._node_runtime_contract_paths(web_root)
    ) == expected

    (web_root / "unexpected_test.cjs").write_text("// unknown\n", encoding="utf-8")
    with pytest.raises(report_module.GateFailure, match="unknown"):
        report_module._node_runtime_contract_paths(web_root)
    (web_root / "unexpected_test.cjs").unlink()
    (web_root / "visible_member_identity_test.cjs").unlink()
    with pytest.raises(report_module.GateFailure, match="missing"):
        report_module._node_runtime_contract_paths(web_root)


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


def test_s16a_bundle_identity_must_match_report_provenance(
    monkeypatch, tmp_path: Path
) -> None:
    _json_path, _markdown_path, bundle_path = _redirect_reports(monkeypatch, tmp_path)
    report_module._publish_authoritative_bundle(_passing_report(), "# verified PASS\n")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["reviewed_head"] = "d" * 40
    core = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    bundle["bundle_sha256"] = report_module._sha256(
        report_module._canonical_json_bytes(core)
    )
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(report_module.GateFailure, match="reviewed_head provenance"):
        report_module._load_authoritative_report()


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


def _git_text(*arguments: str, cwd: Path = report_module.ROOT) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def _git_ok(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _two_phase_repo(root: Path, *, nonartifact_middle: bool = False) -> tuple[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        _git_ok(root, "init", "-q")
        _git_ok(root, "config", "user.name", "S16A Tester")
        _git_ok(root, "config", "user.email", "s16a@example.invalid")
        (root / ".gitignore").write_text(".ignored-review/\n", encoding="utf-8")
        (root / "baseline.txt").write_text("baseline\n", encoding="utf-8")
        (root / "BUILD_LOG.md").write_text("S16A open\n", encoding="utf-8")
        _git_ok(root, "add", ".gitignore", "baseline.txt", "BUILD_LOG.md")
        _git_ok(root, "commit", "-q", "-m", "baseline")
        (root / "code.txt").write_text("clean source\n", encoding="utf-8")
        _git_ok(root, "add", "code.txt")
        _git_ok(root, "commit", "-q", "-m", "code source")
        _git_ok(root, "tag", "s16a-source")
        if nonartifact_middle:
            (root / "code.txt").write_text("forbidden middle change\n", encoding="utf-8")
            _git_ok(root, "add", "code.txt")
            _git_ok(root, "commit", "-q", "-m", "nonartifact middle")
        artifact_directory = root / "reports" / "eval"
        artifact_directory.mkdir(parents=True)
        for name in (
            "s16a_memory_uncertainty_v1.bundle.json",
            "s16a_memory_uncertainty_v1.json",
            "s16a_memory_uncertainty_v1.md",
        ):
            (artifact_directory / name).write_text(f"artifact {name}\n", encoding="utf-8")
        _git_ok(root, "add", "reports/eval")
        _git_ok(root, "commit", "-q", "-m", "artifact-only report")
    return (
        _git_text("rev-parse", "refs/tags/s16a-source", cwd=root).strip(),
        _git_text("rev-parse", "HEAD", cwd=root).strip(),
    )


def _set_review_roots(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(report_module, "REVIEW_EVIDENCE_ROOT", root.resolve())
    monkeypatch.setattr(
        report_module,
        "REVIEW_GIT_ROOT",
        root.resolve(),
        raising=False,
    )


def _real_review_fixture(
    root: Path,
    *,
    nonartifact_middle: bool = False,
    ignored_evidence: bool = False,
) -> tuple[str, dict, Path, Path]:
    source, head = _two_phase_repo(root, nonartifact_middle=nonartifact_middle)
    commits = [
        line
        for line in _git_text(
            "rev-list", "--reverse", f"{source}..{head}", cwd=root
        ).splitlines()
        if line
    ]
    diff = _git_text(
        "diff", "--no-ext-diff", "--binary", source, head, cwd=root
    )
    if ignored_evidence:
        review_directory = root / ".ignored-review"
        package_path = review_directory / "fixed-package.json"
        review_path = review_directory / "review-output.txt"
    else:
        package_path = root / _FIXED_PACKAGE_RELATIVE
        review_path = root / _REVIEW_OUTPUT_RELATIVE
        review_directory = package_path.parent
    review_directory.mkdir(parents=True, exist_ok=True)
    package_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_revision": source,
                "head_revision": head,
                "tested_source_revision": source,
                "commits": commits,
                "diff": diff,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    review_path.write_text(
        "reviewed exact fixed package\n"
        "[S16A_REVIEW_RESULT_V1]\n"
        f"source_revision={source}\n"
        f"reviewed_head={head}\n"
        "final_verdict=PASS\n"
        "P0=0\n"
        "P1=0\n"
        "P2=0\n"
        "[/S16A_REVIEW_RESULT_V1]\n",
        encoding="utf-8",
    )
    descriptor = {
        "schema_version": 1,
        "expected_source_revision": source,
        "reviewed_head": head,
        "fixed_package_path": str(package_path.resolve()),
        "review_output_path": str(review_path.resolve()),
    }
    return source, descriptor, package_path, review_path


def _redirect_repo_reports(monkeypatch, root: Path) -> tuple[Path, Path, Path]:
    report_root = root / "reports" / "eval"
    json_path = report_root / "s16a_memory_uncertainty_v1.json"
    markdown_path = report_root / "s16a_memory_uncertainty_v1.md"
    bundle_path = report_root / "s16a_memory_uncertainty_v1.bundle.json"
    monkeypatch.setattr(report_module, "REPORT_JSON", json_path)
    monkeypatch.setattr(report_module, "REPORT_MD", markdown_path)
    monkeypatch.setattr(report_module, "REPORT_BUNDLE", bundle_path)
    return json_path, markdown_path, bundle_path


def _commit_exact_final_publication(root: Path) -> str:
    (root / "BUILD_LOG.md").write_text("S16A closed after review PASS\n", encoding="utf-8")
    _git_ok(root, "add", *sorted(_FINAL_PUBLICATION_PATHS))
    staged = {
        line.replace("\\", "/")
        for line in _git_text("diff", "--cached", "--name-only", cwd=root).splitlines()
        if line
    }
    assert staged == _FINAL_PUBLICATION_PATHS
    _git_ok(root, "commit", "-q", "-m", "final S16A publication")
    return _git_text("rev-parse", "HEAD", cwd=root).strip()


def _write_synthetic_publication_changes(root: Path) -> None:
    report_root = root / "reports" / "eval"
    for name in (
        "s16a_memory_uncertainty_v1.bundle.json",
        "s16a_memory_uncertainty_v1.json",
        "s16a_memory_uncertainty_v1.md",
    ):
        path = report_root / name
        path.write_text(path.read_text(encoding="utf-8") + "final PASS\n", encoding="utf-8")
    (root / "BUILD_LOG.md").write_text("S16A closed after review PASS\n", encoding="utf-8")


def _commit_selected_publication(
    root: Path, paths: set[str], *, extra: bool = False
) -> str:
    _write_synthetic_publication_changes(root)
    selected = set(paths)
    if extra:
        (root / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
        selected.add("unexpected.txt")
    _git_ok(root, "add", *sorted(selected))
    _git_ok(root, "commit", "-q", "-m", "candidate final publication")
    return _git_text("rev-parse", "HEAD", cwd=root).strip()


def _fake_execute_gate(root: Path, temp_root: Path):
    def fake_gate_run(
        command: list[str], label: str
    ) -> subprocess.CompletedProcess[str]:
        stdout = ""
        if label == "torch128 Python process.execPath":
            stdout = report_module.PYTHON + "\n"
        elif label == "torch128 Node process.execPath":
            stdout = str((temp_root / "node.exe").absolute()) + "\n"
        elif label == "torch128 Node version":
            stdout = "v22.0.0\n"
        elif label == "clean current HEAD status":
            stdout = _git_text(
                "status", "--porcelain=v1", "--untracked-files=all", cwd=root
            )
        elif label == "current HEAD revision":
            stdout = _git_text("rev-parse", "HEAD", cwd=root).strip() + "\n"
        elif label == "current HEAD tree":
            stdout = _git_text("rev-parse", "HEAD^{tree}", cwd=root).strip() + "\n"
        elif label == "CPA closed-envelope collection":
            manifest = _expected_cpa_manifest()
            stdout = "\n".join(manifest) + "\n\n43 tests collected in 0.01s\n"
        elif label == "CPA closed-envelope pytest":
            stdout = "43 passed in 0.01s\n"
        elif "pytest" in label:
            stdout = "1 passed in 0.01s\n"
        elif label == "fixture validation":
            stdout = "fixtures_v1.0: PASS\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    return fake_gate_run


def test_s16a_review_descriptor_reads_real_files_and_derives_hashes(
    monkeypatch, tmp_path: Path
) -> None:
    _set_review_roots(monkeypatch, tmp_path)
    source, descriptor, package_path, review_path = _real_review_fixture(tmp_path)
    reviewed_head = descriptor["reviewed_head"]
    accepted = report_module._review_gate_from_descriptor(reviewed_head, descriptor)
    assert accepted["status"] == "PASS"
    assert accepted["findings"] == {"P0": 0, "P1": 0, "P2": 0}
    assert accepted["source_revision"] == source
    assert accepted["reviewed_head"] == reviewed_head
    assert accepted["reviewed_head"] != accepted["source_revision"]
    assert accepted["fixed_package_sha256"] == report_module._sha256(
        package_path.read_bytes()
    )
    assert accepted["review_output_sha256"] == report_module._sha256(
        review_path.read_bytes()
    )
    assert "sha" not in " ".join(descriptor).lower()
    caller_hash = dict(descriptor, fixed_package_sha256="0" * 64)
    assert (
        report_module._review_gate_from_descriptor(reviewed_head, caller_hash)["status"]
        == "PENDING"
    )
    empty_interval = dict(descriptor, expected_source_revision=reviewed_head)
    assert (
        report_module._review_gate_from_descriptor(reviewed_head, empty_interval)["status"]
        == "PENDING"
    )


def test_s16a_review_content_change_wrong_head_tamper_and_fake_pass_fail_closed(
    monkeypatch, tmp_path: Path
) -> None:
    _set_review_roots(monkeypatch, tmp_path)
    source, descriptor, package_path, review_path = _real_review_fixture(tmp_path)
    reviewed_head = descriptor["reviewed_head"]
    accepted = report_module._review_gate_from_descriptor(reviewed_head, descriptor)

    review_path.write_text(
        review_path.read_text(encoding="utf-8") + "post-read modification\n",
        encoding="utf-8",
    )
    assert (
        report_module._preserve_reviewer_gate(reviewed_head, accepted)["status"]
        == "PENDING"
    )

    source, descriptor, package_path, review_path = _real_review_fixture(tmp_path)
    wrong_source = dict(descriptor, expected_source_revision="0" * 40)
    assert (
        report_module._review_gate_from_descriptor(reviewed_head, wrong_source)["status"]
        == "PENDING"
    )

    source, descriptor, package_path, review_path = _real_review_fixture(tmp_path)
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["head_revision"] = package["base_revision"]
    package_path.write_text(json.dumps(package), encoding="utf-8")
    assert report_module._review_gate_from_descriptor(reviewed_head, descriptor)["status"] == "PENDING"

    source, descriptor, package_path, review_path = _real_review_fixture(tmp_path)
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["commits"] = list(reversed(package["commits"])) + [package["base_revision"]]
    package_path.write_text(json.dumps(package), encoding="utf-8")
    assert report_module._review_gate_from_descriptor(reviewed_head, descriptor)["status"] == "PENDING"

    source, descriptor, package_path, review_path = _real_review_fixture(tmp_path)
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["diff"] += "tampered diff\n"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    assert report_module._review_gate_from_descriptor(reviewed_head, descriptor)["status"] == "PENDING"

    source, descriptor, _package_path, review_path = _real_review_fixture(tmp_path)
    review_path.write_text("Final Verdict: PASS\nP0=0 P1=0 P2=0\n", encoding="utf-8")
    assert report_module._review_gate_from_descriptor(reviewed_head, descriptor)["status"] == "PENDING"


def test_s16a_review_output_rejects_ambiguous_blocks_and_finding_count_mismatch(
    monkeypatch, tmp_path: Path
) -> None:
    _set_review_roots(monkeypatch, tmp_path)
    source, descriptor, _package_path, review_path = _real_review_fixture(tmp_path)
    reviewed_head = descriptor["reviewed_head"]
    valid_block = review_path.read_text(encoding="utf-8")
    review_path.write_text(valid_block + valid_block, encoding="utf-8")
    assert report_module._review_gate_from_descriptor(reviewed_head, descriptor)["status"] == "PENDING"

    _source, descriptor, _package_path, review_path = _real_review_fixture(tmp_path)
    mismatch = (
        "[P1] actual finding\n"
        "[S16A_REVIEW_RESULT_V1]\n"
        f"source_revision={source}\nreviewed_head={reviewed_head}\n"
        "final_verdict=CHANGES REQUIRED\nP0=0\nP1=0\nP2=0\n"
        "[/S16A_REVIEW_RESULT_V1]\n"
    )
    review_path.write_text(mismatch, encoding="utf-8")
    assert report_module._review_gate_from_descriptor(reviewed_head, descriptor)["status"] == "PENDING"


def test_s16a_review_paths_reject_escape_and_dotdot(
    monkeypatch, tmp_path: Path
) -> None:
    evidence_root = tmp_path / "inside"
    evidence_root.mkdir()
    _set_review_roots(monkeypatch, evidence_root)
    source, descriptor, package_path, review_path = _real_review_fixture(evidence_root)
    reviewed_head = descriptor["reviewed_head"]
    outside = tmp_path / "outside-review.md"
    outside.write_text(review_path.read_text(encoding="utf-8"), encoding="utf-8")
    escaped = dict(descriptor, review_output_path=str(outside.resolve()))
    assert report_module._review_gate_from_descriptor(reviewed_head, escaped)["status"] == "PENDING"

    dotdot = dict(
        descriptor,
        fixed_package_path=str(package_path.parent / "nested" / ".." / package_path.name),
    )
    assert report_module._review_gate_from_descriptor(reviewed_head, dotdot)["status"] == "PENDING"


def test_s16a_review_descriptor_rejects_ignored_noncanonical_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    _set_review_roots(monkeypatch, tmp_path)
    _source, descriptor, _package_path, _review_path = _real_review_fixture(
        tmp_path, ignored_evidence=True
    )
    assert (
        report_module._review_gate_from_descriptor(
            descriptor["reviewed_head"], descriptor
        )["status"]
        == "PENDING"
    )


def test_s16a_review_path_rejects_external_symlink(monkeypatch, tmp_path: Path) -> None:
    evidence_root = tmp_path / "inside"
    evidence_root.mkdir()
    _set_review_roots(monkeypatch, evidence_root)
    source, descriptor, _package_path, review_path = _real_review_fixture(evidence_root)
    reviewed_head = descriptor["reviewed_head"]
    outside = tmp_path / "outside-review.md"
    outside.write_bytes(review_path.read_bytes())
    review_path.unlink()
    try:
        os.symlink(outside, review_path)
    except OSError as exc:
        pytest.skip(f"host does not permit file symlink creation: {exc}")
    assert report_module._review_gate_from_descriptor(reviewed_head, descriptor)["status"] == "PENDING"


def test_s16a_review_path_rejects_external_windows_junction(
    monkeypatch, tmp_path: Path
) -> None:
    evidence_root = tmp_path / "inside"
    evidence_root.mkdir()
    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    _set_review_roots(monkeypatch, evidence_root)
    source, descriptor, package_path, review_path = _real_review_fixture(evidence_root)
    reviewed_head = descriptor["reviewed_head"]
    package_bytes = package_path.read_bytes()
    review_bytes = review_path.read_bytes()
    evidence_directory = package_path.parent
    package_path.unlink()
    review_path.unlink()
    evidence_directory.rmdir()
    (outside_directory / package_path.name).write_bytes(package_bytes)
    (outside_directory / review_path.name).write_bytes(review_bytes)
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(evidence_directory), str(outside_directory)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(
            "host cannot create Windows junction: "
            + (created.stderr or created.stdout).strip()
        )
    assert report_module._review_gate_from_descriptor(reviewed_head, descriptor)["status"] == "PENDING"


def test_s16a_same_source_bundle_preserves_evidence_and_source_drift_clears_it(
    monkeypatch, tmp_path: Path
) -> None:
    _set_review_roots(monkeypatch, tmp_path)
    source, descriptor, _package_path, _review_path = _real_review_fixture(tmp_path)
    reviewed_head = descriptor["reviewed_head"]
    accepted = report_module._review_gate_from_descriptor(reviewed_head, descriptor)
    report = _passing_report()
    report["provenance"] = {
        "source_revision": source,
        "git_tree": _git_text("rev-parse", f"{source}^{{tree}}", cwd=tmp_path).strip(),
        "reviewed_head": reviewed_head,
        "reviewed_git_tree": _git_text(
            "rev-parse", f"{reviewed_head}^{{tree}}", cwd=tmp_path
        ).strip(),
    }
    report["reviewer_gate"] = accepted
    _redirect_reports(monkeypatch, tmp_path / "review" / "generated")
    report_module._publish_authoritative_bundle(report, "# reviewed PASS\n")

    assert report_module._preserve_reviewer_gate(reviewed_head)["status"] == "PASS"
    assert report_module._preserve_reviewer_gate("f" * 40) == {
        "status": "PENDING",
        "findings": {"P0": None, "P1": None, "P2": None},
        "note": "no valid hash-bound reviewer evidence for current source revision",
    }


def test_s16a_execute_publication_preserve_and_drift_fail_closed(
    monkeypatch, tmp_path: Path
) -> None:
    _set_review_roots(monkeypatch, tmp_path)
    source, descriptor, package_path, review_path = _real_review_fixture(tmp_path)
    reviewed_head = descriptor["reviewed_head"]
    report_paths = _redirect_repo_reports(monkeypatch, tmp_path)
    fake_gate_run = _fake_execute_gate(tmp_path, tmp_path)

    assert report_module.FIXED_REVIEW_EVIDENCE_PATHS == {
        "fixed_package": _FIXED_PACKAGE_RELATIVE,
        "review_output": _REVIEW_OUTPUT_RELATIVE,
    }
    assert report_module.FINAL_PUBLICATION_EXACT_PATHS == _FINAL_PUBLICATION_PATHS
    assert report_module.main(descriptor, run_gate=fake_gate_run) == 0
    first = report_module._load_authoritative_report()
    assert first["reviewer_gate"]["status"] == "PASS"
    assert first["provenance"]["source_revision"] == source
    assert first["provenance"]["reviewed_head"] == reviewed_head
    assert first["provenance"]["source_revision"] != first["provenance"]["reviewed_head"]
    assert first["reviewer_gate"]["fixed_package_sha256"] == report_module._sha256(
        package_path.read_bytes()
    )
    assert first["reviewer_gate"]["review_output_sha256"] == report_module._sha256(
        review_path.read_bytes()
    )
    closed_contract = first["provider_determinism"]["closed_envelope_contracts"]
    assert closed_contract["passed"] == 43
    assert closed_contract["collected"] == 43
    assert closed_contract["manifest_nodeids"] == list(_expected_cpa_manifest())
    collection_commands = [
        entry
        for entry in first["commands"]
        if entry["label"] == "CPA closed-envelope collection"
    ]
    assert len(collection_commands) == 1
    cpa_commands = [
        entry for entry in first["commands"] if entry["label"] == "CPA closed-envelope pytest"
    ]
    assert len(cpa_commands) == 1
    assert all(
        test_id in cpa_commands[0]["command"]
        for test_id in report_module.CPA_CLOSED_ENVELOPE_MANIFEST
    )
    first_bytes = tuple(path.read_bytes() for path in report_paths)

    publication_head = _commit_exact_final_publication(tmp_path)
    assert publication_head != reviewed_head
    assert _git_text("status", "--porcelain", cwd=tmp_path) == ""
    assert report_module.main(run_gate=fake_gate_run) == 0
    preserved = report_module._load_authoritative_report()
    assert preserved["reviewer_gate"] == first["reviewer_gate"]
    assert preserved["provenance"]["source_revision"] == source
    assert preserved["provenance"]["reviewed_head"] == reviewed_head
    assert tuple(path.read_bytes() for path in report_paths) == first_bytes
    assert _git_text("status", "--porcelain", cwd=tmp_path) == ""

    (tmp_path / "later.txt").write_text("later nonpublication commit\n", encoding="utf-8")
    _git_ok(tmp_path, "add", "later.txt")
    _git_ok(tmp_path, "commit", "-q", "-m", "later nonpublication commit")
    assert report_module.main(run_gate=fake_gate_run) == 0
    drifted = report_module._load_authoritative_report()["reviewer_gate"]
    assert drifted["status"] == "PENDING"
    assert drifted["findings"] == {"P0": None, "P1": None, "P2": None}


@pytest.mark.parametrize("mode", ["subset", "extra", "multiple"])
def test_s16a_final_publication_rejects_wrong_commit_shape(
    monkeypatch, tmp_path: Path, mode: str
) -> None:
    root = tmp_path / mode
    _set_review_roots(monkeypatch, root)
    _source, descriptor, _package_path, _review_path = _real_review_fixture(root)
    reviewed_head = descriptor["reviewed_head"]
    accepted = report_module._review_gate_from_descriptor(reviewed_head, descriptor)
    assert accepted["status"] == "PASS"
    paths = set(_FINAL_PUBLICATION_PATHS)
    if mode == "subset":
        paths.remove("reports/eval/s16a_memory_uncertainty_v1.md")
        current = _commit_selected_publication(root, paths)
    else:
        current = _commit_selected_publication(root, paths, extra=mode == "extra")
    if mode == "multiple":
        (root / "later.txt").write_text("later\n", encoding="utf-8")
        _git_ok(root, "add", "later.txt")
        _git_ok(root, "commit", "-q", "-m", "later commit")
        current = _git_text("rev-parse", "HEAD", cwd=root).strip()
    assert report_module._preserve_reviewer_gate(current, accepted)["status"] == "PENDING"


def test_s16a_final_publication_rejects_merge_and_evidence_drift(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "merge-and-drift"
    _set_review_roots(monkeypatch, root)
    _source, descriptor, package_path, review_path = _real_review_fixture(root)
    reviewed_head = descriptor["reviewed_head"]
    accepted = report_module._review_gate_from_descriptor(reviewed_head, descriptor)
    _write_synthetic_publication_changes(root)
    publication_head = _commit_exact_final_publication(root)
    assert report_module._preserve_reviewer_gate(publication_head, accepted)["status"] == "PASS"

    package_bytes = package_path.read_bytes()
    review_bytes = review_path.read_bytes()
    review_path.write_bytes(review_bytes + b"drift\n")
    assert report_module._preserve_reviewer_gate(publication_head, accepted)["status"] == "PENDING"
    review_path.write_bytes(review_bytes)
    package_path.unlink()
    assert report_module._preserve_reviewer_gate(publication_head, accepted)["status"] == "PENDING"

    package_path.write_bytes(package_bytes)
    branch = _git_text("branch", "--show-current", cwd=root).strip()
    _git_ok(root, "switch", "-q", "-c", "side", reviewed_head)
    (root / "side.txt").write_text("side\n", encoding="utf-8")
    _git_ok(root, "add", "side.txt")
    _git_ok(root, "commit", "-q", "-m", "side commit")
    _git_ok(root, "switch", "-q", branch)
    _git_ok(root, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    merge_head = _git_text("rev-parse", "HEAD", cwd=root).strip()
    assert report_module._preserve_reviewer_gate(merge_head, accepted)["status"] == "PENDING"


def test_s16a_execute_rejects_nonartifact_middle_and_wrong_ancestor(
    monkeypatch, tmp_path: Path
) -> None:
    nonartifact_root = tmp_path / "nonartifact"
    _set_review_roots(monkeypatch, nonartifact_root)
    _source, descriptor, _package_path, _review_path = _real_review_fixture(
        nonartifact_root, nonartifact_middle=True
    )
    _redirect_reports(monkeypatch, nonartifact_root / "review" / "generated")
    assert (
        report_module.main(
            descriptor,
            run_gate=_fake_execute_gate(nonartifact_root, tmp_path),
        )
        == 0
    )
    assert report_module._load_authoritative_report()["reviewer_gate"]["status"] == "PENDING"

    wrong_root = tmp_path / "wrong-ancestor"
    _set_review_roots(monkeypatch, wrong_root)
    source, descriptor, package_path, review_path = _real_review_fixture(wrong_root)
    reviewed_head = descriptor["reviewed_head"]
    source_tree = _git_text("rev-parse", f"{source}^{{tree}}", cwd=wrong_root).strip()
    orphan = subprocess.run(
        ["git", "commit-tree", source_tree, "-m", "unrelated source"],
        cwd=wrong_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["base_revision"] = orphan
    package["tested_source_revision"] = orphan
    package_path.write_text(json.dumps(package), encoding="utf-8")
    review_path.write_text(
        "[S16A_REVIEW_RESULT_V1]\n"
        f"source_revision={orphan}\n"
        f"reviewed_head={reviewed_head}\n"
        "final_verdict=PASS\nP0=0\nP1=0\nP2=0\n"
        "[/S16A_REVIEW_RESULT_V1]\n",
        encoding="utf-8",
    )
    wrong_descriptor = dict(descriptor, expected_source_revision=orphan)
    _redirect_reports(monkeypatch, wrong_root / "review" / "generated")
    assert (
        report_module.main(
            wrong_descriptor,
            run_gate=_fake_execute_gate(wrong_root, tmp_path),
        )
        == 0
    )
    assert report_module._load_authoritative_report()["reviewer_gate"]["status"] == "PENDING"


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
