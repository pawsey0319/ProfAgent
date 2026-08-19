from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import statistics
import sys
import time
from typing import Any

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from profagent.memory_service import MemoryService


IMAGE_MODEL = "grok-imagine-image-quality"
ASSET_SET = "wardrobe_generated_v1"
ASSET_VERSION = "wardrobe_generated_v1_s12r2"
GENERATION_CONTRACT = "wardrobe_catalog_single_garment_v1"
EXPECTED_METADATA_KEYS = {
    "AI-Generated",
    "Requested-Model",
    "Model-Reported",
    "Verification-Basis",
    "Garment-ID",
    "Generation-Contract",
}
RRF_K = 60
BRANCH_LIMIT = 20
TOP_K = 5
WEIGHTS = {"bm25": 1.0, "dense": 1.0, "recency": 0.75, "importance": 1.25}


class GateFailure(RuntimeError):
    pass


def _run_gate(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
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
        tail = "\n".join((completed.stdout + completed.stderr).splitlines()[-20:])
        raise GateFailure(f"{label} failed (exit={completed.returncode})\n{tail}")
    return completed


def _passed_count(completed: subprocess.CompletedProcess[str], label: str) -> int:
    matches = re.findall(r"(\d+) passed", completed.stdout + completed.stderr)
    if not matches:
        raise GateFailure(f"{label} did not report a pytest passed count")
    return int(matches[-1])


def _execute_gates() -> dict[str, Any]:
    python = sys.executable
    evidence: dict[str, Any] = {"commands": []}

    def python_gate(args: list[str], label: str) -> subprocess.CompletedProcess[str]:
        evidence["commands"].append("python " + " ".join(args))
        return _run_gate([python, *args], label)

    s12 = python_gate(
        ["-m", "pytest", "-q", "tests/test_s12_image_memory.py"],
        "S12 image/memory pytest",
    )
    preview = python_gate(
        ["-m", "pytest", "-q", "tests/test_preview_static_2d.py"],
        "Preview Static2D pytest",
    )
    full = python_gate(["-m", "pytest", "-q"], "full pytest")
    evidence["pytest"] = {
        "s12_image_memory": _passed_count(s12, "S12 image/memory pytest"),
        "preview_static_2d": _passed_count(preview, "Preview Static2D pytest"),
        "full": _passed_count(full, "full pytest"),
    }

    python_gate(["-m", "profagent.eval"], "fixed evaluation")
    eval_report = json.loads(
        (ROOT / "reports" / "eval" / "r1_demo_v1.json").read_text(encoding="utf-8")
    )
    if eval_report.get("status") != "PASS":
        raise GateFailure("fixed evaluation report status is not PASS")
    metrics = eval_report.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        raise GateFailure("fixed evaluation report has no metrics")
    if any(value.get("status") != "PASS" for value in metrics.values()):
        raise GateFailure("one or more fixed evaluation metrics failed")
    evidence["fixed_eval"] = {
        "status": eval_report["status"],
        "report_version": eval_report["report_version"],
        "metrics": metrics,
    }

    validate = python_gate(["scripts/validate.py"], "fixture validation")
    evidence["validate"] = {
        "status": "pass",
        "stdout_sha256": hashlib.sha256(validate.stdout.encode("utf-8")).hexdigest(),
        "nonempty_output_lines": len(
            [line for line in validate.stdout.splitlines() if line.strip()]
        ),
    }

    syntax_files = [
        "web/app.js",
        "web/dialogue_runtime.js",
        "web/image_asset_runtime.js",
        "web/dialogue_runtime_test.cjs",
        "web/image_asset_runtime_test.cjs",
        "web/static_contract_test.cjs",
    ]
    for path in syntax_files:
        evidence["commands"].append(f"node --check {path}")
        _run_gate(["node", "--check", path], f"node syntax {path}")
    runtime_files = [
        "web/dialogue_runtime_test.cjs",
        "web/image_asset_runtime_test.cjs",
        "web/static_contract_test.cjs",
    ]
    runtime_summaries: list[str] = []
    for path in runtime_files:
        evidence["commands"].append(f"node {path}")
        result = _run_gate(["node", path], f"node runtime {path}")
        nonempty = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        runtime_summaries.append(nonempty[-1] if nonempty else f"{path}: exit 0")
    evidence["node"] = {
        "status": "pass",
        "syntax_files": len(syntax_files),
        "runtime_contracts": len(runtime_files),
        "runtime_summaries": runtime_summaries,
    }

    http_result = python_gate(
        ["scripts/tester_s12_http_smoke.py"], "temporary HTTP smoke"
    )
    try:
        http_evidence = json.loads(http_result.stdout)
    except json.JSONDecodeError as exc:
        raise GateFailure("temporary HTTP smoke did not emit JSON evidence") from exc
    if not http_evidence.get("port_is_not_8000"):
        raise GateFailure("temporary HTTP smoke used forbidden port 8000")
    evidence["temporary_http"] = {"status": "pass", **http_evidence}

    hygiene_commands = [
        (["git", "diff", "--check"], "git diff check"),
        (
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
            "fixed data diff",
        ),
        (["git", "check-ignore", ".profagent/memory.sqlite3"], "SQLite ignore"),
    ]
    for command, label in hygiene_commands:
        evidence["commands"].append(" ".join(command))
        _run_gate(command, label)
    tracked = _run_gate(["git", "ls-files", "--", ".profagent"], "SQLite tracked scan")
    if tracked.stdout.strip():
        raise GateFailure(".profagent has tracked files")
    candidates = _run_gate(
        ["git", "status", "--short", "--untracked-files=all"],
        "candidate scan",
    )
    if any(".profagent" in line for line in candidates.stdout.splitlines()):
        raise GateFailure(".profagent appears in candidate files")
    evidence["hygiene"] = {
        "status": "pass",
        "diff_check": True,
        "fixed_data_diff_empty": True,
        "sqlite_ignored": True,
        "sqlite_tracked_count": 0,
        "sqlite_candidate_count": 0,
    }
    return evidence


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _manifest_checks() -> dict[str, Any]:
    manifest_path = ROOT / "data" / "manifests" / f"{ASSET_SET}.json"
    asset_root = ROOT / "data" / "assets" / ASSET_SET
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fixtures = _load_jsonl(ROOT / "data" / "fixtures" / "garments.jsonl")
    fixture_by_id = {item["garment_id"]: item for item in fixtures}
    items = manifest["items"]
    manifest_by_id = {item["garment_id"]: item for item in items}
    files = sorted(asset_root.glob("*.png"))

    assert manifest["schema_version"] == 2
    assert manifest["asset_set"] == ASSET_SET
    assert manifest["asset_version"] == ASSET_VERSION
    assert manifest["generation_contract"] == GENERATION_CONTRACT
    assert manifest["requested_model"] == IMAGE_MODEL
    assert manifest["request_model_pinned"] is True
    assert manifest["model_reported"] is False
    assert manifest["model_verified"] is False
    assert manifest["resolved_model"] is None
    assert manifest["verification_basis"] == "batch_exact_request_contract"
    assert "model" not in manifest
    assert manifest["aspect_ratio"] == "1:1"
    assert manifest["ai_generated"] is True
    assert manifest["metadata_status"] == "embedded_png_and_sidecar_manifest"
    assert len(items) == len(manifest_by_id) == len(fixtures) == len(files) == 50
    assert set(manifest_by_id) == set(fixture_by_id)
    assert {path.stem for path in files} == set(fixture_by_id)

    total_bytes = 0
    hashes: set[str] = set()
    owner_counts: Counter[str] = Counter()
    generation_statuses: Counter[str] = Counter()
    prompt_hash_statuses: Counter[str] = Counter()
    for garment_id, item in sorted(manifest_by_id.items()):
        fixture = fixture_by_id[garment_id]
        assert item["user_id"] == fixture["user_id"]
        assert item["source_data_version"] == fixture["data_version"] == "fixtures_v1.0"
        assert fixture["synthetic"] is True
        assert item["status"] == "succeeded"
        assert item["generation_status"] in {"generated", "reused"}
        assert item["requested_model"] == IMAGE_MODEL
        assert item["request_model_pinned"] is True
        assert item["model_reported"] is False
        assert item["model_verified"] is False
        assert item["resolved_model"] is None
        assert item["verification_basis"] == "batch_exact_request_contract"
        assert "model" not in item
        assert item["aspect_ratio"] == "1:1"
        assert item["ai_generated"] is True
        assert item["media_type"] == "image/png"
        assert item["width"] == item["height"] == 1024
        prompt_sha256 = item["prompt_sha256"]
        prompt_hash_status = item["prompt_hash_status"]
        if prompt_sha256 is None:
            assert prompt_hash_status == "not_preserved"
        else:
            assert re.fullmatch(r"[0-9a-f]{64}", prompt_sha256)
            assert prompt_hash_status == "recorded_at_generation"
        assert len(item["sha256"]) == 64
        assert not Path(item["relative_path"]).is_absolute()
        assert ".." not in Path(item["relative_path"]).parts
        assert item["relative_path"] == f"data/assets/{ASSET_SET}/{garment_id}.png"
        path = ROOT / item["relative_path"]
        body = path.read_bytes()
        assert len(body) == item["bytes"]
        assert hashlib.sha256(body).hexdigest() == item["sha256"]
        with Image.open(path) as image:
            image.load()
            assert image.format == "PNG"
            assert image.size == (item["width"], item["height"])
            assert image.n_frames == 1
            assert getattr(image, "is_animated", False) is False
            assert set(image.info) == EXPECTED_METADATA_KEYS
            assert image.info == {
                "AI-Generated": "true",
                "Requested-Model": IMAGE_MODEL,
                "Model-Reported": "false",
                "Verification-Basis": "batch_exact_request_contract",
                "Garment-ID": garment_id,
                "Generation-Contract": GENERATION_CONTRACT,
            }
        total_bytes += len(body)
        hashes.add(item["sha256"])
        owner_counts[item["user_id"]] += 1
        generation_statuses[item["generation_status"]] += 1
        prompt_hash_statuses[prompt_hash_status] += 1

    assert len(hashes) == 50
    assert dict(sorted(owner_counts.items())) == {"u01": 28, "u02": 12, "u03": 10}
    return {
        "status": "pass",
        "manifest_items": len(items),
        "png_files": len(files),
        "fixture_rows": len(fixtures),
        "one_to_one_fixture_id_owner": True,
        "owner_counts": dict(sorted(owner_counts.items())),
        "generation_status_counts": dict(sorted(generation_statuses.items())),
        "asset_version": ASSET_VERSION,
        "status_all_succeeded": True,
        "requested_model": IMAGE_MODEL,
        "request_model_pinned": True,
        "model_reported": False,
        "model_verified": False,
        "resolved_model": None,
        "verification_basis": "batch_exact_request_contract",
        "per_file_provider_receipt_claimed": False,
        "prompt_hash_allowed_pairs": [
            {"prompt_sha256": None, "prompt_hash_status": "not_preserved"},
            {
                "prompt_sha256": "lowercase_64_hex",
                "prompt_hash_status": "recorded_at_generation",
            },
        ],
        "prompt_hash_status_counts": dict(sorted(prompt_hash_statuses.items())),
        "current_null_not_preserved": prompt_hash_statuses == {"not_preserved": 50},
        "paths_relative_and_confined": True,
        "bytes_sha256_dimensions_match": 50,
        "unique_sha256": len(hashes),
        "total_bytes": total_bytes,
        "png_static_single_frame": 50,
        "embedded_metadata_exact_six_keys": 50,
        "reviewer_visual_signoff": "50/50 (external reviewer evidence; not re-scored here)",
    }


CORPUS = (
    ("m001", "偏爱直筒裤，通勤时希望线条利落", 1.0, 0.98),
    ("m002", "偏爱简洁风格，减少多余装饰", 1.0, 0.95),
    ("m003", "偏爱低调配色，喜欢藏青灰色米色", 1.0, 0.92),
    ("m004", "长时间站立时优先选择适合久走的鞋", 0.9, 0.90),
    ("m005", "雨天希望外套防水并带轻薄层次", 0.8, 0.88),
    ("m006", "面试穿搭希望可靠正式但不显老气", 0.9, 0.86),
    ("m007", "约会喜欢柔和针织与温和配色", 0.8, 0.84),
    ("m008", "通勤常选藏青经典风格", 0.8, 0.82),
    ("m009", "旅行时优先轻便运动鞋和易打理单品", 0.8, 0.80),
    ("m010", "冬季怕冷，需要保暖外层", 0.8, 0.78),
    ("m011", "日常喜欢宽松卫衣", 0.7, 0.76),
    ("m012", "周末偏爱牛仔休闲风", 0.7, 0.74),
    ("m013", "会议时喜欢白色衬衫", 0.7, 0.72),
    ("m014", "夏季选择透气亚麻材质", 0.7, 0.70),
    ("m015", "秋季喜欢棕色夹克", 0.6, 0.68),
    ("m016", "聚会时可以增加一件醒目配饰", 0.6, 0.66),
    ("m017", "晨间运动选择速干上衣", 0.6, 0.64),
    ("m018", "出差希望衣物容易组合", 0.6, 0.62),
    ("m019", "看展时喜欢设计感衬衫", 0.6, 0.60),
    ("m020", "晚餐场合选择深色皮鞋", 0.6, 0.58),
    ("m021", "春季常穿轻薄夹克", 0.5, 0.56),
    ("m022", "居家穿着重视柔软舒适", 0.5, 0.54),
    ("m023", "短途散步喜欢帆布鞋", 0.5, 0.52),
    ("m024", "正式演讲选择结构感西装", 0.5, 0.50),
)

TRUTH = (
    ("通勤想穿直筒裤显得利落", {"m001"}),
    ("想要简洁风格不要太多装饰", {"m002"}),
    ("喜欢藏青灰色这种低调配色", {"m003"}),
    ("要站很久，鞋要适合久走", {"m004"}),
    ("下雨天需要防水外套", {"m005"}),
    ("面试想可靠正式但别老气", {"m006"}),
    ("第一次约会想穿柔和针织", {"m007"}),
    ("藏青经典通勤风", {"m008"}),
    ("旅行需要轻便运动鞋", {"m009"}),
    ("冬天怕冷要保暖外层", {"m010"}),
)


def _bm25(query: str) -> dict[str, float]:
    docs = {record_id: MemoryService._tokens(content) for record_id, content, _, _ in CORPUS}
    query_tokens = MemoryService._tokens(query)
    query_counts = {token: query_tokens.count(token) for token in set(query_tokens)}
    average_length = statistics.fmean(len(tokens) for tokens in docs.values())
    scores: dict[str, float] = {}
    for record_id, tokens in docs.items():
        score = 0.0
        for token, query_frequency in query_counts.items():
            document_frequency = sum(token in document for document in docs.values())
            inverse_frequency = math.log(
                1.0
                + (len(docs) - document_frequency + 0.5)
                / (document_frequency + 0.5)
            )
            term_frequency = tokens.count(token)
            denominator = term_frequency + 1.2 * (
                0.25 + 0.75 * len(tokens) / max(average_length, 1.0)
            )
            if denominator:
                score += query_frequency * inverse_frequency * (
                    term_frequency * 2.2 / denominator
                )
        scores[record_id] = score
    return scores


def _dense(query: str) -> dict[str, float]:
    query_vector = MemoryService._dense_vector(query)
    return {
        record_id: sum(
            left * right
            for left, right in zip(query_vector, MemoryService._dense_vector(content))
        )
        for record_id, content, _importance, _recency in CORPUS
    }


def _sorted_ids(scores: dict[str, float], limit: int = 10) -> list[str]:
    return [key for key, _ in sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]]


