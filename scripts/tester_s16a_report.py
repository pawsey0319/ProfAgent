from __future__ import annotations

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
REPORT_COMMAND = (
    "conda run --no-capture-output -n torch128 "
    "python scripts/tester_s16a_report.py"
)
PYTHON = str(Path(sys.executable).resolve())
_node_on_path = shutil.which("node")
NODE = str(Path(_node_on_path).resolve()) if _node_on_path else ""

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
    if not NODE or not Path(NODE).is_absolute() or not Path(NODE).is_file():
        raise GateFailure("Node runtime was not resolved to an absolute executable")


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


def _pytest_count(completed: subprocess.CompletedProcess[str]) -> int:
    matches = re.findall(r"(\d+) passed", completed.stdout + completed.stderr)
    if not matches:
        raise GateFailure("pytest output did not contain an auditable passed count")
    return int(matches[-1])


def _command_display(command: list[str]) -> str:
    display = list(command)
    if display and Path(display[0]).resolve() == Path(PYTHON).resolve():
        display[0] = "python[torch128]"
    elif display and NODE and Path(display[0]).resolve() == Path(NODE).resolve():
        display[0] = "node[torch128-path]"
    return " ".join(display)


def _atomic_replace_pair(
    json_path: Path,
    json_content: str,
    markdown_path: Path,
    markdown_content: str,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_temp = json_path.with_name(json_path.name + ".tmp")
    markdown_temp = markdown_path.with_name(markdown_path.name + ".tmp")
    previous = {
        json_path: json_path.read_bytes() if json_path.exists() else None,
        markdown_path: markdown_path.read_bytes() if markdown_path.exists() else None,
    }
    json_replaced = False
    markdown_replaced = False
    try:
        json_temp.write_text(json_content, encoding="utf-8")
        markdown_temp.write_text(markdown_content, encoding="utf-8")
        os.replace(json_temp, json_path)
        json_replaced = True
        os.replace(markdown_temp, markdown_path)
        markdown_replaced = True
    except Exception:
        for path, was_replaced in (
            (json_path, json_replaced),
            (markdown_path, markdown_replaced),
        ):
            if not was_replaced:
                continue
            old_content = previous[path]
            if old_content is None:
                path.unlink(missing_ok=True)
                continue
            rollback = path.with_name(path.name + ".rollback.tmp")
            try:
                rollback.write_bytes(old_content)
                os.replace(rollback, path)
            finally:
                rollback.unlink(missing_ok=True)
        raise
    finally:
        json_temp.unlink(missing_ok=True)
        markdown_temp.unlink(missing_ok=True)


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
    if reviewer.get("status") != "PENDING" or reviewer.get("findings") != {
        "P0": None,
        "P1": None,
        "P2": None,
    }:
        raise GateFailure("reviewer findings must remain honestly pending")


def _selected_metric(metric: dict[str, Any]) -> dict[str, Any]:
    return {
        "numerator": metric["numerator"],
        "denominator": metric["denominator"],
        "value": metric["value"],
        "unit": metric["unit"],
        "threshold": metric["threshold"],
        "status": metric["status"],
    }


def _execute() -> dict[str, Any]:
    _assert_runtime()
    commands: list[dict[str, Any]] = []

    def gate(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
        completed = _run(command, label)
        commands.append(
            {
                "label": label,
                "command": _command_display(command),
                "exit_status": completed.returncode,
            }
        )
        return completed

    node_version_result = gate([NODE, "--version"], "locked Node version")
    node_version = node_version_result.stdout.strip()

    focused_ids = [
        "tests/test_s16_memory_candidates.py::test_http_extract_is_multicard_opaque_idempotent_and_raw_free",
        "tests/test_s16_memory_candidates.py::test_http_sensitive_extract_blocks_before_provider_and_leaks_no_raw_text",
        "tests/test_s16_memory_candidates.py::test_http_supersede_is_server_authored_atomic_and_not_browser_selectable",
        "tests/test_s16_preference_uncertainty.py::test_neutral_answer_and_unanswered_followup_do_not_ask_or_write_memory",
        "tests/test_s16_preference_uncertainty.py::test_high_urgency_question_budget_and_retry_do_not_duplicate_cpa_or_candidate",
    ]
    focused = gate(
        [PYTHON, "-m", "pytest", "-q", *focused_ids],
        "S16A focused contract pytest",
    )
    memory = gate(
        [PYTHON, "-m", "pytest", "-q", "tests/test_s16_memory_candidates.py"],
        "S16A Memory pytest",
    )
    dialogue = gate(
        [
            PYTHON,
            "-m",
            "pytest",
            "-q",
            "tests/test_s16_preference_uncertainty.py",
            "tests/test_dialogue_turn.py",
        ],
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
        gate([NODE, "--check", relative], f"Node syntax {path.name}")
    for path in node_test_paths:
        relative = path.relative_to(ROOT).as_posix()
        gate([NODE, relative], f"Node runtime {path.name}")

    full = gate([PYTHON, "-m", "pytest", "-q"], "full pytest")
    validate = gate([PYTHON, "scripts/validate.py"], "fixture validation")
    gate([PYTHON, "-m", "profagent.eval"], "fixed R1 evaluation")

    eval_path = ROOT / "reports" / "eval" / "r1_demo_v1.json"
    eval_report = json.loads(eval_path.read_text(encoding="utf-8"))
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
    source_revision = gate(
        ["git", "rev-parse", "HEAD"], "source revision"
    ).stdout.strip()

    report = {
        "schema_version": 1,
        "report_id": "s16a_memory_uncertainty_v1",
        "scope": "S16A free-text memory candidates and preference uncertainty atomic acceptance",
        "overall_status": "PASS",
        "report_command": REPORT_COMMAND,
        "provenance": {
            "source_revision": source_revision,
            "conda_environment": "torch128",
            "python": {
                "version": platform.python_version(),
                "binding": "current sys.executable resolved inside torch128",
            },
            "node": {
                "version": node_version,
                "binding": "absolute executable resolved from torch128 process PATH",
            },
            "fixtures": "fixtures_v1.0",
            "fixed_eval": "data/eval/eval.jsonl",
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
        "reviewer_gate": {
            "status": "PENDING",
            "findings": {"P0": None, "P1": None, "P2": None},
            "note": "read-only reviewer evidence has not yet been run for this report commit",
        },
        "privacy_hygiene": {
            "raw_dialogue_in_report": False,
            "sensitive_values_in_report": False,
            "direct_identifiers_in_report": False,
            "model_reasoning_in_report": False,
            "credentials_in_report": False,
            "absolute_workspace_path_in_report": False,
        },
        "claim_boundary": {
            "reviewer_findings_zero_claimed": False,
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
        "结论：PASS（tester 技术门禁）；后续只读 reviewer 状态：PENDING。未伪造 reviewer P0/P1/P2 为 0。",
        "",
        "## 一条命令",
        "",
        f"`{report['report_command']}`",
        "",
        "只有全部门禁成功后才原子替换本 JSON+MD。任一门禁或第二次 replace 失败，既有 PASS 报告保持不变且不留下临时半成品。",
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
        "本报告只证明 tester 技术门禁。S16A 的后续只读 reviewer 尚未运行，因此 finding 数量保持 unknown；S16B、S16C、S16R 均未关闭。",
        "",
        "报告不包含原始对话、敏感值、直接标识符、模型推理或外部 Provider 正文。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    try:
        report = _execute()
        _validate_report_contract(report)
        json_content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        markdown_content = _markdown(report)
        _atomic_replace_pair(
            REPORT_JSON,
            json_content,
            REPORT_MD,
            markdown_content,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error_type": type(exc).__name__,
                    "note": "existing PASS report, if any, was not replaced",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": report["overall_status"],
                "pytest": report["pytest"],
                "node": report["node"]["passed"],
                "fixed_r1": {
                    name: metric["value"]
                    for name, metric in report["fixed_r1"]["metrics"].items()
                },
                "provider_external_calls": report["provider_determinism"][
                    "external_calls"
                ],
                "reviewer": report["reviewer_gate"]["status"],
                "reports": [
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
    raise SystemExit(main())
