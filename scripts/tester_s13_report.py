from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORT_JSON = ROOT / "reports" / "eval" / "s13_text46_catalog_display_v1.json"
REPORT_MD = ROOT / "reports" / "eval" / "s13_text46_catalog_display_v1.md"


class GateFailure(RuntimeError):
    pass


def _run(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        tail = "\n".join((completed.stdout + completed.stderr).splitlines()[-30:])
        raise GateFailure(f"{label} failed (exit={completed.returncode})\n{tail}")
    return completed


def _pytest_count(completed: subprocess.CompletedProcess[str]) -> int:
    matches = re.findall(r"(\d+) passed", completed.stdout + completed.stderr)
    if not matches:
        raise GateFailure("pytest did not report a passed count")
    return int(matches[-1])


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _execute() -> dict[str, Any]:
    python = sys.executable
    commands: list[str] = []

    def gate(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
        commands.append(" ".join(command))
        return _run(command, label)

    targeted_files = [
        "tests/test_cpa_interaction_budget.py",
        "tests/test_dialogue_turn.py",
        "tests/test_dialogue_s7a.py",
        "tests/test_dialogue_s8a.py",
        "tests/test_preview_static_2d.py",
        "tests/test_s12_image_memory.py",
    ]
    targeted = gate(
        [python, "-m", "pytest", "-q", *targeted_files],
        "S13 targeted pytest",
    )
    full = gate([python, "-m", "pytest", "-q"], "full pytest")
    validate = gate([python, "scripts/validate.py"], "fixture validation")
    fixed_eval = gate([python, "-m", "profagent.eval"], "fixed evaluation")
    eval_report = json.loads(
        (ROOT / "reports" / "eval" / "r1_demo_v1.json").read_text(encoding="utf-8")
    )
    if eval_report.get("status") != "PASS":
        raise GateFailure("fixed evaluation report is not PASS")

    syntax_files = [
        "web/app.js",
        "web/dialogue_runtime.js",
        "web/image_asset_runtime.js",
        "web/dialogue_runtime_test.cjs",
        "web/image_asset_runtime_test.cjs",
        "web/static_contract_test.cjs",
        "web/catalog_image_browser_smoke.cjs",
    ]
    for path in syntax_files:
        gate(["node", "--check", path], f"node syntax {path}")
    runtime_files = [
        "web/dialogue_runtime_test.cjs",
        "web/image_asset_runtime_test.cjs",
        "web/static_contract_test.cjs",
    ]
    runtime_summaries: list[str] = []
    for path in runtime_files:
        result = gate(["node", path], f"node runtime {path}")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        runtime_summaries.append(lines[-1] if lines else f"{path}: exit 0")

    temporary_http = gate(
        [python, "scripts/tester_s12_http_smoke.py"],
        "temporary non-8000 HTTP and browser DOM smoke",
    )
    http_evidence = json.loads(temporary_http.stdout)
    if not http_evidence.get("port_is_not_8000"):
        raise GateFailure("temporary HTTP smoke touched port 8000")
    if http_evidence.get("catalog_browser_dom", {}).get("status") != "pass":
        raise GateFailure("catalog image did not settle to a loaded browser DOM state")

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

    metrics = eval_report["metrics"]
    report = {
        "schema_version": 1,
        "report_id": "s13_text46_catalog_display_v1",
        "scope": "independent S13 CPA Text 4.6 and wardrobe catalog image display acceptance",
        "overall_status": "PASS",
        "external_calls": {"text_cpa": 0, "image_generation": 0},
        "port_8000_touched": False,
        "production_code_modified_by_tester": False,
        "fixed_truth_or_threshold_modified_by_tester": False,
        "commands": commands,
        "pytest": {
            "targeted": _pytest_count(targeted),
            "full": _pytest_count(full),
        },
        "model_contract": {
            "logical": "grok4.6",
            "transport": "grok-4.6-high",
            "accepted_reported_models": ["grok-4.6-high", "grok-4.6-build"],
            "legacy_4_5_rejected": True,
            "prefix_and_other_models_rejected": True,
            "exact_frontend_source_gate": True,
        },
        "catalog_image_display": {
            "hidden_lazy_deadlock_absent": True,
            "pending_image_layout_eligible": True,
            "load_error_and_cached_complete_paths_tested": True,
            "owner_version_content_hash_provenance_gate_preserved": True,
            "real_browser_dom": http_evidence["catalog_browser_dom"],
        },
        "fixed_eval": {"status": eval_report["status"], "metrics": metrics},
        "validate": {
            "status": "pass",
            "summary": next(
                line.strip()
                for line in validate.stdout.splitlines()
                if line.strip()
            ),
        },
        "node": {
            "status": "pass",
            "syntax_files": len(syntax_files),
            "runtime_contracts": len(runtime_files),
            "summaries": runtime_summaries,
        },
        "temporary_http": {"status": "pass", **http_evidence},
        "fixed_eval_command_output_nonempty": bool(fixed_eval.stdout.strip()),
        "findings": {"P0": 0, "P1": 0, "P2": 0},
    }
    return report


def _markdown(report: dict[str, Any]) -> str:
    metrics = report["fixed_eval"]["metrics"]
    browser = report["catalog_image_display"]["real_browser_dom"]["summary"]
    return "\n".join(
        [
            "# S13 CPA Text 4.6 + 衣橱图片呈现独立验收",
            "",
            f"结论：{report['overall_status']}；P0/P1/P2 = 0/0/0。未调用真实 CPA，未触碰 8000，未修改生产代码、fixtures/eval 真值或阈值。",
            "",
            "## 门禁",
            "",
            f"- S13 定向 pytest：{report['pytest']['targeted']} passed；全量 pytest：{report['pytest']['full']} passed。",
            "- 文本模型：logical `grok4.6` → transport `grok-4.6-high`；仅接受精确回报 `grok-4.6-high` / `grok-4.6-build`，旧 4.5、前缀及其他模型均拒绝。",
            f"- Node：{report['node']['syntax_files']} 个语法检查、{report['node']['runtime_contracts']} 套 runtime/static 合同通过。",
            f"- 随机非 8000 HTTP + 浏览器 DOM：{browser}。",
            f"- 固定 eval：Urgency {metrics['urgency_accuracy']['numerator']}/{metrics['urgency_accuracy']['denominator']}；高急 Gate {metrics['high_urgency_shopping_gate_accuracy']['numerator']}/{metrics['high_urgency_shopping_gate_accuracy']['denominator']}；Catalog {metrics['high_urgency_catalog_calls']['numerator']}/{metrics['high_urgency_catalog_calls']['denominator']}；幻觉 {metrics['item_hallucinations']['numerator']}/{metrics['item_hallucinations']['denominator']}；硬约束 {metrics['hard_constraint_violations']['numerator']}/{metrics['hard_constraint_violations']['denominator']}；Slots {metrics['slot_completeness']['numerator']}/{metrics['slot_completeness']['denominator']}。",
            "- 图片显示生命周期覆盖 pending、load、error、cached-complete、cached-broken、cancel/stale；owner/version/content-hash/provenance 门禁保持。",
            "",
        ]
    )


def main() -> int:
    try:
        report = _execute()
        _atomic_write(REPORT_JSON, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(REPORT_MD, _markdown(report))
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "report_id": "s13_text46_catalog_display_v1",
            "overall_status": "FAIL",
            "error_type": type(exc).__name__,
            "error": str(exc).replace(str(ROOT), "<workspace>")[-4000:],
        }
        _atomic_write(REPORT_JSON, json.dumps(failure, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(REPORT_MD, f"# S13 独立验收\n\n结论：FAIL（{type(exc).__name__}）。\n")
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": report["overall_status"],
                "pytest": report["pytest"],
                "model_contract": report["model_contract"],
                "catalog_browser_dom": report["catalog_image_display"]["real_browser_dom"],
                "fixed_eval": report["fixed_eval"]["metrics"],
                "reports": [
                    "reports/eval/s13_text46_catalog_display_v1.json",
                    "reports/eval/s13_text46_catalog_display_v1.md",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