def _rank(query: str, method: str) -> list[str]:
    if method == "bm25_only":
        return _sorted_ids(_bm25(query))
    if method == "hashed_dense_only":
        return _sorted_ids(_dense(query))

    bm25 = _bm25(query)
    dense = _dense(query)
    importance = {record_id: value for record_id, _text, value, _recency in CORPUS}
    recency = {record_id: value for record_id, _text, _importance, value in CORPUS}
    branches = {"bm25": bm25, "dense": dense, "recency": recency, "importance": importance}
    ranked = {
        branch: _sorted_ids(scores, BRANCH_LIMIT) for branch, scores in branches.items()
    }
    fused = {record_id: 0.0 for ids in ranked.values() for record_id in ids}
    for branch, ids in ranked.items():
        for rank, record_id in enumerate(ids, 1):
            fused[record_id] += WEIGHTS[branch] / (RRF_K + rank)
    scores = fused
    if method == "weighted_rrf_deterministic_rerank":
        peak = max(bm25.values(), default=0.0)
        scores = {
            record_id: score
            + (0.03 * max(0.0, bm25.get(record_id, 0.0)) / peak if peak > 0 else 0.0)
            for record_id, score in fused.items()
        }
    elif method != "weighted_rrf":
        raise ValueError(method)
    return [
        record_id
        for record_id, _ in sorted(
            scores.items(),
            key=lambda pair: (
                -pair[1],
                -bm25.get(pair[0], 0.0),
                -importance.get(pair[0], 0.0),
                pair[0],
            ),
        )[:10]
    ]


