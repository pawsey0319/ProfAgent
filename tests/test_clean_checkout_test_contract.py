from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
DETERMINISTIC_MANIFEST = "data/manifests/fixtures_s16_womenswear_v1.json"
DIAGNOSTIC_PNG = ROOT / "tests" / "fixtures" / "diagnostic_input.synthetic.png"
DIAGNOSTIC_JSONL = (
    ROOT / "tests" / "fixtures" / "diagnostic_evidence.synthetic.jsonl"
)
DIAGNOSTIC_TEST_MODULE = ROOT / "tests" / "test_diagnose_cpa_vision_envelope.py"
LICENSED_ASSET_TEST_MODULE = ROOT / "tests" / "test_s16_licensed_assets.py"
REPORT_TEST_MODULE = ROOT / "tests" / "test_r1_eval_report.py"
REPORT_PATHS = (
    ROOT / "reports" / "eval" / "r1_demo_v1.json",
    ROOT / "reports" / "eval" / "r1_demo_v1.md",
)
FROZEN_PRIVATE_SHA256 = {
    "0f981797ee2dc37d41a0563e1d5d34b8d31802ac5589beae4aa223309843dd28",
    "33c9439e4516e571925689f0ef501a9771f3d25888ff24ecbe044c6b16d19749",
    "46c5d710493b59daa823f72d9c68ab18d059e313ead69531c4cb70c2058a4e98",
    "afc00f9705193d7d0ea2072989b07bdb6b74ceae757b1f20ba5f8575db662abf",
}
FORBIDDEN_FIXTURE_TEXT = (
    "private_quarantine",
    "cpa_generated_quarantine_diagnostics",
    "provider-secret",
    "authorization",
    "bearer ",
    "api_key",
    "api-key",
    "data/assets/",
)
FORBIDDEN_PROSE_OR_SECRET_KEYS = {
    "api_key",
    "authorization",
    "content",
    "credential",
    "message",
    "password",
    "prompt",
    "provider_body",
    "raw_body",
    "raw_response",
    "secret",
    "token",
}


def _check_attr(path: str) -> dict[str, str]:
    result = subprocess.run(
        ["git", "check-attr", "text", "eol", "--", path],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    attributes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        _, name, value = line.split(": ", 2)
        attributes[name] = value
    return attributes


def _is_tracked(path: Path) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", path.relative_to(ROOT).as_posix()],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _walk(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _snapshot_reports() -> dict[Path, bytes | None]:
    return {
        path: path.read_bytes() if path.is_file() else None for path in REPORT_PATHS
    }


def _restore_reports(snapshot: dict[Path, bytes | None]) -> None:
    for path, payload in snapshot.items():
        if payload is None:
            if path.exists():
                path.unlink()
        else:
            path.write_bytes(payload)


def test_deterministic_generated_manifest_forces_lf_checkout() -> None:
    assert _check_attr(DETERMINISTIC_MANIFEST) == {"text": "set", "eol": "lf"}


@pytest.mark.parametrize("fixture_path", (DIAGNOSTIC_PNG, DIAGNOSTIC_JSONL))
def test_repository_tracks_sanitized_diagnostic_fixture(fixture_path: Path) -> None:
    relative = fixture_path.relative_to(ROOT).as_posix()
    assert fixture_path.is_file(), f"repository-only synthetic fixture missing: {relative}"
    assert _is_tracked(fixture_path), f"synthetic fixture must be tracked: {relative}"

    payload = fixture_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    assert digest not in FROZEN_PRIVATE_SHA256
    lowered = payload.lower()
    assert not any(token.encode() in lowered for token in FORBIDDEN_FIXTURE_TEXT)
    assert not any(digest.encode() in lowered for digest in FROZEN_PRIVATE_SHA256)


def test_synthetic_png_is_small_and_contains_no_private_metadata() -> None:
    assert DIAGNOSTIC_PNG.is_file(), (
        "repository-only synthetic PNG fixture must exist before safety checks"
    )
    payload = DIAGNOSTIC_PNG.read_bytes()
    assert 60 <= len(payload) <= 16 * 1024
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(DIAGNOSTIC_PNG) as image:
        image.verify()
    with Image.open(DIAGNOSTIC_PNG) as image:
        assert image.format == "PNG"
        assert 1 <= image.width <= 32 and 1 <= image.height <= 32
        assert image.info == {}


def test_synthetic_diagnostic_jsonl_is_structured_and_non_sensitive() -> None:
    assert DIAGNOSTIC_JSONL.is_file(), (
        "repository-only synthetic JSONL fixture must exist before safety checks"
    )
    payload = DIAGNOSTIC_JSONL.read_bytes()
    assert 2 <= len(payload) <= 16 * 1024
    text = payload.decode("utf-8")
    assert not re.search(r"\b[gu]\d{2,3}\b", text, flags=re.IGNORECASE)
    assert not re.search(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        text,
        flags=re.IGNORECASE,
    )
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert 1 <= len(rows) <= 5
    assert all(isinstance(row, dict) for row in rows)
    assert all(row.get("synthetic_fixture") is True for row in rows)
    assert all(row.get("fixture_scope") == "repository_only_synthetic" for row in rows)
    for row in rows:
        for key, value in _walk(row):
            assert key.lower() not in FORBIDDEN_PROSE_OR_SECRET_KEYS
            if isinstance(value, str):
                assert "\n" not in value and "\r" not in value
                assert len(value) <= 160


def test_diagnostic_tests_use_repository_fixtures_without_private_evidence_skips() -> None:
    diagnostic_source = DIAGNOSTIC_TEST_MODULE.read_text(encoding="utf-8")
    licensed_source = LICENSED_ASSET_TEST_MODULE.read_text(encoding="utf-8")
    assert DIAGNOSTIC_PNG.relative_to(ROOT).as_posix() in diagnostic_source
    assert DIAGNOSTIC_JSONL.relative_to(ROOT).as_posix() in licensed_source
    assert "_RP_PRIVATE_EVIDENCE_REQUIRED" not in licensed_source
    assert "pytest.mark.skipif" not in licensed_source
    assert "pytest.skip(" not in diagnostic_source
    assert "pytest.skip(" not in licensed_source


def test_focused_eval_report_test_does_not_modify_tracked_reports() -> None:
    before = _snapshot_reports()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            f"{REPORT_TEST_MODULE.relative_to(ROOT).as_posix()}::test_frozen_eval_writes_stable_json_and_markdown",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    after = _snapshot_reports()
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        assert after == before, "focused eval test modified tracked report bytes"
    finally:
        _restore_reports(before)


def test_eval_run_endpoint_test_does_not_modify_tracked_reports() -> None:
    before = _snapshot_reports()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            f"{REPORT_TEST_MODULE.relative_to(ROOT).as_posix()}::test_eval_run_endpoint_returns_same_stable_contract",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    after = _snapshot_reports()
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        assert after == before, "eval endpoint test modified tracked report bytes"
    finally:
        _restore_reports(before)
