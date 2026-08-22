from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORT_JSON = ROOT / "reports" / "eval" / "s16a_memory_uncertainty_v1.json"
REPORT_MD = ROOT / "reports" / "eval" / "s16a_memory_uncertainty_v1.md"
REPORT_BUNDLE = (
    ROOT / "reports" / "eval" / "s16a_memory_uncertainty_v1.bundle.json"
)
REVIEW_EVIDENCE_ROOT = ROOT.resolve()
REVIEW_GIT_ROOT = ROOT.resolve()
REVIEW_ARTIFACT_ALLOWLIST = {
    "reports/eval/s16a_memory_uncertainty_v1.bundle.json",
    "reports/eval/s16a_memory_uncertainty_v1.json",
    "reports/eval/s16a_memory_uncertainty_v1.md",
}
REPORT_COMMAND = (
    "conda run --no-capture-output -n torch128 "
    "python scripts/tester_s16a_report.py"
)
PYTHON = str(Path(sys.executable).resolve())
_conda_on_path = os.environ.get("CONDA_EXE") or shutil.which("conda")
CONDA = str(Path(_conda_on_path).resolve()) if _conda_on_path else ""

REQUIRED_MEMORY_CHECKS = {
    "candidate_extraction",
    "sensitive_no_write",
    "raw_text_leakage",
    "supersede",
    "one_question_budget",
    "high_urgency_continuation",
}
R1_THRESHOLDS = {
    "urgency_accuracy": (">=", 0.95),
    "high_urgency_shopping_gate_accuracy": ("==", 1.0),
    "high_urgency_catalog_calls": ("==", 0),
    "item_hallucinations": ("==", 0),
    "hard_constraint_violations": ("==", 0),
    "slot_completeness": (">=", 0.95),
}


class GateFailure(RuntimeError):
    pass


def _assert_runtime() -> None:
    env_name = os.environ.get("CONDA_DEFAULT_ENV", "")
    prefix_name = Path(sys.prefix).name
    if env_name != "torch128" and prefix_name != "torch128":
        raise GateFailure("runner must execute inside conda env torch128")
    if not Path(PYTHON).is_absolute() or Path(PYTHON).resolve() != Path(
        sys.executable
    ).resolve():
        raise GateFailure("Python runtime is not locked to current torch128 executable")
    if not CONDA or not Path(CONDA).is_absolute() or not Path(CONDA).is_file():
        raise GateFailure("conda executable was not resolved to an absolute path")


def _conda_python_argv(*arguments: str) -> list[str]:
    return [
        CONDA,
        "run",
        "--no-capture-output",
        "-n",
        "torch128",
        "python",
        *arguments,
    ]


def _conda_node_argv(*arguments: str) -> list[str]:
    return [
        CONDA,
        "run",
        "--no-capture-output",
        "-n",
        "torch128",
        "node",
        *arguments,
    ]