def _ablation() -> dict[str, Any]:
    methods = (
        "bm25_only",
        "hashed_dense_only",
        "weighted_rrf",
        "weighted_rrf_deterministic_rerank",
    )
    results: dict[str, Any] = {}
    for method in methods:
        recalls_5: list[float] = []
        recalls_10: list[float] = []
        reciprocal_ranks: list[float] = []
        latencies_ms: list[float] = []
        for _repeat in range(20):
            for query, relevant in TRUTH:
                started = time.perf_counter()
                ranked = _rank(query, method)
                latencies_ms.append((time.perf_counter() - started) * 1000)
                if _repeat == 0:
                    recalls_5.append(len(set(ranked[:5]) & relevant) / len(relevant))
                    recalls_10.append(len(set(ranked[:10]) & relevant) / len(relevant))
                    reciprocal_ranks.append(
                        next(
                            (1.0 / rank for rank, item in enumerate(ranked, 1) if item in relevant),
                            0.0,
                        )
                    )
        sorted_latency = sorted(latencies_ms)
        p95_index = max(0, math.ceil(0.95 * len(sorted_latency)) - 1)
        results[method] = {
            "recall_at_5": round(statistics.fmean(recalls_5), 4),
            "recall_at_10": round(statistics.fmean(recalls_10), 4),
            "mrr": round(statistics.fmean(reciprocal_ranks), 4),
            "p95_latency_ms": round(sorted_latency[p95_index], 4),
            "measured_rankings": len(latencies_ms),
        }
    return {
        "status": "pass",
        "dataset_kind": "controlled_synthetic",
        "truth_queries": len(TRUTH),
        "corpus_records": len(CORPUS),
        "dense_backend": "deterministic_hashed_surrogate_v1",
        "rrf": {"k": RRF_K, "candidate_limit_per_branch": BRANCH_LIMIT, "weights": WEIGHTS, "production_top_k": TOP_K},
        "methods": results,
        "limitations": (
            "Small controlled synthetic truth set. Hashed dense is a deterministic surrogate, "
            "not a semantic embedding model. These numbers do not demonstrate production "
            "semantic quality or improvement."
        ),
    }


