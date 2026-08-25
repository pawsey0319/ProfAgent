from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from profagent.config import Settings
from profagent.vision import VisionAdapter, VisionUnavailable


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "diagnose_cpa_vision_envelope.py"
FIXED_RELATIVE_PATH = Path(
    "data/assets/private_quarantine/cpa_generated/"
    "33c9439e4516e571925689f0ef501a9771f3d25888ff24ecbe044c6b16d19749.png"
)
FIXED_SHA256 = "33c9439e4516e571925689f0ef501a9771f3d25888ff24ecbe044c6b16d19749"
PROFILE = "cpa_chat_completion_metadata_v1"
RULING_U_PROFILES = frozenset(
    {"cpa_chat_completion_metadata_v1", "cpa_chat_completion_metadata_v2"}
)
OUTPUT_KEYS = {
    "reason_code",
    "schema_stage",
    "envelope_failure_code",
    "envelope_metadata_profile",
    "model_provenance",
    "vision_call_count",
}
PROVENANCE_KEYS = {"requested_model", "resolved_model", "model_verified"}
PRODUCTION_TRACE_KEYS = {
    "component",
    "operation",
    "status",
    "reason_code",
    "requested_model",
    "transport_model",
    "resolved_model",
    "model_verified",
    "schema",
    "image_logged",
    "latency_ms",
    "interaction_budget_seconds",
    "assessment_count",
    "quality_issue_count",
    "schema_stage",
    "envelope_failure_code",
    "envelope_metadata_profile",
}
EVIDENCE_PATHS = (
    ROOT / "data/manifests/wardrobe_assets_v2.json",
    ROOT / "data/sources/wardrobe_s16_sources.jsonl",
    ROOT / "data/assets/cpa_generated_quarantine_diagnostics.jsonl",
    ROOT / FIXED_RELATIVE_PATH,
    ROOT
    / "data/assets/private_quarantine/cpa_generated/"
    "46c5d710493b59daa823f72d9c68ab18d059e313ead69531c4cb70c2058a4e98.png",
    ROOT
    / "data/assets/private_quarantine/cpa_generated/"
    "afc00f9705193d7d0ea2072989b07bdb6b74ceae757b1f20ba5f8575db662abf.png",
)
TRANSACTION_MARKER = ROOT / "data/assets/cpa_generated_quarantine_transaction.json"
REPORT_PATHS = (
    ROOT / "reports/eval/r1_demo_v1.json",
    ROOT / "reports/eval/r1_demo_v1.md",
)
PROVIDER_SECRET = "provider-secret-body-user-g051-must-not-leak"


