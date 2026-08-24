from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import math
import re
import socket
import ssl
import warnings
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Protocol

import httpcore
import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StrictBytes,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from .catalog_product_types import CATALOG_PRODUCT_TYPES
from .config import (
    LICENSED_ASSET_MAX_DIMENSION,
    LICENSED_ASSET_MAX_PIXELS,
    LICENSED_ASSET_MAX_REDIRECTS,
    LICENSED_ASSET_MAX_RESPONSE_BYTES,
    LICENSED_ASSET_MIN_DIMENSION,
    LICENSED_ASSET_CONNECT_TIMEOUT_SECONDS,
    LICENSED_ASSET_DNS_TIMEOUT_SECONDS,
    LICENSED_ASSET_POOL_TIMEOUT_SECONDS,
    LICENSED_ASSET_READ_TIMEOUT_SECONDS,
    LICENSED_ASSET_TOTAL_TIMEOUT_SECONDS,
    LICENSED_ASSET_WRITE_TIMEOUT_SECONDS,
    Settings,
)
from .models import Slot

if TYPE_CHECKING:
    from .vision import CatalogAssetAssessment


LicenseCode = Literal[
    "CC0",
    "PDM",
    "CC-BY-2.0",
    "CC-BY-3.0",
    "CC-BY-4.0",
    "user-owned",
    "ai-generated",
]
SourceKind = Literal[
    "licensed_photo",
    "user_owned_photo",
    "ai_generated_reference",
]
AssetStatus = Literal["ready", "missing", "failed", "quarantined", "takedown"]
ProviderCode = Literal["openverse", "user_upload", "partner_api", "cpa_generated"]
ImageFetchReasonCode = Literal[
    "invalid_url",
    "unsafe_address",
    "dns_failure",
    "too_many_redirects",
    "invalid_redirect",
    "http_status",
    "network_error",
    "missing_mime",
    "unsupported_mime",
    "mime_mismatch",
    "response_too_large",
    "decode_failed",
    "dimension_out_of_range",
    "pixel_limit_exceeded",
    "hash_mismatch",
    "unsupported_content_encoding",
    "timeout",
]


_PUBLIC_LICENSES = frozenset({"CC0", "PDM", "CC-BY-2.0", "CC-BY-3.0", "CC-BY-4.0"})
_CC_BY_LICENSES = frozenset({"CC-BY-2.0", "CC-BY-3.0", "CC-BY-4.0"})
_FETCH_REASONS = frozenset(
    {
        "invalid_url",
        "unsafe_address",
        "dns_failure",
        "too_many_redirects",
        "invalid_redirect",
        "http_status",
        "network_error",
        "missing_mime",
        "unsupported_mime",
        "mime_mismatch",
        "response_too_large",
        "decode_failed",
        "dimension_out_of_range",
        "pixel_limit_exceeded",
        "hash_mismatch",
        "unsupported_content_encoding",
        "timeout",
    }
)
_MIME_TO_FORMAT = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CANONICAL_LICENSES = {
    "CC0": (
        "CC0 1.0 Universal",
        "https://creativecommons.org/publicdomain/zero/1.0/",
        "CC0 1.0",
    ),
    "PDM": (
        "Public Domain Mark 1.0",
        "https://creativecommons.org/publicdomain/mark/1.0/",
        "Public Domain Mark 1.0",
    ),
    "CC-BY-2.0": (
        "Creative Commons Attribution 2.0",
        "https://creativecommons.org/licenses/by/2.0/",
        "CC BY 2.0",
    ),
    "CC-BY-3.0": (
        "Creative Commons Attribution 3.0",
        "https://creativecommons.org/licenses/by/3.0/",
        "CC BY 3.0",
    ),
    "CC-BY-4.0": (
        "Creative Commons Attribution 4.0",
        "https://creativecommons.org/licenses/by/4.0/",
        "CC BY 4.0",
    ),
}
_CONTROLLED_NONPUBLIC = {
    "user-owned": "用户自有图片",
    "ai-generated": "AI 生成参考",
}


def _nonblank(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.strip():
        raise ValueError("blank_controlled_field")
    return value


def _require_safe_https_url(value: AnyHttpUrl | None) -> AnyHttpUrl | None:
    if value is None:
        return None
    if (
        value.scheme != "https"
        or value.username is not None
        or value.password is not None
        or value.fragment is not None
        or value.port == 0
    ):
        raise ValueError("unsafe_metadata_url")
    return value


def _canonical_attribution(code: str, creator: str | None) -> str:
    label = _CANONICAL_LICENSES[code][2]
    return f"{creator} / {label}" if creator is not None else label


def public_license_from_source(
    source: "LicensedSourceReceipt",
) -> "PublicLicenseReceipt":
    """Derive displayable license truth from a validated source receipt."""
    if source.license_code in _PUBLIC_LICENSES:
        name, url, _ = _CANONICAL_LICENSES[source.license_code]
        return PublicLicenseReceipt(
            code=source.license_code,
            name=name,
            url=url,
            author=source.creator,
            attribution=source.attribution,
        )
    controlled = _CONTROLLED_NONPUBLIC[source.license_code]
    return PublicLicenseReceipt(
        code=source.license_code,
        name=controlled,
        url=None,
        author=None,
        attribution=controlled,
    )


def _bounded_identity(value: str | None) -> str | None:
    checked = _nonblank(value)
    if checked is not None and (
        len(checked) > 200
        or checked != checked.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in checked)
    ):
        raise ValueError("invalid_controlled_identity")
    return checked


