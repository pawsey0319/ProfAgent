from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import re
import stat
import subprocess
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

from profagent.config import Settings
from profagent.vision import (
    CPA_CHAT_COMPLETION_METADATA_PROFILE,
    VISION_REPORTED_MODELS,
    VISION_TRANSPORT_MODEL,
    VisionAdapter,
    VisionUnavailable,
)


ROOT = Path(__file__).resolve().parents[1]
FIXED_IMAGE_RELATIVE_PATH = Path(
    "data/assets/private_quarantine/cpa_generated/"
    "33c9439e4516e571925689f0ef501a9771f3d25888ff24ecbe044c6b16d19749.png"
)
FIXED_IMAGE_SHA256 = (
    "33c9439e4516e571925689f0ef501a9771f3d25888ff24ecbe044c6b16d19749"
)
FIXED_IMAGE_SIZE = 784_326
FIXED_IMAGE_DIMENSIONS = (1024, 1024)
FIXED_MIME_TYPE = "image/png"
FIXED_SLOT = "top"
FIXED_PRODUCT_TYPE = "tie-neck blouse"
FIXED_AUDIENCES = ("womenswear", "unisex_womenswear_compatible")

LOGICAL_VISION_MODEL = "grok4.6"
EXPECTED_CPA_BASE_URL = "http://127.0.0.1:8317/v1"
EXPECTED_VISION_TRANSPORT_MODEL = "grok-4.6-high"
EXPECTED_VISION_REPORTED_MODELS = frozenset(
    {"grok-4.6-high", "grok-4.6-build"}
)
METADATA_PROFILE = CPA_CHAT_COMPLETION_METADATA_PROFILE

_HEAD_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SCHEMA_STAGES = frozenset({"envelope", "content", "payload"})
_ENVELOPE_FAILURE_CODES = frozenset(
    {
        "envelope_json_invalid",
        "unsupported_envelope_fields",
        "invalid_envelope_metadata",
        "invalid_choices",
        "invalid_choice_metadata",
        "invalid_message",
        "invalid_message_metadata",
    }
)
_FAILURE_REASONS = frozenset(
    {
        "input_rejected",
        "timeout",
        "provider_unavailable",
        "http_error",
        "response_schema_invalid",
        "model_mismatch",
        "slot_mismatch",
        "product_type_mismatch",
        "audience_rejected",
        "identifiable_person",
        "low_confidence",
        "invalid_region",
        "quality_rejected",
    }
)
_TRACE_KEYS = frozenset(
    {
        "reason_code",
        "schema_stage",
        "envelope_failure_code",
        "envelope_metadata_profile",
        "requested_model",
        "resolved_model",
        "model_verified",
    }
)


class _ClosedArgumentParser(argparse.ArgumentParser):
    """Reject CLI misuse without echoing caller-controlled values."""

    def error(self, _message: str) -> None:
        self.exit(2, "diagnostic arguments rejected\n")


def _parser() -> argparse.ArgumentParser:
    parser = _ClosedArgumentParser(add_help=False)
    parser.add_argument("--expected-head", required=True, type=_expected_head)
    return parser


def _expected_head(value: str) -> str:
    if not _HEAD_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("expected head rejected")
    return value


def _current_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    head = result.stdout.strip()
    if not _HEAD_PATTERN.fullmatch(head):
        raise ValueError("invalid repository head")
    return head


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _validated_image_bytes(root: Path) -> bytes:
    path = root / FIXED_IMAGE_RELATIVE_PATH
    file_stat = path.lstat()
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_size != FIXED_IMAGE_SIZE
    ):
        raise ValueError("fixed image rejected")

    image_bytes = path.read_bytes()
    if (
        len(image_bytes) != FIXED_IMAGE_SIZE
        or hashlib.sha256(image_bytes).hexdigest() != FIXED_IMAGE_SHA256
    ):
        raise ValueError("fixed image rejected")

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(image_bytes)) as candidate:
            if (
                candidate.format != "PNG"
                or candidate.size != FIXED_IMAGE_DIMENSIONS
            ):
                raise ValueError("fixed image rejected")
            candidate.verify()
        with Image.open(io.BytesIO(image_bytes)) as decoded:
            decoded.load()
            if decoded.format != "PNG" or decoded.size != FIXED_IMAGE_DIMENSIONS:
                raise ValueError("fixed image rejected")
    return image_bytes


def _settings_are_exact(settings: Any) -> bool:
    base_url = getattr(settings, "cpa_base_url", None)
    api_key = getattr(settings, "cpa_api_key", None)
    return (
        type(base_url) is str
        and base_url.rstrip("/") == EXPECTED_CPA_BASE_URL
        and type(api_key) is str
        and bool(api_key.strip())
        and getattr(settings, "grok_model", None) == LOGICAL_VISION_MODEL
        and getattr(settings, "cpa_text_enabled", None) is True
        and getattr(settings, "vision_force_failure", None) is False
        and VISION_TRANSPORT_MODEL == EXPECTED_VISION_TRANSPORT_MODEL
        and frozenset(VISION_REPORTED_MODELS) == EXPECTED_VISION_REPORTED_MODELS
    )


