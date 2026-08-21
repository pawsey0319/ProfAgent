from __future__ import annotations

import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from profagent.memory_service import MemoryRecord, MemoryService
from profagent.models import SceneConstraints, SceneRequest, UICapabilities
from profagent.tracing import TraceStore, utc_now


REPORT_JSON = ROOT / "reports" / "eval" / "s15_memory_route_a_v1.json"
REPORT_MD = ROOT / "reports" / "eval" / "s15_memory_route_a_v1.md"
REPORT_COMMAND = (
    "conda run --no-capture-output -n torch128 "
    "python scripts/tester_s15_report.py"
)


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
        tail = "\n".join((completed.stdout + completed.stderr).splitlines()[-40:])
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


class _SyntheticTruthRepository:
    def __init__(self, records: list[MemoryRecord]) -> None:
        self.records = records

    def load_state(self):
        return [], [], {}, {}

    def active_records(self, user_id, namespaces, _now):
        assert user_id == "u01"
        return [
            (item.model_dump(mode="json"), None)
            for item in self.records
            if item.namespace in namespaces
        ]

    def expire_ids(self, _ids, _expired_at):
        return None

    def close(self):
        return None


def _synthetic_scene(
    *,
    scenario_id: str,
    occasion: str,
    goals: list[str],
    comfort_notes: list[str] | None = None,
) -> SceneRequest:
    return SceneRequest(
        request_id=f"req_{scenario_id}",
        user_id="u01",
        styling_session_id=f"session_{scenario_id}",
        query_text="controlled synthetic benchmark",
        intent="recommend",
        occasion=occasion,
        event_horizon="soon",
        urgency="medium",
        shopping_allowed=False,
        goals=goals,
        constraints=SceneConstraints(comfort_notes=comfort_notes or []),
        backend="rule_fallback",
        ui_capabilities=UICapabilities(shopping_cta=False),
        trace_id=f"trace_{scenario_id}",
    )


def _ranking_metrics(order: list[str], relevant: set[str]) -> dict[str, float]:
    first = next((index for index, item in enumerate(order, 1) if item in relevant), None)
    return {
        "recall_at_5": round(len(set(order[:5]) & relevant) / len(relevant), 6),
        "recall_at_10": round(len(set(order[:10]) & relevant) / len(relevant), 6),
        "reciprocal_rank": round(1.0 / first if first else 0.0, 6),
    }


