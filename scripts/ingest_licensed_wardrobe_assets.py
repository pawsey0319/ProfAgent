from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from profagent.config import CPA_IMAGE_MODEL, Settings
from profagent.catalog_product_types import (
    CATALOG_PRODUCT_TYPE_BY_GARMENT_ID,
    product_type_for_garment,
)
from profagent.image_provider import GrokImageProvider
from profagent.licensed_assets import (
    CatalogCroppedImage,
    CatalogVisionInspector,
    FetchedImage,
    ImageFetchError,
    LicenseCode,
    LicensedSourceReceipt,
    PublicLicenseReceipt,
    SafeImageConfig,
    SafeImageFetcher,
    assess_and_crop_catalog_asset,
    public_license_from_source,
)
from profagent.models import Garment
from profagent.providers import (
    CPA_REPORTED_MODELS,
    LOGICAL_GROK_MODEL,
    ProviderUnavailable,
)
from profagent.vision import VisionAdapter, VisionUnavailable
from profagent.wardrobe_assets import (
    WARDROBE_GENERATED_ASSET_SET,
    WARDROBE_GENERATED_ASSET_VERSION,
    validate_generated_v1_manifest,
    validate_generated_v1_reference,
)


CONTROLLED_GARMENT_FILE = ROOT / "data" / "fixtures" / "garments_s16_womenswear.jsonl"
BASE_GARMENT_FILE = ROOT / "data" / "fixtures" / "garments.jsonl"
AI_REFERENCE_MANIFEST = ROOT / "data" / "manifests" / "wardrobe_generated_v1.json"
AI_REFERENCE_ASSET_ROOT = ROOT / "data" / "assets" / WARDROBE_GENERATED_ASSET_SET
DEFAULT_MANIFEST_PATH = ROOT / "data" / "manifests" / "wardrobe_assets_v2.json"
DEFAULT_SOURCES_PATH = ROOT / "data" / "sources" / "wardrobe_s16_sources.jsonl"
DEFAULT_ASSET_DIRECTORY = ROOT / "data" / "assets" / "wardrobe_licensed_v1"
OPENVERSE_API_BASE = "https://api.openverse.org/v1/images/"
OPENVERSE_RESPONSE_LIMIT = 1024 * 1024
OPENVERSE_THUMBNAIL_ACCEPT = "image/png,image/jpeg,image/webp"
OPENVERSE_THUMBNAIL_TOTAL_TIMEOUT_SECONDS = 20.0
MANIFEST_INPUT_LIMIT = 4 * 1024 * 1024
SOURCE_HISTORY_LIMIT = 8 * 1024 * 1024
MAX_CANDIDATE_BUDGET = 10
MAX_RETRY_BUDGET = 3
MAX_CONCURRENCY = 8
MAX_TOTAL_BUDGET = 70
MAX_SEARCH_BUDGET = 3
CPA_GENERATED_PROMPT_VERSION = "womenswear_catalog_reference_prompt_v1"
CPA_GENERATED_HISTORICAL_MODEL_ALLOWLIST = (CPA_IMAGE_MODEL,)

_CPA_GENERATED_RECEIPT_FIELDS = frozenset(
    {
        "bound_garment_id",
        "bound_user_id",
        "prompt_template_version",
        "prompt_sha256",
        "requested_model",
        "transport_model",
        "request_model_pinned",
        "cpa_trace_verified",
        "model_reported",
        "resolved_model",
        "model_verified",
        "verification_basis",
    }
)

IngestionFailureReason = Literal[
    "candidate_budget_exhausted",
    "no_results",
    "provider_rate_limited",
    "provider_auth_rejected",
    "provider_unavailable",
    "provider_schema_invalid",
    "provider_response_too_large",
    "source_takedown",
    "total_budget_exhausted",
    "user_owned_loader_unavailable",
    "user_owned_source_missing",
    "user_owned_source_rejected",
    "timeout",
    "provider_unavailable",
    "response_schema_invalid",
    "model_mismatch",
    "unsupported_mime",
    "decode_failed",
    "dimension_out_of_range",
    "pixel_limit_exceeded",
    "response_too_large",
    "hash_mismatch",
    "mime_mismatch",
    "missing_mime",
    "unsupported_content_encoding",
    "input_rejected",
    "http_error",
    "slot_mismatch",
    "product_type_mismatch",
    "audience_rejected",
    "identifiable_person",
    "low_confidence",
    "invalid_region",
    "quality_rejected",
]

_IMAGE_FAILURE_REASON_BY_CODE: dict[str, IngestionFailureReason] = {
    "CPA_IMAGE_OUTPUT_REJECTED": "response_schema_invalid",
    "CPA_IMAGE_INPUT_REJECTED": "input_rejected",
    "CPA_IMAGE_PROVIDER_UNAVAILABLE": "provider_unavailable",
    "CPA_IMAGE_PROVIDER_TIMEOUT": "timeout",
    "CPA_IMAGE_BUDGET_EXCEEDED": "timeout",
    "CPA_IMAGE_PROVIDER_DISABLED": "provider_unavailable",
    "CPA_IMAGE_CIRCUIT_OPEN": "provider_unavailable",
    "CPA_IMAGE_TRACE_VERIFICATION_FAILED": "model_mismatch",
    "CPA_IMAGE_MODEL_VERIFICATION_FAILED": "model_mismatch",
}

FailureStage = Literal["image_provider", "local_validation", "vision"]
SchemaStage = Literal["envelope", "content", "payload"]
VisionFailureReason = Literal[
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
]

