from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORT_JSON = ROOT / "reports" / "eval" / "s14_continuity_preview_v1.json"
REPORT_MD = ROOT / "reports" / "eval" / "s14_continuity_preview_v1.md"
REAL_REPORT = ROOT / "reports" / "demo" / "s14_real_recommendation_previews.json"


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

    def gate(command: list[str], label: str, display: str | None = None):
        commands.append(display or " ".join(command))
        return _run(command, label)

    s14 = gate(
        [python, "-m", "pytest", "-q", "tests/test_s14_backend.py"],
        "S14 backend pytest",
        "python -m pytest -q tests/test_s14_backend.py",
    )
    related_files = [
        "tests/test_dialogue_turn.py",
        "tests/test_dialogue_s7a.py",
        "tests/test_dialogue_s8a.py",
        "tests/test_preview_static_2d.py",
        "tests/test_r1_workflows.py",
        "tests/test_s12_image_memory.py",
        "tests/test_s14_backend.py",
    ]
    related = gate(
        [python, "-m", "pytest", "-q", *related_files],
        "S14 related dialogue/preview/shopping/memory pytest",
        "python -m pytest -q " + " ".join(related_files),
    )
    full = gate(
        [python, "-m", "pytest", "-q"],
        "full pytest",
        "python -m pytest -q",
    )
    validate = gate(
        [python, "scripts/validate.py"],
        "fixture validation",
        "python scripts/validate.py",
    )
    gate(
        [python, "-m", "profagent.eval"],
        "fixed evaluation",
        "python -m profagent.eval",
    )
    eval_report = json.loads(
        (ROOT / "reports" / "eval" / "r1_demo_v1.json").read_text(encoding="utf-8")
    )
    if eval_report.get("status") != "PASS" or any(
        metric.get("status") != "PASS"
        for metric in eval_report.get("metrics", {}).values()
    ):
        raise GateFailure("fixed evaluation did not pass")

    web_files = sorted(
        path for path in (ROOT / "web").iterdir() if path.suffix in {".js", ".cjs"}
    )
    for path in web_files:
        gate(
            ["node", "--check", str(path)],
            f"node syntax {path.name}",
            f"node --check web/{path.name}",
        )
    runtime_files = sorted((ROOT / "web").glob("*test.cjs"))
    runtime_summaries: list[str] = []
    for path in runtime_files:
        result = gate(
            ["node", str(path)],
            f"node runtime {path.name}",
            f"node web/{path.name}",
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        runtime_summaries.append(lines[-1] if lines else f"{path.name}: exit 0")

    http = gate(
        [python, "scripts/tester_s14_http_smoke.py"],
        "S14 random non-8000 HTTP + mock provider + Chrome DOM",
        "python scripts/tester_s14_http_smoke.py",
    )
    http_evidence = json.loads(http.stdout)
    if not http_evidence.get("port_is_not_8000"):
        raise GateFailure("S14 HTTP smoke touched port 8000")
    if http_evidence.get("provider_kind") != "mock_image_provider_no_external_calls":
        raise GateFailure("mock HTTP evidence did not identify itself as mock")
    if http_evidence.get("browser_dom", {}).get("status") != "pass":
        raise GateFailure("real Chrome wardrobe accordion evidence failed")

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

    real = json.loads(REAL_REPORT.read_text(encoding="utf-8"))
    if real.get("evidence_kind") != "real_cpa_image_provider":
        raise GateFailure("real CPA evidence kind is missing")
    if real.get("port_8000_touched") is not False or real.get("text_cpa_calls") != 0:
        raise GateFailure("real CPA evidence violated the isolated transport boundary")
    if real.get("image_cpa_attempts") != 2:
        raise GateFailure("real CPA evidence did not attempt exactly two images")

    report = {
        "schema_version": 1,
        "report_id": "s14_continuity_preview_v1",
        "scope": "independent S14 dialogue continuity, recommendation preview and wardrobe accordion acceptance",
        "overall_status": "PASS",
        "tester_external_calls": {
            "text_cpa": 0,
            "image_cpa": 2,
            "basis": "separate real TestClient smoke; random HTTP smoke remained mock",
        },
        "port_8000_touched": False,
        "production_code_modified_by_tester": False,
        "fixed_truth_or_threshold_modified_by_tester": False,
        "commands": commands,
        "pytest": {
            "s14_backend": _pytest_count(s14),
            "related_dialogue_preview_shopping_memory": _pytest_count(related),
            "full": _pytest_count(full),
        },
        "s14_contract": {
            "enter_submits": True,
            "shift_enter_newline": True,
            "ime_enter_no_submit": True,
            "single_flight_one_post": True,
            "scene_inherited_across_two_turns": True,
            "requested_outfit_count": 2,
            "exactly_two_legal_owner_outfits": True,
            "cpa_wardrobe_reask_rejected": True,
            "cpa_outfit_count_conflict_rejected": True,
            "cpa_high_repetition_rejected": True,
            "high_urgency_shopping_allowed": False,
            "high_urgency_catalog_calls": 0,
            "batch_previews_idempotent": True,
            "single_image_failure_isolated": True,
            "cross_owner_wrong_id_wrong_model_rejected": True,
            "three_d_video_client_fields_rejected": True,
        },
        "validate": {
            "status": "pass",
            "summary": next(line.strip() for line in validate.stdout.splitlines() if line.strip()),
        },
        "fixed_eval": {
            "status": eval_report["status"],
            "metrics": eval_report["metrics"],
        },
        "node": {
            "status": "pass",
            "syntax_files": len(web_files),
            "runtime_contracts": len(runtime_files),
            "runtime_summaries": runtime_summaries,
        },
        "random_non_8000_http": {
            "evidence_kind": "mock_image_provider",
            "must_not_be_reported_as_real_cpa": True,
            **http_evidence,
        },
        "real_cpa_recommendation_previews": real,
        "findings": {"P0": 0, "P1": 0, "P2": 0},
    }
    serialized = json.dumps(report, ensure_ascii=False)
    if str(ROOT) in serialized or "data:image/" in serialized.lower():
        raise GateFailure("report privacy scan failed")
    return report


def _markdown(report: dict[str, Any]) -> str:
    pytest = report["pytest"]
    metrics = report["fixed_eval"]["metrics"]
    http = report["random_non_8000_http"]
    real = report["real_cpa_recommendation_previews"]
    lines = [
        "# S14 连续搭配 + 推荐 2D + 衣橱折叠独立验收",
        "",
        f"结论：{report['overall_status']}；tester P0/P1/P2 = 0/0/0。未触碰现有 8000，未修改 production、fixtures、eval 真值或阈值。",
        "",
        "## 自动门禁",
        "",
        f"- S14 backend：{pytest['s14_backend']} passed；Dialogue/Preview/购物/Memory 相关：{pytest['related_dialogue_preview_shopping_memory']} passed；full：{pytest['full']} passed。",
        f"- Node：{report['node']['syntax_files']} 个 syntax、{report['node']['runtime_contracts']} 套 runtime/static 合同通过。",
        f"- 固定 eval：Urgency {metrics['urgency_accuracy']['numerator']}/{metrics['urgency_accuracy']['denominator']}；高急 Gate {metrics['high_urgency_shopping_gate_accuracy']['numerator']}/{metrics['high_urgency_shopping_gate_accuracy']['denominator']}；Catalog {metrics['high_urgency_catalog_calls']['numerator']}/{metrics['high_urgency_catalog_calls']['denominator']}；幻觉 {metrics['item_hallucinations']['numerator']}/{metrics['item_hallucinations']['denominator']}；硬约束 {metrics['hard_constraint_violations']['numerator']}/{metrics['hard_constraint_violations']['denominator']}；Slots {metrics['slot_completeness']['numerator']}/{metrics['slot_completeness']['denominator']}。",
        "- Enter/Shift+Enter/IME/single-flight、两轮 Scene 继承、requested=2/实际恰好 2、owner/硬约束、CPA 反问/套数冲突/高复读拒绝、Catalog0、预览幂等和单项失败隔离全部通过。",
        "",
        "## Mock HTTP 与真实浏览器（不是 CPA 证据）",
        "",
        f"随机非 8000 HTTP 明确使用 `{http['provider_kind']}`；mock 两图={http['mock_batch_statuses']}，mock 单项失败={http['mock_partial_statuses']}。Chrome：{http['browser_dom']['summary']}。这部分不得解释为真实 CPA 成功。",
        "",
        "## 独立真实 CPA 推荐两图",
        "",
        f"真实 TestClient 隔离调用状态：{real['status']}；image attempts={real['image_cpa_attempts']}；text CPA calls=0；未监听端口且 `port_8000_touched=false`。",
    ]
    for attempt in real.get("per_provider_attempt", []):
        detail = f"，{attempt.get('media_type')}，{attempt.get('bytes')} bytes，SHA-256 `{attempt.get('sha256')}`" if attempt["status"] == "succeeded" else f"，reason `{attempt.get('reason_code')}`"
        lines.append(f"- 图 {attempt['attempt']}：{attempt['status']}，{attempt['wall_clock_ms']} ms{detail}")
    lines.extend(
        [
            "",
            "真实 CPA 与 mock 证据在 JSON 中使用不同 `evidence_kind`，报告不保存 prompt、raw receipt、base64、API key 或图片正文。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    try:
        report = _execute()
        _atomic_write(REPORT_JSON, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(REPORT_MD, _markdown(report))
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "report_id": "s14_continuity_preview_v1",
            "overall_status": "FAIL",
            "error_type": type(exc).__name__,
            "error": str(exc).replace(str(ROOT), "<workspace>")[-4000:],
        }
        _atomic_write(REPORT_JSON, json.dumps(failure, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(REPORT_MD, f"# S14 独立验收\n\n结论：FAIL（{type(exc).__name__}）。\n")
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": report["overall_status"],
                "pytest": report["pytest"],
                "fixed_eval": report["fixed_eval"]["metrics"],
                "node": report["node"],
                "mock_http": report["random_non_8000_http"],
                "real_cpa": {
                    "status": report["real_cpa_recommendation_previews"]["status"],
                    "image_cpa_attempts": report["real_cpa_recommendation_previews"]["image_cpa_attempts"],
                    "per_provider_attempt": report["real_cpa_recommendation_previews"]["per_provider_attempt"],
                },
                "reports": [
                    "reports/eval/s14_continuity_preview_v1.json",
                    "reports/eval/s14_continuity_preview_v1.md",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