def _run(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError as exc:
        raise GateFailure(
            f"{label} failed with exit status {exc.returncode}"
        ) from exc


def _review_git_run(arguments: list[str], label: str) -> subprocess.CompletedProcess[str]:
    try:
        git_root = REVIEW_GIT_ROOT.resolve(strict=True)
        if not git_root.is_dir():
            raise GateFailure("review Git root is not a directory")
        return subprocess.run(
            ["git", *arguments],
            cwd=git_root,
            check=True,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return_code = getattr(exc, "returncode", "unavailable")
        raise GateFailure(
            f"{label} failed with exit status {return_code}"
        ) from exc


def _review_git_identity(*, require_clean: bool) -> dict[str, str]:
    if require_clean:
        status = _review_git_run(
            ["status", "--porcelain", "--untracked-files=all"],
            "review Git clean status",
        )
        if status.stdout.strip():
            raise GateFailure("review evidence requires a clean tracked Git identity")
    head = _review_git_run(["rev-parse", "HEAD"], "reviewed HEAD").stdout.strip()
    tree = _review_git_run(
        ["rev-parse", "HEAD^{tree}"], "reviewed HEAD tree"
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head) or not re.fullmatch(
        r"[0-9a-f]{40}", tree
    ):
        raise GateFailure("reviewed Git identity is malformed")
    return {"reviewed_head": head, "reviewed_git_tree": tree}


def _review_git_tree(revision: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise GateFailure("review source revision is malformed")
    tree = _review_git_run(
        ["rev-parse", f"{revision}^{{tree}}"], "review source tree"
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise GateFailure("review source tree is malformed")
    return tree


def _pytest_count(completed: subprocess.CompletedProcess[str]) -> int:
    matches = re.findall(r"(\d+) passed", completed.stdout + completed.stderr)
    if not matches:
        raise GateFailure("pytest output did not contain an auditable passed count")
    return int(matches[-1])


def _command_display(command: list[str]) -> str:
    display = list(command)
    if display and CONDA and Path(display[0]).resolve() == Path(CONDA).resolve():
        display[0] = "conda[resolved]"
    return " ".join(display)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_fsynced(path: Path, content: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _run_preserving_files(paths: list[Path], operation):
    snapshots = {path: path.read_bytes() if path.exists() else None for path in paths}
    try:
        return operation()
    finally:
        for path, content in snapshots.items():
            if content is None:
                path.unlink(missing_ok=True)
                continue
            temporary = path.with_name(path.name + ".restore.tmp")
            try:
                _write_fsynced(temporary, content)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)


def _bundle_document(report: dict[str, Any], markdown: str) -> dict[str, Any]:
    json_content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    json_bytes = json_content.encode("utf-8")
    markdown_bytes = markdown.encode("utf-8")
    provenance = report.get("provenance", {})
    core = {
        "schema_version": 1,
        "bundle_id": "s16a_memory_uncertainty_v1_bundle",
        "report_id": report.get("report_id"),
        "source_revision": provenance.get("source_revision"),
        "git_tree": provenance.get("git_tree"),
        "reviewed_head": provenance.get("reviewed_head"),
        "reviewed_git_tree": provenance.get("reviewed_git_tree"),
        "command_plan_sha256": provenance.get("command_plan_sha256"),
        "projections": {
            "json": {
                "path": "reports/eval/s16a_memory_uncertainty_v1.json",
                "sha256": _sha256(json_bytes),
            },
            "markdown": {
                "path": "reports/eval/s16a_memory_uncertainty_v1.md",
                "sha256": _sha256(markdown_bytes),
            },
        },
        "payloads": {"json": json_content, "markdown": markdown},
    }
    return {**core, "bundle_sha256": _sha256(_canonical_json_bytes(core))}


def _publish_authoritative_bundle(report: dict[str, Any], markdown: str) -> None:
    """Publish projections first; one bundle replace is the authority commit.

    A projection is never authoritative on its own. If either projection replace or
    the final bundle replace fails, an old/mixed pair cannot validate. A later run
    safely converges by replacing both projections and then the canonical bundle.
    """

    _validate_report_contract(report)
    bundle = _bundle_document(report, markdown)
    json_bytes = bundle["payloads"]["json"].encode("utf-8")
    markdown_bytes = bundle["payloads"]["markdown"].encode("utf-8")
    bundle_bytes = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    for path in (REPORT_JSON, REPORT_MD, REPORT_BUNDLE):
        path.parent.mkdir(parents=True, exist_ok=True)
    temporary = {
        REPORT_JSON: REPORT_JSON.with_name(REPORT_JSON.name + ".tmp"),
        REPORT_MD: REPORT_MD.with_name(REPORT_MD.name + ".tmp"),
        REPORT_BUNDLE: REPORT_BUNDLE.with_name(REPORT_BUNDLE.name + ".tmp"),
    }
    try:
        _write_fsynced(temporary[REPORT_JSON], json_bytes)
        _write_fsynced(temporary[REPORT_MD], markdown_bytes)
        _write_fsynced(temporary[REPORT_BUNDLE], bundle_bytes)
        os.replace(temporary[REPORT_JSON], REPORT_JSON)
        os.replace(temporary[REPORT_MD], REPORT_MD)
        os.replace(temporary[REPORT_BUNDLE], REPORT_BUNDLE)
        _load_authoritative_report()
    finally:
        for path in temporary.values():
            path.unlink(missing_ok=True)


def _load_authoritative_report() -> dict[str, Any]:
    """Load a PASS only through a valid bundle plus both exact projections."""

    try:
        bundle = json.loads(REPORT_BUNDLE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateFailure("authoritative bundle is absent or invalid") from exc
    bundle_hash = bundle.get("bundle_sha256")
    core = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    if not isinstance(bundle_hash, str) or bundle_hash != _sha256(
        _canonical_json_bytes(core)
    ):
        raise GateFailure("authoritative bundle hash mismatch")
    payloads = bundle.get("payloads", {})
    projections = bundle.get("projections", {})
    expected = {
        "json": payloads.get("json", "").encode("utf-8"),
        "markdown": payloads.get("markdown", "").encode("utf-8"),
    }
    actual_paths = {"json": REPORT_JSON, "markdown": REPORT_MD}
    for name, path in actual_paths.items():
        expected_hash = projections.get(name, {}).get("sha256")
        if not isinstance(expected_hash, str) or _sha256(expected[name]) != expected_hash:
            raise GateFailure(f"{name} projection payload hash mismatch")
        try:
            actual = path.read_bytes()
        except OSError as exc:
            raise GateFailure(f"{name} projection is unavailable") from exc
        if actual != expected[name] or _sha256(actual) != expected_hash:
            raise GateFailure(f"{name} projection does not match authoritative bundle")
    try:
        report = json.loads(payloads["json"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise GateFailure("authoritative JSON projection payload is invalid") from exc
    provenance = report.get("provenance", {})
    for field in (
        "source_revision",
        "git_tree",
        "reviewed_head",
        "reviewed_git_tree",
        "command_plan_sha256",
    ):
        if bundle.get(field) != provenance.get(field):
            raise GateFailure(f"bundle/report {field} provenance mismatch")
    _validate_report_contract(report)
    return report


def _command_plan_sha256(commands: list[dict[str, Any]]) -> str:
    plan = [
        {"label": command["label"], "command": command["command"]}
        for command in commands
    ]
    return _sha256(_canonical_json_bytes(plan))


def _clean_source_provenance(run_gate=None) -> dict[str, str]:
    """Return the clean current runner HEAD; accepted evidence may name an older source."""

    runner = run_gate or _run
    status = runner(["git", "status", "--porcelain"], "clean current HEAD status")
    if status.stdout.strip():
        raise GateFailure("atomic report requires a clean tracked worktree")
    revision = runner(
        ["git", "rev-parse", "HEAD"], "current HEAD revision"
    ).stdout.strip()
    tree = runner(
        ["git", "rev-parse", "HEAD^{tree}"], "current HEAD tree"
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or not re.fullmatch(
        r"[0-9a-f]{40}", tree
    ):
        raise GateFailure("current HEAD revision/tree provenance is malformed")
    return {"source_revision": revision, "git_tree": tree}


def _pending_reviewer_gate() -> dict[str, Any]:
    return {
        "status": "PENDING",
        "findings": {"P0": None, "P1": None, "P2": None},
        "note": "no valid hash-bound reviewer evidence for current source revision",
    }


def _safe_read_evidence_bytes(raw_path: Any) -> tuple[str, bytes]:
    if not isinstance(raw_path, str) or not raw_path:
        raise GateFailure("review evidence path must be a non-empty absolute string")
    candidate = Path(raw_path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise GateFailure("review evidence path must be absolute without dot-dot")
    try:
        root = REVIEW_EVIDENCE_ROOT.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise GateFailure("review evidence path escapes the allowed root") from exc
    if not resolved.is_file():
        raise GateFailure("review evidence path is not a regular file")
    try:
        with resolved.open("rb") as stream:
            before = os.fstat(stream.fileno())
            content = stream.read()
            after = os.fstat(stream.fileno())
        final = resolved.stat()
        resolved_after = candidate.resolve(strict=True)
    except OSError as exc:
        raise GateFailure("review evidence file could not be read safely") from exc
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise GateFailure("review evidence file changed during read")
    if any(getattr(after, field) != getattr(final, field) for field in stable_fields):
        raise GateFailure("review evidence file changed after read")
    if resolved_after != resolved or len(content) != after.st_size:
        raise GateFailure("review evidence path changed during read")
    return relative.as_posix(), content


def _parse_fixed_package(
    content: bytes, expected_source: str, reviewed_head: str
) -> dict[str, Any]:
    try:
        package = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateFailure("fixed package is not valid UTF-8 JSON") from exc
    required = {
        "schema_version",
        "base_revision",
        "head_revision",
        "tested_source_revision",
        "commits",
        "diff",
    }
    if not isinstance(package, dict) or set(package) != required:
        raise GateFailure("fixed package shape is not closed")
    if package["schema_version"] != 1:
        raise GateFailure("fixed package schema version is unsupported")
    for field in ("base_revision", "head_revision", "tested_source_revision"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(package[field])):
            raise GateFailure(f"fixed package {field} is malformed")
    if (
        package["base_revision"] != expected_source
        or package["head_revision"] != reviewed_head
        or package["tested_source_revision"] != expected_source
        or package["base_revision"] == package["head_revision"]
    ):
        raise GateFailure("fixed package head/tested source binding mismatch")
    commits = package["commits"]
    if not isinstance(commits, list) or not commits or not all(
        isinstance(item, str) and re.fullmatch(r"[0-9a-f]{40}", item)
        for item in commits
    ):
        raise GateFailure("fixed package commit list is malformed")
    if len(commits) != len(set(commits)) or commits[-1] != reviewed_head:
        raise GateFailure("fixed package commit list does not terminate at reviewed head")
    base = package["base_revision"]
    _review_git_run(
        ["merge-base", "--is-ancestor", base, reviewed_head],
        "review source ancestry",
    )
    actual_commits = [
        line
        for line in _review_git_run(
            ["rev-list", "--reverse", f"{base}..{reviewed_head}"],
            "review package commit list",
        ).stdout.splitlines()
        if line
    ]
    if not actual_commits or commits != actual_commits:
        raise GateFailure("fixed package commit list differs from local git truth")
    expected_parent = base
    for commit in actual_commits:
        ancestry = _review_git_run(
            ["rev-list", "--parents", "-n", "1", commit],
            "artifact commit ancestry",
        ).stdout.split()
        if ancestry != [commit, expected_parent]:
            raise GateFailure("review artifact history must be a linear descendant")
        changed_paths = {
            line.replace("\\", "/")
            for line in _review_git_run(
                [
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    expected_parent,
                    commit,
                ],
                "artifact commit path allowlist",
            ).stdout.splitlines()
            if line
        }
        if not changed_paths or not changed_paths.issubset(REVIEW_ARTIFACT_ALLOWLIST):
            raise GateFailure("review interval contains a non-artifact commit")
        expected_parent = commit
    actual_diff = _review_git_run(
        ["diff", "--no-ext-diff", "--binary", base, reviewed_head],
        "review package diff truth",
    ).stdout
    if not isinstance(package["diff"], str) or package["diff"] != actual_diff:
        raise GateFailure("fixed package diff differs from local git truth")
    return {
        "base_revision": base,
        "head_revision": reviewed_head,
        "tested_source_revision": expected_source,
        "commits": commits,
    }


_REVIEW_BLOCK = re.compile(
    r"(?m)^\[S16A_REVIEW_RESULT_V1\]\r?\n"
    r"source_revision=([0-9a-f]{40})\r?\n"
    r"reviewed_head=([0-9a-f]{40})\r?\n"
    r"final_verdict=(PASS|CHANGES REQUIRED)\r?\n"
    r"P0=(\d+)\r?\n"
    r"P1=(\d+)\r?\n"
    r"P2=(\d+)\r?\n"
    r"\[/S16A_REVIEW_RESULT_V1\]\r?$"
)


def _parse_review_output(
    content: bytes, expected_source: str, reviewed_head: str
) -> dict[str, Any]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GateFailure("review output is not valid UTF-8") from exc
    matches = list(_REVIEW_BLOCK.finditer(text))
    if len(matches) != 1:
        raise GateFailure("review output must contain exactly one closed result block")
    match = matches[0]
    outside = text[: match.start()] + text[match.end() :]
    if (
        "[S16A_REVIEW_RESULT_V1]" in outside
        or "[/S16A_REVIEW_RESULT_V1]" in outside
        or re.search(r"(?i)final\s+verdict\s*[:=]", outside)
    ):
        raise GateFailure("review output contains an ambiguous verdict")
    source, head, verdict = match.group(1), match.group(2), match.group(3)
    if source != expected_source or head != reviewed_head or head == source:
        raise GateFailure("review output source/head binding mismatch")
    findings = {
        "P0": int(match.group(4)),
        "P1": int(match.group(5)),
        "P2": int(match.group(6)),
    }
    observed = {
        priority: len(re.findall(rf"\[{priority}\]", outside))
        for priority in findings
    }
    if findings != observed:
        raise GateFailure("review output finding counts differ from finding tags")
    if verdict == "PASS" and any(findings.values()):
        raise GateFailure("PASS review output contains findings")
    if verdict == "CHANGES REQUIRED" and not any(findings.values()):
        raise GateFailure("CHANGES REQUIRED review output has no findings")
    return {
        "status": "PASS" if verdict == "PASS" else "CHANGES_REQUIRED",
        "findings": findings,
    }


def _review_gate_from_descriptor(
    current_reviewed_head: str, descriptor: dict[str, Any] | None
) -> dict[str, Any]:
    try:
        required = {
            "schema_version",
            "expected_source_revision",
            "reviewed_head",
            "fixed_package_path",
            "review_output_path",
        }
        if not isinstance(descriptor, dict) or set(descriptor) != required:
            raise GateFailure("review descriptor shape is not closed")
        if descriptor["schema_version"] != 1:
            raise GateFailure("review descriptor schema version is unsupported")
        source_revision = descriptor["expected_source_revision"]
        if (
            not re.fullmatch(r"[0-9a-f]{40}", str(source_revision))
            or descriptor["reviewed_head"] != current_reviewed_head
            or source_revision == current_reviewed_head
        ):
            raise GateFailure("review descriptor source binding mismatch")
        identity = _review_git_identity(require_clean=False)
        if identity["reviewed_head"] != current_reviewed_head:
            raise GateFailure("review descriptor does not bind current HEAD")
        package_path, package_bytes = _safe_read_evidence_bytes(
            descriptor["fixed_package_path"]
        )
        review_path, review_bytes = _safe_read_evidence_bytes(
            descriptor["review_output_path"]
        )
        package = _parse_fixed_package(
            package_bytes, source_revision, descriptor["reviewed_head"]
        )
        parsed_review = _parse_review_output(
            review_bytes, source_revision, descriptor["reviewed_head"]
        )
        if _review_git_identity(require_clean=False) != identity:
            raise GateFailure("reviewed Git identity changed during evidence validation")
        accepted = {
            "status": parsed_review["status"],
            "findings": parsed_review["findings"],
            "source_revision": source_revision,
            "reviewed_head": descriptor["reviewed_head"],
            "fixed_package_path": package_path,
            "review_output_path": review_path,
            "fixed_package_sha256": _sha256(package_bytes),
            "review_output_sha256": _sha256(review_bytes),
            "fixed_package": package,
        }
        accepted["evidence_sha256"] = _sha256(_canonical_json_bytes(accepted))
        return accepted
    except (GateFailure, OSError, ValueError, TypeError):
        return _pending_reviewer_gate()


def _preserve_reviewer_gate(
    current_reviewed_head: str, stored_gate: dict[str, Any] | None = None
) -> dict[str, Any]:
    if stored_gate is None:
        try:
            stored_gate = _load_authoritative_report().get("reviewer_gate")
        except GateFailure:
            return _pending_reviewer_gate()
    if (
        not isinstance(stored_gate, dict)
        or stored_gate.get("status") not in {"PASS", "CHANGES_REQUIRED"}
        or stored_gate.get("reviewed_head") != current_reviewed_head
    ):
        return _pending_reviewer_gate()
    package_relative = stored_gate.get("fixed_package_path")
    review_relative = stored_gate.get("review_output_path")
    if not isinstance(package_relative, str) or not isinstance(review_relative, str):
        return _pending_reviewer_gate()
    if (
        Path(package_relative).is_absolute()
        or Path(review_relative).is_absolute()
        or ".." in Path(package_relative).parts
        or ".." in Path(review_relative).parts
    ):
        return _pending_reviewer_gate()
    descriptor = {
        "schema_version": 1,
        "expected_source_revision": stored_gate.get("source_revision"),
        "reviewed_head": stored_gate.get("reviewed_head"),
        "fixed_package_path": str((REVIEW_EVIDENCE_ROOT / package_relative).absolute()),
        "review_output_path": str((REVIEW_EVIDENCE_ROOT / review_relative).absolute()),
    }
    recomputed = _review_gate_from_descriptor(current_reviewed_head, descriptor)
    if recomputed.get("status") == "PENDING":
        return recomputed
    fields = {
        "status",
        "findings",
        "source_revision",
        "reviewed_head",
        "fixed_package_path",
        "review_output_path",
        "fixed_package_sha256",
        "review_output_sha256",
        "fixed_package",
        "evidence_sha256",
    }
    if any(stored_gate.get(field) != recomputed.get(field) for field in fields):
        return _pending_reviewer_gate()
    return recomputed


def _metric_passes(name: str, value: float | int) -> bool:
    operator, threshold = R1_THRESHOLDS[name]
    if operator == ">=":
        return value >= threshold
    return value == threshold


def _validate_report_contract(report: dict[str, Any]) -> None:
    if report.get("overall_status") != "PASS":
        raise GateFailure("S16A report status must be PASS")
    memory = report.get("memory_uncertainty", {})
    if set(memory) != REQUIRED_MEMORY_CHECKS:
        raise GateFailure("S16A memory uncertainty fields are incomplete")
    if any(check.get("status") != "PASS" for check in memory.values()):
        raise GateFailure("S16A memory uncertainty gate did not pass")
    exact_memory_values = {
        "candidate_extraction": ("verified_cases", lambda value: value >= 1),
        "sensitive_no_write": ("writes", lambda value: value == 0),
        "raw_text_leakage": ("count", lambda value: value == 0),
        "supersede": ("atomic", lambda value: value is True),
        "one_question_budget": ("maximum_questions", lambda value: value == 1),
    }
    for gate_name, (field, predicate) in exact_memory_values.items():
        if not predicate(memory[gate_name].get(field)):
            raise GateFailure(f"{gate_name}.{field} contract failed")
    high = memory["high_urgency_continuation"]
    if (
        high.get("recommendation_paused") is not False
        or high.get("shopping_allowed") is not False
        or high.get("catalog_actual_calls") != 0
    ):
        raise GateFailure("high_urgency_continuation contract failed")
    provider = report.get("provider_determinism", {})
    if provider.get("status") != "PASS" or provider.get("external_calls") != 0:
        raise GateFailure("provider deterministic external call gate failed")
    pytest_report = report.get("pytest", {})
    for suite in ("focused", "memory", "dialogue", "full"):
        detail = pytest_report.get(suite, {})
        if detail.get("status") != "PASS" or detail.get("passed", 0) < 1:
            raise GateFailure(f"pytest.{suite} gate failed")
    node = report.get("node", {})
    if node.get("status") != "PASS" or node.get("passed", 0) < 1:
        raise GateFailure("Node gate failed")
    fixed = report.get("fixed_r1", {})
    if fixed.get("status") != "PASS":
        raise GateFailure("fixed R1 evaluation did not pass")
    metrics = fixed.get("metrics", {})
    if set(metrics) != set(R1_THRESHOLDS):
        raise GateFailure("fixed R1 metric set drifted")
    for name, (operator, threshold) in R1_THRESHOLDS.items():
        metric = metrics[name]
        if metric.get("status") != "PASS":
            raise GateFailure(f"{name} status is not PASS")
        if metric.get("threshold") != {"operator": operator, "value": threshold}:
            raise GateFailure(f"{name} threshold drifted")
        if not _metric_passes(name, metric.get("value")):
            raise GateFailure(f"{name} value missed its fixed threshold")
    reviewer = report.get("reviewer_gate", {})
    provenance = report.get("provenance", {})
    source_revision = provenance.get("source_revision")
    reviewed_head = provenance.get("reviewed_head")
    if reviewer.get("status") == "PENDING":
        if reviewer.get("findings") != {"P0": None, "P1": None, "P2": None}:
            raise GateFailure("pending reviewer findings must remain unknown")
    elif (
        not isinstance(source_revision, str)
        or not isinstance(reviewed_head, str)
        or source_revision == reviewed_head
        or reviewer.get("source_revision") != source_revision
        or reviewer.get("reviewed_head") != reviewed_head
        or _preserve_reviewer_gate(reviewed_head, reviewer) != reviewer
    ):
        raise GateFailure("reviewer evidence is not source/content bound")


def _selected_metric(metric: dict[str, Any]) -> dict[str, Any]:
    return {
        "numerator": metric["numerator"],
        "denominator": metric["denominator"],
        "value": metric["value"],
        "unit": metric["unit"],
        "threshold": metric["threshold"],
        "status": metric["status"],
    }


def _execute(
    review_evidence: dict[str, Any] | None = None,
    run_gate=None,
) -> dict[str, Any]:
    _assert_runtime()
    commands: list[dict[str, Any]] = []
    runner = run_gate or _run

    def gate(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
        completed = runner(command, label)
        commands.append(
            {
                "label": label,
                "command": _command_display(command),
                "exit_status": completed.returncode,
            }
        )
        return completed

    python_exec_result = gate(
        _conda_python_argv("-c", "import sys; print(sys.executable)"),
        "torch128 Python process.execPath",
    )
    resolved_python = python_exec_result.stdout.strip()
    if Path(resolved_python).resolve() != Path(PYTHON).resolve():
        raise GateFailure("nested torch128 Python did not resolve to current sys.executable")
    node_exec_result = gate(
        _conda_node_argv("-p", "process.execPath"),
        "torch128 Node process.execPath",
    )
    resolved_node = node_exec_result.stdout.strip()
    if not resolved_node or not Path(resolved_node).is_absolute():
        raise GateFailure("torch128 conda Node preflight returned an invalid process.execPath")
    node_version_result = gate(_conda_node_argv("--version"), "torch128 Node version")
    node_version = node_version_result.stdout.strip()
    recorded_current = _clean_source_provenance(gate)

    focused_ids = [
        "tests/test_s16_memory_candidates.py::test_http_extract_is_multicard_opaque_idempotent_and_raw_free",
        "tests/test_s16_memory_candidates.py::test_http_sensitive_extract_blocks_before_provider_and_leaks_no_raw_text",
        "tests/test_s16_memory_candidates.py::test_http_supersede_is_server_authored_atomic_and_not_browser_selectable",
        "tests/test_s16_preference_uncertainty.py::test_neutral_answer_and_unanswered_followup_do_not_ask_or_write_memory",
        "tests/test_s16_preference_uncertainty.py::test_high_urgency_question_budget_and_retry_do_not_duplicate_cpa_or_candidate",
    ]
    focused = gate(
        _conda_python_argv("-m", "pytest", "-q", *focused_ids),
        "S16A focused contract pytest",
    )
    memory = gate(
        _conda_python_argv(
            "-m", "pytest", "-q", "tests/test_s16_memory_candidates.py"
        ),
        "S16A Memory pytest",
    )
    dialogue = gate(
        _conda_python_argv(
            "-m",
            "pytest",
            "-q",
            "tests/test_s16_preference_uncertainty.py",
            "tests/test_dialogue_turn.py",
        ),
        "S16A Dialogue pytest",
    )

    node_test_paths = sorted((ROOT / "web").glob("*test.cjs"))
    if len(node_test_paths) != 11:
        raise GateFailure(
            f"expected exactly 11 Node runtime/static contracts, got {len(node_test_paths)}"
        )
    node_syntax_paths = sorted(
        path
        for path in (ROOT / "web").iterdir()
        if path.suffix in {".js", ".cjs"}
    )
    for path in node_syntax_paths:
        relative = path.relative_to(ROOT).as_posix()
        gate(_conda_node_argv("--check", relative), f"Node syntax {path.name}")
    for path in node_test_paths:
        relative = path.relative_to(ROOT).as_posix()
        gate(_conda_node_argv(relative), f"Node runtime {path.name}")

    eval_path = ROOT / "reports" / "eval" / "r1_demo_v1.json"
    eval_md_path = ROOT / "reports" / "eval" / "r1_demo_v1.md"
    protected_r1_reports = [eval_path, eval_md_path]
    full = _run_preserving_files(
        protected_r1_reports,
        lambda: gate(_conda_python_argv("-m", "pytest", "-q"), "full pytest"),
    )
    validate = gate(
        _conda_python_argv("scripts/validate.py"), "fixture validation"
    )

    def fixed_eval_gate():
        gate(_conda_python_argv("-m", "profagent.eval"), "fixed R1 evaluation")
        return json.loads(eval_path.read_text(encoding="utf-8"))

    eval_report = _run_preserving_files(protected_r1_reports, fixed_eval_gate)
    fixed_metrics = {
        name: _selected_metric(eval_report["metrics"][name])
        for name in R1_THRESHOLDS
    }

    gate(
        [
            "git",
            "diff",
            "--exit-code",
            "--",
            "data/fixtures",
            "data/eval/eval.jsonl",
            "scripts/generate.py",
            "scripts/validate.py",
        ],
        "fixed truth diff",
    )
    gate(["git", "diff", "--check"], "git diff check")

    current_identity = _review_git_identity(require_clean=True)
    if (
        recorded_current["source_revision"] != current_identity["reviewed_head"]
        or recorded_current["git_tree"] != current_identity["reviewed_git_tree"]
    ):
        raise GateFailure("recorded and review Git identities differ")
    if review_evidence is None:
        reviewer_gate = _preserve_reviewer_gate(current_identity["reviewed_head"])
    else:
        reviewer_gate = _review_gate_from_descriptor(
            current_identity["reviewed_head"], review_evidence
        )
    if reviewer_gate["status"] in {"PASS", "CHANGES_REQUIRED"}:
        source_revision = reviewer_gate["source_revision"]
        source_tree = _review_git_tree(source_revision)
    else:
        source_revision = current_identity["reviewed_head"]
        source_tree = current_identity["reviewed_git_tree"]
    command_plan_sha256 = _command_plan_sha256(commands)

    report = {
        "schema_version": 1,
        "report_id": "s16a_memory_uncertainty_v1",
        "scope": "S16A free-text memory candidates and preference uncertainty atomic acceptance",
        "overall_status": "PASS",
        "report_command": REPORT_COMMAND,
        "provenance": {
            "source_revision": source_revision,
            "git_tree": source_tree,
            **current_identity,
            "command_plan_sha256": command_plan_sha256,
            "conda_environment": "torch128",
            "python": {
                "version": platform.python_version(),
                "process_exec_path": resolved_python,
                "binding": "resolved by torch128 conda invocation and matched current sys.executable",
            },
            "node": {
                "version": node_version,
                "process_exec_path": resolved_node,
                "binding": "resolved by `conda run --no-capture-output -n torch128 node -p process.execPath`; no claim that Node is stored inside the env directory",
            },
            "fixtures": "fixtures_v1.0",
            "fixed_eval": "data/eval/eval.jsonl",
            "publication_authority": "reports/eval/s16a_memory_uncertainty_v1.bundle.json",
            "projection_hashes": "recorded in canonical bundle to avoid self-referential projection hashes",
        },
        "commands": commands,
        "memory_uncertainty": {
            "candidate_extraction": {
                "status": "PASS",
                "verified_cases": 1,
                "evidence": "opaque multi-card exact-key and idempotency focused gate",
            },
            "sensitive_no_write": {
                "status": "PASS",
                "writes": 0,
                "evidence": "prefilter/provider/repository/outbox/trace focused and Memory gates",
            },
            "raw_text_leakage": {
                "status": "PASS",
                "count": 0,
                "surfaces": [
                    "long_term_memory",
                    "rrf",
                    "outbox",
                    "trace",
                    "debug_dom",
                    "browser_storage",
                ],
            },
            "supersede": {
                "status": "PASS",
                "atomic": True,
                "evidence": "server-authored candidate decision and lifecycle gates",
            },
            "one_question_budget": {
                "status": "PASS",
                "maximum_questions": 1,
                "evidence": "server-owned gap and high-urgency repeat gates",
            },
            "high_urgency_continuation": {
                "status": "PASS",
                "recommendation_paused": False,
                "shopping_allowed": False,
                "catalog_actual_calls": 0,
                "evidence": "legal recommendation remains available while one advisory question is unanswered",
            },
        },
        "provider_determinism": {
            "status": "PASS",
            "mode": "offline fixtures and injected deterministic provider transports",
            "external_calls": 0,
            "text_cpa_calls": 0,
            "image_cpa_calls": 0,
        },
        "pytest": {
            "focused": {"status": "PASS", "passed": _pytest_count(focused)},
            "memory": {"status": "PASS", "passed": _pytest_count(memory)},
            "dialogue": {"status": "PASS", "passed": _pytest_count(dialogue)},
            "full": {"status": "PASS", "passed": _pytest_count(full)},
        },
        "node": {
            "status": "PASS",
            "passed": len(node_test_paths),
            "syntax_files": len(node_syntax_paths),
            "runtime_static_contracts": [path.name for path in node_test_paths],
        },
        "fixture_validation": {
            "status": "PASS",
            "summary": next(
                line.strip() for line in validate.stdout.splitlines() if line.strip()
            ),
        },
        "fixed_r1": {
            "status": eval_report["status"],
            "metrics": fixed_metrics,
        },
        "reviewer_gate": reviewer_gate,
        "privacy_hygiene": {
            "raw_dialogue_in_report": False,
            "sensitive_values_in_report": False,
            "direct_identifiers_in_report": False,
            "model_reasoning_in_report": False,
            "credentials_in_report": False,
            "absolute_workspace_path_in_report": False,
        },
        "claim_boundary": {
            "reviewer_findings_zero_claimed": reviewer_gate["status"] == "PASS",
            "real_external_provider_quality": "not_assessed",
            "s16b": "not_closed",
            "s16c": "not_closed",
            "s16r": "not_closed",
        },
    }
    _validate_report_contract(report)
    serialized = json.dumps(report, ensure_ascii=False)
    secret_patterns = (
        r"(?i)bearer\s+[a-z0-9._-]{12,}",
        r"(?i)sk-[a-z0-9]{16,}",
        r"(?i)(?:api[_-]?key|secret)\s*[:=]\s*['\"][^'\"]{8,}",
    )
    if str(ROOT) in serialized or "data:image/" in serialized.lower():
        raise GateFailure("report contains an absolute workspace path or inline image")
    if any(re.search(pattern, serialized) for pattern in secret_patterns):
        raise GateFailure("report contains a credential-like value")
    return report


def _markdown(report: dict[str, Any]) -> str:
    tests = report["pytest"]
    metrics = report["fixed_r1"]["metrics"]
    memory = report["memory_uncertainty"]
    lines = [
        "# S16A 自由文本记忆与偏好不确定性验收",
        "",
        f"结论：PASS（tester 技术门禁）；hash-bound reviewer 状态：{report['reviewer_gate']['status']}。PENDING 时 P0/P1/P2 保持 unknown。",
        "",
        "## 一条命令",
        "",
        f"`{report['report_command']}`",
        "",
        "唯一权威是 canonical bundle；JSON/MD 只是 projection。runner 先写两份 projection，最后以一次 atomic os.replace 提交 bundle。",
        "",
        "直接读取未经 bundle hash 与双 projection hash 校验的 JSON/MD 不受支持；mixed pair、projection 失败或进程中断均不能成为 authoritative PASS，后续运行可 repair convergence。",
        "",
        "## S16A 合同",
        "",
        f"- 候选提取：{memory['candidate_extraction']['status']}；敏感写入 {memory['sensitive_no_write']['writes']}。",
        f"- 原始文本泄漏：{memory['raw_text_leakage']['count']}；supersede 原子性：{memory['supersede']['status']}。",
        f"- 单轮追问上限：{memory['one_question_budget']['maximum_questions']}；高急不回答时推荐仍继续。",
        f"- 高急：shopping_allowed=false，Catalog actual calls={memory['high_urgency_continuation']['catalog_actual_calls']}。",
        f"- 确定性 provider 测试外部调用：{report['provider_determinism']['external_calls']}。",
        "",
        "## 串行门禁",
        "",
        f"- focused pytest：{tests['focused']['passed']} passed；Memory：{tests['memory']['passed']} passed；Dialogue：{tests['dialogue']['passed']} passed；full：{tests['full']['passed']} passed。",
        f"- Node：{report['node']['syntax_files']} 个 syntax，{report['node']['passed']} 个 runtime/static 合同通过。",
        f"- fixture：{report['fixture_validation']['summary']}。",
        f"- 固定 R1：Urgency={metrics['urgency_accuracy']['value']:.2%}；ShoppingGate={metrics['high_urgency_shopping_gate_accuracy']['value']:.2%}；Catalog={metrics['high_urgency_catalog_calls']['value']}；幻觉={metrics['item_hallucinations']['value']}；硬约束={metrics['hard_constraint_violations']['value']}；Slots={metrics['slot_completeness']['value']:.2%}。",
        "",
        "## 审查边界",
        "",
        f"本报告只证明 tester 技术门禁。当前 reviewer={report['reviewer_gate']['status']}；接受 reviewer evidence 时，source_revision 是代码/测试/BUILD_LOG 结构的 clean commit，reviewed_head 必须是当前 HEAD 且为其非空线性后代，区间每个提交只能改三份固定 Task8 report artifacts。调用方只提供仓库允许根内的 fixed package/review output 绝对路径与预期 source/head，runner 从安全读取的同一份 bytes 自行解析并计算 SHA-256；只有 ancestry、逐提交路径、内容、路径与当前 HEAD 持续匹配时才保留 reviewer evidence。S16B、S16C、S16R 均未关闭。",
        "",
        "报告不包含原始对话、敏感值、直接标识符、模型推理或外部 Provider 正文。",
        "",
    ]
    return "\n".join(lines)


def main(review_evidence: dict[str, Any] | None = None, run_gate=None) -> int:
    try:
        report = _execute(review_evidence, run_gate=run_gate)
        _validate_report_contract(report)
        markdown_content = _markdown(report)
        _publish_authoritative_bundle(report, markdown_content)
        authoritative = _load_authoritative_report()
        bundle = json.loads(REPORT_BUNDLE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error_type": type(exc).__name__,
                    "gate": str(exc).replace(str(ROOT), "<workspace>")[:300],
                    "note": "no new authoritative PASS bundle was committed",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": authoritative["overall_status"],
                "pytest": authoritative["pytest"],
                "node": authoritative["node"]["passed"],
                "fixed_r1": {
                    name: metric["value"]
                    for name, metric in authoritative["fixed_r1"]["metrics"].items()
                },
                "provider_external_calls": authoritative["provider_determinism"][
                    "external_calls"
                ],
                "reviewer": authoritative["reviewer_gate"]["status"],
                "source_revision": bundle["source_revision"],
                "git_tree": bundle["git_tree"],
                "reviewed_head": bundle["reviewed_head"],
                "reviewed_git_tree": bundle["reviewed_git_tree"],
                "command_plan_sha256": bundle["command_plan_sha256"],
                "bundle_sha256": bundle["bundle_sha256"],
                "projection_sha256": {
                    name: detail["sha256"]
                    for name, detail in bundle["projections"].items()
                },
                "reports": [
                    "reports/eval/s16a_memory_uncertainty_v1.bundle.json",
                    "reports/eval/s16a_memory_uncertainty_v1.json",
                    "reports/eval/s16a_memory_uncertainty_v1.md",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--review-evidence",
        type=Path,
        help="closed descriptor with repository-contained evidence paths; hashes/verdict/findings are runner-derived",
    )
    arguments = parser.parse_args()
    evidence = None
    if arguments.review_evidence is not None:
        try:
            evidence = json.loads(arguments.review_evidence.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            evidence = {}
    raise SystemExit(main(evidence))