@pytest.fixture
def diagnostic_module() -> ModuleType:
    if not SCRIPT.is_file():
        pytest.fail(
            "Ruling P RED: scripts/diagnose_cpa_vision_envelope.py is missing",
            pytrace=False,
        )
    name = "_ruling_p_diagnose_cpa_vision_envelope"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _snapshot() -> tuple[tuple[tuple[str, bool, int | None, str | None], ...], bool, str]:
    evidence = tuple(
        (
            str(path.relative_to(ROOT)),
            path.is_file(),
            path.stat().st_size if path.is_file() else None,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in EVIDENCE_PATHS
    )
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return evidence, TRANSACTION_MARKER.exists(), status


def _direct_script_write_surface_snapshot() -> tuple[
    tuple[tuple[str, bool, int | None, str | None], ...],
    tuple[tuple[str, int, str], ...],
    tuple[str, ...],
    tuple[tuple[str, bool, int | None, str | None], ...],
    bool,
    str,
]:
    reports = tuple(
        (
            str(path.relative_to(ROOT)),
            path.is_file(),
            path.stat().st_size if path.is_file() else None,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in REPORT_PATHS
    )
    pyc_files = tuple(
        (
            str(path.relative_to(ROOT)),
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(ROOT.rglob("*.pyc"))
        if path.is_file()
    )
    cache_dirs = tuple(
        str(path.relative_to(ROOT))
        for path in sorted(ROOT.rglob("*"))
        if path.is_dir() and path.name in {"__pycache__", ".pytest_cache"}
    )
    evidence, marker, status = _snapshot()
    return reports, pyc_files, cache_dirs, evidence, marker, status


def _settings(**updates: Any) -> Settings:
    settings = Settings(
        root_dir=ROOT,
        cpa_base_url="http://127.0.0.1:8317/v1",
        cpa_api_key="mock-only-key",
        grok_model="grok4.6",
        cpa_text_enabled=True,
        cpa_image_enabled=True,
        vision_force_failure=False,
    )
    return replace(settings, **updates)


class _VisionSpy:
    def __init__(self, outcome: dict[str, Any] | BaseException) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def inspect_catalog_asset(self, **kwargs: Any) -> tuple[object, dict[str, Any]]:
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return object(), self.outcome


def _install_runtime(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    *,
    settings: Settings,
    vision: _VisionSpy,
) -> None:
    class _SettingsFactory:
        @staticmethod
        def from_env() -> Settings:
            return settings

    monkeypatch.setattr(module, "Settings", _SettingsFactory)
    monkeypatch.setattr(module, "VisionAdapter", lambda actual: vision)

    async def image_must_never_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Ruling P is Vision-only; Image calls are forbidden")

    from profagent.image_provider import GrokImageProvider

    monkeypatch.setattr(GrokImageProvider, "generate_static_2d", image_must_never_run)


def _invoke(module: ModuleType, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = module.main(list(argv))
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    _assert_minimized(payload)
    return code, payload


def _assert_minimized(payload: dict[str, Any]) -> None:
    assert set(payload) == OUTPUT_KEYS
    assert set(payload["model_provenance"]) == PROVENANCE_KEYS
    rendered = json.dumps(payload, ensure_ascii=False)
    forbidden = (
        PROVIDER_SECRET,
        "provider_body",
        "unknown_provider_key",
        "transport_model",
        "latency",
        "trace",
        "token",
        "secret",
        "g051",
        "u01",
        str(FIXED_RELATIVE_PATH),
    )
    assert all(value not in rendered for value in forbidden)


def _success_trace() -> dict[str, Any]:
    trace = VisionAdapter(_settings())._catalog_trace(
        status="ok",
        reason_code=None,
        latency_ms=999,
        resolved_model="grok-4.6-high",
        model_verified=True,
        assessment_count=1,
        quality_issue_count=0,
        schema_stage=None,
        envelope_failure_code=None,
        envelope_metadata_profile=None,
    )
    _assert_production_trace_shape(trace, status="ok", reason_code=None)
    assert trace["envelope_metadata_profile"] is None
    return trace


def _vision_failure(**updates: Any) -> VisionUnavailable:
    values: dict[str, Any] = {
        "reason_code": "response_schema_invalid",
        "latency_ms": 999,
        "resolved_model": None,
        "model_verified": False,
        "assessment_count": 0,
        "quality_issue_count": 0,
        "schema_stage": "envelope",
        "envelope_failure_code": "invalid_envelope_metadata",
        "envelope_metadata_profile": PROFILE,
    }
    values.update(updates)
    trace = VisionAdapter(_settings())._catalog_trace(
        status="quarantined",
        **values,
    )
    _assert_production_trace_shape(
        trace,
        status="quarantined",
        reason_code=trace["reason_code"],
    )
    return VisionUnavailable(PROVIDER_SECRET, trace)


def _literal_catalog_trace(**updates: Any) -> dict[str, Any]:
    """Independent test contract: do not derive diagnostic states from its code."""
    trace: dict[str, Any] = {
        "component": "vision",
        "operation": "catalog_asset_assessment",
        "status": "quarantined",
        "reason_code": "provider_unavailable",
        "requested_model": "grok4.6",
        "transport_model": "grok-4.6-high",
        "resolved_model": None,
        "model_verified": False,
        "schema": "catalog_asset_assessment_v2",
        "image_logged": False,
        "latency_ms": 17,
        "interaction_budget_seconds": 30,
        "assessment_count": 0,
        "quality_issue_count": 0,
        "schema_stage": None,
        "envelope_failure_code": None,
        "envelope_metadata_profile": None,
    }
    trace.update(updates)
    return trace


def _assert_production_trace_shape(
    trace: dict[str, Any], *, status: str, reason_code: str | None
) -> None:
    assert set(trace) == PRODUCTION_TRACE_KEYS
    assert trace["component"] == "vision"
    assert trace["operation"] == "catalog_asset_assessment"
    assert trace["status"] == status
    assert trace["reason_code"] == reason_code
    assert trace["requested_model"] == "grok4.6"
    assert trace["transport_model"] == "grok-4.6-high"
    assert trace["schema"] == "catalog_asset_assessment_v2"
    assert trace["image_logged"] is False
    assert trace["resolved_model"] is None or type(trace["resolved_model"]) is str
    assert type(trace["model_verified"]) is bool
    assert type(trace["latency_ms"]) in {int, float}
    assert type(trace["interaction_budget_seconds"]) in {int, float}
    assert type(trace["assessment_count"]) is int
    assert type(trace["quality_issue_count"]) is int
    assert trace["assessment_count"] >= 0
    assert trace["quality_issue_count"] >= 0
    assert trace["schema_stage"] is None or type(trace["schema_stage"]) is str
    assert (
        trace["envelope_failure_code"] is None
        or type(trace["envelope_failure_code"]) is str
    )
    assert (
        trace["envelope_metadata_profile"] is None
        or type(trace["envelope_metadata_profile"]) is str
    )


def test_cli_surface_and_fixed_private_input_are_closed(
    diagnostic_module: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = diagnostic_module
    assert module.FIXED_IMAGE_RELATIVE_PATH == FIXED_RELATIVE_PATH
    assert module.FIXED_IMAGE_SHA256 == FIXED_SHA256
    assert module.FIXED_MIME_TYPE == "image/png"
    assert module.FIXED_SLOT == "top"
    assert module.FIXED_PRODUCT_TYPE == "tie-neck blouse"
    assert tuple(module.FIXED_AUDIENCES) == (
        "womenswear",
        "unisex_womenswear_compatible",
    )
    before = _snapshot()
    for argv in (
        (),
        ("--expected-head", "short"),
        ("--path", PROVIDER_SECRET),
        ("--garment-id", "g051"),
        ("--product-type", "dress"),
        ("--user", "u01"),
    ):
        with pytest.raises(SystemExit) as caught:
            module.main(list(argv))
        assert caught.value.code != 0
        assert capsys.readouterr().out == ""
    assert _snapshot() == before


def test_success_stdout_is_exact_and_calls_fixed_vision_once_without_image_or_write(
    diagnostic_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = diagnostic_module
    vision = _VisionSpy(_success_trace())
    _install_runtime(monkeypatch, module, settings=_settings(), vision=vision)
    before = _snapshot()
    code, payload = _invoke(module, capsys, "--expected-head", _head())
    assert code == 0
    assert payload == {
        "reason_code": None,
        "schema_stage": None,
        "envelope_failure_code": None,
        "envelope_metadata_profile": PROFILE,
        "model_provenance": {
            "requested_model": "grok4.6",
            "resolved_model": "grok-4.6-high",
            "model_verified": True,
        },
        "vision_call_count": 1,
    }
    assert len(vision.calls) == 1
    call = vision.calls[0]
    assert set(call) == {
        "image_bytes",
        "mime_type",
        "allowed_slot",
        "expected_product_type",
    }
    assert hashlib.sha256(call["image_bytes"]).hexdigest() == FIXED_SHA256
    assert call["image_bytes"] == (ROOT / FIXED_RELATIVE_PATH).read_bytes()
    assert call["mime_type"] == "image/png"
    assert call["allowed_slot"] == "top"
    assert call["expected_product_type"] == "tie-neck blouse"
    assert _snapshot() == before


def test_known_vision_failures_map_to_closed_fields_with_one_attempt_no_retry(
    diagnostic_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = diagnostic_module
    cases = (
        (
            _vision_failure(),
            (
                "response_schema_invalid",
                "envelope",
                "invalid_envelope_metadata",
                PROFILE,
                None,
                False,
            ),
        ),
        (
            _vision_failure(
                schema_stage="content",
                envelope_failure_code=None,
                resolved_model=None,
                model_verified=False,
            ),
            (
                "response_schema_invalid",
                "content",
                None,
                PROFILE,
                None,
                False,
            ),
        ),
        (
            _vision_failure(
                schema_stage="payload",
                envelope_failure_code=None,
                resolved_model=None,
                model_verified=False,
            ),
            (
                "response_schema_invalid",
                "payload",
                None,
                PROFILE,
                None,
                False,
            ),
        ),
        (
            _vision_failure(
                reason_code="slot_mismatch",
                schema_stage="payload",
                envelope_failure_code=None,
                envelope_metadata_profile=None,
                resolved_model="grok-4.6-high",
                model_verified=True,
            ),
            ("slot_mismatch", "payload", None, None, "grok-4.6-high", True),
        ),
    )
    before = _snapshot()
    for error, expected in cases:
        vision = _VisionSpy(error)
        _install_runtime(monkeypatch, module, settings=_settings(), vision=vision)
        code, payload = _invoke(module, capsys, "--expected-head", _head())
        reason, stage, envelope_code, profile, resolved, verified = expected
        assert code != 0
        assert payload == {
            "reason_code": reason,
            "schema_stage": stage,
            "envelope_failure_code": envelope_code,
            "envelope_metadata_profile": profile,
            "model_provenance": {
                "requested_model": "grok4.6",
                "resolved_model": resolved,
                "model_verified": verified,
            },
            "vision_call_count": 1,
        }
        assert len(vision.calls) == 1
    assert _snapshot() == before


def test_head_hash_config_and_model_startup_mismatch_fail_before_vision(
    diagnostic_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = diagnostic_module
    cases = (
        ("head", _settings()),
        ("hash", _settings()),
        ("base", _settings(cpa_base_url="")),
        ("key", _settings(cpa_api_key=None)),
        ("disabled", _settings(cpa_text_enabled=False)),
        ("forced", _settings(vision_force_failure=True)),
        ("logical", _settings(grok_model="future-model")),
        ("transport", _settings()),
        ("reported", _settings()),
    )
    before = _snapshot()
    for kind, settings in cases:
        with monkeypatch.context() as local:
            vision = _VisionSpy(AssertionError("Vision must not run"))
            _install_runtime(local, module, settings=settings, vision=vision)
            expected_head = _head()
            if kind == "head":
                expected_head = "0" * 40
            elif kind == "hash":
                local.setattr(module, "FIXED_IMAGE_SHA256", "0" * 64)
            elif kind == "transport":
                local.setattr(module, "VISION_TRANSPORT_MODEL", "future-transport")
            elif kind == "reported":
                local.setattr(module, "VISION_REPORTED_MODELS", frozenset({"future-build"}))
            code, payload = _invoke(
                module, capsys, "--expected-head", expected_head
            )
            assert code != 0
            assert payload == {
                "reason_code": "input_rejected",
                "schema_stage": None,
                "envelope_failure_code": None,
                "envelope_metadata_profile": None,
                "model_provenance": {
                    "requested_model": "grok4.6",
                    "resolved_model": None,
                    "model_verified": False,
                },
                "vision_call_count": 0,
            }
            assert vision.calls == []
    assert _snapshot() == before


@pytest.mark.parametrize("extra_key", ("provider_body", "unknown_provider_key"))
def test_extra_provider_trace_key_fails_closed_without_leak_or_retry(
    diagnostic_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra_key: str,
) -> None:
    module = diagnostic_module
    trace = _success_trace()
    trace["envelope_metadata_profile"] = PROFILE
    trace[extra_key] = PROVIDER_SECRET
    vision = _VisionSpy(trace)
    _install_runtime(monkeypatch, module, settings=_settings(), vision=vision)
    before = _snapshot()
    code, payload = _invoke(module, capsys, "--expected-head", _head())
    assert code != 0
    assert payload == {
        "reason_code": "provider_unavailable",
        "schema_stage": None,
        "envelope_failure_code": None,
        "envelope_metadata_profile": None,
        "model_provenance": {
            "requested_model": "grok4.6",
            "resolved_model": None,
            "model_verified": False,
        },
        "vision_call_count": 1,
    }
    assert len(vision.calls) == 1
    assert _snapshot() == before


@pytest.mark.parametrize("profile", tuple(sorted(RULING_U_PROFILES)))
@pytest.mark.parametrize(
    ("reason", "stage", "failure_code"),
    (
        ("response_schema_invalid", "envelope", "invalid_envelope_metadata"),
        ("response_schema_invalid", "content", None),
        ("response_schema_invalid", "payload", None),
        ("model_mismatch", "envelope", None),
    ),
)
def test_ruling_u_profiled_failure_trace_has_only_minimized_diagnostic_output(
    diagnostic_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: str,
    reason: str,
    stage: str,
    failure_code: str | None,
) -> None:
    module = diagnostic_module
    trace = _literal_catalog_trace(
        reason_code=reason,
        schema_stage=stage,
        envelope_failure_code=failure_code,
        envelope_metadata_profile=profile,
    )
    vision = _VisionSpy(VisionUnavailable(PROVIDER_SECRET, trace))
    _install_runtime(monkeypatch, module, settings=_settings(), vision=vision)
    before = _snapshot()
    code, payload = _invoke(module, capsys, "--expected-head", _head())
    assert code == 1
    assert payload == {
        "reason_code": reason,
        "schema_stage": stage,
        "envelope_failure_code": failure_code,
        "envelope_metadata_profile": profile,
        "model_provenance": {
            "requested_model": "grok4.6",
            "resolved_model": None,
            "model_verified": False,
        },
        "vision_call_count": 1,
    }
    assert len(vision.calls) == 1
    assert _snapshot() == before


@pytest.mark.parametrize("profile", ("future_profile", PROVIDER_SECRET))
def test_ruling_u_unknown_profile_is_rejected_without_output_leak(
    diagnostic_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: str,
) -> None:
    module = diagnostic_module
    trace = _literal_catalog_trace(
        reason_code="response_schema_invalid",
        schema_stage="envelope",
        envelope_failure_code="invalid_envelope_metadata",
        envelope_metadata_profile=profile,
    )
    vision = _VisionSpy(VisionUnavailable(PROVIDER_SECRET, trace))
    _install_runtime(monkeypatch, module, settings=_settings(), vision=vision)
    before = _snapshot()
    code, payload = _invoke(module, capsys, "--expected-head", _head())
    assert code == 1
    assert payload == {
        "reason_code": "provider_unavailable",
        "schema_stage": None,
        "envelope_failure_code": None,
        "envelope_metadata_profile": None,
        "model_provenance": {
            "requested_model": "grok4.6",
            "resolved_model": None,
            "model_verified": False,
        },
        "vision_call_count": 1,
    }
    assert len(vision.calls) == 1
    assert _snapshot() == before


def test_ruling_u_v2_trace_with_raw_metadata_is_rejected_without_output_leak(
    diagnostic_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = diagnostic_module
    trace = _literal_catalog_trace(
        reason_code="response_schema_invalid",
        schema_stage="envelope",
        envelope_failure_code="invalid_envelope_metadata",
        envelope_metadata_profile="cpa_chat_completion_metadata_v2",
    )
    trace["serializer_metadata"] = {"raw_key": PROVIDER_SECRET}
    vision = _VisionSpy(VisionUnavailable(PROVIDER_SECRET, trace))
    _install_runtime(monkeypatch, module, settings=_settings(), vision=vision)
    before = _snapshot()
    code, payload = _invoke(module, capsys, "--expected-head", _head())
    assert code == 1
    assert payload == {
        "reason_code": "provider_unavailable",
        "schema_stage": None,
        "envelope_failure_code": None,
        "envelope_metadata_profile": None,
        "model_provenance": {
            "requested_model": "grok4.6",
            "resolved_model": None,
            "model_verified": False,
        },
        "vision_call_count": 1,
    }
    assert len(vision.calls) == 1
    assert _snapshot() == before


@pytest.mark.parametrize(
    ("trace", "expected"),
    (
        (
            _literal_catalog_trace(
                status="ok",
                reason_code=None,
                resolved_model="grok-4.6-high",
                model_verified=True,
                assessment_count=1,
            ),
            (0, None, None, None, PROFILE, "grok-4.6-high", True),
        ),
        *(
            (
                _literal_catalog_trace(reason_code=reason),
                (1, reason, None, None, None, None, False),
            )
            for reason in (
                "input_rejected",
                "timeout",
                "provider_unavailable",
                "http_error",
            )
        ),
        (
            _literal_catalog_trace(
                reason_code="model_mismatch",
                schema_stage="envelope",
            ),
            (1, "model_mismatch", "envelope", None, None, None, False),
        ),
        (
            _literal_catalog_trace(
                reason_code="response_schema_invalid",
                schema_stage="envelope",
                envelope_failure_code="invalid_envelope_metadata",
                envelope_metadata_profile=PROFILE,
            ),
            (
                1,
                "response_schema_invalid",
                "envelope",
                "invalid_envelope_metadata",
                PROFILE,
                None,
                False,
            ),
        ),
        *(
            (
                _literal_catalog_trace(
                    reason_code="response_schema_invalid",
                    schema_stage=stage,
                    envelope_metadata_profile=PROFILE,
                ),
                (
                    1,
                    "response_schema_invalid",
                    stage,
                    None,
                    PROFILE,
                    None,
                    False,
                ),
            )
            for stage in ("content", "payload")
        ),
        *(
            (
                _literal_catalog_trace(
                    reason_code=reason,
                    resolved_model=(
                        "grok-4.6-high" if index % 2 == 0 else "grok-4.6-build"
                    ),
                    model_verified=True,
                    schema_stage="payload",
                    quality_issue_count=(2 if reason == "quality_rejected" else 0),
                ),
                (
                    1,
                    reason,
                    "payload",
                    None,
                    None,
                    "grok-4.6-high" if index % 2 == 0 else "grok-4.6-build",
                    True,
                ),
            )
            for index, reason in enumerate(
                (
                    "slot_mismatch",
                    "product_type_mismatch",
                    "audience_rejected",
                    "identifiable_person",
                    "low_confidence",
                    "invalid_region",
                    "quality_rejected",
                )
            )
        ),
    ),
)
def test_real_catalog_trace_state_matrix_is_reported_honestly(
    diagnostic_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    trace: dict[str, Any],
    expected: tuple[int, str | None, str | None, str | None, str | None, str | None, bool],
) -> None:
    module = diagnostic_module
    vision = _VisionSpy(
        trace if trace["status"] == "ok" else VisionUnavailable(PROVIDER_SECRET, trace)
    )
    _install_runtime(monkeypatch, module, settings=_settings(), vision=vision)
    before = _snapshot()
    code, payload = _invoke(module, capsys, "--expected-head", _head())
    (
        expected_code,
        reason,
        stage,
        envelope_code,
        profile,
        resolved,
        verified,
    ) = expected
    assert code == expected_code
    assert payload == {
        "reason_code": reason,
        "schema_stage": stage,
        "envelope_failure_code": envelope_code,
        "envelope_metadata_profile": profile,
        "model_provenance": {
            "requested_model": "grok4.6",
            "resolved_model": resolved,
            "model_verified": verified,
        },
        "vision_call_count": 1,
    }
    assert len(vision.calls) == 1
    assert _snapshot() == before


@pytest.mark.parametrize(
    ("trace", "accepted"),
    (
        *(
            pytest.param(
                _literal_catalog_trace(
                    reason_code=reason,
                    resolved_model="grok-4.6-high",
                    model_verified=True,
                    schema_stage="payload",
                    quality_issue_count=5,
                ),
                True,
                id=f"{reason}-allows-five-quality-issues",
            )
            for reason in (
                "slot_mismatch",
                "product_type_mismatch",
                "identifiable_person",
                "low_confidence",
            )
        ),
        pytest.param(
            _literal_catalog_trace(
                reason_code="quality_rejected",
                resolved_model="grok-4.6-high",
                model_verified=True,
                schema_stage="payload",
                quality_issue_count=1,
            ),
            True,
            id="quality-rejected-allows-one-quality-issue",
        ),
        pytest.param(
            _literal_catalog_trace(
                reason_code="quality_rejected",
                resolved_model="grok-4.6-high",
                model_verified=True,
                schema_stage="payload",
                quality_issue_count=5,
            ),
            True,
            id="quality-rejected-allows-five-quality-issues",
        ),
        *(
            pytest.param(
                _literal_catalog_trace(
                    reason_code="quality_rejected",
                    resolved_model="grok-4.6-high",
                    model_verified=True,
                    schema_stage="payload",
                    quality_issue_count=count,
                ),
                False,
                id=f"quality-rejected-rejects-{count}",
            )
            for count in (0, 6, 999)
        ),
        *(
            pytest.param(
                _literal_catalog_trace(
                    reason_code="invalid_region",
                    resolved_model="grok-4.6-high",
                    model_verified=True,
                    schema_stage="payload",
                    quality_issue_count=count,
                ),
                False,
                id=f"invalid-region-rejects-{count}",
            )
            for count in (1, 999)
        ),
        pytest.param(
            _literal_catalog_trace(
                reason_code="audience_rejected",
                resolved_model="grok-4.6-high",
                model_verified=True,
                schema_stage="payload",
                quality_issue_count=1,
            ),
            False,
            id="audience-rejected-requires-zero-quality-issues",
        ),
        *(
            pytest.param(
                _literal_catalog_trace(
                    reason_code=reason,
                    resolved_model="grok-4.6-high",
                    model_verified=True,
                    schema_stage="payload",
                    quality_issue_count=count,
                ),
                False,
                id=f"{reason}-rejects-{count}",
            )
            for reason, count in (
                ("slot_mismatch", 6),
                ("product_type_mismatch", 999),
                ("identifiable_person", 6),
                ("low_confidence", 999),
            )
        ),
        pytest.param(
            _literal_catalog_trace(
                status="ok",
                reason_code=None,
                resolved_model="grok-4.6-high",
                model_verified=True,
                assessment_count=1,
                quality_issue_count=1,
            ),
            False,
            id="success-requires-zero-quality-issues",
        ),
        pytest.param(
            _literal_catalog_trace(
                reason_code="timeout",
                quality_issue_count=1,
            ),
            False,
            id="pre-model-failure-requires-zero-quality-issues",
        ),
        pytest.param(
            _literal_catalog_trace(
                reason_code="response_schema_invalid",
                schema_stage="envelope",
                envelope_failure_code="invalid_envelope_metadata",
                envelope_metadata_profile=PROFILE,
                quality_issue_count=1,
            ),
            False,
            id="schema-failure-requires-zero-quality-issues",
        ),
        pytest.param(
            _literal_catalog_trace(
                reason_code="model_mismatch",
                schema_stage="envelope",
                quality_issue_count=1,
            ),
            False,
            id="model-failure-requires-zero-quality-issues",
        ),
        pytest.param(
            _literal_catalog_trace(
                reason_code="slot_mismatch",
                resolved_model="grok-4.6-high",
                model_verified=True,
                schema_stage="payload",
                quality_issue_count=True,
            ),
            False,
            id="quality-count-rejects-bool",
        ),
        pytest.param(
            _literal_catalog_trace(
                reason_code="quality_rejected",
                resolved_model="grok-4.6-high",
                model_verified=True,
                schema_stage="payload",
                quality_issue_count=1.0,
            ),
            False,
            id="quality-count-rejects-float",
        ),
        pytest.param(
            _literal_catalog_trace(
                reason_code="slot_mismatch",
                resolved_model="grok-4.6-high",
                model_verified=True,
                schema_stage="payload",
                quality_issue_count=-1,
            ),
            False,
            id="quality-count-rejects-negative",
        ),
        pytest.param(
            _literal_catalog_trace(assessment_count=True),
            False,
            id="assessment-count-rejects-bool",
        ),
        pytest.param(
            _literal_catalog_trace(assessment_count=0.0),
            False,
            id="assessment-count-rejects-float",
        ),
        pytest.param(
            _literal_catalog_trace(assessment_count=-1),
            False,
            id="assessment-count-rejects-negative",
        ),
        pytest.param(
            _literal_catalog_trace(assessment_count=6),
            False,
            id="pre-model-failure-rejects-unreachable-assessment-count",
        ),
    ),
)
def test_catalog_trace_quality_counts_match_production_reachable_bounds(
    diagnostic_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    trace: dict[str, Any],
    accepted: bool,
) -> None:
    module = diagnostic_module
    vision = _VisionSpy(
        trace if trace["status"] == "ok" else VisionUnavailable(PROVIDER_SECRET, trace)
    )
    _install_runtime(monkeypatch, module, settings=_settings(), vision=vision)
    before = _snapshot()
    code, payload = _invoke(module, capsys, "--expected-head", _head())
    assert code != 0
    if accepted:
        assert payload == {
            "reason_code": trace["reason_code"],
            "schema_stage": "payload",
            "envelope_failure_code": None,
            "envelope_metadata_profile": None,
            "model_provenance": {
                "requested_model": "grok4.6",
                "resolved_model": "grok-4.6-high",
                "model_verified": True,
            },
            "vision_call_count": 1,
        }
    else:
        assert payload == {
            "reason_code": "provider_unavailable",
            "schema_stage": None,
            "envelope_failure_code": None,
            "envelope_metadata_profile": None,
            "model_provenance": {
                "requested_model": "grok4.6",
                "resolved_model": None,
                "model_verified": False,
            },
            "vision_call_count": 1,
        }
    assert len(vision.calls) == 1
    assert _snapshot() == before


@pytest.mark.parametrize(
    ("updates", "missing_key"),
    (
        (
            {
                "reason_code": "response_schema_invalid",
                "schema_stage": "envelope",
                "envelope_failure_code": "invalid_envelope_metadata",
                "envelope_metadata_profile": PROFILE,
                "resolved_model": "grok-4.6-high",
                "model_verified": True,
            },
            None,
        ),
        (
            {
                "reason_code": "slot_mismatch",
                "resolved_model": "grok-4.6-high",
                "model_verified": True,
                "schema_stage": None,
            },
            None,
        ),
        (
            {"reason_code": "timeout", "schema_stage": "payload"},
            None,
        ),
        (
            {"reason_code": "model_mismatch", "schema_stage": None},
            None,
        ),
        (
            {
                "reason_code": "response_schema_invalid",
                "schema_stage": "content",
                "envelope_metadata_profile": PROFILE,
                "resolved_model": "grok-4.6-build",
                "model_verified": True,
            },
            None,
        ),
        ({}, "status"),
        ({"status": PROVIDER_SECRET}, None),
        ({"reason_code": PROVIDER_SECRET}, None),
    ),
)
def test_impossible_missing_or_poisoned_trace_state_fails_closed(
    diagnostic_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    updates: dict[str, Any],
    missing_key: str | None,
) -> None:
    module = diagnostic_module
    trace = _literal_catalog_trace(**updates)
    if missing_key is not None:
        del trace[missing_key]
    vision = _VisionSpy(VisionUnavailable(PROVIDER_SECRET, trace))
    _install_runtime(monkeypatch, module, settings=_settings(), vision=vision)
    before = _snapshot()
    code, payload = _invoke(module, capsys, "--expected-head", _head())
    assert code != 0
    assert payload == {
        "reason_code": "provider_unavailable",
        "schema_stage": None,
        "envelope_failure_code": None,
        "envelope_metadata_profile": None,
        "model_provenance": {
            "requested_model": "grok4.6",
            "resolved_model": None,
            "model_verified": False,
        },
        "vision_call_count": 1,
    }
    assert len(vision.calls) == 1
    assert _snapshot() == before


def test_unexpected_provider_exception_is_one_closed_attempt_and_zero_write(
    diagnostic_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = diagnostic_module
    vision = _VisionSpy(RuntimeError(PROVIDER_SECRET))
    _install_runtime(monkeypatch, module, settings=_settings(), vision=vision)
    before = _snapshot()
    code, payload = _invoke(module, capsys, "--expected-head", _head())
    assert code != 0
    assert payload["reason_code"] == "provider_unavailable"
    assert payload["schema_stage"] is None
    assert payload["envelope_failure_code"] is None
    assert payload["envelope_metadata_profile"] is None
    assert payload["model_provenance"] == {
        "requested_model": "grok4.6",
        "resolved_model": None,
        "model_verified": False,
    }
    assert payload["vision_call_count"] == 1
    assert len(vision.calls) == 1
    assert _snapshot() == before


def test_direct_script_from_repository_root_fails_closed_before_provider_on_head_mismatch() -> None:
    controlled_mismatch = "0" * 40
    assert controlled_mismatch != _head()
    assert "torch128" in str(Path(sys.executable)).lower()

    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    before = _direct_script_write_surface_snapshot()
    result = subprocess.run(
        [
            sys.executable,
            "scripts/diagnose_cpa_vision_envelope.py",
            "--expected-head",
            controlled_mismatch,
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    after = _direct_script_write_surface_snapshot()

    assert after == before
    assert "Traceback" not in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert result.stderr == ""
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    _assert_minimized(payload)
    assert payload == {
        "reason_code": "input_rejected",
        "schema_stage": None,
        "envelope_failure_code": None,
        "envelope_metadata_profile": None,
        "model_provenance": {
            "requested_model": "grok4.6",
            "resolved_model": None,
            "model_verified": False,
        },
        "vision_call_count": 0,
    }
