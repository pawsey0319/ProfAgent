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


def _git_text(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=report_module.ROOT,
        check=True,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def _real_review_fixture(root: Path) -> tuple[str, dict, Path, Path]:
    head = _git_text("rev-parse", "HEAD").strip()
    base = _git_text("rev-parse", "HEAD^").strip()
    commits = [
        line for line in _git_text("rev-list", "--reverse", f"{base}..{head}").splitlines()
        if line
    ]
    diff = _git_text("diff", "--no-ext-diff", "--binary", base, head)
    package_path = root / "fixed-package.json"
    package_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_revision": base,
                "head_revision": head,
                "tested_source_revision": head,
                "commits": commits,
                "diff": diff,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    review_path = root / "review-output.md"
    review_path.write_text(
        "reviewed exact fixed package\n"
        "[S16A_REVIEW_RESULT_V1]\n"
        f"source_revision={head}\n"
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
        "expected_source_revision": head,
        "reviewed_head": head,
        "fixed_package_path": str(package_path.resolve()),
        "review_output_path": str(review_path.resolve()),
    }
    return head, descriptor, package_path, review_path


def test_s16a_review_descriptor_reads_real_files_and_derives_hashes(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(report_module, "REVIEW_EVIDENCE_ROOT", tmp_path.resolve())
    source, descriptor, package_path, review_path = _real_review_fixture(tmp_path)
    accepted = report_module._review_gate_from_descriptor(source, descriptor)
    assert accepted["status"] == "PASS"
    assert accepted["findings"] == {"P0": 0, "P1": 0, "P2": 0}
    assert accepted["source_revision"] == source
    assert accepted["reviewed_head"] == source
    assert accepted["fixed_package_sha256"] == report_module._sha256(
        package_path.read_bytes()
    )
    assert accepted["review_output_sha256"] == report_module._sha256(
        review_path.read_bytes()
    )
    assert "sha" not in " ".join(descriptor).lower()
    caller_hash = dict(descriptor, fixed_package_sha256="0" * 64)
    assert (
        report_module._review_gate_from_descriptor(source, caller_hash)["status"]
        == "PENDING"
    )


def test_s16a_review_content_change_wrong_head_tamper_and_fake_pass_fail_closed(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(report_module, "REVIEW_EVIDENCE_ROOT", tmp_path.resolve())
    source, descriptor, package_path, review_path = _real_review_fixture(tmp_path)
    accepted = report_module._review_gate_from_descriptor(source, descriptor)

    review_path.write_text(
        review_path.read_text(encoding="utf-8") + "post-read modification\n",
        encoding="utf-8",
    )
    assert report_module._preserve_reviewer_gate(source, accepted)["status"] == "PENDING"

    source, descriptor, package_path, review_path = _real_review_fixture(tmp_path)
    wrong_source = dict(descriptor, expected_source_revision="0" * 40)
    assert (
        report_module._review_gate_from_descriptor(source, wrong_source)["status"]
        == "PENDING"
    )

    source, descriptor, package_path, review_path = _real_review_fixture(tmp_path)
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["head_revision"] = package["base_revision"]
    package_path.write_text(json.dumps(package), encoding="utf-8")
    assert report_module._review_gate_from_descriptor(source, descriptor)["status"] == "PENDING"

    source, descriptor, package_path, review_path = _real_review_fixture(tmp_path)
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["commits"] = list(reversed(package["commits"])) + [package["base_revision"]]
    package_path.write_text(json.dumps(package), encoding="utf-8")
    assert report_module._review_gate_from_descriptor(source, descriptor)["status"] == "PENDING"

    source, descriptor, package_path, review_path = _real_review_fixture(tmp_path)
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["diff"] += "tampered diff\n"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    assert report_module._review_gate_from_descriptor(source, descriptor)["status"] == "PENDING"

    source, descriptor, _package_path, review_path = _real_review_fixture(tmp_path)
    review_path.write_text("Final Verdict: PASS\nP0=0 P1=0 P2=0\n", encoding="utf-8")
    assert report_module._review_gate_from_descriptor(source, descriptor)["status"] == "PENDING"


def test_s16a_review_output_rejects_ambiguous_blocks_and_finding_count_mismatch(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(report_module, "REVIEW_EVIDENCE_ROOT", tmp_path.resolve())
    source, descriptor, _package_path, review_path = _real_review_fixture(tmp_path)
    valid_block = review_path.read_text(encoding="utf-8")
    review_path.write_text(valid_block + valid_block, encoding="utf-8")
    assert report_module._review_gate_from_descriptor(source, descriptor)["status"] == "PENDING"

    _source, descriptor, _package_path, review_path = _real_review_fixture(tmp_path)
    mismatch = (
        "[P1] actual finding\n"
        "[S16A_REVIEW_RESULT_V1]\n"
        f"source_revision={source}\nreviewed_head={source}\n"
        "final_verdict=CHANGES REQUIRED\nP0=0\nP1=0\nP2=0\n"
        "[/S16A_REVIEW_RESULT_V1]\n"
    )
    review_path.write_text(mismatch, encoding="utf-8")
    assert report_module._review_gate_from_descriptor(source, descriptor)["status"] == "PENDING"


def test_s16a_review_paths_reject_escape_and_dotdot(
    monkeypatch, tmp_path: Path
) -> None:
    evidence_root = tmp_path / "inside"
    evidence_root.mkdir()
    monkeypatch.setattr(report_module, "REVIEW_EVIDENCE_ROOT", evidence_root.resolve())
    source, descriptor, package_path, review_path = _real_review_fixture(evidence_root)
    outside = tmp_path / "outside-review.md"
    outside.write_text(review_path.read_text(encoding="utf-8"), encoding="utf-8")
    escaped = dict(descriptor, review_output_path=str(outside.resolve()))
    assert report_module._review_gate_from_descriptor(source, escaped)["status"] == "PENDING"

    dotdot = dict(
        descriptor,
        fixed_package_path=str(package_path.parent / "nested" / ".." / package_path.name),
    )
    assert report_module._review_gate_from_descriptor(source, dotdot)["status"] == "PENDING"


def test_s16a_review_path_rejects_external_symlink(monkeypatch, tmp_path: Path) -> None:
    evidence_root = tmp_path / "inside"
    evidence_root.mkdir()
    monkeypatch.setattr(report_module, "REVIEW_EVIDENCE_ROOT", evidence_root.resolve())
    source, descriptor, _package_path, review_path = _real_review_fixture(evidence_root)
    outside = tmp_path / "outside-review.md"
    outside.write_bytes(review_path.read_bytes())
    link = evidence_root / "external-review-link.md"
    try:
        os.symlink(outside, link)
    except OSError as exc:
        pytest.skip(f"host does not permit file symlink creation: {exc}")
    linked = dict(descriptor, review_output_path=str(link.absolute()))
    assert report_module._review_gate_from_descriptor(source, linked)["status"] == "PENDING"


def test_s16a_review_path_rejects_external_windows_junction(
    monkeypatch, tmp_path: Path
) -> None:
    evidence_root = tmp_path / "inside"
    evidence_root.mkdir()
    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    monkeypatch.setattr(report_module, "REVIEW_EVIDENCE_ROOT", evidence_root.resolve())
    source, descriptor, _package_path, review_path = _real_review_fixture(evidence_root)
    outside_review = outside_directory / "review-output.md"
    outside_review.write_bytes(review_path.read_bytes())
    junction = evidence_root / "external-review-junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside_directory)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(
            "host cannot create Windows junction: "
            + (created.stderr or created.stdout).strip()
        )
    linked = dict(
        descriptor,
        review_output_path=str((junction / outside_review.name).absolute()),
    )
    assert report_module._review_gate_from_descriptor(source, linked)["status"] == "PENDING"


def test_s16a_same_source_bundle_preserves_evidence_and_source_drift_clears_it(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(report_module, "REVIEW_EVIDENCE_ROOT", tmp_path.resolve())
    source, descriptor, _package_path, _review_path = _real_review_fixture(tmp_path)
    accepted = report_module._review_gate_from_descriptor(source, descriptor)
    report = _passing_report()
    report["provenance"] = {"source_revision": source}
    report["reviewer_gate"] = accepted
    _redirect_reports(monkeypatch, tmp_path)
    report_module._publish_authoritative_bundle(report, "# reviewed PASS\n")

    assert report_module._preserve_reviewer_gate(source)["status"] == "PASS"
    assert report_module._preserve_reviewer_gate("f" * 40) == {
        "status": "PENDING",
        "findings": {"P0": None, "P1": None, "P2": None},
        "note": "no valid hash-bound reviewer evidence for current source revision",
    }


def test_s16a_execute_publication_preserve_and_drift_fail_closed(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(report_module, "REVIEW_EVIDENCE_ROOT", tmp_path.resolve())
    source, descriptor, package_path, review_path = _real_review_fixture(tmp_path)
    _redirect_reports(monkeypatch, tmp_path)
    real_run = report_module._run
    provenance = {"source": source, "tree": _git_text("rev-parse", "HEAD^{tree}").strip()}

    def fake_gate_run(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
        if label in {"review package commit list", "review package diff truth"}:
            return real_run(command, label)
        stdout = ""
        if label == "torch128 Python process.execPath":
            stdout = report_module.PYTHON + "\n"
        elif label == "torch128 Node process.execPath":
            stdout = str((tmp_path / "node.exe").absolute()) + "\n"
        elif label == "torch128 Node version":
            stdout = "v22.0.0\n"
        elif label == "source revision":
            stdout = provenance["source"] + "\n"
        elif label == "source tree":
            stdout = provenance["tree"] + "\n"
        elif "pytest" in label:
            stdout = "1 passed in 0.01s\n"
        elif label == "fixture validation":
            stdout = "fixtures_v1.0: PASS\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    assert report_module.main(descriptor, run_gate=fake_gate_run) == 0
    first = report_module._load_authoritative_report()
    assert first["reviewer_gate"]["status"] == "PASS"
    assert first["reviewer_gate"]["fixed_package_sha256"] == report_module._sha256(
        package_path.read_bytes()
    )
    assert first["reviewer_gate"]["review_output_sha256"] == report_module._sha256(
        review_path.read_bytes()
    )

    assert report_module.main(run_gate=fake_gate_run) == 0
    preserved = report_module._load_authoritative_report()
    assert preserved["reviewer_gate"] == first["reviewer_gate"]

    review_path.write_bytes(review_path.read_bytes() + b"changed after review\n")
    assert report_module.main(run_gate=fake_gate_run) == 0
    changed = report_module._load_authoritative_report()["reviewer_gate"]
    assert changed == {
        "status": "PENDING",
        "findings": {"P0": None, "P1": None, "P2": None},
        "note": "no valid hash-bound reviewer evidence for current source revision",
    }

    source, descriptor, _package_path, _review_path = _real_review_fixture(tmp_path)
    assert report_module.main(descriptor, run_gate=fake_gate_run) == 0
    assert report_module._load_authoritative_report()["reviewer_gate"]["status"] == "PASS"
    provenance["source"] = "f" * 40
    provenance["tree"] = "e" * 40
    assert report_module.main(run_gate=fake_gate_run) == 0
    drifted = report_module._load_authoritative_report()["reviewer_gate"]
    assert drifted["status"] == "PENDING"
    assert drifted["findings"] == {"P0": None, "P1": None, "P2": None}


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