def _bounded_attribution(value: str) -> str:
    checked = _nonblank(value)
    assert checked is not None
    if (
        len(checked) > 300
        or checked != checked.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in checked)
    ):
        raise ValueError("invalid_controlled_attribution")
    return checked


class LicensedSourceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider: ProviderCode
    source_url: AnyHttpUrl | None
    creator: StrictStr | None
    license_code: LicenseCode
    license_url: AnyHttpUrl | None
    attribution: StrictStr
    imported_at: datetime

    @field_validator("imported_at")
    @classmethod
    def _validate_imported_at(cls, value: datetime) -> datetime:
        # Keep strict datetime semantics without allowing Pydantic to coerce a
        # date/string into provenance authority.
        if value.tzinfo is None:
            raise ValueError("invalid_imported_at")
        return value

    @field_validator("source_url", "license_url")
    @classmethod
    def _validate_url(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        return _require_safe_https_url(value)

    @field_validator("creator")
    @classmethod
    def _validate_creator(cls, value: str | None) -> str | None:
        return _bounded_identity(value)

    @field_validator("attribution")
    @classmethod
    def _validate_attribution(cls, value: str) -> str:
        return _bounded_attribution(value)

    @model_validator(mode="after")
    def _validate_authority(self) -> "LicensedSourceReceipt":
        if self.provider in {"openverse", "partner_api"}:
            if self.license_code not in _PUBLIC_LICENSES or self.source_url is None:
                raise ValueError("incoherent_source_authority")
            _, canonical_url, _ = _CANONICAL_LICENSES[self.license_code]
            if self.license_url is None or str(self.license_url) != canonical_url:
                raise ValueError("noncanonical_license_url")
            if self.license_code in _CC_BY_LICENSES and self.creator is None:
                raise ValueError("incomplete_attribution")
            if self.attribution != _canonical_attribution(
                self.license_code, self.creator
            ):
                raise ValueError("noncanonical_attribution")
        elif self.provider == "user_upload":
            if self.license_code != "user-owned":
                raise ValueError("incoherent_source_authority")
            if any(
                value is not None
                for value in (self.source_url, self.creator, self.license_url)
            ):
                raise ValueError("incoherent_source_authority")
            if self.attribution != _CONTROLLED_NONPUBLIC["user-owned"]:
                raise ValueError("noncanonical_attribution")
        elif self.provider == "cpa_generated":
            if self.license_code != "ai-generated":
                raise ValueError("incoherent_source_authority")
            if self.attribution != _CONTROLLED_NONPUBLIC["ai-generated"]:
                raise ValueError("noncanonical_attribution")
            if any(
                value is not None
                for value in (self.source_url, self.creator, self.license_url)
            ):
                raise ValueError("incoherent_source_authority")

        if self.license_code in _CC_BY_LICENSES and (
            self.creator is None or self.license_url is None
        ):
            raise ValueError("incomplete_attribution")
        return self


class PublicLicenseReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: LicenseCode
    name: StrictStr
    url: AnyHttpUrl | None
    author: StrictStr | None
    attribution: StrictStr

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        return _require_safe_https_url(value)

    @field_validator("name", "attribution")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        return _bounded_attribution(value)

    @field_validator("author")
    @classmethod
    def _validate_author(cls, value: str | None) -> str | None:
        return _bounded_identity(value)

    @model_validator(mode="after")
    def _validate_attribution(self) -> "PublicLicenseReceipt":
        if self.code in _PUBLIC_LICENSES:
            canonical_name, canonical_url, _ = _CANONICAL_LICENSES[self.code]
            if self.name != canonical_name:
                raise ValueError("noncanonical_license_name")
            if self.url is None or str(self.url) != canonical_url:
                raise ValueError("noncanonical_license_url")
            if self.code in _CC_BY_LICENSES and self.author is None:
                raise ValueError("incomplete_attribution")
            if self.attribution != _canonical_attribution(self.code, self.author):
                raise ValueError("noncanonical_attribution")
        else:
            controlled = _CONTROLLED_NONPUBLIC[self.code]
            if (
                self.url is not None
                or self.author is not None
                or self.name != controlled
                or self.attribution != controlled
            ):
                raise ValueError("incoherent_license_authority")
        return self


class ProcessedWardrobeAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    garment_id: StrictStr
    user_id: StrictStr
    source_kind: SourceKind
    status: AssetStatus
    original_sha256: StrictStr
    processed_sha256: StrictStr | None
    relative_path: StrictStr | None
    license: PublicLicenseReceipt

    @field_validator("original_sha256", "processed_sha256")
    @classmethod
    def _validate_hash(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError("invalid_sha256")
        return value

    @field_validator("relative_path")
    @classmethod
    def _validate_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not value
            or "\\" in value
            or ":" in value
            or value.startswith(("/", "//"))
            or re.fullmatch(r"[A-Za-z0-9._/-]+", value) is None
        ):
            raise ValueError("unsafe_asset_path")
        parts = value.split("/")
        path = PurePosixPath(value)
        if any(part in {"", ".", ".."} for part in parts) or path.is_absolute():
            raise ValueError("unsafe_asset_path")
        return value

    @model_validator(mode="after")
    def _validate_state_and_source(self) -> "ProcessedWardrobeAsset":
        expected_codes = {
            "licensed_photo": _PUBLIC_LICENSES,
            "user_owned_photo": frozenset({"user-owned"}),
            "ai_generated_reference": frozenset({"ai-generated"}),
        }
        if self.license.code not in expected_codes[self.source_kind]:
            raise ValueError("incoherent_source_kind")
        if self.status == "ready":
            if self.processed_sha256 is None or self.relative_path is None:
                raise ValueError("incomplete_ready_asset")
        elif self.processed_sha256 is not None or self.relative_path is not None:
            raise ValueError("nonready_asset_is_serveable")
        return self


class SafeImageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_response_bytes: StrictInt = Field(
        default=LICENSED_ASSET_MAX_RESPONSE_BYTES, gt=0, le=25 * 1024 * 1024
    )
    max_pixels: StrictInt = Field(default=LICENSED_ASSET_MAX_PIXELS, gt=0, le=40_000_000)
    min_dimension: StrictInt = Field(default=LICENSED_ASSET_MIN_DIMENSION, gt=0, le=8192)
    max_dimension: StrictInt = Field(default=LICENSED_ASSET_MAX_DIMENSION, gt=0, le=8192)
    max_redirects: StrictInt = Field(default=LICENSED_ASSET_MAX_REDIRECTS, ge=0, le=5)
    dns_timeout_seconds: float = Field(
        default=LICENSED_ASSET_DNS_TIMEOUT_SECONDS, gt=0, le=30
    )
    connect_timeout_seconds: float = Field(
        default=LICENSED_ASSET_CONNECT_TIMEOUT_SECONDS, gt=0, le=30
    )
    read_timeout_seconds: float = Field(
        default=LICENSED_ASSET_READ_TIMEOUT_SECONDS, gt=0, le=30
    )
    write_timeout_seconds: float = Field(
        default=LICENSED_ASSET_WRITE_TIMEOUT_SECONDS, gt=0, le=30
    )
    pool_timeout_seconds: float = Field(
        default=LICENSED_ASSET_POOL_TIMEOUT_SECONDS, gt=0, le=30
    )
    total_timeout_seconds: float = Field(
        default=LICENSED_ASSET_TOTAL_TIMEOUT_SECONDS, gt=0, le=30
    )

    @model_validator(mode="after")
    def _validate_dimensions(self) -> "SafeImageConfig":
        if self.min_dimension > self.max_dimension:
            raise ValueError("invalid_dimension_bounds")
        return self

    @classmethod
    def from_settings(cls, settings: Settings) -> "SafeImageConfig":
        return cls(
            max_response_bytes=settings.effective_licensed_asset_max_response_bytes,
            max_pixels=settings.effective_licensed_asset_max_pixels,
            min_dimension=settings.effective_licensed_asset_min_dimension,
            max_dimension=settings.effective_licensed_asset_max_dimension,
            max_redirects=settings.effective_licensed_asset_max_redirects,
            dns_timeout_seconds=settings.effective_licensed_asset_dns_timeout_seconds,
            connect_timeout_seconds=(
                settings.effective_licensed_asset_connect_timeout_seconds
            ),
            read_timeout_seconds=settings.effective_licensed_asset_read_timeout_seconds,
            write_timeout_seconds=(
                settings.effective_licensed_asset_write_timeout_seconds
            ),
            pool_timeout_seconds=settings.effective_licensed_asset_pool_timeout_seconds,
            total_timeout_seconds=settings.effective_licensed_asset_total_timeout_seconds,
        )


class FetchedImage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    processed_mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    original_sha256: StrictStr
    processed_sha256: StrictStr
    original_bytes: StrictBytes
    processed_bytes: StrictBytes
    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)

    @field_validator("original_sha256", "processed_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("invalid_sha256")
        return value

    @model_validator(mode="after")
    def _validate_content_hashes(self) -> "FetchedImage":
        if hashlib.sha256(self.original_bytes).hexdigest() != self.original_sha256:
            raise ValueError("original_hash_mismatch")
        if hashlib.sha256(self.processed_bytes).hexdigest() != self.processed_sha256:
            raise ValueError("processed_hash_mismatch")
        return self


class CatalogCroppedImage(BaseModel):
    """In-memory local crop; deliberately carries no asset authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    input_sha256: StrictStr
    processed_sha256: StrictStr
    processed_mime_type: Literal["image/png"] = "image/png"
    processed_bytes: StrictBytes
    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)
    object_region: tuple[float, float, float, float]

    @field_validator("input_sha256", "processed_sha256")
    @classmethod
    def _validate_crop_hash(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("invalid_sha256")
        return value

    @model_validator(mode="after")
    def _validate_crop_content_hash(self) -> "CatalogCroppedImage":
        if hashlib.sha256(self.processed_bytes).hexdigest() != self.processed_sha256:
            raise ValueError("processed_hash_mismatch")
        return self


class ImageFetchError(Exception):
    def __init__(self, reason_code: ImageFetchReasonCode) -> None:
        if reason_code not in _FETCH_REASONS:
            raise ValueError("invalid_reason_code")
        self.reason_code = reason_code
        super().__init__(reason_code)


class CatalogVisionInspector(Protocol):
    async def inspect_catalog_asset(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        allowed_slot: Slot,
        expected_product_type: str,
    ) -> tuple[CatalogAssetAssessment, dict[str, Any]]: ...


def _crop_catalog_image(
    fetched_image: FetchedImage,
    object_region: tuple[float, float, float, float],
) -> CatalogCroppedImage:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(fetched_image.processed_bytes)) as image:
                if image.format != _MIME_TO_FORMAT[fetched_image.processed_mime_type]:
                    raise ImageFetchError("mime_mismatch")
                image.load()
                if image.size != (fetched_image.width, fetched_image.height):
                    raise ImageFetchError("decode_failed")
                left, top, right, bottom = object_region
                if (
                    object_region == (0.0, 0.0, 1.0, 1.0)
                    and fetched_image.processed_mime_type == "image/png"
                ):
                    return CatalogCroppedImage(
                        input_sha256=fetched_image.processed_sha256,
                        processed_sha256=fetched_image.processed_sha256,
                        processed_bytes=fetched_image.processed_bytes,
                        width=fetched_image.width,
                        height=fetched_image.height,
                        object_region=object_region,
                    )
                crop_box = (
                    math.floor(left * fetched_image.width),
                    math.floor(top * fetched_image.height),
                    math.ceil(right * fetched_image.width),
                    math.ceil(bottom * fetched_image.height),
                )
                if (
                    crop_box[0] < 0
                    or crop_box[1] < 0
                    or crop_box[2] > fetched_image.width
                    or crop_box[3] > fetched_image.height
                    or crop_box[0] >= crop_box[2]
                    or crop_box[1] >= crop_box[3]
                ):
                    raise ImageFetchError("decode_failed")
                cropped = image.crop(crop_box)
                if cropped.mode not in {"RGB", "RGBA", "L"}:
                    target_mode = "RGBA" if "A" in cropped.getbands() else "RGB"
                    cropped = cropped.convert(target_mode)
                else:
                    cropped = cropped.copy()
                clean_crop = Image.frombytes(
                    cropped.mode, cropped.size, cropped.tobytes()
                )
                output = BytesIO()
                clean_crop.save(
                    output, format="PNG", optimize=False, compress_level=9
                )
                processed_bytes = output.getvalue()
                return CatalogCroppedImage(
                    input_sha256=fetched_image.processed_sha256,
                    processed_sha256=hashlib.sha256(processed_bytes).hexdigest(),
                    processed_bytes=processed_bytes,
                    width=clean_crop.width,
                    height=clean_crop.height,
                    object_region=object_region,
                )
    except ImageFetchError:
        raise
    except (
        KeyError,
        OSError,
        SyntaxError,
        TypeError,
        UnidentifiedImageError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise ImageFetchError("decode_failed") from None


def _local_catalog_quarantine_trace(
    provider_trace: dict[str, Any],
    *,
    reason_code: str = "input_rejected",
) -> dict[str, Any]:
    allowed_models = {"grok-4.6-high", "grok-4.6-build"}
    resolved_model = provider_trace.get("resolved_model")
    model_verified = (
        provider_trace.get("model_verified") is True
        and resolved_model in allowed_models
    )
    latency_ms = provider_trace.get("latency_ms")
    if (
        isinstance(latency_ms, bool)
        or not isinstance(latency_ms, (int, float))
        or not math.isfinite(latency_ms)
        or latency_ms < 0
    ):
        latency_ms = 0
    budget = provider_trace.get("interaction_budget_seconds")
    if (
        isinstance(budget, bool)
        or not isinstance(budget, (int, float))
        or not math.isfinite(budget)
        or not 0 < budget <= 10
    ):
        budget = 10.0
    return {
        "component": "vision",
        "operation": "catalog_asset_assessment",
        "status": "quarantined",
        "reason_code": reason_code,
        "requested_model": "grok4.6",
        "transport_model": "grok-4.6-high",
        "resolved_model": resolved_model if model_verified else None,
        "model_verified": model_verified,
        "schema": "catalog_asset_assessment_v2",
        "image_logged": False,
        "latency_ms": latency_ms,
        "interaction_budget_seconds": budget,
        "assessment_count": 0,
        "quality_issue_count": 0,
    }


async def assess_and_crop_catalog_asset(
    *,
    vision: CatalogVisionInspector,
    fetched_image: FetchedImage,
    allowed_slot: Slot,
    expected_product_type: str,
) -> tuple[CatalogCroppedImage, CatalogAssetAssessment, dict[str, Any]]:
    """Apply Provider advice locally without writing identity or manifest truth."""
    assessment, provider_trace = await vision.inspect_catalog_asset(
        image_bytes=fetched_image.processed_bytes,
        mime_type=fetched_image.processed_mime_type,
        allowed_slot=allowed_slot,
        expected_product_type=expected_product_type,
    )
    from .vision import CatalogAssetAssessment, VisionUnavailable

    if not isinstance(provider_trace, dict):
        provider_trace = {}
    reason_code: str | None = None
    if not isinstance(assessment, CatalogAssetAssessment):
        reason_code = "response_schema_invalid"
    elif assessment.slot != allowed_slot:
        reason_code = "slot_mismatch"
    elif (
        expected_product_type not in CATALOG_PRODUCT_TYPES
        or assessment.product_type != expected_product_type
    ):
        reason_code = "product_type_mismatch"
    elif assessment.audience not in {
        "womenswear",
        "unisex_womenswear_compatible",
    }:
        reason_code = "audience_rejected"
    elif assessment.contains_identifiable_person:
        reason_code = "identifiable_person"
    elif assessment.confidence_band == "low":
        reason_code = "low_confidence"
    elif assessment.quality_issues:
        reason_code = "quality_rejected"
    if reason_code is not None:
        raise VisionUnavailable(
            "catalog_asset_quarantined",
            _local_catalog_quarantine_trace(
                provider_trace,
                reason_code=reason_code,
            ),
        ) from None
    try:
        cropped = _crop_catalog_image(
            fetched_image, assessment.object_region
        )
    except ImageFetchError:
        raise VisionUnavailable(
            "catalog_asset_quarantined",
            _local_catalog_quarantine_trace(provider_trace),
        ) from None
    return cropped, assessment, provider_trace


Resolver = Callable[[str, int], Awaitable[tuple[str, ...]]]
NetworkBackendFactory = Callable[[], httpcore.AsyncNetworkBackend]


async def _default_resolver(hostname: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(
        hostname,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return tuple(dict.fromkeys(str(record[4][0]) for record in records))


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Force TCP to a validated address while retaining logical TLS authority."""

    def __init__(
        self,
        *,
        underlying: httpcore.AsyncNetworkBackend,
        logical_hostname: str,
        validated_address: str,
    ) -> None:
        self._underlying = underlying
        self._logical_hostname = logical_hostname
        self._validated_address = validated_address

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        if host != self._logical_hostname:
            raise RuntimeError("unexpected_logical_authority")
        return await self._underlying.connect_tcp(
            self._validated_address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        raise httpcore.UnsupportedProtocol("unix_socket_forbidden")

    async def sleep(self, seconds: float) -> None:
        await self._underlying.sleep(seconds)


class SafeImageFetcher:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        config: SafeImageConfig | None = None,
        ready_dir: Path,
        quarantine_dir: Path,
        _network_backend_factory: NetworkBackendFactory | None = None,
    ) -> None:
        self._resolver = resolver or _default_resolver
        self._config = config or SafeImageConfig()
        self._network_backend_factory = (
            _network_backend_factory or httpcore.AnyIOBackend
        )
        self._ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        self._ssl_context.check_hostname = True
        self._ssl_context.verify_mode = ssl.CERT_REQUIRED
        # Task 2 intentionally writes neither accepted nor rejected bytes. The
        # directories are retained in the constructor contract for the later,
        # atomic ingestion task and must not be created on fetch failure.
        self._ready_dir = Path(ready_dir)
        self._quarantine_dir = Path(quarantine_dir)

    async def fetch(
        self, url: str, expected_sha256: str | None = None
    ) -> FetchedImage:
        reason: ImageFetchReasonCode | None = None
        try:
            return await asyncio.wait_for(
                self._fetch_inner(url, expected_sha256),
                timeout=self._config.total_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except ImageFetchError as exc:
            reason = exc.reason_code
        except (asyncio.TimeoutError, httpcore.TimeoutException):
            reason = "timeout"
        except Exception:
            reason = "network_error"
        assert reason is not None
        # This raise deliberately occurs outside every except block so an
        # upstream exception cannot survive as cause or context.
        raise ImageFetchError(reason)

    @property
    def max_response_bytes(self) -> int:
        """Expose the already-validated Task 2 byte cap to trusted byte transports."""

        return self._config.max_response_bytes

    def validate_local_bytes(
        self,
        payload: bytes,
        source_mime: str,
        expected_sha256: str | None = None,
    ) -> FetchedImage:
        """Apply the Task 2 local byte/hash/decode/normalization contract."""

        if source_mime not in _MIME_TO_FORMAT:
            raise ImageFetchError("unsupported_mime")
        if len(payload) > self._config.max_response_bytes:
            raise ImageFetchError("response_too_large")
        original_hash = hashlib.sha256(payload).hexdigest()
        if expected_sha256 is not None and original_hash != expected_sha256:
            raise ImageFetchError("hash_mismatch")
        processed, width, height = self._decode_and_normalize(payload, source_mime)
        return FetchedImage(
            source_mime_type=source_mime,
            processed_mime_type="image/png",
            original_sha256=original_hash,
            processed_sha256=hashlib.sha256(processed).hexdigest(),
            original_bytes=payload,
            processed_bytes=processed,
            width=width,
            height=height,
        )

    async def load_user_owned(
        self,
        path: Path,
        expected_sha256: str | None = None,
    ) -> FetchedImage:
        """Safely decode one explicit local user-owned source without networking."""
        reason: ImageFetchReasonCode | None = None
        try:
            return await asyncio.to_thread(
                self._load_user_owned_sync,
                Path(path),
                expected_sha256,
            )
        except asyncio.CancelledError:
            raise
        except ImageFetchError as exc:
            reason = exc.reason_code
        except Exception:
            reason = "decode_failed"
        assert reason is not None
        raise ImageFetchError(reason)

    def _load_user_owned_sync(
        self,
        path: Path,
        expected_sha256: str | None,
    ) -> FetchedImage:
        suffix_to_mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }
        source_mime = suffix_to_mime.get(path.suffix.lower())
        if source_mime is None or path.is_symlink() or not path.is_file():
            raise ImageFetchError("decode_failed")
        try:
            if path.stat().st_size > self._config.max_response_bytes:
                raise ImageFetchError("response_too_large")
            with path.open("rb") as source:
                original = source.read(self._config.max_response_bytes + 1)
        except ImageFetchError:
            raise
        except OSError:
            raise ImageFetchError("decode_failed") from None
        if len(original) > self._config.max_response_bytes:
            raise ImageFetchError("response_too_large")
        original_hash = hashlib.sha256(original).hexdigest()
        if expected_sha256 is not None and original_hash != expected_sha256:
            raise ImageFetchError("hash_mismatch")
        processed, width, height = self._decode_and_normalize(original, source_mime)
        return FetchedImage(
            source_mime_type=source_mime,
            processed_mime_type="image/png",
            original_sha256=original_hash,
            processed_sha256=hashlib.sha256(processed).hexdigest(),
            original_bytes=original,
            processed_bytes=processed,
            width=width,
            height=height,
        )

    async def _fetch_inner(
        self, url: str, expected_sha256: str | None
    ) -> FetchedImage:
        current = self._parse_url(url, redirect=False)
        redirects = 0

        while True:
            hostname, port = self._logical_authority(current)
            address = await self._resolve_global(hostname, port)
            outcome, value = await self._request_hop(
                current=current,
                logical_hostname=hostname,
                validated_address=address,
                expected_sha256=expected_sha256,
            )
            if outcome == "image":
                assert isinstance(value, FetchedImage)
                return value
            assert isinstance(value, str)
            if redirects >= self._config.max_redirects:
                raise ImageFetchError("too_many_redirects")
            current = self._parse_redirect(current, value)
            redirects += 1

    async def _request_hop(
        self,
        *,
        current: httpx.URL,
        logical_hostname: str,
        validated_address: str,
        expected_sha256: str | None,
    ) -> tuple[str, str | FetchedImage]:
        pool = self._new_pool(logical_hostname, validated_address)
        response: httpcore.Response | None = None
        request_url = httpcore.URL(
            scheme=b"https",
            host=current.raw_host,
            port=current.port,
            target=current.raw_path,
        )
        raw_host = current.raw_host
        host_header = b"[" + raw_host + b"]" if b":" in raw_host else raw_host
        if current.port not in {None, 443}:
            host_header += b":" + str(current.port).encode("ascii")
        request = httpcore.Request(
            method=b"GET",
            url=request_url,
            headers=[
                (b"host", host_header),
                (b"accept", b"image/png,image/jpeg,image/webp"),
                (b"accept-encoding", b"identity"),
                (b"connection", b"close"),
            ],
            extensions={
                "timeout": {
                    "connect": self._config.connect_timeout_seconds,
                    "read": self._config.read_timeout_seconds,
                    "write": self._config.write_timeout_seconds,
                    "pool": self._config.pool_timeout_seconds,
                }
            },
        )
        try:
            response = await pool.handle_async_request(request)
            if response.status in _REDIRECT_STATUSES:
                locations = self._header_values(response.headers, b"location")
                if len(locations) != 1:
                    raise ImageFetchError("invalid_redirect")
                try:
                    location = locations[0].decode("ascii")
                except UnicodeDecodeError:
                    raise ImageFetchError("invalid_redirect")
                return "redirect", location
            if response.status < 200 or response.status >= 300:
                raise ImageFetchError("http_status")
            result = await self._validate_image_response(
                response, expected_sha256
            )
            return "image", result
        finally:
            await self._cleanup(response, pool)

    def _new_pool(
        self, logical_hostname: str, validated_address: str
    ) -> httpcore.AsyncConnectionPool:
        underlying = self._network_backend_factory()
        pinned = _PinnedNetworkBackend(
            underlying=underlying,
            logical_hostname=logical_hostname,
            validated_address=validated_address,
        )
        return httpcore.AsyncConnectionPool(
            ssl_context=self._ssl_context,
            proxy=None,
            max_connections=1,
            max_keepalive_connections=0,
            keepalive_expiry=0.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=pinned,
        )

    async def _cleanup(
        self,
        response: httpcore.Response | None,
        pool: httpcore.AsyncConnectionPool,
    ) -> None:
        async def close_all() -> None:
            if response is not None:
                try:
                    await response.aclose()
                except BaseException:
                    pass
            try:
                await pool.aclose()
            except BaseException:
                pass

        cleanup_task = asyncio.create_task(close_all())
        try:
            await asyncio.wait_for(
                asyncio.shield(cleanup_task),
                timeout=self._config.pool_timeout_seconds,
            )
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(
                    asyncio.shield(cleanup_task),
                    timeout=self._config.pool_timeout_seconds,
                )
            except BaseException:
                cleanup_task.cancel()
                await asyncio.gather(cleanup_task, return_exceptions=True)
            raise
        except asyncio.TimeoutError:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)

    @staticmethod
    def _parse_url(url: str, *, redirect: bool) -> httpx.URL:
        reason: ImageFetchReasonCode = "invalid_redirect" if redirect else "invalid_url"
        if not isinstance(url, str) or url != url.strip() or "#" in url:
            raise ImageFetchError(reason)
        authority_match = re.match(r"(?i)^https://([^/?#]*)", url)
        if authority_match is None:
            raise ImageFetchError(reason)
        raw_authority = authority_match.group(1)
        if (
            not raw_authority
            or "@" in raw_authority
            or "%" in raw_authority
            or raw_authority.endswith(":")
        ):
            raise ImageFetchError(reason)
        try:
            parsed = httpx.URL(url)
            port = 443 if parsed.port is None else parsed.port
        except Exception:
            raise ImageFetchError(reason)
        if (
            parsed.scheme != "https"
            or not parsed.host
            or port <= 0
            or port > 65535
        ):
            raise ImageFetchError(reason)
        if re.fullmatch(r"[0-9.]+", parsed.host):
            try:
                ipaddress.ip_address(parsed.host)
            except ValueError:
                raise ImageFetchError(reason)
        if re.fullmatch(r"0[xX][0-9A-Fa-f]+", parsed.host):
            raise ImageFetchError(reason)
        return parsed

    def _parse_redirect(self, current: httpx.URL, location: str) -> httpx.URL:
        if "#" in location:
            raise ImageFetchError("invalid_redirect")
        if re.match(r"(?i)^[a-z][a-z0-9+.-]*://", location):
            return self._parse_url(location, redirect=True)
        if location.startswith("//"):
            authority = location[2:].split("/", 1)[0]
            if "@" in authority or "%" in authority:
                raise ImageFetchError("invalid_redirect")
        try:
            joined = current.join(location)
        except Exception:
            raise ImageFetchError("invalid_redirect")
        return self._parse_url(str(joined), redirect=True)

    @staticmethod
    def _logical_authority(url: httpx.URL) -> tuple[str, int]:
        hostname = url.host
        assert hostname is not None
        port = url.port or 443
        return hostname, port

    async def _resolve_global(self, hostname: str, port: int) -> str:
        try:
            direct_address = ipaddress.ip_address(hostname)
        except ValueError:
            failure_reason: ImageFetchReasonCode | None = None
            try:
                addresses = await asyncio.wait_for(
                    self._resolver(hostname, port),
                    timeout=self._config.dns_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                failure_reason = "timeout"
            except Exception:
                failure_reason = "dns_failure"
            if failure_reason is not None:
                raise ImageFetchError(failure_reason)
        else:
            addresses = (str(direct_address),)
        if not addresses:
            raise ImageFetchError("dns_failure")
        parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        try:
            parsed = [ipaddress.ip_address(address) for address in addresses]
        except ValueError as exc:
            raise ImageFetchError("unsafe_address") from exc
        if any(
            not address.is_global
            or address.is_multicast
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_unspecified
            or getattr(address, "is_site_local", False)
            for address in parsed
        ):
            raise ImageFetchError("unsafe_address")
        # Resolver order is retained only after every answer passes the same
        # fail-closed global-address predicate.
        return str(parsed[0])

    async def _validate_image_response(
        self, response: httpcore.Response, expected_sha256: str | None
    ) -> FetchedImage:
        encodings = self._header_values(response.headers, b"content-encoding")
        if len(encodings) > 1 or (
            len(encodings) == 1 and encodings[0].strip().lower() != b"identity"
        ):
            raise ImageFetchError("unsupported_content_encoding")

        content_types = self._header_values(response.headers, b"content-type")
        if not content_types:
            raise ImageFetchError("missing_mime")
        if len(content_types) != 1:
            raise ImageFetchError("unsupported_mime")
        try:
            source_mime = (
                content_types[0].decode("ascii").split(";", 1)[0].strip().lower()
            )
        except UnicodeDecodeError:
            raise ImageFetchError("unsupported_mime")
        if source_mime not in _MIME_TO_FORMAT:
            raise ImageFetchError("unsupported_mime")

        lengths = self._header_values(response.headers, b"content-length")
        if len(lengths) > 1:
            raise ImageFetchError("response_too_large")
        if lengths:
            try:
                length = int(lengths[0].decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                raise ImageFetchError("response_too_large")
            if length < 0 or length > self._config.max_response_bytes:
                raise ImageFetchError("response_too_large")

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_stream():
            total += len(chunk)
            if total > self._config.max_response_bytes:
                raise ImageFetchError("response_too_large")
            chunks.append(chunk)
        original = b"".join(chunks)
        original_hash = hashlib.sha256(original).hexdigest()
        if expected_sha256 is not None and original_hash != expected_sha256:
            raise ImageFetchError("hash_mismatch")

        processed, width, height = self._decode_and_normalize(original, source_mime)
        return FetchedImage(
            source_mime_type=source_mime,
            processed_mime_type="image/png",
            original_sha256=original_hash,
            processed_sha256=hashlib.sha256(processed).hexdigest(),
            original_bytes=original,
            processed_bytes=processed,
            width=width,
            height=height,
        )

    @staticmethod
    def _header_values(
        headers: list[tuple[bytes, bytes]], name: bytes
    ) -> list[bytes]:
        lowered = name.lower()
        return [value for key, value in headers if key.lower() == lowered]

    def _decode_and_normalize(self, payload: bytes, source_mime: str) -> tuple[bytes, int, int]:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(payload)) as verifier:
                    if verifier.format != _MIME_TO_FORMAT[source_mime]:
                        raise ImageFetchError("mime_mismatch")
                    self._require_complete_container(payload, source_mime)
                    verifier.verify()
                with Image.open(BytesIO(payload)) as image:
                    width, height = image.size
                    if width * height > self._config.max_pixels:
                        raise ImageFetchError("pixel_limit_exceeded")
                    if (
                        width < self._config.min_dimension
                        or height < self._config.min_dimension
                        or width > self._config.max_dimension
                        or height > self._config.max_dimension
                    ):
                        raise ImageFetchError("dimension_out_of_range")
                    image.load()
                    normalized = ImageOps.exif_transpose(image)
                    if normalized.mode not in {"RGB", "RGBA", "L"}:
                        target_mode = "RGBA" if "A" in normalized.getbands() else "RGB"
                        normalized = normalized.convert(target_mode)
                    else:
                        normalized = normalized.copy()
                    width, height = normalized.size
                    pixel_bytes = normalized.tobytes()
                    clean_image = Image.frombytes(
                        normalized.mode, normalized.size, pixel_bytes
                    )
                    output = BytesIO()
                    clean_image.save(
                        output, format="PNG", optimize=False, compress_level=9
                    )
                    return output.getvalue(), width, height
        except ImageFetchError:
            raise
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            SyntaxError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise ImageFetchError("decode_failed") from exc

    @staticmethod
    def _require_complete_container(payload: bytes, source_mime: str) -> None:
        complete = False
        if source_mime == "image/png":
            complete = payload.startswith(b"\x89PNG\r\n\x1a\n") and payload.endswith(
                b"\x00\x00\x00\x00IEND\xaeB`\x82"
            )
        elif source_mime == "image/jpeg":
            complete = payload.startswith(b"\xff\xd8") and payload.endswith(b"\xff\xd9")
        elif source_mime == "image/webp":
            complete = (
                len(payload) >= 12
                and payload[:4] == b"RIFF"
                and payload[8:12] == b"WEBP"
                and int.from_bytes(payload[4:8], "little") + 8 == len(payload)
            )
        if not complete:
            raise ImageFetchError("decode_failed")


__all__ = [
    "AssetStatus",
    "CatalogCroppedImage",
    "CatalogVisionInspector",
    "FetchedImage",
    "ImageFetchError",
    "ImageFetchReasonCode",
    "LicenseCode",
    "LicensedSourceReceipt",
    "ProcessedWardrobeAsset",
    "ProviderCode",
    "PublicLicenseReceipt",
    "SafeImageConfig",
    "SafeImageFetcher",
    "SourceKind",
    "assess_and_crop_catalog_asset",
    "public_license_from_source",
]
