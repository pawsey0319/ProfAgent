from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from profagent.config import Settings
from profagent.licensed_assets import (
    CatalogCroppedImage,
    CatalogVisionInspector,
    FetchedImage,
    ImageFetchError,
    LicenseCode,
    LicensedSourceReceipt,
    PublicLicenseReceipt,
    SafeImageFetcher,
    assess_and_crop_catalog_asset,
    public_license_from_source,
)
from profagent.models import Garment
from profagent.vision import VisionAdapter, VisionUnavailable


CONTROLLED_GARMENT_FILE = ROOT / "data" / "fixtures" / "garments_s16_womenswear.jsonl"
DEFAULT_MANIFEST_PATH = ROOT / "data" / "manifests" / "wardrobe_assets_v2.json"
DEFAULT_SOURCES_PATH = ROOT / "data" / "sources" / "wardrobe_s16_sources.jsonl"
DEFAULT_ASSET_DIRECTORY = ROOT / "data" / "assets" / "wardrobe_licensed_v1"
OPENVERSE_API_BASE = "https://api.openverse.org/v1/images/"
OPENVERSE_RESPONSE_LIMIT = 1024 * 1024
MANIFEST_INPUT_LIMIT = 4 * 1024 * 1024
SOURCE_HISTORY_LIMIT = 8 * 1024 * 1024
MAX_CANDIDATE_BUDGET = 10
MAX_RETRY_BUDGET = 3
MAX_CONCURRENCY = 8
MAX_TOTAL_BUDGET = 70

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
_RETRIABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class IngestionConfig(BaseModel):
    """Closed, bounded configuration for the injectable ingestion boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider: Literal["openverse", "user-owned"]
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
        elif self.source_map is None or self.license_allowlist:
            raise ValueError("invalid_user_owned_inputs")
        if len({self.manifest_path, self.sources_path}) != 2:
            raise ValueError("state_paths_must_be_distinct")
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
    failure_reason: str | None = None
    source: _NormalizedSource | None = None
    fetched: FetchedImage | None = None
    cropped: CatalogCroppedImage | None = None
    public_license: PublicLicenseReceipt | None = None
    existing_item: dict[str, Any] | None = None


@dataclass
class _Counters:
    api_attempts: int = 0
    candidate_attempts: int = 0


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
    provider_item_id = _bounded_text(raw["id"], reason="source_metadata_invalid")
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
    image_url = _validate_https_url(raw["url"], reason="image_url_invalid")
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


async def _search_openverse(
    *,
    api_client: httpx.AsyncClient,
    garment: Garment,
    config: IngestionConfig,
    counters: _Counters,
) -> tuple[Any, ...]:
    query = f"{garment.name} {garment.slot} {garment.search_text}"[:500]
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
    for attempt in range(config.retry_budget + 1):
        counters.api_attempts += 1
        try:
            async with api_client.stream(
                "GET",
                "",
                params={
                    "q": query,
                    "license": license_param,
                    "page_size": config.candidate_budget,
                },
                follow_redirects=False,
            ) as response:
                _validate_official_response(response)
                if response.status_code in _RETRIABLE_STATUS_CODES:
                    retriable = True
                    payload_bytes = b""
                elif response.status_code != 200:
                    return ()
                else:
                    retriable = False
                    content_type = response.headers.get("content-type", "")
                    if content_type.split(";", 1)[0].strip().lower() != "application/json":
                        raise ValueError("provider_response_invalid")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > OPENVERSE_RESPONSE_LIMIT:
                            raise ValueError("provider_response_too_large")
                        chunks.append(chunk)
                    payload_bytes = b"".join(chunks)
        except (httpx.HTTPError, ValueError):
            if attempt < config.retry_budget:
                await asyncio.sleep(0)
                continue
            return ()
        if retriable:
            if attempt < config.retry_budget:
                await asyncio.sleep(0)
                continue
            return ()
        try:
            return _parse_openverse_response(payload_bytes)
        except ValueError:
            return ()
    return ()


def _load_existing_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"manifest_version": "wardrobe_assets_v2", "items": []}
    payload = _load_json(path, limit=MANIFEST_INPUT_LIMIT)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("invalid_asset_manifest")
    items = payload["items"]
    if len(items) > 120 or any(not isinstance(item, dict) for item in items):
        raise ValueError("invalid_asset_manifest")
    garment_ids = [item.get("garment_id") for item in items]
    if any(not isinstance(item_id, str) for item_id in garment_ids):
        raise ValueError("invalid_asset_manifest")
    if len(set(garment_ids)) != len(garment_ids):
        raise ValueError("duplicate_manifest_garment")
    return {**payload, "manifest_version": "wardrobe_assets_v2"}


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
    return {key: receipt.get(key) for key in keys}


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
    raw_candidates = await _search_openverse(
        api_client=api_client,
        garment=garment,
        config=config,
        counters=counters,
    )
    for raw in raw_candidates[: config.candidate_budget]:
        counters.candidate_attempts += 1
        try:
            source = _normalize_openverse_candidate(
                raw, allowlist=config.license_allowlist
            )
        except ValueError:
            continue
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
            assert source.image_url is not None
            fetched = await safe_fetcher.fetch(source.image_url)
            cropped, _, _ = await assess_and_crop_catalog_asset(
                vision=vision,
                fetched_image=fetched,
                allowed_slot=garment.slot,
            )
        except asyncio.CancelledError:
            raise
        except (ImageFetchError, VisionUnavailable, ValueError, TypeError):
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
        cropped, _, _ = await assess_and_crop_catalog_asset(
            vision=vision,
            fetched_image=fetched,
            allowed_slot=garment.slot,
        )
    except asyncio.CancelledError:
        raise
    except (ImageFetchError, VisionUnavailable, ValueError, TypeError):
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
) -> dict[str, Any]:
    return {
        **source.identity(),
        "imported_at": source.source_receipt.imported_at.isoformat(),
        "original_sha256": fetched.original_sha256,
        "processed_sha256": cropped.processed_sha256,
        "processing": "safe_decode_normalize_then_server_crop",
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
            "user_owned_photo"
            if outcome.source.source_receipt.provider == "user_upload"
            else "licensed_photo"
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
            "user_owned_photo"
            if outcome.source is not None
            and outcome.source.source_receipt.provider == "user_upload"
            else "licensed_photo"
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


def _append_source_records(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    existing = b""
    if path.exists():
        if path.stat().st_size > SOURCE_HISTORY_LIMIT:
            raise ValueError("source_history_too_large")
        existing = path.read_bytes()
        if existing and not existing.endswith(b"\n"):
            raise ValueError("invalid_source_history")
    appended = b"".join(
        (
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        for record in records
    )
    if len(existing) + len(appended) > SOURCE_HISTORY_LIMIT:
        raise ValueError("source_history_too_large")
    _atomic_write_bytes(path, existing + appended)


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


async def run_ingestion(
    config: IngestionConfig,
    api_client: httpx.AsyncClient | None,
    safe_fetcher: Any,
    vision: CatalogVisionInspector,
) -> IngestionResult:
    """Run bounded ingestion with all network and Vision dependencies injected."""
    garments = _controlled_garments(config.garment_file)
    if config.provider == "openverse":
        if api_client is None:
            raise ValueError("openverse_client_required")
        _validate_official_client(api_client)

    manifest = (
        {"manifest_version": "wardrobe_assets_v2", "items": []}
        if config.dry_run
        else _load_existing_manifest(config.manifest_path)
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

    if not config.dry_run:
        replacements: dict[str, dict[str, Any]] = {}
        source_records: list[dict[str, Any]] = []
        for outcome in outcomes:
            if outcome.status == "reused":
                assert outcome.existing_item is not None
                replacements[outcome.garment.garment_id] = outcome.existing_item
            elif outcome.status == "ready":
                item, source_record = _ready_manifest_item(outcome)
                assert outcome.cropped is not None
                asset_path = _safe_asset_path(
                    config.asset_directory, item["relative_path"]
                )
                if asset_path is None:
                    raise ValueError("unsafe_asset_path")
                _atomic_write_bytes(asset_path, outcome.cropped.processed_bytes)
                replacements[outcome.garment.garment_id] = item
                source_records.append(source_record)
            else:
                replacements[outcome.garment.garment_id] = (
                    _quarantined_manifest_item(outcome)
                )
        _atomic_write_json(config.manifest_path, _upsert_items(manifest, replacements))
        _append_source_records(config.sources_path, source_records)

    return IngestionResult(
        dry_run=config.dry_run,
        ready_ids=ready_ids,
        reused_ids=reused_ids,
        quarantined_ids=quarantined_ids,
        remaining_ids=remaining_ids,
        api_attempts=counters.api_attempts,
        candidate_attempts=counters.candidate_attempts,
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
    parser.add_argument("--provider", required=True, choices=("openverse", "user-owned"))
    parser.add_argument("--license", type=_parse_license_argument)
    parser.add_argument("--garment-file", required=True, type=Path)
    parser.add_argument("--source-map", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    return parser


async def _run_cli(config: IngestionConfig) -> IngestionResult:
    settings = Settings()
    fetcher = SafeImageFetcher(
        ready_dir=config.asset_directory,
        quarantine_dir=config.asset_directory.parent / "quarantine",
    )
    vision = VisionAdapter(settings)
    if config.provider != "openverse":
        return await run_ingestion(config, None, fetcher, vision)
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
    async with httpx.AsyncClient(
        base_url=OPENVERSE_API_BASE,
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": "ProfAgent-R1-LicensedAssetIngestor/1.0"},
    ) as api_client:
        return await run_ingestion(config, api_client, fetcher, vision)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.provider == "openverse":
        if args.license is None or args.source_map is not None:
            raise SystemExit("openverse requires --license and forbids --source-map")
        license_allowlist = args.license
        source_map = None
    else:
        if args.license is not None or args.source_map is None:
            raise SystemExit("user-owned requires --source-map and forbids --license")
        license_allowlist = ()
        source_map = args.source_map
    config = IngestionConfig(
        provider=args.provider,
        license_allowlist=license_allowlist,
        garment_file=args.garment_file,
        source_map=source_map,
        dry_run=args.dry_run,
        resume=args.resume,
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


__all__ = ["IngestionConfig", "IngestionResult", "main", "run_ingestion"]