def _synthetic_rerank_benchmark() -> dict[str, Any]:
    now = utc_now()
    groups = {
        "straight": "偏爱直筒裤",
        "simple": "偏爱简洁风格",
        "low_key": "偏爱低调配色",
        "long_walk": "长时间站立时优先选择适合久走的鞋。",
    }
    records: list[MemoryRecord] = []
    group_ids: dict[str, set[str]] = {key: set() for key in groups}
    for group_index, (group, content) in enumerate(groups.items()):
        for index in range(6):
            memory_id = f"mem_synthetic_{group}_{index:02d}"
            created = now.replace(microsecond=0)
            confirmation_count = 1 + ((group_index + index) % 4)
            record = MemoryRecord(
                memory_id=memory_id,
                user_id="u01",
                namespace="stylist" if index % 2 else "shared",
                type="preference",
                content=content,
                ttl_days=None,
                source_proposal_id=f"mprop_synthetic_{group}_{index:02d}",
                created_at=created,
                expires_at=None,
                valid_from=created,
                supersedes_memory_id=(
                    f"mem_synthetic_prior_{group}_{index:02d}"
                    if confirmation_count > 1
                    else None
                ),
                confirmation_count=confirmation_count,
            )
            records.append(record)
            group_ids[group].add(memory_id)
    hard = MemoryRecord(
        memory_id="mem_synthetic_hard_excluded",
        user_id="u01",
        namespace="shared",
        type="constraint",
        content="不穿高跟鞋",
        ttl_days=None,
        source_proposal_id="mprop_synthetic_hard_excluded",
        created_at=now.replace(microsecond=0),
        expires_at=None,
        valid_from=now.replace(microsecond=0),
        memory_class="hard_constraint",
    )
    records.append(hard)
    service = MemoryService(
        TraceStore(), repository=_SyntheticTruthRepository(records)
    )
    scenarios = [
        (
            "long_walk",
            "今天要久走和长时间站立",
            _synthetic_scene(
                scenario_id="long_walk",
                occasion="commute",
                goals=["reliable"],
                comfort_notes=["long_walk"],
            ),
            group_ids["long_walk"],
        ),
        (
            "interview_simple",
            "面试想要可靠简洁",
            _synthetic_scene(
                scenario_id="interview_simple",
                occasion="interview",
                goals=["reliable"],
            ),
            group_ids["simple"],
        ),
        (
            "comfortable_straight",
            "希望舒服直筒",
            _synthetic_scene(
                scenario_id="comfortable_straight",
                occasion="daily",
                goals=["comfortable"],
            ),
            group_ids["straight"],
        ),
        (
            "meeting_low_key",
            "会议希望低调可靠",
            _synthetic_scene(
                scenario_id="meeting_low_key",
                occasion="meeting",
                goals=["low_key", "reliable"],
            ),
            group_ids["low_key"],
        ),
    ]
    per_scenario: list[dict[str, Any]] = []
    aggregated: dict[str, list[dict[str, float]]] = {
        "bm25": [],
        "dense": [],
        "weighted_rrf": [],
        "rrf_plus_structured_rerank": [],
    }
    latencies: list[float] = []
    for scenario_id, query, scene, relevant in scenarios:
        _terms, first = service.retrieve_soft("u01", query, scene=scene)
        _second_terms, second = service.retrieve_soft("u01", query, scene=scene)
        for key in ("branches", "controlled_scores", "selected_memory_ids"):
            if first[key] != second[key]:
                raise GateFailure(f"synthetic rerank is non-deterministic: {scenario_id}/{key}")
        if first["context_source"] != "authoritative_scene_v1":
            raise GateFailure("synthetic rerank did not use the authoritative Scene")
        if first["candidate_limit"] != 20 or any(
            len(first["branches"][branch]) != 20
            for branch in ("bm25", "dense", "recency", "importance")
        ):
            raise GateFailure("each S15 retrieval branch must be frozen at Top 20")
        serialized_trace = json.dumps(first, ensure_ascii=False)
        if hard.memory_id in serialized_trace or first["hard_memory_in_rrf"] is not False:
            raise GateFailure("hard memory entered the synthetic soft RRF trace")
        if first["content_logged"] is not False or first["vectors_logged"] is not False:
            raise GateFailure("synthetic trace exposed content or vectors")

        branch_orders = first["branches"]
        fused: dict[str, float] = {}
        for branch, ids in branch_orders.items():
            weight = first["weights"][branch]
            for rank, memory_id in enumerate(ids, 1):
                fused[memory_id] = fused.get(memory_id, 0.0) + weight / (
                    first["k"] + rank
                )
        rrf_order = [
            memory_id
            for memory_id, _score in sorted(
                fused.items(), key=lambda pair: (-pair[1], pair[0])
            )
        ]
        bm25_positions = {
            memory_id: index
            for index, memory_id in enumerate(branch_orders["bm25"], 1)
        }
        rerank_order = [
            memory_id
            for memory_id, _score in sorted(
                first["controlled_scores"].items(),
                key=lambda pair: (
                    -pair[1]["final"],
                    bm25_positions.get(pair[0], math.inf),
                    pair[0],
                ),
            )
        ]
        expected_formula = {
            "rrf_norm": 0.60,
            "context_match": 0.15,
            "specificity": 0.10,
            "confirmation_strength": 0.10,
            "lexical_norm": 0.05,
        }
        if first["rerank_formula"] != expected_formula:
            raise GateFailure("structured_rerank_v1 weights drifted")
        for memory_id, score in first["controlled_scores"].items():
            recomputed = (
                0.60 * score["rrf_norm"]
                + 0.15 * score["context_match"]
                + 0.10 * score["specificity"]
                + 0.10 * score["confirmation_strength"]
                + 0.05 * score["lexical_norm"]
            )
            if abs(recomputed - score["final"]) > 0.000002:
                raise GateFailure(
                    f"structured_rerank_v1 exact formula mismatch: {memory_id}"
                )
        orders = {
            "bm25": branch_orders["bm25"],
            "dense": branch_orders["dense"],
            "weighted_rrf": rrf_order,
            "rrf_plus_structured_rerank": rerank_order,
        }
        metrics = {
            name: _ranking_metrics(order, relevant) for name, order in orders.items()
        }
        for name, values in metrics.items():
            aggregated[name].append(values)
        latencies.extend([float(first["latency_ms"]), float(second["latency_ms"])])
        per_scenario.append(
            {
                "scenario_id": scenario_id,
                "relevant_count": len(relevant),
                "eligible_soft_count": first["eligible_count"],
                "fused_candidate_count": first["fused_candidate_count"],
                "branch_top_n": {
                    key: len(value) for key, value in branch_orders.items()
                },
                "metrics": metrics,
                "context_source": first["context_source"],
                "hard_memory_in_rrf": False,
                "deterministic_repeat": True,
            }
        )
    service.close()
    summary: dict[str, dict[str, float]] = {}
    for name, values in aggregated.items():
        summary[name] = {
            key: round(statistics.fmean(item[key] for item in values), 6)
            for key in ("recall_at_5", "recall_at_10", "reciprocal_rank")
        }
    ordered_latency = sorted(latencies)
    p95_index = max(0, math.ceil(0.95 * len(ordered_latency)) - 1)
    return {
        "status": "PASS",
        "evidence_kind": "controlled_synthetic_memory_retrieval_v1",
        "dataset": {
            "soft_records": 24,
            "hard_exclusion_records": 1,
            "scenarios": 4,
            "contains_real_user_data": False,
        },
        "algorithm": {
            "rrf_k": 60,
            "branch_candidate_limit": 20,
            "weights": {
                "bm25": 1.0,
                "dense": 1.0,
                "recency": 0.75,
                "importance": 1.25,
            },
            "dense_backend": "deterministic_hashed_surrogate_v1",
            "rerank": "structured_rerank_v1",
            "formula": {
                "rrf_norm": 0.60,
                "context_match": 0.15,
                "specificity": 0.10,
                "confirmation_strength": 0.10,
                "lexical_norm": 0.05,
            },
            "external_model_calls": 0,
        },
        "aggregate_metrics": summary,
        "latency_ms": {
            "samples": len(ordered_latency),
            "p95": ordered_latency[p95_index],
        },
        "pollution": {
            "hard_memory_in_rrf": 0,
            "unconfirmed_deleted_expired_superseded_cross_acl": 0,
            "basis": "S15 unit/HTTP gates plus controlled hard-exclusion record",
        },
        "per_scenario": per_scenario,
        "claim_boundary": {
            "real_user_quality_improvement": "not_assessed",
            "production_semantic_embedding": "not_implemented",
            "cross_encoder": "not_implemented",
            "allowed_interpretation": "deterministic contract and synthetic diagnostic only",
        },
    }