_VISION_FAILURE_REASONS = frozenset(
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
_SCHEMA_STAGES = frozenset({"envelope", "content", "payload"})

_PUBLIC_LICENSE_CODES = frozenset(
    {"CC0", "PDM", "CC-BY-2.0", "CC-BY-3.0", "CC-BY-4.0"}
)
_CANONICAL_OPENVERSE_LICENSES: dict[tuple[str, str], tuple[LicenseCode, str]] = {
    ("cc0", "1.0"): (
        "CC0",
        "https://creativecommons.org/publicdomain/zero/1.0/",
    ),
    ("pdm", "1.0"): (
        "PDM",
        "https://creativecommons.org/publicdomain/mark/1.0/",
    ),
    ("by", "2.0"): (
        "CC-BY-2.0",
        "https://creativecommons.org/licenses/by/2.0/",
    ),
    ("by", "3.0"): (
        "CC-BY-3.0",
        "https://creativecommons.org/licenses/by/3.0/",
    ),
    ("by", "4.0"): (
        "CC-BY-4.0",
        "https://creativecommons.org/licenses/by/4.0/",
    ),
}
_HASH_CHARS = frozenset("0123456789abcdef")
_MANIFEST_ITEM_KEYS = frozenset(
    {
        "garment_id",
        "user_id",
        "slot",
        "audience",
        "source_kind",
        "status",
        "original_sha256",
        "processed_sha256",
        "relative_path",
        "license",
        "receipt_history",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "provider",
        "provider_item_id",
        "provider_license",
        "provider_license_version",
        "license_code",
        "license_url",
        "creator",
        "source_url",
        "image_url",
        "source_ref",
        "expected_sha256",
        "attribution",
        "imported_at",
        "original_sha256",
        "processed_sha256",
        "processing",
    }
)
_CPA_GENERATED_RECEIPT_KEYS = _RECEIPT_KEYS | _CPA_GENERATED_RECEIPT_FIELDS
_CONTROLLED_FAILURE_REASONS = frozenset(
    {
        "candidate_budget_exhausted",
        "no_results",
        "provider_rate_limited",
        "provider_auth_rejected",
        "provider_unavailable",
        "provider_schema_invalid",
        "provider_response_too_large",
        "source_takedown",
        "user_owned_loader_unavailable",
        "user_owned_source_missing",
        "user_owned_source_rejected",
        "timeout",
        "provider_unavailable",
        "response_schema_invalid",
        "model_mismatch",
        "unsupported_mime",
        "decode_failed",
        "dimension_out_of_range",
        "pixel_limit_exceeded",
        "response_too_large",
        "hash_mismatch",
        "mime_mismatch",
        "missing_mime",
        "unsupported_content_encoding",
        "input_rejected",
        "http_error",
        "slot_mismatch",
        "product_type_mismatch",
        "audience_rejected",
        "identifiable_person",
        "low_confidence",
        "invalid_region",
        "quality_rejected",
    }
)


class IngestionConfig(BaseModel):
    """Closed, bounded configuration for the injectable ingestion boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider: Literal["openverse", "user-owned", "cpa-generated"]
    license_allowlist: tuple[LicenseCode, ...] = ()
    garment_file: Path
    source_map: Path | None = None
    manifest_path: Path = DEFAULT_MANIFEST_PATH
    sources_path: Path = DEFAULT_SOURCES_PATH
    asset_directory: Path = DEFAULT_ASSET_DIRECTORY
    dry_run: bool = False
    resume: bool = False
    candidate_budget: StrictInt = Field(default=5, gt=0, le=MAX_CANDIDATE_BUDGET)
    retry_budget: StrictInt = Field(default=2, ge=0, le=MAX_RETRY_BUDGET)
    concurrency: StrictInt = Field(default=3, gt=0, le=MAX_CONCURRENCY)
    total_budget: StrictInt = Field(default=70, gt=0, le=MAX_TOTAL_BUDGET)
    search_budget: StrictInt = Field(default=3, gt=0, le=MAX_SEARCH_BUDGET)
    diagnostic_canary: bool = False

    @field_validator("license_allowlist")
    @classmethod
    def _validate_license_allowlist(
        cls, value: tuple[LicenseCode, ...]
    ) -> tuple[LicenseCode, ...]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate_license_code")
        if any(code not in _PUBLIC_LICENSE_CODES for code in value):
            raise ValueError("license_not_allowed_for_ingestion")
        return value

    @model_validator(mode="after")
    def _validate_provider_inputs(self) -> "IngestionConfig":
        if self.provider == "openverse":
            if not self.license_allowlist or self.source_map is not None:
                raise ValueError("invalid_openverse_inputs")
        elif self.provider == "user-owned" and (
            self.source_map is None or self.license_allowlist
        ):
            raise ValueError("invalid_user_owned_inputs")
        elif self.provider == "cpa-generated" and (
            self.source_map is not None or self.license_allowlist
        ):
            raise ValueError("invalid_cpa_generated_inputs")
        if len({self.manifest_path, self.sources_path}) != 2:
            raise ValueError("state_paths_must_be_distinct")
        if self.diagnostic_canary and (
            self.provider != "openverse"
            or not self.dry_run
            or self.resume
            or self.candidate_budget > 3
            or self.retry_budget != 0
            or self.concurrency != 1
            or self.total_budget != 1
        ):
            raise ValueError("invalid_diagnostic_canary_bounds")
        return self


class MinimalModelProvenance(BaseModel):
    """Content-free model routing evidence suitable for public diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    requested_model: str = Field(min_length=1, max_length=100)
    resolved_model: str | None = Field(default=None, max_length=100)
    model_verified: bool

    @field_validator("requested_model", "resolved_model")
    @classmethod
    def _validate_model_token(cls, value: str | None) -> str | None:
        if value is not None and not all(
            char.isalnum() or char in "-._:/" for char in value
        ):
            raise ValueError("invalid_model_token")
        return value

    @model_validator(mode="after")
    def _validate_verification(self) -> "MinimalModelProvenance":
        if self.model_verified != (self.resolved_model is not None):
            raise ValueError("invalid_model_verification")
        return self


class ItemIngestionOutcome(BaseModel):
    """Closed public status for one server-controlled garment ID."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    garment_id: str = Field(pattern=r"^g\d{3}$")
    status: Literal["ready", "reused", "quarantined", "unattempted"]
    reason_code: IngestionFailureReason | None = None
    failure_stage: FailureStage | None = None
    schema_stage: SchemaStage | None = None
    image_model_provenance: MinimalModelProvenance | None = None
    vision_model_provenance: MinimalModelProvenance | None = None

    @model_validator(mode="after")
    def _validate_status_reason(self) -> "ItemIngestionOutcome":
        if self.status == "quarantined" and self.reason_code in {
            None,
            "total_budget_exhausted",
        }:
            raise ValueError("quarantined_reason_required")
        if self.status == "unattempted" and self.reason_code != "total_budget_exhausted":
            raise ValueError("unattempted_budget_reason_required")
        if self.status in {"ready", "reused"} and self.reason_code is not None:
            raise ValueError("successful_outcome_must_not_have_reason")
        if self.failure_stage is not None and self.status != "quarantined":
            raise ValueError("failure_stage_requires_quarantine")
        if self.failure_stage != "vision" and self.vision_model_provenance is not None:
            raise ValueError("vision_provenance_requires_vision_failure")
        if (
            self.failure_stage == "vision"
            and self.reason_code == "response_schema_invalid"
        ):
            if self.schema_stage is None:
                raise ValueError("vision_schema_failure_requires_stage")
        elif self.schema_stage is not None:
            raise ValueError("schema_stage_requires_vision_schema_failure")
        return self


class IngestionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dry_run: bool
    ready_ids: tuple[str, ...]
    reused_ids: tuple[str, ...]
    quarantined_ids: tuple[str, ...]
    remaining_ids: tuple[str, ...]
    api_attempts: int = Field(ge=0)
    candidate_attempts: int = Field(ge=0)
    vision_call_count: int = Field(default=0, ge=0)
    vision_model_provenance: str | None = Field(default=None, max_length=100)
    item_outcomes: tuple[ItemIngestionOutcome, ...] = Field(
        default=(), max_length=120
    )
    manifest_path: Path


@dataclass(frozen=True)
class _NormalizedSource:
    provider_item_id: str
    source_receipt: LicensedSourceReceipt
    image_url: str | None
    provider_license: str
    provider_license_version: str
    source_ref: str | None = None
    expected_sha256: str | None = None

    def identity(self) -> dict[str, Any]:
        return {
            "provider": self.source_receipt.provider,
            "provider_item_id": self.provider_item_id,
            "provider_license": self.provider_license,
            "provider_license_version": self.provider_license_version,
            "license_code": self.source_receipt.license_code,
            "license_url": (
                str(self.source_receipt.license_url)
                if self.source_receipt.license_url is not None
                else None
            ),
            "creator": self.source_receipt.creator,
            "source_url": (
                str(self.source_receipt.source_url)
                if self.source_receipt.source_url is not None
                else None
            ),
            "image_url": self.image_url,
            "source_ref": self.source_ref,
            "expected_sha256": self.expected_sha256,
            "attribution": self.source_receipt.attribution,
        }


@dataclass(frozen=True)
class _AttemptOutcome:
    garment: Garment
    status: Literal["ready", "reused", "quarantined"]
    failure_reason: IngestionFailureReason | None = None
    source: _NormalizedSource | None = None
    fetched: FetchedImage | None = None
    cropped: CatalogCroppedImage | None = None
    public_license: PublicLicenseReceipt | None = None
    existing_item: dict[str, Any] | None = None
    receipt_extensions: dict[str, Any] | None = None
    source_kind: Literal[
        "licensed_photo", "user_owned_photo", "ai_generated_reference"
    ] | None = None
    failure_stage: FailureStage | None = None
    schema_stage: SchemaStage | None = None
    image_model_provenance: MinimalModelProvenance | None = None
    vision_model_provenance: MinimalModelProvenance | None = None
    private_quarantine_bytes: bytes | None = None
    private_quarantine_record: dict[str, Any] | None = None


_SearchReason = Literal[
    "no_results",
    "provider_rate_limited",
    "provider_auth_rejected",
    "provider_unavailable",
    "provider_schema_invalid",
    "provider_response_too_large",
]


@dataclass(frozen=True, repr=False)
class _SearchOutcome:
    candidates: tuple[Any, ...]
    reason_code: _SearchReason | None

    def __repr__(self) -> str:
        return (
            "_SearchOutcome("
            f"candidate_count={len(self.candidates)}, "
            f"reason_code={self.reason_code!r})"
        )


class _OpenverseTransportLogFilter(logging.Filter):
    """Suppress official search request URLs so frozen queries never enter logs."""

    _profagent_openverse_url_filter = True

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return OPENVERSE_API_BASE not in record.getMessage()
        except Exception:
            return False


def _suppress_openverse_transport_url_logging() -> None:
    logger = logging.getLogger("httpx")
    if not any(
        getattr(item, "_profagent_openverse_url_filter", False)
        for item in logger.filters
    ):
        logger.addFilter(_OpenverseTransportLogFilter())


@dataclass
class _Counters:
    api_attempts: int = 0
    candidate_attempts: int = 0
    vision_call_count: int = 0
    vision_model_provenance: str | None = None


def _load_json(path: Path, *, limit: int) -> Any:
    try:
        if path.stat().st_size > limit:
            raise ValueError("state_file_too_large")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_state_file") from exc


def _read_jsonl(path: Path, *, limit: int) -> list[Any]:
    try:
        if path.stat().st_size > limit:
            raise ValueError("state_file_too_large")
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_jsonl_file") from exc


def _authoritative_garments() -> dict[str, Garment]:
    authoritative: dict[str, Garment] = {}
    for path in (BASE_GARMENT_FILE, CONTROLLED_GARMENT_FILE):
        for row in _read_jsonl(path, limit=MANIFEST_INPUT_LIMIT):
            garment = Garment.model_validate(row)
            if garment.garment_id in authoritative:
                raise ValueError("duplicate_authoritative_garment")
            authoritative[garment.garment_id] = garment
    if len(authoritative) != 120:
        raise ValueError("authoritative_garment_count_mismatch")
    return authoritative


@lru_cache(maxsize=50)
def _verified_ai_reference(garment_id: str) -> dict[str, Any]:
    manifest = _load_json(AI_REFERENCE_MANIFEST, limit=2 * 1024 * 1024)
    if not validate_generated_v1_manifest(manifest):
        raise ValueError("invalid_ai_reference_manifest")
    raw_items = manifest["items"]
    garment_ids = [
        item.get("garment_id") if isinstance(item, dict) else None
        for item in raw_items
    ]
    if (
        len(raw_items) != 50
        or any(not isinstance(item_id, str) for item_id in garment_ids)
        or len(set(garment_ids)) != len(garment_ids)
    ):
        raise ValueError("invalid_ai_reference_manifest")
    truth = _authoritative_garments().get(garment_id)
    matches = [
        item
        for item in raw_items
        if isinstance(item, dict) and item.get("garment_id") == garment_id
    ]
    if (
        truth is None
        or truth.data_version != "fixtures_v1.0"
        or truth.source_id != "fixtures"
        or len(matches) != 1
    ):
        raise ValueError("invalid_ai_reference_identity")
    item = matches[0]
    ready = validate_generated_v1_reference(
        root_dir=ROOT.resolve(),
        asset_root=AI_REFERENCE_ASSET_ROOT.resolve(),
        item=item,
        garment_id=garment_id,
        user_id=truth.user_id,
        data_version="fixtures_v1.0",
    )
    imported_at = item.get("provenance_normalized_at")
    if ready is None or not isinstance(imported_at, str):
        raise ValueError("invalid_ai_reference_provenance")
    _parse_imported_at(imported_at)
    return item


def _controlled_garments(garment_file: Path) -> tuple[Garment, ...]:
    canonical_rows = _read_jsonl(CONTROLLED_GARMENT_FILE, limit=MANIFEST_INPUT_LIMIT)
    canonical: dict[str, Garment] = {}
    for row in canonical_rows:
        garment = Garment.model_validate(row)
        canonical[garment.garment_id] = garment

    requested_rows = _read_jsonl(Path(garment_file), limit=MANIFEST_INPUT_LIMIT)
    if not requested_rows or len(requested_rows) > MAX_TOTAL_BUDGET:
        raise ValueError("invalid_controlled_garment_count")
    requested: list[Garment] = []
    seen: set[str] = set()
    for row in requested_rows:
        supplied = Garment.model_validate(row)
        truth = canonical.get(supplied.garment_id)
        if truth is None or supplied.garment_id in seen:
            raise ValueError("unknown_or_duplicate_garment")
        if (
            supplied.user_id != truth.user_id
            or supplied.slot != truth.slot
            or supplied.audience != truth.audience
        ):
            raise ValueError("garment_authority_mismatch")
        if (
            supplied.data_version != "fixtures_s16_womenswear_v1"
            or supplied.source_id != "fixtures_s16"
            or supplied.synthetic is not True
        ):
            raise ValueError("garment_fixture_mismatch")
        seen.add(supplied.garment_id)
        requested.append(truth)
    return tuple(sorted(requested, key=lambda item: item.garment_id))


def _validate_https_url(value: Any, *, reason: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(reason)
    if len(value) > 2048 or "#" in value:
        raise ValueError(reason)
    try:
        parsed = httpx.URL(value)
    except Exception:
        raise ValueError(reason) from None
    if (
        parsed.scheme != "https"
        or parsed.host is None
        or bool(parsed.username)
        or bool(parsed.password)
        or (parsed.port is not None and not 0 < parsed.port <= 65535)
    ):
        raise ValueError(reason)
    return str(parsed)


def _bounded_text(value: Any, *, reason: str, maximum: int = 300) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(reason)
    return value


def _normalize_openverse_candidate(
    raw: Any,
    *,
    allowlist: tuple[LicenseCode, ...],
) -> _NormalizedSource:
    if not isinstance(raw, dict):
        raise ValueError("source_metadata_invalid")
    required = {
        "id",
        "license",
        "license_version",
        "license_url",
        "creator",
        "foreign_landing_url",
        "url",
    }
    if not required.issubset(raw):
        raise ValueError("source_metadata_incomplete")
    provider_item_id = _canonical_openverse_uuid(raw["id"])
    provider_license = _bounded_text(
        raw["license"], reason="source_metadata_invalid", maximum=32
    ).lower()
    provider_version = _bounded_text(
        raw["license_version"], reason="source_metadata_invalid", maximum=16
    )
    canonical = _CANONICAL_OPENVERSE_LICENSES.get(
        (provider_license, provider_version)
    )
    if canonical is None or canonical[0] not in allowlist:
        raise ValueError("license_rejected")
    license_code, canonical_url = canonical
    license_url = _validate_https_url(raw["license_url"], reason="license_invalid")
    if license_url != canonical_url:
        raise ValueError("license_invalid")
    creator = _bounded_text(raw["creator"], reason="creator_missing", maximum=200)
    source_url = _validate_https_url(
        raw["foreign_landing_url"], reason="source_url_invalid"
    )
    # The provider's raw image URL is required metadata, but it is not a trusted
    # byte endpoint and must never become fetch provenance or receipt identity.
    _validate_https_url(raw["url"], reason="image_url_invalid")
    image_url = _official_thumbnail_url(provider_item_id)
    attribution_label = {
        "CC0": "CC0 1.0",
        "PDM": "Public Domain Mark 1.0",
        "CC-BY-2.0": "CC BY 2.0",
        "CC-BY-3.0": "CC BY 3.0",
        "CC-BY-4.0": "CC BY 4.0",
    }[license_code]
    source = LicensedSourceReceipt(
        provider="openverse",
        source_url=source_url,
        creator=creator,
        license_code=license_code,
        license_url=license_url,
        attribution=f"{creator} / {attribution_label}",
        imported_at=datetime.now(timezone.utc),
    )
    return _NormalizedSource(
        provider_item_id=provider_item_id,
        source_receipt=source,
        image_url=image_url,
        provider_license=provider_license,
        provider_license_version=provider_version,
    )


def _canonical_openverse_uuid(
    value: Any,
    *,
    reason: str = "source_metadata_invalid",
) -> str:
    provider_item_id = _bounded_text(value, reason=reason, maximum=36)
    try:
        parsed = uuid.UUID(provider_item_id)
    except (AttributeError, ValueError):
        raise ValueError(reason) from None
    if str(parsed) != provider_item_id:
        raise ValueError(reason)
    return provider_item_id


def _validate_official_client(api_client: httpx.AsyncClient) -> None:
    base = api_client.base_url
    if (
        base.scheme != "https"
        or base.host != "api.openverse.org"
        or bool(base.username)
        or bool(base.password)
        or (base.port not in {None, 443})
        or base.path != "/v1/images/"
    ):
        raise ValueError("unapproved_provider_endpoint")


def _validate_official_response(response: httpx.Response) -> None:
    request_url = response.request.url
    if (
        request_url.scheme != "https"
        or request_url.host != "api.openverse.org"
        or bool(request_url.username)
        or bool(request_url.password)
        or request_url.port not in {None, 443}
        or request_url.path != "/v1/images/"
    ):
        raise ValueError("unapproved_provider_endpoint")


def _official_thumbnail_path(provider_item_id: str) -> str:
    canonical_id = _canonical_openverse_uuid(provider_item_id)
    return f"{canonical_id}/thumb/"


def _official_thumbnail_url(
    provider_item_id: str,
    *,
    reason: str = "source_metadata_invalid",
) -> str:
    canonical_id = _canonical_openverse_uuid(provider_item_id, reason=reason)
    return f"{OPENVERSE_API_BASE}{canonical_id}/thumb/"


def _validate_official_thumbnail_response(
    response: httpx.Response,
    *,
    provider_item_id: str,
) -> None:
    expected = httpx.URL(f"{OPENVERSE_API_BASE}{_official_thumbnail_path(provider_item_id)}")
    if response.request.method != "GET" or response.request.url != expected:
        raise ImageFetchError("invalid_url")


def _thumbnail_byte_validator(
    safe_fetcher: Any,
    *,
    config: IngestionConfig,
) -> SafeImageFetcher:
    if isinstance(safe_fetcher, SafeImageFetcher):
        return safe_fetcher
    return SafeImageFetcher(
        ready_dir=config.asset_directory,
        quarantine_dir=config.asset_directory.parent / "quarantine",
        config=SafeImageConfig(),
    )


@asynccontextmanager
async def _send_stateless_official_request(
    api_client: httpx.AsyncClient,
    request: httpx.Request,
) -> Any:
    request.headers.pop("cookie", None)
    response = await api_client.send(
        request,
        stream=True,
        follow_redirects=False,
    )
    try:
        yield response
    finally:
        try:
            await response.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


async def _fetch_trusted_openverse_thumbnail(
    *,
    api_client: httpx.AsyncClient,
    provider_item_id: str,
    safe_fetcher: Any,
    config: IngestionConfig,
) -> FetchedImage:
    validator = _thumbnail_byte_validator(safe_fetcher, config=config)
    reason: str | None = None
    try:
        return await asyncio.wait_for(
            _fetch_trusted_openverse_thumbnail_inner(
                api_client=api_client,
                provider_item_id=provider_item_id,
                validator=validator,
            ),
            timeout=OPENVERSE_THUMBNAIL_TOTAL_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except ImageFetchError as exc:
        reason = exc.reason_code
    except (asyncio.TimeoutError, httpx.TimeoutException):
        reason = "timeout"
    except Exception:
        reason = "network_error"
    assert reason is not None
    raise ImageFetchError(reason) from None


async def _fetch_trusted_openverse_thumbnail_inner(
    *,
    api_client: httpx.AsyncClient,
    provider_item_id: str,
    validator: SafeImageFetcher,
) -> FetchedImage:
    path = _official_thumbnail_path(provider_item_id)
    request = api_client.build_request(
        "GET",
        path,
        headers={
            "Accept": OPENVERSE_THUMBNAIL_ACCEPT,
            "Accept-Encoding": "identity",
        },
    )
    async with _send_stateless_official_request(api_client, request) as response:
        _validate_official_thumbnail_response(
            response,
            provider_item_id=provider_item_id,
        )
        if response.status_code != 200:
            raise ImageFetchError("http_status")
        encodings = response.headers.get_list("content-encoding")
        if len(encodings) > 1 or (
            len(encodings) == 1 and encodings[0].strip().lower() != "identity"
        ):
            raise ImageFetchError("unsupported_content_encoding")
        content_types = response.headers.get_list("content-type")
        if len(content_types) != 1:
            raise ImageFetchError("unsupported_mime")
        source_mime = content_types[0].split(";", 1)[0].strip().lower()
        if source_mime not in {"image/png", "image/jpeg", "image/webp"}:
            raise ImageFetchError("unsupported_mime")
        lengths = response.headers.get_list("content-length")
        if len(lengths) > 1:
            raise ImageFetchError("response_too_large")
        if lengths:
            try:
                content_length = int(lengths[0])
            except ValueError:
                raise ImageFetchError("response_too_large") from None
            if content_length < 0 or content_length > validator.max_response_bytes:
                raise ImageFetchError("response_too_large")
        if response.is_stream_consumed:
            payload = response.content
            if len(payload) > validator.max_response_bytes:
                raise ImageFetchError("response_too_large")
        else:
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_raw():
                total += len(chunk)
                if total > validator.max_response_bytes:
                    raise ImageFetchError("response_too_large")
                chunks.append(chunk)
            payload = b"".join(chunks)
    return await asyncio.to_thread(
        validator.validate_local_bytes,
        payload,
        source_mime,
    )


def _parse_openverse_response(payload_bytes: bytes) -> tuple[Any, ...]:
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("provider_response_invalid") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("provider_response_invalid")
    result_count = payload.get("result_count")
    if (
        isinstance(result_count, bool)
        or not isinstance(result_count, int)
        or result_count < 0
    ):
        raise ValueError("provider_response_invalid")
    return tuple(payload["results"])


_QUERY_PRODUCTS: dict[str, str] = {
    "top": "blouse",
    "bottom": "trousers",
    "dress": "dress",
    "outer": "jacket",
    "shoes": "shoes",
    "bag": "handbag",
    "accessory": "accessory",
}
_QUERY_COLORS = frozenset(
    {
        "beige",
        "black",
        "blue",
        "brown",
        "gray",
        "green",
        "khaki",
        "navy",
        "orange",
        "pink",
        "purple",
        "red",
        "white",
        "yellow",
    }
)


def _frozen_openverse_query_plan(garment: Garment) -> tuple[str, ...]:
    try:
        product = _QUERY_PRODUCTS[garment.slot]
    except KeyError:
        raise ValueError("unsupported_query_authority") from None
    if garment.color not in _QUERY_COLORS:
        raise ValueError("unsupported_query_authority")
    return (
        product,
        f"{garment.color} {product}",
        f"women's {product} flat lay",
    )


async def _search_openverse(
    *,
    api_client: httpx.AsyncClient,
    garment: Garment,
    config: IngestionConfig,
    counters: _Counters,
) -> _SearchOutcome:
    _suppress_openverse_transport_url_logging()
    queries = _frozen_openverse_query_plan(garment)
    provider_licenses = (
        {
            "CC0": "cc0",
            "PDM": "pdm",
            "CC-BY-2.0": "by",
            "CC-BY-3.0": "by",
            "CC-BY-4.0": "by",
        }[code]
        for code in config.license_allowlist
    )
    license_param = ",".join(dict.fromkeys(provider_licenses))
    searches_used = 0
    last_reason: _SearchReason = "no_results"
    for query in queries:
        retries_used = 0
        while searches_used < config.search_budget:
            searches_used += 1
            counters.api_attempts += 1
            reason: _SearchReason | None = None
            payload_bytes = b""
            try:
                request = api_client.build_request(
                    "GET",
                    "",
                    params={
                        "q": query,
                        "license": license_param,
                        "page_size": config.candidate_budget,
                    },
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                )
                async with _send_stateless_official_request(
                    api_client,
                    request,
                ) as response:
                    _validate_official_response(response)
                    if response.status_code == 429:
                        reason = "provider_rate_limited"
                    elif response.status_code in {401, 403}:
                        reason = "provider_auth_rejected"
                    elif response.status_code != 200:
                        reason = "provider_unavailable"
                    else:
                        content_type = response.headers.get("content-type", "")
                        if (
                            content_type.split(";", 1)[0].strip().lower()
                            != "application/json"
                        ):
                            reason = "provider_schema_invalid"
                        else:
                            chunks: list[bytes] = []
                            total = 0
                            async for chunk in response.aiter_bytes():
                                total += len(chunk)
                                if total > OPENVERSE_RESPONSE_LIMIT:
                                    reason = "provider_response_too_large"
                                    break
                                chunks.append(chunk)
                            if reason is None:
                                payload_bytes = b"".join(chunks)
            except httpx.HTTPError:
                reason = "provider_unavailable"

            if reason is None:
                try:
                    candidates = _parse_openverse_response(payload_bytes)
                except ValueError:
                    return _SearchOutcome((), "provider_schema_invalid")
                if candidates:
                    return _SearchOutcome(candidates, None)
                last_reason = "no_results"
                break

            last_reason = reason
            if (
                reason in {"provider_rate_limited", "provider_unavailable"}
                and retries_used < config.retry_budget
                and searches_used < config.search_budget
            ):
                retries_used += 1
                await asyncio.sleep(0)
                continue
            return _SearchOutcome((), reason)

        if searches_used >= config.search_budget:
            break
    return _SearchOutcome((), last_reason)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HASH_CHARS for char in value)
    )


def _parse_imported_at(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid_receipt_timestamp")
    try:
        imported_at = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid_receipt_timestamp") from None
    if imported_at.tzinfo is None:
        raise ValueError("invalid_receipt_timestamp")
    return imported_at


_CPA_PRODUCT_NOUN_BY_GARMENT_ID = CATALOG_PRODUCT_TYPE_BY_GARMENT_ID
_CPA_COLORS = frozenset(
    {"beige", "black", "blue", "brown", "gray", "green", "khaki", "navy",
     "orange", "pink", "purple", "red", "white", "yellow"}
)
_CPA_MATERIALS = frozenset(
    {"cotton", "denim", "knit", "leather", "linen", "synthetic", "wool"}
)
_CPA_STYLES = frozenset(
    {"business", "classic", "formal", "simple", "smart", "soft", "sporty",
     "street", "vintage"}
)
_CPA_OCCASIONS = frozenset(
    {"commute", "daily", "date", "home", "interview", "meeting", "outdoor",
     "party", "sports", "travel"}
)


def _cpa_generated_prompt(garment: Garment) -> str:
    """Build a deterministic prompt from server-owned closed vocabulary only."""

    try:
        noun = product_type_for_garment(garment.garment_id)
    except ValueError:
        raise ValueError("invalid_cpa_generated_product_noun") from None
    styles = tuple(garment.styles)
    occasions = tuple(garment.occasions)
    if (
        garment.color not in _CPA_COLORS
        or garment.material not in _CPA_MATERIALS
        or not styles
        or any(value not in _CPA_STYLES for value in styles)
        or not occasions
        or any(value not in _CPA_OCCASIONS for value in occasions)
        or len(styles) > 4
        or len(occasions) > 4
    ):
        raise ValueError("invalid_cpa_generated_prompt_inputs")
    prompt = (
        f"Create a clean catalog photograph of a single women's {noun}; "
        f"color {garment.color}; material {garment.material}; "
        f"style {', '.join(styles)}; occasion {', '.join(occasions)}. "
        "Show one isolated item as a flat lay on a neutral background. "
        "No person, model, mannequin, logo, watermark, writing, or extra item."
    )
    if len(prompt) > 700:
        raise ValueError("invalid_cpa_generated_prompt_inputs")
    return prompt


def _validated_cpa_model_receipt(
    raw: Any,
    *,
    allowlist: tuple[str, ...],
    expected_requested_model: str | None = None,
    expected_transport_model: str | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict) or not allowlist or len(set(allowlist)) != len(allowlist):
        raise ValueError("invalid_cpa_model_receipt")
    requested = raw.get("requested_model")
    transport = raw.get("transport_model")
    if (
        not isinstance(requested, str)
        or not isinstance(transport, str)
        or requested not in allowlist
        or transport not in allowlist
        or (expected_requested_model is not None and requested != expected_requested_model)
        or (expected_transport_model is not None and transport != expected_transport_model)
        or raw.get("request_model_pinned") is not True
        or raw.get("cpa_trace_verified") is not True
        or not isinstance(raw.get("model_reported"), bool)
        or not isinstance(raw.get("model_verified"), bool)
    ):
        raise ValueError("invalid_cpa_model_receipt")
    if raw["model_reported"]:
        if (
            raw.get("resolved_model") != transport
            or raw.get("model_verified") is not True
            or raw.get("verification_basis") != "reported_model_exact"
        ):
            raise ValueError("invalid_cpa_model_receipt")
    elif (
        raw.get("resolved_model") is not None
        or raw.get("model_verified") is not False
        or raw.get("verification_basis") != "exact_request_with_cpa_trace"
    ):
        raise ValueError("invalid_cpa_model_receipt")
    return {key: raw.get(key) for key in _CPA_GENERATED_RECEIPT_FIELDS if key not in {
        "bound_garment_id", "bound_user_id", "prompt_template_version", "prompt_sha256"
    }}


def _validated_receipt(
    raw: Any,
    *,
    garment: Garment,
    image_model_allowlist: tuple[str, ...] = (),
) -> tuple[_NormalizedSource, str, str]:
    if not isinstance(raw, dict) or frozenset(raw) not in {
        _RECEIPT_KEYS,
        _CPA_GENERATED_RECEIPT_KEYS,
    }:
        raise ValueError("invalid_source_receipt")
    garment_id = garment.garment_id
    processing = raw.get("processing")
    original_sha256 = raw.get("original_sha256")
    processed_sha256 = raw.get("processed_sha256")
    if not _is_sha256(original_sha256) or not _is_sha256(processed_sha256):
        raise ValueError("invalid_source_receipt")
    provider_item_id = _bounded_text(
        raw.get("provider_item_id"), reason="invalid_source_receipt"
    )
    provider_license = _bounded_text(
        raw.get("provider_license"),
        reason="invalid_source_receipt",
        maximum=32,
    )
    provider_version = _bounded_text(
        raw.get("provider_license_version"),
        reason="invalid_source_receipt",
        maximum=40,
    )
    provider = raw.get("provider")
    image_url = raw.get("image_url")
    source_ref = raw.get("source_ref")
    expected_sha256 = raw.get("expected_sha256")
    if provider == "openverse":
        if processing != "safe_decode_normalize_then_server_crop":
            raise ValueError("invalid_source_receipt")
        provider_item_id = _canonical_openverse_uuid(
            provider_item_id, reason="invalid_source_receipt"
        )
        _bounded_text(raw.get("creator"), reason="invalid_source_receipt", maximum=200)
        _validate_https_url(raw.get("source_url"), reason="invalid_source_receipt")
        canonical = _CANONICAL_OPENVERSE_LICENSES.get(
            (provider_license, provider_version)
        )
        if (
            canonical is None
            or raw.get("license_code") != canonical[0]
            or raw.get("license_url") != canonical[1]
            or source_ref is not None
            or expected_sha256 is not None
        ):
            raise ValueError("invalid_source_receipt")
        canonical_image_url = _official_thumbnail_url(
            provider_item_id, reason="invalid_source_receipt"
        )
        if image_url != canonical_image_url:
            raise ValueError("invalid_source_receipt")
        image_url = canonical_image_url
    elif provider == "user_upload":
        if (
            processing != "safe_decode_normalize_then_server_crop"
            or raw.get("source_url") is not None
            or raw.get("creator") is not None
            or raw.get("license_url") is not None
            or provider_item_id != garment_id
            or provider_license != "user-owned"
            or provider_version != "owner-asserted-v1"
            or image_url is not None
            or not isinstance(source_ref, str)
            or not source_ref
            or "\\" in source_ref
            or ":" in source_ref
            or not _is_sha256(expected_sha256)
            or expected_sha256 != original_sha256
        ):
            raise ValueError("invalid_source_receipt")
        pure = PurePosixPath(source_ref)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise ValueError("invalid_source_receipt")
    elif provider == "cpa_generated" and set(raw) == _RECEIPT_KEYS:
        reference = _verified_ai_reference(garment_id)
        reference_sha256 = reference["sha256"]
        expected_source_ref = (
            f"data/manifests/wardrobe_generated_v1.json#{garment_id}"
        )
        if (
            processing != "verified_s12r_ai_reference_passthrough"
            or provider_item_id != garment_id
            or provider_license != "ai-generated"
            or provider_version != WARDROBE_GENERATED_ASSET_VERSION
            or raw.get("license_code") != "ai-generated"
            or raw.get("license_url") is not None
            or raw.get("creator") is not None
            or raw.get("source_url") is not None
            or image_url is not None
            or source_ref != expected_source_ref
            or expected_sha256 != reference_sha256
            or original_sha256 != reference_sha256
            or processed_sha256 != reference_sha256
            or raw.get("attribution") != "AI 生成参考"
            or raw.get("imported_at") != reference.get("provenance_normalized_at")
        ):
            raise ValueError("invalid_ai_reference_receipt")
    elif provider == "cpa_generated":
        prompt = _cpa_generated_prompt(garment)
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        _validated_cpa_model_receipt(
            raw, allowlist=CPA_GENERATED_HISTORICAL_MODEL_ALLOWLIST
        )
        if (
            processing != "safe_decode_normalize_then_server_crop"
            or provider_item_id != prompt_sha256
            or provider_license != "ai-generated"
            or provider_version != CPA_GENERATED_PROMPT_VERSION
            or raw.get("license_code") != "ai-generated"
            or raw.get("license_url") is not None
            or raw.get("creator") is not None
            or raw.get("source_url") is not None
            or image_url is not None
            or source_ref is not None
            or expected_sha256 != original_sha256
            or raw.get("attribution") != "AI 生成参考"
            or raw.get("bound_garment_id") != garment.garment_id
            or raw.get("bound_user_id") != garment.user_id
            or raw.get("prompt_template_version") != CPA_GENERATED_PROMPT_VERSION
            or raw.get("prompt_sha256") != prompt_sha256
        ):
            raise ValueError("invalid_cpa_generated_receipt")
    else:
        raise ValueError("unsupported_manifest_provider")
    source_receipt = LicensedSourceReceipt(
        provider=provider,
        source_url=raw.get("source_url"),
        creator=raw.get("creator"),
        license_code=raw.get("license_code"),
        license_url=raw.get("license_url"),
        attribution=raw.get("attribution"),
        imported_at=_parse_imported_at(raw.get("imported_at")),
    )
    return (
        _NormalizedSource(
            provider_item_id=provider_item_id,
            source_receipt=source_receipt,
            image_url=image_url,
            provider_license=provider_license,
            provider_license_version=provider_version,
            source_ref=source_ref,
            expected_sha256=expected_sha256,
        ),
        original_sha256,
        processed_sha256,
    )


def _validate_source_record(
    raw: Any,
    *,
    authoritative: dict[str, Garment],
    image_model_allowlist: tuple[str, ...] = (),
) -> tuple[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"garment_id", "receipt"}:
        raise ValueError("invalid_source_history")
    garment_id = raw.get("garment_id")
    if not isinstance(garment_id, str) or garment_id not in authoritative:
        raise ValueError("invalid_source_history")
    _validated_receipt(
        raw.get("receipt"),
        garment=authoritative[garment_id],
        image_model_allowlist=image_model_allowlist,
    )
    fingerprint = json.dumps(
        raw["receipt"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return garment_id, fingerprint


def _load_source_history(
    path: Path,
    *,
    authoritative: dict[str, Garment],
    image_model_allowlist: tuple[str, ...] = (),
) -> tuple[bytes, frozenset[tuple[str, str]]]:
    if not path.exists():
        return b"", frozenset()
    try:
        if path.stat().st_size > SOURCE_HISTORY_LIMIT:
            raise ValueError("source_history_too_large")
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError("invalid_source_history") from exc
    if payload and not payload.endswith(b"\n"):
        raise ValueError("invalid_source_history")
    try:
        rows = [json.loads(line) for line in payload.splitlines() if line]
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("invalid_source_history") from None
    fingerprints = {
        _validate_source_record(
            row,
            authoritative=authoritative,
            image_model_allowlist=image_model_allowlist,
        )
        for row in rows
    }
    if len(fingerprints) != len(rows):
        raise ValueError("duplicate_source_receipt")
    return payload, frozenset(fingerprints)


def _validate_manifest_item(
    raw: Any,
    *,
    authoritative: dict[str, Garment],
    asset_directory: Path,
    durable_receipts: frozenset[tuple[str, str]],
    staged_assets: dict[str, bytes] | None = None,
    image_model_allowlist: tuple[str, ...] = (),
) -> None:
    if not isinstance(raw, dict):
        raise ValueError("invalid_asset_manifest")
    keys = set(raw)
    if keys not in {_MANIFEST_ITEM_KEYS, _MANIFEST_ITEM_KEYS | {"failure_reason"}}:
        raise ValueError("invalid_asset_manifest")
    garment_id = raw.get("garment_id")
    truth = authoritative.get(garment_id) if isinstance(garment_id, str) else None
    if truth is None or any(
        raw.get(key) != value
        for key, value in {
            "user_id": truth.user_id,
            "slot": truth.slot,
            "audience": truth.audience,
        }.items()
    ):
        raise ValueError("manifest_authority_mismatch")
    source_kind = raw.get("source_kind")
    if source_kind not in {
        "licensed_photo",
        "user_owned_photo",
        "ai_generated_reference",
    }:
        raise ValueError("invalid_manifest_source_kind")
    if truth.data_version == "fixtures_v1.0" and source_kind != "ai_generated_reference":
        raise ValueError("manifest_source_kind_mismatch")
    status = raw.get("status")
    if status not in {"ready", "missing", "failed", "quarantined", "takedown"}:
        raise ValueError("invalid_manifest_status")
    failure_reason = raw.get("failure_reason")
    if failure_reason is not None and failure_reason not in _CONTROLLED_FAILURE_REASONS:
        raise ValueError("invalid_manifest_failure_reason")
    if status == "ready" and failure_reason is not None:
        raise ValueError("ready_asset_has_failure_reason")
    if status == "takedown" and failure_reason != "source_takedown":
        raise ValueError("invalid_takedown_reason")
    if status in {"missing", "failed", "quarantined"} and failure_reason is None:
        raise ValueError("missing_manifest_failure_reason")
    history = raw.get("receipt_history")
    if not isinstance(history, list) or len(history) > 100:
        raise ValueError("invalid_receipt_history")
    validated_history = [
        _validated_receipt(
            receipt,
            garment=truth,
            image_model_allowlist=image_model_allowlist,
        )
        for receipt in history
    ]
    identities = [entry[0].identity() for entry in validated_history]
    if len({json.dumps(item, sort_keys=True) for item in identities}) != len(identities):
        raise ValueError("duplicate_receipt_history")
    for receipt in history:
        receipt_fingerprint = json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if (garment_id, receipt_fingerprint) not in durable_receipts:
            raise ValueError("manifest_receipt_not_durable")
    if status in {"ready", "takedown"} and not validated_history:
        raise ValueError("missing_authoritative_receipt")
    latest = validated_history[-1] if validated_history else None
    expected_providers = {
        "licensed_photo": {"openverse", "partner_api"},
        "user_owned_photo": {"user_upload"},
        "ai_generated_reference": {"cpa_generated"},
    }
    if latest is not None and latest[0].source_receipt.provider not in expected_providers[source_kind]:
        raise ValueError("manifest_source_kind_mismatch")
    license_payload = raw.get("license")
    public_license = (
        PublicLicenseReceipt.model_validate(license_payload)
        if license_payload is not None
        else None
    )
    if status in {"ready", "takedown"} and public_license is None:
        raise ValueError("missing_manifest_license")
    if latest is not None and public_license is not None:
        expected_license = public_license_from_source(latest[0].source_receipt)
        if public_license != expected_license:
            raise ValueError("manifest_license_mismatch")
    original_sha256 = raw.get("original_sha256")
    processed_sha256 = raw.get("processed_sha256")
    relative_path = raw.get("relative_path")
    if status == "ready":
        if (
            not _is_sha256(original_sha256)
            or not _is_sha256(processed_sha256)
            or latest is None
            or latest[1] != original_sha256
            or latest[2] != processed_sha256
        ):
            raise ValueError("invalid_ready_hash_binding")
        if source_kind == "ai_generated_reference" and truth.data_version == "fixtures_v1.0":
            reference = _verified_ai_reference(garment_id)
            if (
                relative_path != reference.get("relative_path")
                or original_sha256 != reference.get("sha256")
                or processed_sha256 != reference.get("sha256")
            ):
                raise ValueError("invalid_ai_reference_binding")
        else:
            path = _safe_asset_path(asset_directory, relative_path)
            if path is None or relative_path != f"{garment_id}/{processed_sha256}.png":
                raise ValueError("unsafe_ready_path")
            staged = None if staged_assets is None else staged_assets.get(relative_path)
            if staged is not None:
                payload = staged
            else:
                if not path.is_file() or path.is_symlink():
                    raise ValueError("missing_ready_asset")
                try:
                    if path.stat().st_size > 25 * 1024 * 1024:
                        raise ValueError("ready_asset_too_large")
                    payload = path.read_bytes()
                except OSError as exc:
                    raise ValueError("missing_ready_asset") from exc
            if hashlib.sha256(payload).hexdigest() != processed_sha256:
                raise ValueError("ready_asset_hash_mismatch")
    else:
        if processed_sha256 is not None or relative_path is not None:
            raise ValueError("nonready_asset_is_serveable")
        if original_sha256 is not None and not _is_sha256(original_sha256):
            raise ValueError("invalid_nonready_hash")
        if status == "takedown":
            if latest is None or latest[1] != original_sha256:
                raise ValueError("invalid_takedown_hash_binding")
        elif original_sha256 is not None or public_license is not None:
            raise ValueError("invalid_nonready_authority")


def _validate_manifest_payload(
    payload: Any,
    *,
    authoritative: dict[str, Garment],
    asset_directory: Path,
    durable_receipts: frozenset[tuple[str, str]],
    staged_assets: dict[str, bytes] | None = None,
    image_model_allowlist: tuple[str, ...] = (),
) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"manifest_version", "items"}
        or payload.get("manifest_version") != "wardrobe_assets_v2"
        or not isinstance(payload.get("items"), list)
        or len(payload["items"]) > 120
    ):
        raise ValueError("invalid_asset_manifest")
    garment_ids: list[str] = []
    for item in payload["items"]:
        _validate_manifest_item(
            item,
            authoritative=authoritative,
            asset_directory=asset_directory,
            durable_receipts=durable_receipts,
            staged_assets=staged_assets,
            image_model_allowlist=image_model_allowlist,
        )
        garment_ids.append(item["garment_id"])
    if len(set(garment_ids)) != len(garment_ids):
        raise ValueError("duplicate_manifest_garment")
    return payload


def _load_existing_manifest(
    path: Path,
    *,
    authoritative: dict[str, Garment],
    asset_directory: Path,
    durable_receipts: frozenset[tuple[str, str]],
    image_model_allowlist: tuple[str, ...] = (),
) -> dict[str, Any]:
    if not path.exists():
        return {"manifest_version": "wardrobe_assets_v2", "items": []}
    return _validate_manifest_payload(
        _load_json(path, limit=MANIFEST_INPUT_LIMIT),
        authoritative=authoritative,
        asset_directory=asset_directory,
        durable_receipts=durable_receipts,
        image_model_allowlist=image_model_allowlist,
    )


def _receipt_identity(receipt: Any) -> dict[str, Any] | None:
    if not isinstance(receipt, dict):
        return None
    keys = {
        "provider",
        "provider_item_id",
        "provider_license",
        "provider_license_version",
        "license_code",
        "license_url",
        "creator",
        "source_url",
        "image_url",
        "source_ref",
        "expected_sha256",
        "attribution",
    }
    identity = {key: receipt.get(key) for key in keys}
    if receipt.get("provider") == "cpa_generated" and "prompt_sha256" in receipt:
        # A generated source is the immutable server recipe, not the mutable
        # output bytes.  This identity is available before the Provider call,
        # so a prior takedown cannot be regenerated into ready state.
        return {
            "provider": receipt.get("provider"),
            "provider_license": receipt.get("provider_license"),
            "provider_license_version": receipt.get("provider_license_version"),
            "license_code": receipt.get("license_code"),
            "prompt_template_version": receipt.get("prompt_template_version"),
            "prompt_sha256": receipt.get("prompt_sha256"),
            "requested_model": receipt.get("requested_model"),
            "transport_model": receipt.get("transport_model"),
        }
    return identity


def _cpa_generated_source_identity(
    *, prompt_sha256: str, requested_model: str, transport_model: str
) -> dict[str, Any]:
    return {
        "provider": "cpa_generated",
        "provider_license": "ai-generated",
        "provider_license_version": CPA_GENERATED_PROMPT_VERSION,
        "license_code": "ai-generated",
        "prompt_template_version": CPA_GENERATED_PROMPT_VERSION,
        "prompt_sha256": prompt_sha256,
        "requested_model": requested_model,
        "transport_model": transport_model,
    }


def _safe_asset_path(asset_directory: Path, relative_path: Any) -> Path | None:
    if not isinstance(relative_path, str) or not relative_path:
        return None
    if "\\" in relative_path or ":" in relative_path:
        return None
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    root = asset_directory.resolve()
    candidate = (root / Path(*pure.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _reusable_item(
    *,
    existing_item: dict[str, Any] | None,
    garment: Garment,
    source: _NormalizedSource,
    asset_directory: Path,
) -> bool:
    if not isinstance(existing_item, dict) or existing_item.get("status") != "ready":
        return False
    if any(
        existing_item.get(key) != value
        for key, value in {
            "garment_id": garment.garment_id,
            "user_id": garment.user_id,
            "slot": garment.slot,
            "audience": garment.audience,
        }.items()
    ):
        return False
    history = existing_item.get("receipt_history")
    if not isinstance(history, list) or not history:
        return False
    if _receipt_identity(history[-1]) != source.identity():
        return False
    digest = existing_item.get("processed_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        return False
    path = _safe_asset_path(asset_directory, existing_item.get("relative_path"))
    if path is None or not path.is_file() or path.is_symlink():
        return False
    try:
        if path.stat().st_size > 25 * 1024 * 1024:
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == digest
    except OSError:
        return False


def _is_same_source_takedown(
    existing_item: dict[str, Any] | None,
    source: _NormalizedSource,
) -> bool:
    if not isinstance(existing_item, dict) or existing_item.get("status") != "takedown":
        return False
    history = existing_item.get("receipt_history")
    return (
        isinstance(history, list)
        and bool(history)
        and _receipt_identity(history[-1]) == source.identity()
    )


def _record_vision_model_provenance(
    counters: _Counters,
    source: Any,
) -> None:
    value = (
        source.get("resolved_model")
        if isinstance(source, dict)
        else getattr(source, "model_provenance", None)
    )
    if (
        isinstance(value, str)
        and value
        and len(value) <= 100
        and all(char.isalnum() or char in "-._:/" for char in value)
    ):
        counters.vision_model_provenance = value


def _reusable_cpa_generated_item(
    *,
    existing_item: dict[str, Any] | None,
    garment: Garment,
    prompt_sha256: str,
    asset_directory: Path,
) -> bool:
    if not isinstance(existing_item, dict) or existing_item.get("status") != "ready":
        return False
    history = existing_item.get("receipt_history")
    if not isinstance(history, list) or not history:
        return False
    latest = history[-1]
    if (
        not isinstance(latest, dict)
        or latest.get("provider") != "cpa_generated"
        or latest.get("bound_garment_id") != garment.garment_id
        or latest.get("bound_user_id") != garment.user_id
        or latest.get("prompt_sha256") != prompt_sha256
        or latest.get("prompt_template_version") != CPA_GENERATED_PROMPT_VERSION
    ):
        return False
    digest = existing_item.get("processed_sha256")
    path = _safe_asset_path(asset_directory, existing_item.get("relative_path"))
    if not _is_sha256(digest) or path is None or not path.is_file() or path.is_symlink():
        return False
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == digest
    except OSError:
        return False


class _PrivateQuarantineDiagnostic(BaseModel):
    """Internal-only binding for one locally valid, Vision-rejected image."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] | None = None
    transaction_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    garment_id: str = Field(pattern=r"^g\d{3}$")
    user_id: str = Field(pattern=r"^u\d{2}$")
    slot: str = Field(min_length=1, max_length=32)
    product_type: str = Field(min_length=1, max_length=100)
    prompt_sha256: str = Field(min_length=64, max_length=64)
    original_sha256: str = Field(min_length=64, max_length=64)
    processed_sha256: str = Field(min_length=64, max_length=64)
    quarantine_sha256: str = Field(min_length=64, max_length=64)
    quarantine_relative_path: str = Field(min_length=68, max_length=68)
    failure_stage: Literal["vision"]
    reason_code: VisionFailureReason
    schema_stage: SchemaStage | None = None
    image_model_provenance: MinimalModelProvenance
    vision_model_provenance: MinimalModelProvenance

    @model_validator(mode="after")
    def _validate_binding(self) -> "_PrivateQuarantineDiagnostic":
        version_is_set = "schema_version" in self.model_fields_set
        stage_is_set = "schema_stage" in self.model_fields_set
        if self.schema_version is None:
            if version_is_set or stage_is_set:
                raise ValueError("invalid_private_quarantine_schema_version")
        elif not version_is_set or not stage_is_set:
            raise ValueError("invalid_private_quarantine_schema_version")
        elif self.reason_code == "response_schema_invalid":
            if self.schema_stage is None:
                raise ValueError("vision_schema_failure_requires_stage")
        elif self.schema_stage is not None:
            raise ValueError("schema_stage_requires_vision_schema_failure")
        digests = (
            self.prompt_sha256,
            self.original_sha256,
            self.processed_sha256,
            self.quarantine_sha256,
        )
        if any(not _is_sha256(value) for value in digests):
            raise ValueError("invalid_private_quarantine_hash")
        if (
            self.processed_sha256 != self.quarantine_sha256
            or self.quarantine_relative_path != f"{self.quarantine_sha256}.png"
        ):
            raise ValueError("invalid_private_quarantine_binding")
        return self


def _minimal_model_provenance(
    raw: Any,
    *,
    requested_model: str,
    resolved_allowlist: tuple[str, ...] | frozenset[str],
) -> MinimalModelProvenance:
    """Project a provider trace onto server-owned model policy.

    The provider may report routing evidence, but it cannot choose either the
    logical requested model or the set of accepted resolved models.  Any
    disagreement is represented as an unverified, content-free observation.
    """

    resolved: str | None = None
    verified = False
    if isinstance(raw, dict):
        claimed_requested = raw.get("requested_model")
        claimed_resolved = raw.get("resolved_model")
        claimed_verified = raw.get("model_verified")
        if (
            claimed_requested == requested_model
            and claimed_verified is True
            and isinstance(claimed_resolved, str)
            and claimed_resolved in resolved_allowlist
        ):
            resolved = claimed_resolved
            verified = True
    return MinimalModelProvenance(
        requested_model=requested_model,
        resolved_model=resolved,
        model_verified=verified,
    )


def _image_failure_reason(error: ProviderUnavailable | ValueError | TypeError) -> IngestionFailureReason:
    code = getattr(error, "reason_code", "")
    if isinstance(code, str) and code in _IMAGE_FAILURE_REASON_BY_CODE:
        return _IMAGE_FAILURE_REASON_BY_CODE[code]
    if isinstance(error, (ValueError, TypeError)):
        return "response_schema_invalid"
    return "provider_unavailable"


def _vision_failure_details(
    error: VisionUnavailable,
) -> tuple[VisionFailureReason, SchemaStage | None]:
    trace = error.provider_trace
    reason = trace.get("reason_code") if isinstance(trace, dict) else None
    if not isinstance(reason, str) or reason not in _VISION_FAILURE_REASONS:
        raise ValueError("invalid_vision_schema_stage") from None
    stage = trace.get("schema_stage") if isinstance(trace, dict) else None
    if reason == "response_schema_invalid":
        if not isinstance(stage, str) or stage not in _SCHEMA_STAGES:
            raise ValueError("invalid_vision_schema_stage") from None
        return reason, stage  # type: ignore[return-value]
    if stage is not None:
        raise ValueError("invalid_vision_schema_stage") from None
    return reason, None  # type: ignore[return-value]


async def _attempt_cpa_generated_garment(
    *,
    garment: Garment,
    config: IngestionConfig,
    safe_fetcher: Any,
    vision: CatalogVisionInspector,
    image_provider: Any,
    image_model_allowlist: tuple[str, ...],
    existing_item: dict[str, Any] | None,
    counters: _Counters,
) -> _AttemptOutcome:
    prompt = _cpa_generated_prompt(garment)
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    requested_model = getattr(image_provider, "requested_model", None)
    transport_model = getattr(image_provider, "transport_model", None)
    if not isinstance(requested_model, str) or not isinstance(transport_model, str):
        raise ValueError("cpa_image_provider_required")
    history = existing_item.get("receipt_history") if isinstance(existing_item, dict) else None
    if (
        isinstance(existing_item, dict)
        and existing_item.get("status") == "takedown"
        and isinstance(history, list)
        and bool(history)
        and _receipt_identity(history[-1]) == _cpa_generated_source_identity(
            prompt_sha256=prompt_sha256,
            requested_model=requested_model,
            transport_model=transport_model,
        )
    ):
        return _AttemptOutcome(
            garment=garment,
            status="quarantined",
            failure_reason="source_takedown",
            existing_item=existing_item,
            source_kind="ai_generated_reference",
        )
    if config.resume and _reusable_cpa_generated_item(
        existing_item=existing_item,
        garment=garment,
        prompt_sha256=prompt_sha256,
        asset_directory=config.asset_directory,
    ):
        return _AttemptOutcome(
            garment=garment,
            status="reused",
            existing_item=existing_item,
            source_kind="ai_generated_reference",
        )
    counters.candidate_attempts += 1
    try:
        raw, mime_type, provider_trace = await image_provider.generate_static_2d(prompt)
        model_receipt = _validated_cpa_model_receipt(
            provider_trace,
            allowlist=CPA_GENERATED_HISTORICAL_MODEL_ALLOWLIST,
            expected_requested_model=requested_model,
            expected_transport_model=transport_model,
        )
    except asyncio.CancelledError:
        raise
    except (ProviderUnavailable, ValueError, TypeError) as error:
        return _AttemptOutcome(
            garment=garment,
            status="quarantined",
            failure_reason=_image_failure_reason(error),
            failure_stage="image_provider",
            image_model_provenance=MinimalModelProvenance(
                requested_model=requested_model,
                resolved_model=None,
                model_verified=False,
            ),
            existing_item=existing_item,
            source_kind="ai_generated_reference",
        )

    image_model_provenance = _minimal_model_provenance(
        model_receipt,
        requested_model=requested_model,
        resolved_allowlist=CPA_GENERATED_HISTORICAL_MODEL_ALLOWLIST,
    )
    try:
        validator = getattr(safe_fetcher, "validate_local_bytes", None)
        if validator is None:
            validator = SafeImageFetcher(
                ready_dir=config.asset_directory,
                quarantine_dir=config.asset_directory.parent / "quarantine",
                config=SafeImageConfig(),
            ).validate_local_bytes
        fetched = validator(raw, mime_type)
    except asyncio.CancelledError:
        raise
    except ImageFetchError as error:
        return _AttemptOutcome(
            garment=garment,
            status="quarantined",
            failure_reason=error.reason_code,
            failure_stage="local_validation",
            image_model_provenance=image_model_provenance,
            existing_item=existing_item,
            source_kind="ai_generated_reference",
        )
    except (ValueError, TypeError):
        return _AttemptOutcome(
            garment=garment,
            status="quarantined",
            failure_reason="decode_failed",
            failure_stage="local_validation",
            image_model_provenance=image_model_provenance,
            existing_item=existing_item,
            source_kind="ai_generated_reference",
        )

    counters.vision_call_count += 1
    _record_vision_model_provenance(counters, vision)
    try:
        cropped, _, vision_trace = await assess_and_crop_catalog_asset(
            vision=vision,
            fetched_image=fetched,
            allowed_slot=garment.slot,
            expected_product_type=product_type_for_garment(garment.garment_id),
        )
        _record_vision_model_provenance(counters, vision_trace)
    except asyncio.CancelledError:
        raise
    except VisionUnavailable as error:
        reason, schema_stage = _vision_failure_details(error)
        vision_model_provenance = _minimal_model_provenance(
            error.provider_trace,
            requested_model=LOGICAL_GROK_MODEL,
            resolved_allowlist=CPA_REPORTED_MODELS,
        )
        private_bytes = fetched.processed_bytes
        private_record = _PrivateQuarantineDiagnostic(
            schema_version=2,
            transaction_id=uuid.uuid4().hex,
            garment_id=garment.garment_id,
            user_id=garment.user_id,
            slot=garment.slot,
            product_type=product_type_for_garment(garment.garment_id),
            prompt_sha256=prompt_sha256,
            original_sha256=fetched.original_sha256,
            processed_sha256=fetched.processed_sha256,
            quarantine_sha256=fetched.processed_sha256,
            quarantine_relative_path=f"{fetched.processed_sha256}.png",
            failure_stage="vision",
            reason_code=reason,
            schema_stage=schema_stage,
            image_model_provenance=image_model_provenance,
            vision_model_provenance=vision_model_provenance,
        ).model_dump(mode="json")
        return _AttemptOutcome(
            garment=garment,
            status="quarantined",
            failure_reason=reason,
            failure_stage="vision",
            schema_stage=schema_stage,
            image_model_provenance=image_model_provenance,
            vision_model_provenance=vision_model_provenance,
            private_quarantine_bytes=private_bytes,
            private_quarantine_record=private_record,
            existing_item=existing_item,
            source_kind="ai_generated_reference",
        )
    except (ValueError, TypeError):
        return _AttemptOutcome(
            garment=garment,
            status="quarantined",
            failure_reason="response_schema_invalid",
            failure_stage="vision",
            image_model_provenance=image_model_provenance,
            existing_item=existing_item,
            source_kind="ai_generated_reference",
        )
    imported_at = datetime.now(timezone.utc)
    source_receipt = LicensedSourceReceipt(
        provider="cpa_generated",
        source_url=None,
        creator=None,
        license_code="ai-generated",
        license_url=None,
        attribution="AI 生成参考",
        imported_at=imported_at,
    )
    source = _NormalizedSource(
        provider_item_id=prompt_sha256,
        source_receipt=source_receipt,
        image_url=None,
        provider_license="ai-generated",
        provider_license_version=CPA_GENERATED_PROMPT_VERSION,
        source_ref=None,
        expected_sha256=fetched.original_sha256,
    )
    receipt_extensions = {
        "bound_garment_id": garment.garment_id,
        "bound_user_id": garment.user_id,
        "prompt_template_version": CPA_GENERATED_PROMPT_VERSION,
        "prompt_sha256": prompt_sha256,
        **model_receipt,
    }
    return _AttemptOutcome(
        garment=garment,
        status="ready",
        source=source,
        fetched=fetched,
        cropped=cropped,
        public_license=public_license_from_source(source_receipt),
        existing_item=existing_item,
        receipt_extensions=receipt_extensions,
        source_kind="ai_generated_reference",
        image_model_provenance=image_model_provenance,
    )


async def _attempt_openverse_garment(
    *,
    garment: Garment,
    config: IngestionConfig,
    api_client: httpx.AsyncClient,
    safe_fetcher: Any,
    vision: CatalogVisionInspector,
    existing_item: dict[str, Any] | None,
    counters: _Counters,
) -> _AttemptOutcome:
    search_outcome = await _search_openverse(
        api_client=api_client,
        garment=garment,
        config=config,
        counters=counters,
    )
    if not search_outcome.candidates:
        return _AttemptOutcome(
            garment=garment,
            status="quarantined",
            failure_reason=search_outcome.reason_code or "provider_unavailable",
            existing_item=existing_item,
        )
    for raw in search_outcome.candidates[: config.candidate_budget]:
        counters.candidate_attempts += 1
        try:
            source = _normalize_openverse_candidate(
                raw, allowlist=config.license_allowlist
            )
        except ValueError:
            continue
        if _is_same_source_takedown(existing_item, source):
            return _AttemptOutcome(
                garment=garment,
                status="quarantined",
                failure_reason="source_takedown",
                source=source,
                existing_item=existing_item,
            )
        if config.resume and _reusable_item(
            existing_item=existing_item,
            garment=garment,
            source=source,
            asset_directory=config.asset_directory,
        ):
            return _AttemptOutcome(
                garment=garment,
                status="reused",
                source=source,
                existing_item=existing_item,
            )
        try:
            fetched = await _fetch_trusted_openverse_thumbnail(
                api_client=api_client,
                provider_item_id=source.provider_item_id,
                safe_fetcher=safe_fetcher,
                config=config,
            )
            counters.vision_call_count += 1
            _record_vision_model_provenance(counters, vision)
            cropped, _, vision_trace = await assess_and_crop_catalog_asset(
                vision=vision,
                fetched_image=fetched,
                allowed_slot=garment.slot,
                expected_product_type=product_type_for_garment(garment.garment_id),
            )
            _record_vision_model_provenance(counters, vision_trace)
        except asyncio.CancelledError:
            raise
        except (ImageFetchError, VisionUnavailable, ValueError, TypeError) as error:
            if isinstance(error, VisionUnavailable):
                _record_vision_model_provenance(counters, error.provider_trace)
            continue
        return _AttemptOutcome(
            garment=garment,
            status="ready",
            source=source,
            fetched=fetched,
            cropped=cropped,
            public_license=public_license_from_source(source.source_receipt),
            existing_item=existing_item,
        )
    return _AttemptOutcome(
        garment=garment,
        status="quarantined",
        failure_reason="candidate_budget_exhausted",
        existing_item=existing_item,
    )


def _load_user_owned_source_map(
    source_map: Path,
    *,
    controlled_ids: set[str],
) -> dict[str, tuple[Path, str, str | None]]:
    rows = _read_jsonl(Path(source_map), limit=MANIFEST_INPUT_LIMIT)
    root = Path(source_map).resolve().parent
    result: dict[str, tuple[Path, str, str | None]] = {}
    for row in rows:
        if not isinstance(row, dict) or not set(row).issubset(
            {"garment_id", "path", "expected_sha256"}
        ):
            raise ValueError("invalid_user_owned_source_map")
        garment_id = row.get("garment_id")
        relative = row.get("path")
        expected = row.get("expected_sha256")
        if (
            not isinstance(garment_id, str)
            or garment_id not in controlled_ids
            or garment_id in result
            or not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or ":" in relative
        ):
            raise ValueError("invalid_user_owned_source_map")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise ValueError("invalid_user_owned_source_map")
        unresolved = root / Path(*pure.parts)
        if unresolved.is_symlink():
            raise ValueError("invalid_user_owned_source_map")
        candidate = unresolved.resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            raise ValueError("invalid_user_owned_source_map") from None
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("invalid_user_owned_source_map")
        if expected is not None and (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(char not in "0123456789abcdef" for char in expected)
        ):
            raise ValueError("invalid_user_owned_source_map")
        result[garment_id] = (candidate, pure.as_posix(), expected)
    return result


async def _attempt_user_owned_garment(
    *,
    garment: Garment,
    config: IngestionConfig,
    safe_fetcher: Any,
    vision: CatalogVisionInspector,
    existing_item: dict[str, Any] | None,
    source_entry: tuple[Path, str, str | None] | None,
    counters: _Counters,
) -> _AttemptOutcome:
    if source_entry is None:
        return _AttemptOutcome(
            garment=garment,
            status="quarantined",
            failure_reason="user_owned_source_missing",
            existing_item=existing_item,
        )
    counters.candidate_attempts += 1
    path, source_ref, expected_sha256 = source_entry
    source_receipt = LicensedSourceReceipt(
        provider="user_upload",
        source_url=None,
        creator=None,
        license_code="user-owned",
        license_url=None,
        attribution="用户自有图片",
        imported_at=datetime.now(timezone.utc),
    )
    source = _NormalizedSource(
        provider_item_id=garment.garment_id,
        source_receipt=source_receipt,
        image_url=None,
        provider_license="user-owned",
        provider_license_version="owner-asserted-v1",
        source_ref=source_ref,
        expected_sha256=expected_sha256,
    )
    loader = getattr(safe_fetcher, "load_user_owned", None)
    if loader is None:
        return _AttemptOutcome(
            garment=garment,
            status="quarantined",
            failure_reason="user_owned_loader_unavailable",
            existing_item=existing_item,
        )
    try:
        fetched = await loader(path, expected_sha256=expected_sha256)
        source = replace(source, expected_sha256=fetched.original_sha256)
        if _is_same_source_takedown(existing_item, source):
            return _AttemptOutcome(
                garment=garment,
                status="quarantined",
                failure_reason="source_takedown",
                source=source,
                existing_item=existing_item,
            )
        if config.resume and _reusable_item(
            existing_item=existing_item,
            garment=garment,
            source=source,
            asset_directory=config.asset_directory,
        ):
            return _AttemptOutcome(
                garment=garment,
                status="reused",
                source=source,
                existing_item=existing_item,
            )
        counters.vision_call_count += 1
        _record_vision_model_provenance(counters, vision)
        cropped, _, vision_trace = await assess_and_crop_catalog_asset(
            vision=vision,
            fetched_image=fetched,
            allowed_slot=garment.slot,
            expected_product_type=product_type_for_garment(garment.garment_id),
        )
        _record_vision_model_provenance(counters, vision_trace)
    except asyncio.CancelledError:
        raise
    except (ImageFetchError, VisionUnavailable, ValueError, TypeError) as error:
        if isinstance(error, VisionUnavailable):
            _record_vision_model_provenance(counters, error.provider_trace)
        return _AttemptOutcome(
            garment=garment,
            status="quarantined",
            failure_reason="user_owned_source_rejected",
            existing_item=existing_item,
        )
    return _AttemptOutcome(
        garment=garment,
        status="ready",
        source=source,
        fetched=fetched,
        cropped=cropped,
        public_license=public_license_from_source(source_receipt),
        existing_item=existing_item,
    )


def _receipt_record(
    *,
    source: _NormalizedSource,
    fetched: FetchedImage,
    cropped: CatalogCroppedImage,
    extensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **source.identity(),
        "imported_at": source.source_receipt.imported_at.isoformat(),
        "original_sha256": fetched.original_sha256,
        "processed_sha256": cropped.processed_sha256,
        "processing": "safe_decode_normalize_then_server_crop",
        **(extensions or {}),
    }


def _ready_manifest_item(outcome: _AttemptOutcome) -> tuple[dict[str, Any], dict[str, Any]]:
    assert outcome.source is not None
    assert outcome.fetched is not None
    assert outcome.cropped is not None
    assert outcome.public_license is not None
    existing_history: list[Any] = []
    if outcome.existing_item is not None:
        prior = outcome.existing_item.get("receipt_history")
        if isinstance(prior, list):
            existing_history = list(prior)
    receipt = _receipt_record(
        source=outcome.source,
        fetched=outcome.fetched,
        cropped=outcome.cropped,
        extensions=outcome.receipt_extensions,
    )
    relative_path = (
        f"{outcome.garment.garment_id}/{outcome.cropped.processed_sha256}.png"
    )
    item = {
        "garment_id": outcome.garment.garment_id,
        "user_id": outcome.garment.user_id,
        "slot": outcome.garment.slot,
        "audience": outcome.garment.audience,
        "source_kind": (
            outcome.source_kind
            or (
                "user_owned_photo"
                if outcome.source.source_receipt.provider == "user_upload"
                else "licensed_photo"
            )
        ),
        "status": "ready",
        "original_sha256": outcome.fetched.original_sha256,
        "processed_sha256": outcome.cropped.processed_sha256,
        "relative_path": relative_path,
        "license": outcome.public_license.model_dump(mode="json"),
        "receipt_history": [*existing_history, receipt],
    }
    return item, {
        "garment_id": outcome.garment.garment_id,
        "receipt": receipt,
    }


def _quarantined_manifest_item(outcome: _AttemptOutcome) -> dict[str, Any]:
    history: list[Any] = []
    if outcome.existing_item is not None:
        prior = outcome.existing_item.get("receipt_history")
        if isinstance(prior, list):
            history = list(prior)
    return {
        "garment_id": outcome.garment.garment_id,
        "user_id": outcome.garment.user_id,
        "slot": outcome.garment.slot,
        "audience": outcome.garment.audience,
        "source_kind": (
            outcome.source_kind
            if outcome.source_kind is not None
            else (
                outcome.existing_item["source_kind"]
                if outcome.source is None and outcome.existing_item is not None
                else (
                    "user_owned_photo"
                    if outcome.source is not None
                    and outcome.source.source_receipt.provider == "user_upload"
                    else "licensed_photo"
                )
            )
        ),
        "status": "quarantined",
        "original_sha256": None,
        "processed_sha256": None,
        "relative_path": None,
        "license": None,
        "receipt_history": history,
        "failure_reason": outcome.failure_reason or "candidate_budget_exhausted",
    }


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(path, encoded)


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        if path.exists():
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            if attributes & 0x400:  # Windows FILE_ATTRIBUTE_REPARSE_POINT
                return True
    except OSError:
        raise ValueError("unsafe_private_quarantine_root") from None
    return False


def _paths_overlap(left: Path, right: Path) -> bool:
    return (
        left == right
        or left in right.parents
        or right in left.parents
    )


def _validated_private_quarantine_paths(
    config: IngestionConfig,
) -> tuple[Path, Path, Path, Path]:
    """Return private paths only after structural non-serving validation."""

    state_root = config.asset_directory.parent
    private_parent = state_root / "private_quarantine"
    root = private_parent / "cpa_generated"
    diagnostics = state_root / "cpa_generated_quarantine_diagnostics.jsonl"
    marker = state_root / "cpa_generated_quarantine_transaction.json"
    backup_parent = private_parent / ".transactions"

    # Reject a symlink/junction/reparse point at every lexical parent.  Merely
    # checking the final child is insufficient because an ancestor can redirect
    # the private tree into a static or ready-asset root.
    lexical_chain = (root, private_parent, state_root, *state_root.parents)
    if any(_is_reparse_or_symlink(path) for path in lexical_chain):
        raise ValueError("unsafe_private_quarantine_root")
    for state_file in (diagnostics, marker, backup_parent):
        if _is_reparse_or_symlink(state_file):
            raise ValueError("unsafe_private_quarantine_root")

    resolved_state = state_root.resolve()
    resolved_root = root.resolve()
    resolved_diagnostics = diagnostics.resolve()
    resolved_marker = marker.resolve()
    resolved_backup_parent = backup_parent.resolve()
    try:
        resolved_root.relative_to(resolved_state)
        resolved_diagnostics.relative_to(resolved_state)
        resolved_marker.relative_to(resolved_state)
        resolved_backup_parent.relative_to(resolved_state)
    except ValueError:
        raise ValueError("unsafe_private_quarantine_root") from None

    serving_roots = {
        config.asset_directory.resolve(),
        DEFAULT_ASSET_DIRECTORY.resolve(),
        AI_REFERENCE_ASSET_ROOT.resolve(),
        (ROOT / "data" / "assets" / "wardrobe_catalog_reference_v1").resolve(),
        (ROOT / "static").resolve(),
        (ROOT / "web").resolve(),
    }
    if any(_paths_overlap(resolved_root, serving) for serving in serving_roots):
        raise ValueError("unsafe_private_quarantine_root")
    return resolved_root, resolved_diagnostics, resolved_marker, resolved_backup_parent


def _private_quarantine_paths(config: IngestionConfig) -> tuple[Path, Path]:
    root, diagnostics, _, _ = _validated_private_quarantine_paths(config)
    return root, diagnostics


def _validate_existing_private_quarantine(
    config: IngestionConfig,
    *,
    authoritative: dict[str, Garment],
    requested_image_model: str,
    image_resolved_allowlist: tuple[str, ...] | frozenset[str],
) -> tuple[_PrivateQuarantineDiagnostic, ...]:
    """Validate durable private evidence without granting it resume authority."""

    root, diagnostics = _private_quarantine_paths(config)
    try:
        if not diagnostics.exists():
            if root.exists() and any(path.is_file() for path in root.iterdir()):
                raise ValueError("invalid_private_quarantine_diagnostics")
            return ()
        if (
            diagnostics.is_symlink()
            or not diagnostics.is_file()
            or diagnostics.stat().st_size > MANIFEST_INPUT_LIMIT
        ):
            raise ValueError("invalid_private_quarantine_diagnostics")
        raw_lines = diagnostics.read_bytes().decode("utf-8").splitlines()
        if not raw_lines or len(raw_lines) > 1000 or any(not line for line in raw_lines):
            raise ValueError("invalid_private_quarantine_diagnostics")
        records = tuple(
            _PrivateQuarantineDiagnostic.model_validate(json.loads(line))
            for line in raw_lines
        )
        transaction_ids: set[str] = set()
        relative_paths: set[str] = set()
        for record in records:
            garment = authoritative.get(record.garment_id)
            if garment is None:
                raise ValueError("invalid_private_quarantine_diagnostics")
            expected_image_provenance = _minimal_model_provenance(
                record.image_model_provenance.model_dump(mode="python"),
                requested_model=requested_image_model,
                resolved_allowlist=image_resolved_allowlist,
            )
            expected_vision_provenance = _minimal_model_provenance(
                record.vision_model_provenance.model_dump(mode="python"),
                requested_model=LOGICAL_GROK_MODEL,
                resolved_allowlist=CPA_REPORTED_MODELS,
            )
            expected_prompt_hash = hashlib.sha256(
                _cpa_generated_prompt(garment).encode("utf-8")
            ).hexdigest()
            if (
                record.transaction_id in transaction_ids
                or record.user_id != garment.user_id
                or record.slot != garment.slot
                or record.product_type != product_type_for_garment(garment.garment_id)
                or record.prompt_sha256 != expected_prompt_hash
                or record.image_model_provenance != expected_image_provenance
                or record.vision_model_provenance != expected_vision_provenance
            ):
                raise ValueError("invalid_private_quarantine_diagnostics")
            transaction_ids.add(record.transaction_id)
            relative_paths.add(record.quarantine_relative_path)
            private_png = root / record.quarantine_relative_path
            if (
                private_png.parent != root
                or private_png.is_symlink()
                or not private_png.is_file()
                or hashlib.sha256(private_png.read_bytes()).hexdigest()
                != record.quarantine_sha256
            ):
                raise ValueError("invalid_private_quarantine_diagnostics")
        if root.exists():
            actual_pngs = {
                path.name
                for path in root.iterdir()
                if path.is_file() and path.suffix == ".png"
            }
            if actual_pngs != relative_paths:
                raise ValueError("invalid_private_quarantine_diagnostics")
        return records
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("invalid_private_quarantine_diagnostics") from None


def _transaction_target(
    *, root: Path, diagnostics: Path, kind: Any, name: Any
) -> Path:
    if kind == "diagnostics" and name == diagnostics.name:
        return diagnostics
    if (
        kind == "quarantine"
        and isinstance(name, str)
        and len(name) == 68
        and name.endswith(".png")
        and _is_sha256(name[:-4])
    ):
        candidate = root / name
        if candidate.parent == root:
            return candidate
    raise ValueError("invalid_private_quarantine_transaction")


def _validate_private_transaction(
    payload: Any,
    *,
    root: Path,
    diagnostics: Path,
    backup_parent: Path,
) -> tuple[str, str, list[tuple[Path, Path | None, str | None, str]]]:
    if not isinstance(payload, dict) or set(payload) != {
        "version", "transaction_id", "phase", "entries"
    }:
        raise ValueError("invalid_private_quarantine_transaction")
    transaction_id = payload.get("transaction_id")
    phase = payload.get("phase")
    entries = payload.get("entries")
    if (
        payload.get("version") != "cpa_private_quarantine_tx_v1"
        or not isinstance(transaction_id, str)
        or len(transaction_id) != 32
        or any(char not in "0123456789abcdef" for char in transaction_id)
        or phase not in {"prepared", "committed"}
        or not isinstance(entries, list)
        or not entries
        or len(entries) > 1001
    ):
        raise ValueError("invalid_private_quarantine_transaction")
    backup_root = backup_parent / transaction_id
    if _is_reparse_or_symlink(backup_root):
        raise ValueError("invalid_private_quarantine_transaction")
    validated: list[tuple[Path, Path | None, str | None, str]] = []
    targets: set[Path] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {
            "kind", "name", "previous_sha256", "backup_name", "new_sha256"
        }:
            raise ValueError("invalid_private_quarantine_transaction")
        target = _transaction_target(
            root=root,
            diagnostics=diagnostics,
            kind=entry.get("kind"),
            name=entry.get("name"),
        )
        previous_sha256 = entry.get("previous_sha256")
        backup_name = entry.get("backup_name")
        new_sha256 = entry.get("new_sha256")
        if target in targets or not _is_sha256(new_sha256):
            raise ValueError("invalid_private_quarantine_transaction")
        targets.add(target)
        backup: Path | None = None
        if previous_sha256 is None and backup_name is None:
            pass
        elif (
            _is_sha256(previous_sha256)
            and backup_name == f"{index:04d}.bak"
        ):
            backup = backup_root / backup_name
            if backup.parent != backup_root or _is_reparse_or_symlink(backup):
                raise ValueError("invalid_private_quarantine_transaction")
        else:
            raise ValueError("invalid_private_quarantine_transaction")
        if _is_reparse_or_symlink(target):
            raise ValueError("invalid_private_quarantine_transaction")
        validated.append((target, backup, previous_sha256, new_sha256))
    return transaction_id, phase, validated


def _remove_private_transaction_state(
    *, marker: Path, backup_parent: Path, transaction_id: str, entries: list[tuple[Path, Path | None, str | None, str]]
) -> None:
    marker.unlink(missing_ok=True)
    backup_root = backup_parent / transaction_id
    for _, backup, _, _ in entries:
        if backup is not None:
            backup.unlink(missing_ok=True)
    try:
        backup_root.rmdir()
    except OSError:
        pass
    try:
        backup_parent.rmdir()
    except OSError:
        pass


def _recover_private_file(path: Path, previous: bytes | None) -> None:
    """Durable restart recovery independent from the in-process rollback hook."""

    if previous is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.recovery.tmp")
    try:
        temporary.write_bytes(previous)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _recover_private_quarantine_transaction(
    config: IngestionConfig,
    *,
    durable_recovery: bool = True,
    trusted_payload: dict[str, Any] | None = None,
) -> bool:
    root, diagnostics, marker, backup_parent = _validated_private_quarantine_paths(config)
    if not marker.exists():
        return True
    try:
        if marker.stat().st_size > MANIFEST_INPUT_LIMIT:
            raise ValueError("invalid_private_quarantine_transaction")
        payload = (
            json.loads(marker.read_text(encoding="utf-8"))
            if durable_recovery
            else trusted_payload
        )
        transaction_id, phase, entries = _validate_private_transaction(
            payload,
            root=root,
            diagnostics=diagnostics,
            backup_parent=backup_parent,
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("invalid_private_quarantine_transaction") from None

    # A durable marker is not an authorization token.  Restart recovery may
    # clean only the transaction-scoped backup directory and marker; it never
    # deletes, restores, or overwrites a named final.  If any final already
    # exists, preserve it and fail closed for operator review.
    if durable_recovery:
        if any(target.exists() for target, _, _, _ in entries):
            raise ValueError("invalid_private_quarantine_transaction")
        _remove_private_transaction_state(
            marker=marker,
            backup_parent=backup_parent,
            transaction_id=transaction_id,
            entries=entries,
        )
        return True

    if phase == "committed":
        committed = True
        for target, _, _, new_sha256 in entries:
            try:
                committed = committed and target.is_file() and (
                    hashlib.sha256(target.read_bytes()).hexdigest() == new_sha256
                )
            except OSError:
                committed = False
        if committed:
            _remove_private_transaction_state(
                marker=marker,
                backup_parent=backup_parent,
                transaction_id=transaction_id,
                entries=entries,
            )
            return True

    for target, backup, previous_sha256, _ in reversed(entries):
        previous: bytes | None = None
        if backup is not None:
            try:
                previous = backup.read_bytes()
            except OSError:
                return False
            if hashlib.sha256(previous).hexdigest() != previous_sha256:
                return False
        restore = _recover_private_file if durable_recovery else _restore_private_file
        try:
            restore(target, previous)
        except OSError:
            return False

    restored = True
    for target, _, previous_sha256, _ in entries:
        try:
            if previous_sha256 is None:
                restored = restored and not target.exists()
            else:
                restored = restored and target.is_file() and (
                    hashlib.sha256(target.read_bytes()).hexdigest() == previous_sha256
                )
        except OSError:
            restored = False
    if restored:
        _remove_private_transaction_state(
            marker=marker,
            backup_parent=backup_parent,
            transaction_id=transaction_id,
            entries=entries,
        )
    return restored


def _validate_private_quarantine_transaction_marker(config: IngestionConfig) -> None:
    """Validate durable recovery state without mutating it or granting authority."""

    root, diagnostics, marker, backup_parent = _validated_private_quarantine_paths(config)
    if not marker.exists():
        return
    try:
        if marker.stat().st_size > MANIFEST_INPUT_LIMIT:
            raise ValueError("invalid_private_quarantine_transaction")
        payload = json.loads(marker.read_text(encoding="utf-8"))
        _, _, entries = _validate_private_transaction(
            payload,
            root=root,
            diagnostics=diagnostics,
            backup_parent=backup_parent,
        )
        if any(target.exists() for target, _, _, _ in entries):
            raise ValueError("invalid_private_quarantine_transaction")
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("invalid_private_quarantine_transaction") from None


def _begin_private_quarantine_transaction(
    config: IngestionConfig,
    targets: dict[Path, bytes],
) -> dict[str, Any] | None:
    if not targets:
        return None
    root, diagnostics, marker, backup_parent = _validated_private_quarantine_paths(config)
    if marker.exists():
        raise ValueError("private_quarantine_recovery_required")
    transaction_id = uuid.uuid4().hex
    backup_root = backup_parent / transaction_id
    if _is_reparse_or_symlink(backup_root):
        raise ValueError("invalid_private_quarantine_transaction")
    entries: list[dict[str, Any]] = []
    try:
        for index, (target, new_bytes) in enumerate(targets.items()):
            if target == diagnostics:
                kind, name = "diagnostics", diagnostics.name
            elif target.parent == root and target.name.endswith(".png"):
                kind, name = "quarantine", target.name
            else:
                raise ValueError("invalid_private_quarantine_transaction")
            if _is_reparse_or_symlink(target):
                raise ValueError("invalid_private_quarantine_transaction")
            previous = target.read_bytes() if target.exists() else None
            backup_name: str | None = None
            previous_sha256: str | None = None
            if previous is not None:
                backup_name = f"{index:04d}.bak"
                previous_sha256 = hashlib.sha256(previous).hexdigest()
                _atomic_write_bytes(backup_root / backup_name, previous)
            entries.append(
                {
                    "kind": kind,
                    "name": name,
                    "previous_sha256": previous_sha256,
                    "backup_name": backup_name,
                    "new_sha256": hashlib.sha256(new_bytes).hexdigest(),
                }
            )
        payload = {
            "version": "cpa_private_quarantine_tx_v1",
            "transaction_id": transaction_id,
            "phase": "prepared",
            "entries": entries,
        }
        _atomic_write_json(marker, payload)
        return payload
    except BaseException:
        for entry in entries:
            backup_name = entry["backup_name"]
            if backup_name is not None:
                (backup_root / backup_name).unlink(missing_ok=True)
        try:
            backup_root.rmdir()
            backup_parent.rmdir()
        except OSError:
            pass
        raise


def _commit_private_quarantine_transaction(
    config: IngestionConfig, payload: dict[str, Any]
) -> None:
    root, diagnostics, marker, backup_parent = _validated_private_quarantine_paths(config)
    transaction_id, _, entries = _validate_private_transaction(
        payload,
        root=root,
        diagnostics=diagnostics,
        backup_parent=backup_parent,
    )
    for target, _, _, new_sha256 in entries:
        if (
            not target.is_file()
            or target.is_symlink()
            or hashlib.sha256(target.read_bytes()).hexdigest() != new_sha256
        ):
            raise OSError("private_quarantine_commit_incomplete")
    committed = {**payload, "phase": "committed"}
    _atomic_write_json(marker, committed)
    _remove_private_transaction_state(
        marker=marker,
        backup_parent=backup_parent,
        transaction_id=transaction_id,
        entries=entries,
    )


def _prepare_private_quarantine(
    config: IngestionConfig,
    outcomes: list[_AttemptOutcome],
) -> tuple[dict[Path, bytes], Path | None, bytes | None]:
    root, diagnostics_path = _private_quarantine_paths(config)
    staged: dict[Path, bytes] = {}
    records: list[dict[str, Any]] = []
    for outcome in outcomes:
        record = outcome.private_quarantine_record
        payload = outcome.private_quarantine_bytes
        if record is None and payload is None:
            continue
        if record is None or payload is None:
            raise ValueError("invalid_private_quarantine_state")
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != 2
            or not {"schema_version", "schema_stage"}.issubset(record)
        ):
            raise ValueError("invalid_private_quarantine_schema_version")
        validated = _PrivateQuarantineDiagnostic.model_validate(record)
        if hashlib.sha256(payload).hexdigest() != validated.quarantine_sha256:
            raise ValueError("invalid_private_quarantine_bytes")
        path = root / validated.quarantine_relative_path
        if path.parent != root:
            raise ValueError("invalid_private_quarantine_path")
        if path.exists():
            try:
                if path.is_symlink() or path.read_bytes() != payload:
                    raise ValueError("invalid_private_quarantine_collision")
            except OSError:
                raise ValueError("invalid_private_quarantine_collision") from None
        else:
            staged[path] = payload
        records.append(validated.model_dump(mode="json"))
    if not records:
        return {}, None, None

    prior_records: list[dict[str, Any]] = []
    prior_bytes = b""
    if diagnostics_path.exists():
        try:
            if diagnostics_path.is_symlink() or diagnostics_path.stat().st_size > MANIFEST_INPUT_LIMIT:
                raise ValueError("invalid_private_quarantine_diagnostics")
            prior_bytes = diagnostics_path.read_bytes()
            decoded = prior_bytes.decode("utf-8").splitlines()
            if len(decoded) > 1000:
                raise ValueError("invalid_private_quarantine_diagnostics")
            prior_records = [
                _PrivateQuarantineDiagnostic.model_validate(json.loads(line)).model_dump(
                    mode="json", exclude_unset=True
                )
                for line in decoded
                if line
            ]
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise ValueError("invalid_private_quarantine_diagnostics") from None
    unique = {
        json.dumps(record, ensure_ascii=False, sort_keys=True)
        for record in prior_records
    }
    new_records: list[dict[str, Any]] = []
    for record in records:
        fingerprint = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if fingerprint not in unique:
            unique.add(fingerprint)
            new_records.append(record)
    if len(prior_records) + len(new_records) > 1000:
        raise ValueError("private_quarantine_diagnostics_full")
    suffix = b"".join(
        (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        for record in new_records
    )
    separator = (
        b"\n"
        if prior_bytes and suffix and not prior_bytes.endswith(b"\n")
        else b""
    )
    encoded = prior_bytes + separator + suffix
    return staged, diagnostics_path, (encoded if encoded != prior_bytes else None)


def _restore_private_file(path: Path, previous: bytes | None) -> None:
    """Best-effort rollback isolated from the injectable publication writer."""

    try:
        if previous is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.rollback.tmp")
        try:
            temporary.write_bytes(previous)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        # A rollback failure does not convert an untrusted private record into
        # published authority; callers still receive the original write error.
        return


def _prepare_source_history(
    existing: bytes,
    existing_fingerprints: frozenset[tuple[str, str]],
    records: list[dict[str, Any]],
    *,
    authoritative: dict[str, Garment],
    image_model_allowlist: tuple[str, ...] = (),
) -> tuple[bytes, frozenset[tuple[str, str]]]:
    fingerprints = set(existing_fingerprints)
    encoded: list[bytes] = []
    for record in records:
        fingerprint = _validate_source_record(
            record,
            authoritative=authoritative,
            image_model_allowlist=image_model_allowlist,
        )
        if fingerprint in fingerprints:
            raise ValueError("duplicate_source_receipt")
        fingerprints.add(fingerprint)
        encoded.append(
            (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        )
    payload = existing + b"".join(encoded)
    if len(payload) > SOURCE_HISTORY_LIMIT:
        raise ValueError("source_history_too_large")
    return payload, frozenset(fingerprints)


def _upsert_items(
    manifest: dict[str, Any],
    replacements: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    items_by_id = {
        item["garment_id"]: item
        for item in manifest["items"]
        if isinstance(item, dict) and isinstance(item.get("garment_id"), str)
    }
    items_by_id.update(replacements)
    return {
        **manifest,
        "manifest_version": "wardrobe_assets_v2",
        "items": [items_by_id[key] for key in sorted(items_by_id)],
    }


def _ai_reference_manifest_state(
    garment: Garment,
    reference: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    sha256 = reference["sha256"]
    imported_at = reference["provenance_normalized_at"]
    source_receipt = LicensedSourceReceipt(
        provider="cpa_generated",
        source_url=None,
        creator=None,
        license_code="ai-generated",
        license_url=None,
        attribution="AI 生成参考",
        imported_at=_parse_imported_at(imported_at),
    )
    source = _NormalizedSource(
        provider_item_id=garment.garment_id,
        source_receipt=source_receipt,
        image_url=None,
        provider_license="ai-generated",
        provider_license_version=WARDROBE_GENERATED_ASSET_VERSION,
        source_ref=(
            "data/manifests/wardrobe_generated_v1.json"
            f"#{garment.garment_id}"
        ),
        expected_sha256=sha256,
    )
    receipt = {
        **source.identity(),
        "imported_at": imported_at,
        "original_sha256": sha256,
        "processed_sha256": sha256,
        "processing": "verified_s12r_ai_reference_passthrough",
    }
    item = {
        "garment_id": garment.garment_id,
        "user_id": garment.user_id,
        "slot": garment.slot,
        "audience": garment.audience,
        "source_kind": "ai_generated_reference",
        "status": "ready",
        "original_sha256": sha256,
        "processed_sha256": sha256,
        "relative_path": reference["relative_path"],
        "license": public_license_from_source(source_receipt).model_dump(mode="json"),
        "receipt_history": [receipt],
    }
    return item, {"garment_id": garment.garment_id, "receipt": receipt}


def _compose_verified_ai_references(
    *,
    manifest: dict[str, Any],
    source_history: bytes,
    durable_receipts: frozenset[tuple[str, str]],
    authoritative: dict[str, Garment],
    asset_directory: Path,
    image_model_allowlist: tuple[str, ...] = (),
) -> tuple[dict[str, Any], bytes, frozenset[tuple[str, str]]]:
    base_garments = tuple(
        sorted(
            (
                garment
                for garment in authoritative.values()
                if garment.data_version == "fixtures_v1.0"
                and garment.source_id == "fixtures"
            ),
            key=lambda garment: garment.garment_id,
        )
    )
    expected_ids = tuple(f"g{index:03d}" for index in range(1, 51))
    if tuple(garment.garment_id for garment in base_garments) != expected_ids:
        raise ValueError("invalid_ai_reference_authority")

    existing_by_id = {item["garment_id"]: item for item in manifest["items"]}
    replacements: dict[str, dict[str, Any]] = {}
    source_records: list[dict[str, Any]] = []
    for garment in base_garments:
        reference = _verified_ai_reference(garment.garment_id)
        if garment.garment_id in existing_by_id:
            continue
        item, source_record = _ai_reference_manifest_state(garment, reference)
        receipt_fingerprint = json.dumps(
            source_record["receipt"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        replacements[garment.garment_id] = item
        if (garment.garment_id, receipt_fingerprint) not in durable_receipts:
            source_records.append(source_record)

    composed_history, composed_receipts = _prepare_source_history(
        source_history,
        durable_receipts,
        source_records,
        authoritative=authoritative,
        image_model_allowlist=image_model_allowlist,
    )
    composed_manifest = _upsert_items(manifest, replacements)
    _validate_manifest_payload(
        composed_manifest,
        authoritative=authoritative,
        asset_directory=asset_directory,
        durable_receipts=composed_receipts,
        image_model_allowlist=image_model_allowlist,
    )
    return composed_manifest, composed_history, composed_receipts


async def run_ingestion(
    config: IngestionConfig,
    api_client: httpx.AsyncClient | None,
    safe_fetcher: Any,
    vision: CatalogVisionInspector,
    image_provider: Any | None = None,
    image_model_allowlist: tuple[str, ...] = (),
) -> IngestionResult:
    """Run bounded ingestion with all network and Vision dependencies injected."""
    garments = _controlled_garments(config.garment_file)
    if config.diagnostic_canary and tuple(
        garment.garment_id for garment in garments
    ) != ("g051",):
        raise ValueError("invalid_diagnostic_canary_garment")
    authoritative = _authoritative_garments()
    if config.provider == "openverse":
        if api_client is None:
            raise ValueError("openverse_client_required")
        _validate_official_client(api_client)
    elif config.provider == "cpa-generated":
        requested = getattr(image_provider, "requested_model", None)
        transport = getattr(image_provider, "transport_model", None)
        if (
            image_provider is None
            or requested not in CPA_GENERATED_HISTORICAL_MODEL_ALLOWLIST
            or transport not in CPA_GENERATED_HISTORICAL_MODEL_ALLOWLIST
        ):
            raise ValueError("cpa_image_provider_required")
        _validate_existing_private_quarantine(
            config,
            authoritative=authoritative,
            requested_image_model=requested,
            image_resolved_allowlist=CPA_GENERATED_HISTORICAL_MODEL_ALLOWLIST,
        )
        _validate_private_quarantine_transaction_marker(config)
        if not config.dry_run:
            if not _recover_private_quarantine_transaction(config):
                raise ValueError("private_quarantine_recovery_incomplete")

    if config.dry_run:
        persisted_source_history = source_history = b""
        durable_receipts: frozenset[tuple[str, str]] = frozenset()
        persisted_manifest = manifest = {
            "manifest_version": "wardrobe_assets_v2",
            "items": [],
        }
    else:
        persisted_source_history, durable_receipts = _load_source_history(
            config.sources_path,
            authoritative=authoritative,
            image_model_allowlist=image_model_allowlist,
        )
        persisted_manifest = _load_existing_manifest(
            config.manifest_path,
            authoritative=authoritative,
            asset_directory=config.asset_directory,
            durable_receipts=durable_receipts,
            image_model_allowlist=image_model_allowlist,
        )
        manifest, source_history, durable_receipts = _compose_verified_ai_references(
            manifest=persisted_manifest,
            source_history=persisted_source_history,
            durable_receipts=durable_receipts,
            authoritative=authoritative,
            asset_directory=config.asset_directory,
            image_model_allowlist=image_model_allowlist,
        )
    existing_by_id = {
        item["garment_id"]: item
        for item in manifest["items"]
        if isinstance(item, dict) and isinstance(item.get("garment_id"), str)
    }
    attempted = garments[: config.total_budget]
    unattempted = garments[config.total_budget :]
    semaphore = asyncio.Semaphore(config.concurrency)
    counters = _Counters()
    user_owned_sources: dict[str, tuple[Path, str, str | None]] = {}
    if config.provider == "user-owned":
        assert config.source_map is not None
        user_owned_sources = _load_user_owned_source_map(
            config.source_map,
            controlled_ids={garment.garment_id for garment in garments},
        )

    async def bounded_attempt(garment: Garment) -> _AttemptOutcome:
        async with semaphore:
            if config.provider == "cpa-generated":
                assert image_provider is not None
                return await _attempt_cpa_generated_garment(
                    garment=garment,
                    config=config,
                    safe_fetcher=safe_fetcher,
                    vision=vision,
                    image_provider=image_provider,
                    image_model_allowlist=image_model_allowlist,
                    existing_item=existing_by_id.get(garment.garment_id),
                    counters=counters,
                )
            if config.provider == "user-owned":
                return await _attempt_user_owned_garment(
                    garment=garment,
                    config=config,
                    safe_fetcher=safe_fetcher,
                    vision=vision,
                    existing_item=existing_by_id.get(garment.garment_id),
                    source_entry=user_owned_sources.get(garment.garment_id),
                    counters=counters,
                )
            assert api_client is not None
            return await _attempt_openverse_garment(
                garment=garment,
                config=config,
                api_client=api_client,
                safe_fetcher=safe_fetcher,
                vision=vision,
                existing_item=existing_by_id.get(garment.garment_id),
                counters=counters,
            )

    outcomes = await asyncio.gather(*(bounded_attempt(item) for item in attempted))
    ready_ids = tuple(
        outcome.garment.garment_id
        for outcome in outcomes
        if outcome.status in {"ready", "reused"}
    )
    reused_ids = tuple(
        outcome.garment.garment_id
        for outcome in outcomes
        if outcome.status == "reused"
    )
    quarantined_ids = tuple(
        outcome.garment.garment_id
        for outcome in outcomes
        if outcome.status == "quarantined"
    )
    remaining_ids = tuple(
        sorted(
            {
                *quarantined_ids,
                *(garment.garment_id for garment in unattempted),
            }
        )
    )
    item_outcomes = tuple(
        ItemIngestionOutcome(
            garment_id=outcome.garment.garment_id,
            status=outcome.status,
            reason_code=outcome.failure_reason,
            failure_stage=outcome.failure_stage,
            schema_stage=outcome.schema_stage,
            image_model_provenance=outcome.image_model_provenance,
            vision_model_provenance=outcome.vision_model_provenance,
        )
        for outcome in outcomes
    ) + tuple(
        ItemIngestionOutcome(
            garment_id=garment.garment_id,
            status="unattempted",
            reason_code="total_budget_exhausted",
        )
        for garment in unattempted
    )

    if not config.dry_run:
        replacements: dict[str, dict[str, Any]] = {}
        source_records: list[dict[str, Any]] = []
        staged_assets: dict[str, bytes] = {}
        for outcome in outcomes:
            if outcome.status == "reused":
                assert outcome.existing_item is not None
                replacements[outcome.garment.garment_id] = outcome.existing_item
            elif outcome.status == "ready":
                item, source_record = _ready_manifest_item(outcome)
                assert outcome.cropped is not None
                staged_assets[item["relative_path"]] = outcome.cropped.processed_bytes
                replacements[outcome.garment.garment_id] = item
                source_records.append(source_record)
            elif (
                outcome.existing_item is not None
                and outcome.existing_item.get("status") == "takedown"
            ):
                assert outcome.existing_item is not None
                replacements[outcome.garment.garment_id] = outcome.existing_item
            else:
                replacements[outcome.garment.garment_id] = (
                    _quarantined_manifest_item(outcome)
                )
        new_manifest = _upsert_items(manifest, replacements)
        new_source_history, new_durable_receipts = _prepare_source_history(
            source_history,
            durable_receipts,
            source_records,
            authoritative=authoritative,
            image_model_allowlist=image_model_allowlist,
        )
        _validate_manifest_payload(
            new_manifest,
            authoritative=authoritative,
            asset_directory=config.asset_directory,
            durable_receipts=new_durable_receipts,
            staged_assets=staged_assets,
            image_model_allowlist=image_model_allowlist,
        )
        private_assets, private_diagnostics_path, private_diagnostics = (
            _prepare_private_quarantine(config, outcomes)
        )
        private_payloads = dict(private_assets)
        if private_diagnostics_path is not None and private_diagnostics is not None:
            private_payloads[private_diagnostics_path] = private_diagnostics
        private_transaction = _begin_private_quarantine_transaction(
            config, private_payloads
        )
        try:
            for path, payload in private_assets.items():
                _atomic_write_bytes(path, payload)
            if private_diagnostics_path is not None and private_diagnostics is not None:
                _atomic_write_bytes(private_diagnostics_path, private_diagnostics)
            for relative_path, payload in staged_assets.items():
                asset_path = _safe_asset_path(config.asset_directory, relative_path)
                if asset_path is None:
                    raise ValueError("unsafe_asset_path")
                _atomic_write_bytes(asset_path, payload)
            if new_source_history != persisted_source_history:
                _atomic_write_bytes(config.sources_path, new_source_history)
            if new_manifest != persisted_manifest:
                _atomic_write_json(config.manifest_path, new_manifest)
            if private_transaction is not None:
                _commit_private_quarantine_transaction(config, private_transaction)
        except BaseException:
            try:
                _recover_private_quarantine_transaction(
                    config,
                    durable_recovery=False,
                    trusted_payload=private_transaction,
                )
            except (OSError, TypeError, ValueError):
                pass
            raise

    return IngestionResult(
        dry_run=config.dry_run,
        ready_ids=ready_ids,
        reused_ids=reused_ids,
        quarantined_ids=quarantined_ids,
        remaining_ids=remaining_ids,
        api_attempts=counters.api_attempts,
        candidate_attempts=counters.candidate_attempts,
        vision_call_count=counters.vision_call_count,
        vision_model_provenance=counters.vision_model_provenance,
        item_outcomes=item_outcomes,
        manifest_path=config.manifest_path,
    )


def _parse_license_argument(value: str) -> tuple[LicenseCode, ...]:
    mapping: dict[str, tuple[LicenseCode, ...]] = {
        "CC0": ("CC0",),
        "PDM": ("PDM",),
        "CC-BY": ("CC-BY-2.0", "CC-BY-3.0", "CC-BY-4.0"),
    }
    tokens = value.split(",")
    if not tokens or any(token not in mapping for token in tokens):
        raise argparse.ArgumentTypeError("license must be CC0,PDM,CC-BY")
    expanded: list[LicenseCode] = []
    for token in tokens:
        expanded.extend(mapping[token])
    if len(set(expanded)) != len(expanded):
        raise argparse.ArgumentTypeError("license values must be unique")
    return tuple(expanded)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded official licensed wardrobe asset ingestion"
    )
    parser.add_argument(
        "--provider",
        required=True,
        choices=("openverse", "user-owned", "cpa-generated"),
    )
    parser.add_argument("--license", type=_parse_license_argument)
    parser.add_argument("--garment-file", required=True, type=Path)
    parser.add_argument("--source-map", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--diagnostic-canary", action="store_true")
    return parser


async def _run_cli(config: IngestionConfig) -> IngestionResult:
    settings = Settings.from_env()
    fetcher = SafeImageFetcher(
        ready_dir=config.asset_directory,
        quarantine_dir=config.asset_directory.parent / "quarantine",
        config=SafeImageConfig.from_settings(settings),
    )
    vision = VisionAdapter(settings)
    if config.provider == "cpa-generated":
        if (
            settings.cpa_image_enabled is not True
            or settings.cpa_image_model != CPA_IMAGE_MODEL
            or not isinstance(settings.cpa_base_url, str)
            or not settings.cpa_base_url.strip()
        ):
            raise ValueError("cpa_generated_configuration_unavailable")
        try:
            endpoint = httpx.URL(settings.cpa_base_url)
        except (TypeError, ValueError):
            raise ValueError("cpa_generated_configuration_unavailable") from None
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.host
            or endpoint.userinfo
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("cpa_generated_configuration_unavailable")
        try:
            image_provider = GrokImageProvider(settings)
        except (TypeError, ValueError):
            raise ValueError("cpa_generated_configuration_unavailable") from None
        if (
            image_provider.requested_model not in CPA_GENERATED_HISTORICAL_MODEL_ALLOWLIST
            or image_provider.transport_model
            not in CPA_GENERATED_HISTORICAL_MODEL_ALLOWLIST
        ):
            raise ValueError("cpa_generated_configuration_unavailable")
        return await run_ingestion(
            config,
            None,
            fetcher,
            vision,
            image_provider=image_provider,
            image_model_allowlist=CPA_GENERATED_HISTORICAL_MODEL_ALLOWLIST,
        )
    if config.provider != "openverse":
        return await run_ingestion(config, None, fetcher, vision)
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
    async with httpx.AsyncClient(
        base_url=OPENVERSE_API_BASE,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        verify=True,
        headers={
            "User-Agent": "ProfAgent-R1-LicensedAssetIngestor/1.0",
            "Accept": OPENVERSE_THUMBNAIL_ACCEPT,
            "Accept-Encoding": "identity",
        },
    ) as api_client:
        return await run_ingestion(config, api_client, fetcher, vision)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.provider == "openverse":
        if args.license is None or args.source_map is not None:
            raise SystemExit("openverse requires --license and forbids --source-map")
        license_allowlist = args.license
        source_map = None
    elif args.provider == "user-owned":
        if args.license is not None or args.source_map is None:
            raise SystemExit("user-owned requires --source-map and forbids --license")
        license_allowlist = ()
        source_map = args.source_map
    else:
        if args.license is not None or args.source_map is not None:
            raise SystemExit("cpa-generated forbids --license and --source-map")
        license_allowlist = ()
        source_map = None
    if args.diagnostic_canary:
        if args.provider != "openverse" or args.resume:
            raise SystemExit(
                "diagnostic canary requires openverse and forbids --resume"
            )
        try:
            diagnostic_ids = tuple(
                garment.garment_id
                for garment in _controlled_garments(args.garment_file)
            )
        except (OSError, ValueError) as error:
            raise SystemExit(
                "diagnostic canary requires an exact controlled g051 garment file"
            ) from error
        if diagnostic_ids != ("g051",):
            raise SystemExit(
                "diagnostic canary requires an exact controlled g051 garment file"
            )
    config = IngestionConfig(
        provider=args.provider,
        license_allowlist=license_allowlist,
        garment_file=args.garment_file,
        source_map=source_map,
        dry_run=True if args.diagnostic_canary else args.dry_run,
        resume=args.resume,
        candidate_budget=3 if args.diagnostic_canary else 5,
        retry_budget=0 if args.diagnostic_canary else 2,
        concurrency=1 if args.diagnostic_canary else 3,
        total_budget=1 if args.diagnostic_canary else 70,
        search_budget=3,
        diagnostic_canary=args.diagnostic_canary,
    )
    try:
        result = asyncio.run(_run_cli(config))
    except (ValueError, ImageFetchError, VisionUnavailable, httpx.HTTPError):
        print('{"status":"failed","reason_code":"ingestion_unavailable"}', file=sys.stderr)
        return 2
    print(result.model_dump_json())
    return 0 if not result.remaining_ids else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "IngestionConfig",
    "IngestionResult",
    "ItemIngestionOutcome",
    "main",
    "run_ingestion",
]