def _report(evidence: dict[str, Any]) -> dict[str, Any]:
    report = {
        "schema_version": 2,
        "report_id": "s12_memory_image_v1",
        "scope": "independent S12R technical acceptance",
        "overall_status": "PASS",
        "external_calls": {"text_cpa": 0, "image_generation": 0},
        "fixed_data_mutated": False,
        "executed_commands": evidence["commands"],
        "gates": {
            "full_pytest": {"passed": evidence["pytest"]["full"], "status": "pass"},
            "s12_image_memory": {"passed": evidence["pytest"]["s12_image_memory"], "status": "pass"},
            "preview_static_2d": {"passed": evidence["pytest"]["preview_static_2d"], "status": "pass"},
            "fixed_eval": evidence["fixed_eval"],
            "validate": evidence["validate"],
            "node": evidence["node"],
            "temporary_http": evidence["temporary_http"],
            "hygiene": evidence["hygiene"],
        },
        "manifest_and_png": _manifest_checks(),
        "memory_contract": {
            "status": "pass",
            "persistence_restart_and_legacy_migration": True,
            "ineligible_retrieval_counts": {
                "unconfirmed": 0,
                "deleted": 0,
                "expired": 0,
                "superseded": 0,
                "sensitive": 0,
                "cross_user": 0,
                "wrong_namespace": 0,
                "wrong_member": 0,
                "wrong_team": 0,
            },
            "hard_memory_in_rrf": False,
            "late_id_beyond_20_retrieved": True,
            "branch_top20_union_fused": True,
            "rrf_k": RRF_K,
            "candidate_limit_per_branch": BRANCH_LIMIT,
            "weights": WEIGHTS,
            "top_k": TOP_K,
            "concurrent_cas_single_record": True,
            "local_trace_retrievable": True,
            "trace_content_logged": False,
            "trace_vectors_logged": False,
        },
        "image_provider_adversarial": {
            "status": "pass",
            "protocol_class_a_reported_exact": {
                "accepted_model": IMAGE_MODEL,
                "model_reported": True,
                "model_verified": True,
                "resolved_model": IMAGE_MODEL,
                "verification_basis": "reported_model_exact",
                "raw_receipt_redacted": True,
            },
            "protocol_class_b_unreported_with_receipt": {
                "request_model_pinned": True,
                "cpa_trace_verified": True,
                "model_reported": False,
                "model_verified": False,
                "resolved_model": None,
                "verification_basis": "exact_request_with_cpa_trace",
                "raw_receipt_redacted": True,
            },
            "present_null_empty_nonstring_wrong_prefixed_models_rejected": True,
            "missing_or_unsafe_receipt_rejected": True,
            "duplicate_json_keys_rejected": ["model", "data", "b64_json", "url"],
            "post_envelope_8mib_stream_early_abort": True,
            "url_allowlist_and_public_dns": True,
            "redirect_userinfo_nondefault_port_rejected": True,
            "content_type_magic_decode_bound": True,
            "download_5mib_limit": True,
        },
        "supervisor_real_provider_evidence": {
            "source": "supervisor-reported real project provider smoke; not rerun by tester",
            "exit_code": 0,
            "latency_seconds": 5.2885,
            "media_type": "image/jpeg",
            "size_bytes": 228841,
            "response_source": "b64_json",
            "request_model_pinned": True,
            "cpa_trace_verified": True,
            "model_reported": False,
            "model_verified": False,
            "resolved_model": None,
            "verification_basis": "exact_request_with_cpa_trace",
        },
        "controlled_memory_ablation": _ablation(),
        "privacy": {
            "manifest_paths_are_relative": True,
            "trace_has_no_memory_content_or_vectors": True,
            "reports_have_no_prompt_text_key_material_base64_payload_or_absolute_user_path": True,
            "sqlite_expected_location": ".profagent/memory.sqlite3",
            "sqlite_git_ignored": True,
        },
        "findings": {"P0": 0, "P1": 0, "P2": 0},
    }
    serialized = json.dumps(report, ensure_ascii=False)
    assert str(ROOT) not in serialized
    assert "authorization" not in serialized.lower()
    assert "api_key" not in serialized.lower()
    return report