def _execute() -> dict[str, Any]:
    python = sys.executable
    commands: list[str] = []

    def gate(command: list[str], label: str, display: str | None = None):
        commands.append(display or " ".join(command))
        return _run(command, label)

    s15 = gate(
        [python, "-m", "pytest", "-q", "tests/test_s15_memory.py"],
        "S15 memory pytest",
        "python -m pytest -q tests/test_s15_memory.py",
    )
    related_files = [
        "tests/test_memory_rerank_policy.py",
        "tests/test_s12_image_memory.py",
        "tests/test_s15_memory.py",
        "tests/test_s15_report.py",
    ]
    related = gate(
        [python, "-m", "pytest", "-q", *related_files],
        "Memory related pytest",
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
        path
        for path in (ROOT / "web").iterdir()
        if path.suffix in {".js", ".cjs"}
    )
    for path in web_files:
        relative = path.relative_to(ROOT).as_posix()
        gate(
            ["node", "--check", relative],
            f"node syntax {path.name}",
            f"node --check {relative}",
        )
    runtime_files = sorted((ROOT / "web").glob("*test.cjs"))
    if len(runtime_files) != 7:
        raise GateFailure(f"expected exactly 7 web runtime/static contracts, got {len(runtime_files)}")
    runtime_summaries: list[str] = []
    for path in runtime_files:
        relative = path.relative_to(ROOT).as_posix()
        result = gate(
            ["node", relative],
            f"node runtime {path.name}",
            f"node {relative}",
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        runtime_summaries.append(lines[-1] if lines else f"{path.name}: exit 0")

    http = gate(
        [python, "scripts/tester_s15_http_smoke.py"],
        "S15 random non-8000 HTTP/restart/browser smoke",
        "python scripts/tester_s15_http_smoke.py",
    )
    http_evidence = json.loads(http.stdout)
    if (
        http_evidence.get("port_is_not_8000") is not True
        or http_evidence.get("external_provider_calls") != 0
        or http_evidence.get("browser_dom", {}).get("status") != "pass"
    ):
        raise GateFailure("S15 HTTP/browser evidence did not meet isolation gates")

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
    changed = gate(
        ["git", "status", "--short"], "git status", "git status --short"
    ).stdout
    diff = gate(
        ["git", "diff", "--"], "git diff privacy scan", "git diff --"
    ).stdout
    secret_patterns = [
        r"(?i)bearer\s+[a-z0-9._-]{12,}",
        r"(?i)sk-[a-z0-9]{16,}",
        r"(?i)(?:api[_-]?key|secret)\s*[:=]\s*['\"][^'\"]{8,}",
    ]
    if any(re.search(pattern, diff) for pattern in secret_patterns):
        raise GateFailure("git diff contains a credential-like value")

    benchmark = _synthetic_rerank_benchmark()
    metrics = eval_report["metrics"]
    report = {
        "schema_version": 1,
        "report_id": "s15_memory_route_a_v1",
        "scope": "independent S15 Memory Route A transaction truth, lifecycle, deterministic retrieval and management UI acceptance",
        "overall_status": "PASS",
        "report_command": REPORT_COMMAND,
        "tester_external_calls": {"text_cpa": 0, "image_cpa": 0},
        "port_8000_touched": False,
        "tester_write_scope": ["tests", "scripts", "reports"],
        "production_or_web_code_modified_by_tester": False,
        "fixed_truth_or_threshold_modified_by_tester": False,
        "commands": commands,
        "pytest": {
            "s15_memory": _pytest_count(s15),
            "memory_related": _pytest_count(related),
            "full": _pytest_count(full),
        },
        "validate": {
            "status": "pass",
            "summary": next(
                line.strip() for line in validate.stdout.splitlines() if line.strip()
            ),
        },
        "fixed_eval": {"status": eval_report["status"], "metrics": metrics},
        "node": {
            "status": "pass",
            "syntax_files": len(web_files),
            "runtime_contracts": len(runtime_files),
            "runtime_summaries": runtime_summaries,
        },
        "random_non_8000_http": http_evidence,
        "synthetic_rerank_benchmark": benchmark,
        "memory_contract": {
            "transactional_truth_outbox": "pass",
            "migration_idempotence": "pass",
            "restart_cross_instance_sqlite": "pass",
            "owner_namespace_acl": "pass",
            "future_expire_supersede_quarantine": "pass",
            "delete_receipt": "pass",
            "hard_memory_routed_outside_rrf": "pass",
            "structured_rerank_v1_exact": "pass",
            "scene_context_authoritative": "pass",
            "deterministic_repeats": "pass",
            "browser_invalid_removal_only": "pass",
        },
        "privacy_hygiene": {
            "outbox_contains_content_or_vectors": False,
            "trace_contains_content_or_vectors": False,
            "browser_invalid_body_rendered": False,
            "credentials_in_report": False,
            "absolute_workspace_path_in_report": False,
            "fixed_fixture_or_eval_truth_diff": False,
            "changed_file_count_observed": len(
                [line for line in changed.splitlines() if line.strip()]
            ),
        },
        "claim_boundary": {
            "real_cpa_called": False,
            "real_user_memory_quality": "not_assessed",
            "postgresql_live_integration": "not_run; dialect locking contract is statically gated",
            "langmem_mem0_graphiti_cross_encoder": "not_implemented",
        },
        "findings": {"P0": 0, "P1": 0, "P2": 0},
    }
    serialized = json.dumps(report, ensure_ascii=False)
    if str(ROOT) in serialized or "data:image/" in serialized.lower():
        raise GateFailure("report privacy/path scan failed")
    if any(re.search(pattern, serialized) for pattern in secret_patterns):
        raise GateFailure("report contains a credential-like value")
    return report


def _markdown(report: dict[str, Any]) -> str:
    pytest = report["pytest"]
    metrics = report["fixed_eval"]["metrics"]
    benchmark = report["synthetic_rerank_benchmark"]
    aggregate = benchmark["aggregate_metrics"]
    lines = [
        "# S15 Memory 路线 A 独立验收",
        "",
        "结论：PASS；tester P0/P1/P2 = 0/0/0。未调用 CPA，未触碰 8000，未修改 production/web、fixtures、eval 真值或阈值。",
        "",
        "## 一条命令",
        "",
        f"`{report['report_command']}`",
        "",
        "报告脚本启动时先原子写入 PENDING；仅全部门禁通过后原子替换为 PASS。任何异常都会原子替换为 FAIL 并返回非 0。",
        "",
        "## 串行门禁",
        "",
        f"- S15 专项：{pytest['s15_memory']} passed；Memory 相关：{pytest['memory_related']} passed；full：{pytest['full']} passed。",
        f"- Node：{report['node']['syntax_files']} 个 syntax；全部 {report['node']['runtime_contracts']} 个 runtime/static 合同通过。",
        f"- 固定 eval：Urgency {metrics['urgency_accuracy']['numerator']}/{metrics['urgency_accuracy']['denominator']}；高急 Gate {metrics['high_urgency_shopping_gate_accuracy']['numerator']}/{metrics['high_urgency_shopping_gate_accuracy']['denominator']}；Catalog {metrics['high_urgency_catalog_calls']['numerator']}/{metrics['high_urgency_catalog_calls']['denominator']}；幻觉 {metrics['item_hallucinations']['numerator']}/{metrics['item_hallucinations']['denominator']}；硬约束 {metrics['hard_constraint_violations']['numerator']}/{metrics['hard_constraint_violations']['denominator']}；Slots {metrics['slot_completeness']['numerator']}/{metrics['slot_completeness']['denominator']}。",
        "- 随机非 8000 HTTP 完成 propose→confirm→list→delete、重启/双实例 SQLite、owner/namespace/ACL、future/expire/supersede/quarantine 和无正文删除回执。",
        f"- 真实 Chrome：{report['random_non_8000_http']['browser_dom']['summary']}。",
        "",
        "## 受控合成检索基准",
        "",
        "该基准只验证确定性合同和受控诊断，不代表真实用户质量提升、生产语义 embedding 或 Cross-Encoder 效果。",
        "",
        "| 阶段 | Recall@5 | Recall@10 | MRR |",
        "|---|---:|---:|---:|",
    ]
    labels = {
        "bm25": "BM25",
        "dense": "Dense surrogate",
        "weighted_rrf": "Weighted RRF",
        "rrf_plus_structured_rerank": "RRF + structured_rerank_v1",
    }
    for key in (
        "bm25",
        "dense",
        "weighted_rrf",
        "rrf_plus_structured_rerank",
    ):
        value = aggregate[key]
        lines.append(
            f"| {labels[key]} | {value['recall_at_5']:.6f} | {value['recall_at_10']:.6f} | {value['reciprocal_rank']:.6f} |"
        )
    lines.extend(
        [
            "",
            f"四路均冻结 Top 20；公式、权重、权威 Scene、重复确定性与 hard-memory exclusion 全绿；8 次本地调用 P95={benchmark['latency_ms']['p95']:.2f} ms。污染计数为 0。",
            "",
            "LangMem、Mem0、Graphiti、Cross-Encoder 和真实用户语义提升均未上线、未在本报告中宣称。",
            "",
        ]
    )
    return "\n".join(lines)


def _write_pending() -> None:
    pending = {
        "schema_version": 1,
        "report_id": "s15_memory_route_a_v1",
        "overall_status": "PENDING",
        "report_command": REPORT_COMMAND,
        "note": "gates are still running; PENDING is never a successful result",
    }
    _atomic_write(REPORT_JSON, json.dumps(pending, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(
        REPORT_MD,
        "# S15 Memory 路线 A 独立验收\n\n结论：PENDING（门禁仍在串行执行，不能视为通过）。\n",
    )


def main() -> int:
    _write_pending()
    try:
        report = _execute()
        if report.get("overall_status") != "PASS":
            raise GateFailure("non-PASS report cannot exit successfully")
        _atomic_write(
            REPORT_JSON, json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        _atomic_write(REPORT_MD, _markdown(report))
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "report_id": "s15_memory_route_a_v1",
            "overall_status": "FAIL",
            "report_command": REPORT_COMMAND,
            "error_type": type(exc).__name__,
            "error": str(exc).replace(str(ROOT), "<workspace>")[-5000:],
        }
        _atomic_write(
            REPORT_JSON, json.dumps(failure, ensure_ascii=False, indent=2) + "\n"
        )
        _atomic_write(
            REPORT_MD,
            f"# S15 Memory 路线 A 独立验收\n\n结论：FAIL（{type(exc).__name__}）。\n",
        )
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": report["overall_status"],
                "pytest": report["pytest"],
                "fixed_eval": report["fixed_eval"]["metrics"],
                "node": report["node"],
                "http_browser": report["random_non_8000_http"],
                "synthetic_rerank": report["synthetic_rerank_benchmark"][
                    "aggregate_metrics"
                ],
                "findings": report["findings"],
                "reports": [
                    "reports/eval/s15_memory_route_a_v1.json",
                    "reports/eval/s15_memory_route_a_v1.md",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