def _model_provenance(
    *, resolved_model: str | None = None, model_verified: bool = False
) -> dict[str, Any]:
    return {
        "requested_model": LOGICAL_VISION_MODEL,
        "resolved_model": resolved_model,
        "model_verified": model_verified,
    }


def _output(
    *,
    reason_code: str | None,
    schema_stage: str | None = None,
    envelope_failure_code: str | None = None,
    envelope_metadata_profile: str | None = None,
    resolved_model: str | None = None,
    model_verified: bool = False,
    vision_call_count: int,
) -> dict[str, Any]:
    return {
        "reason_code": reason_code,
        "schema_stage": schema_stage,
        "envelope_failure_code": envelope_failure_code,
        "envelope_metadata_profile": envelope_metadata_profile,
        "model_provenance": _model_provenance(
            resolved_model=resolved_model,
            model_verified=model_verified,
        ),
        "vision_call_count": vision_call_count,
    }


def _input_rejected() -> dict[str, Any]:
    return _output(reason_code="input_rejected", vision_call_count=0)


def _provider_unavailable() -> dict[str, Any]:
    return _output(reason_code="provider_unavailable", vision_call_count=1)


def _closed_provider_result(trace: Any) -> tuple[int, dict[str, Any]]:
    if type(trace) is not dict or not _TRACE_KEYS.issubset(trace):
        return 1, _provider_unavailable()

    reason = trace.get("reason_code")
    stage = trace.get("schema_stage")
    failure_code = trace.get("envelope_failure_code")
    profile = trace.get("envelope_metadata_profile")
    requested = trace.get("requested_model")
    resolved = trace.get("resolved_model")
    verified = trace.get("model_verified")

    values_are_closed = (
        type(requested) is str
        and (reason is None or type(reason) is str)
        and (stage is None or type(stage) is str)
        and (failure_code is None or type(failure_code) is str)
        and (profile is None or type(profile) is str)
        and (resolved is None or type(resolved) is str)
        and type(verified) is bool
    )
    if not values_are_closed:
        return 1, _provider_unavailable()
    if (
        requested != LOGICAL_VISION_MODEL
        or (stage is not None and stage not in _SCHEMA_STAGES)
        or (profile is not None and profile != METADATA_PROFILE)
    ):
        return 1, _provider_unavailable()
    if verified:
        if resolved not in EXPECTED_VISION_REPORTED_MODELS:
            return 1, _provider_unavailable()
    elif resolved is not None:
        return 1, _provider_unavailable()

    if reason is None:
        if (
            stage is not None
            or failure_code is not None
            or profile != METADATA_PROFILE
            or not verified
        ):
            return 1, _provider_unavailable()
        return 0, _output(
            reason_code=None,
            envelope_metadata_profile=METADATA_PROFILE,
            resolved_model=resolved,
            model_verified=True,
            vision_call_count=1,
        )

    if reason not in _FAILURE_REASONS:
        return 1, _provider_unavailable()
    if reason == "response_schema_invalid":
        if profile != METADATA_PROFILE or stage not in _SCHEMA_STAGES:
            return 1, _provider_unavailable()
        if stage == "envelope":
            if failure_code not in _ENVELOPE_FAILURE_CODES:
                return 1, _provider_unavailable()
        elif failure_code is not None:
            return 1, _provider_unavailable()
    elif failure_code is not None:
        return 1, _provider_unavailable()

    return 1, _output(
        reason_code=reason,
        schema_stage=stage,
        envelope_failure_code=failure_code,
        envelope_metadata_profile=profile,
        resolved_model=resolved,
        model_verified=verified,
        vision_call_count=1,
    )


async def _inspect_once(vision: Any, image_bytes: bytes) -> dict[str, Any]:
    _assessment, trace = await vision.inspect_catalog_asset(
        image_bytes=image_bytes,
        mime_type=FIXED_MIME_TYPE,
        allowed_slot=FIXED_SLOT,
        expected_product_type=FIXED_PRODUCT_TYPE,
    )
    return trace


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def main(
    argv: list[str] | None = None,
    *,
    settings_factory: Callable[[], Any] | None = None,
    vision_factory: Callable[[Any], Any] | None = None,
    head_reader: Callable[[Path], str] | None = None,
    file_validator: Callable[[Path], bytes] | None = None,
) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if len(raw_argv) != 2 or raw_argv[0] != "--expected-head":
        _parser().error("diagnostic arguments rejected")
    args = _parser().parse_args(raw_argv)
    expected_head = args.expected_head
    read_head = head_reader or _current_head
    validate_file = file_validator or _validated_image_bytes
    make_settings = settings_factory or Settings.from_env
    make_vision = vision_factory or VisionAdapter
    try:
        if read_head(ROOT) != expected_head:
            raise ValueError("repository head mismatch")
        image_bytes = validate_file(ROOT)
        settings = make_settings()
        if not _settings_are_exact(settings):
            raise ValueError("settings rejected")
        vision = make_vision(settings)
    except Exception:
        _emit(_input_rejected())
        return 1

    try:
        trace = asyncio.run(_inspect_once(vision, image_bytes))
    except VisionUnavailable as error:
        code, payload = _closed_provider_result(error.provider_trace)
        _emit(payload)
        return code
    except Exception:
        _emit(_provider_unavailable())
        return 1

    code, payload = _closed_provider_result(trace)
    _emit(payload)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