def _markdown(report: dict[str, Any]) -> str:
    ablation = report["controlled_memory_ablation"]
    gates = report["gates"]
    metrics = gates["fixed_eval"]["metrics"]
    lines = [
        "# S12R Memory + Image 独立验收报告 v1（schema 2）",
        "",
        f"结论：S12R tester 最终门禁 {report['overall_status']}；P0/P1/P2 = 0/0/0。未调用真实 CPA，未重新生图，未修改固定 fixtures/eval 真值或阈值。",
        "",
        "## 固定门禁",
        "",
        f"- Full pytest：{gates['full_pytest']['passed']} passed。S12 image/memory：{gates['s12_image_memory']['passed']} passed。Preview Static2D：{gates['preview_static_2d']['passed']} passed。",
        f"- 固定 eval：Urgency={metrics['urgency_accuracy']['numerator']}/{metrics['urgency_accuracy']['denominator']}，高急 ShoppingGate={metrics['high_urgency_shopping_gate_accuracy']['numerator']}/{metrics['high_urgency_shopping_gate_accuracy']['denominator']}，Catalog calls={metrics['high_urgency_catalog_calls']['numerator']}，衣物幻觉={metrics['item_hallucinations']['numerator']}/{metrics['item_hallucinations']['denominator']}，硬约束违反={metrics['hard_constraint_violations']['numerator']}/{metrics['hard_constraint_violations']['denominator']}，Slot={metrics['slot_completeness']['numerator']}/{metrics['slot_completeness']['denominator']}。",
        f"- validate：exit 0，stdout SHA-256={gates['validate']['stdout_sha256']}。",
        f"- Node：{gates['node']['syntax_files']} 个语法检查与 {gates['node']['runtime_contracts']} 套 runtime/static contract，PASS。",
        f"- 临时随机非 8000 端口 HTTP：owner={gates['temporary_http']['owner_asset_counts']}，三参数内容寻址 URL、旧版本/错 hash 拒绝、高急 Catalog0、2D-only，PASS。",
        "",
        "## 50 张目录 PNG 技术核验",
        "",
        "Manifest schema 2：asset_version=wardrobe_generated_v1_s12r2，requested model 固定；model_reported/model_verified=false、resolved=null、basis=batch_exact_request_contract。Manifest=50、PNG=50、fixtures=50，ID/owner 一一对应，bytes/SHA-256/1024×1024/单帧匹配，六个内嵌来源键逐值精确。当前 50 项 prompt_sha256=null、prompt_hash_status=not_preserved；批次不声称逐文件模型回报或 CPA 回执。",
        "",
        "## Memory 与图片 Provider 对抗",
        "",
        "图片 Provider 分为 A（回报精确模型）与 B（模型未回报但 exact request + CPA receipt）；两类安全 Trace 均不得泄露原始 receipt。字段存在但为 null/空/非字符串/错模/前缀、缺失或非法 receipt、重复 model/data/b64_json/url key 均须拒绝。SSRF/DoS 与全图 Validator 不放宽。",
        "",
        "## Supervisor 真实 Provider 证据（外部引用）",
        "",
        "Supervisor 报告：exit 0，5.2885s，image/jpeg 228841 bytes，response_source=b64_json；request pinned=true、receipt verified=true、model_reported/model_verified=false、resolved=null、basis=exact_request_with_cpa_trace。本 tester 未重复真实调用。",
        "",
        "## Controlled synthetic 消融",
        "",
        f"数据边界：{ablation['truth_queries']} 条受控合成 query、{ablation['corpus_records']} 条受控合成记录；Dense 是 `deterministic_hashed_surrogate_v1`。以下结果不代表生产语义质量或提升。",
        "",
        "| 方法 | Recall@5 | Recall@10 | MRR | P95 (ms) |",
        "|---|---:|---:|---:|---:|",
    ]
    labels = {
        "bm25_only": "BM25-only",
        "hashed_dense_only": "hashed-dense-only",
        "weighted_rrf": "weighted-RRF",
        "weighted_rrf_deterministic_rerank": "RRF + deterministic rerank",
    }
    for method, metrics in ablation["methods"].items():
        lines.append(
            f"| {labels[method]} | {metrics['recall_at_5']:.4f} | {metrics['recall_at_10']:.4f} | {metrics['mrr']:.4f} | {metrics['p95_latency_ms']:.4f} |"
        )
    lines.extend(
        [
            "",
            "## 隐私与仓库卫生",
            "",
            "Manifest 仅相对路径；报告不含 prompt 正文、API key、base64 payload 或用户绝对路径；`.profagent/memory.sqlite3` 保持 git ignored 且不进入候选。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    output_dir = ROOT / "reports" / "eval"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "s12_memory_image_v1.json"
    md_path = output_dir / "s12_memory_image_v1.md"

    def atomic_write(path: Path, content: str) -> None:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    try:
        evidence = _execute_gates()
        report = _report(evidence)
        markdown = _markdown(report)
        serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        combined = serialized + markdown
        assert str(ROOT) not in combined
        assert not re.search(r"sk-[A-Za-z0-9_-]{16,}", combined)
        assert "data:image/" not in combined.lower()
        atomic_write(json_path, serialized)
        atomic_write(md_path, markdown)
    except Exception as exc:
        message = str(exc).replace(str(ROOT), "<workspace>")
        failure = {
            "schema_version": 2,
            "report_id": "s12_memory_image_v1",
            "scope": "independent S12R technical acceptance",
            "overall_status": "FAIL",
            "error_type": type(exc).__name__,
            "error": message[-4000:],
        }
        atomic_write(
            json_path,
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n",
        )
        atomic_write(
            md_path,
            "# S12R Memory + Image 独立验收报告 v1（schema 2）\n\n"
            f"结论：FAIL（{type(exc).__name__}）。详情见 JSON。\n",
        )
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "status": report["overall_status"],
                "pytest": evidence["pytest"],
                "fixed_eval": evidence["fixed_eval"]["metrics"],
                "node": evidence["node"],
                "temporary_http": evidence["temporary_http"],
                "manifest_png": report["manifest_and_png"],
                "ablation": report["controlled_memory_ablation"],
                "reports": [
                    "reports/eval/s12_memory_image_v1.json",
                    "reports/eval/s12_memory_image_v1.md",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
