from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
import warnings
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable, Literal

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

from .config import (
    LICENSED_ASSET_MAX_DIMENSION,
    LICENSED_ASSET_MAX_PIXELS,
    LICENSED_ASSET_MAX_REDIRECTS,
    LICENSED_ASSET_MAX_RESPONSE_BYTES,
    LICENSED_ASSET_MIN_DIMENSION,
    Settings,
)


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
    }
)
_MIME_TO_FORMAT = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _nonblank(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.strip():
        raise ValueError("blank_controlled_field")
    return value


def _require_safe_https_url(value: AnyHttpUrl | None) -> AnyHttpUrl | None:
    if value is None:
        return None
    if value.scheme != "https" or value.username is not None or value.password is not None:
        raise ValueError("unsafe_metadata_url")
    return value


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
        return _nonblank(value)

    @field_validator("attribution")
    @classmethod
    def _validate_attribution(cls, value: str) -> str:
        checked = _nonblank(value)
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def _validate_authority(self) -> "LicensedSourceReceipt":
        if self.provider in {"openverse", "partner_api"}:
            if self.license_code not in _PUBLIC_LICENSES or self.source_url is None:
                raise ValueError("incoherent_source_authority")
        elif self.provider == "user_upload":
            if self.license_code != "user-owned":
                raise ValueError("incoherent_source_authority")
            if any(
                value is not None
                for value in (self.source_url, self.creator, self.license_url)
            ):
                raise ValueError("incoherent_source_authority")
        elif self.provider == "cpa_generated":
            if self.license_code != "ai-generated":
                raise ValueError("incoherent_source_authority")
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
        checked = _nonblank(value)
        assert checked is not None
        return checked

    @field_validator("author")
    @classmethod
    def _validate_author(cls, value: str | None) -> str | None:
        return _nonblank(value)

    @model_validator(mode="after")
    def _validate_attribution(self) -> "PublicLicenseReceipt":
        if self.code in _CC_BY_LICENSES and (self.url is None or self.author is None):
            raise ValueError("incomplete_attribution")
        if self.code in {"user-owned", "ai-generated"} and (
            self.url is not None or self.author is not None
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


class ImageFetchError(Exception):
    def __init__(self, reason_code: ImageFetchReasonCode) -> None:
        if reason_code not in _FETCH_REASONS:
            raise ValueError("invalid_reason_code")
        self.reason_code = reason_code
        super().__init__(reason_code)


Resolver = Callable[[str, int], Awaitable[tuple[str, ...]]]


async def _default_resolver(hostname: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ImageFetchError("dns_failure") from exc
    return tuple(dict.fromkeys(str(record[4][0]) for record in records))


class SafeImageFetcher:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        resolver: Resolver | None = None,
        config: SafeImageConfig | None = None,
        ready_dir: Path,
        quarantine_dir: Path,
    ) -> None:
        self._client = client
        self._resolver = resolver or _default_resolver
        self._config = config or SafeImageConfig()
        # Task 2 intentionally writes neither accepted nor rejected bytes. The
        # directories are retained in the constructor contract for the later,
        # atomic ingestion task and must not be created on fetch failure.
        self._ready_dir = Path(ready_dir)
        self._quarantine_dir = Path(quarantine_dir)

    async def fetch(
        self, url: str, expected_sha256: str | None = None
    ) -> FetchedImage:
        current = self._parse_url(url, redirect=False)
        redirects = 0

        while True:
            hostname, port, authority = self._logical_authority(current)
            address = await self._resolve_global(hostname, port)
            pinned_url = self._pin_url(current, address)
            request = self._client.build_request(
                "GET",
                pinned_url,
                headers={"host": authority, "accept": "image/png,image/jpeg,image/webp"},
            )
            request.extensions["sni_hostname"] = hostname.encode("ascii")

            response: httpx.Response | None = None
            try:
                response = await self._client.send(
                    request, stream=True, follow_redirects=False
                )
                if response.status_code in _REDIRECT_STATUSES:
                    if redirects >= self._config.max_redirects:
                        raise ImageFetchError("too_many_redirects")
                    location = response.headers.get("location")
                    if not location:
                        raise ImageFetchError("invalid_redirect")
                    current = self._parse_redirect(current, location)
                    redirects += 1
                    continue
                if response.status_code < 200 or response.status_code >= 300:
                    raise ImageFetchError("http_status")
                return await self._validate_image_response(response, expected_sha256)
            except asyncio.CancelledError:
                raise
            except ImageFetchError:
                raise
            except Exception as exc:
                raise ImageFetchError("network_error") from exc
            finally:
                if response is not None:
                    await response.aclose()

    @staticmethod
    def _parse_url(url: str, *, redirect: bool) -> httpx.URL:
        reason: ImageFetchReasonCode = "invalid_redirect" if redirect else "invalid_url"
        try:
            parsed = httpx.URL(url)
        except Exception as exc:
            raise ImageFetchError(reason) from exc
        if (
            not isinstance(url, str)
            or parsed.scheme != "https"
            or not parsed.host
            or bool(parsed.username)
            or bool(parsed.password)
            or parsed.fragment
        ):
            raise ImageFetchError(reason)
        return parsed

    def _parse_redirect(self, current: httpx.URL, location: str) -> httpx.URL:
        try:
            joined = current.join(location)
        except Exception as exc:
            raise ImageFetchError("invalid_redirect") from exc
        return self._parse_url(str(joined), redirect=True)

    @staticmethod
    def _logical_authority(url: httpx.URL) -> tuple[str, int, str]:
        hostname = url.host
        assert hostname is not None
        port = url.port or 443
        host_header = f"[{hostname}]" if ":" in hostname else hostname
        authority = host_header if port == 443 else f"{host_header}:{port}"
        return hostname, port, authority

    async def _resolve_global(self, hostname: str, port: int) -> str:
        try:
            direct_address = ipaddress.ip_address(hostname)
        except ValueError:
            try:
                addresses = await self._resolver(hostname, port)
            except asyncio.CancelledError:
                raise
            except ImageFetchError:
                raise
            except Exception as exc:
                raise ImageFetchError("dns_failure") from exc
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
            for address in parsed
        ):
            raise ImageFetchError("unsafe_address")
        # Resolver order is retained only after every answer passes the same
        # fail-closed global-address predicate.
        return str(parsed[0])

    @staticmethod
    def _pin_url(logical_url: httpx.URL, address: str) -> httpx.URL:
        return logical_url.copy_with(host=address)

    async def _validate_image_response(
        self, response: httpx.Response, expected_sha256: str | None
    ) -> FetchedImage:
        content_type_header = response.headers.get("content-type")
        if content_type_header is None:
            raise ImageFetchError("missing_mime")
        source_mime = content_type_header.split(";", 1)[0].strip().lower()
        if source_mime not in _MIME_TO_FORMAT:
            raise ImageFetchError("unsupported_mime")

        declared_length = response.headers.get("content-length")
        if declared_length is not None:
            try:
                length = int(declared_length)
            except ValueError as exc:
                raise ImageFetchError("response_too_large") from exc
            if length < 0 or length > self._config.max_response_bytes:
                raise ImageFetchError("response_too_large")

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
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
                    output = BytesIO()
                    normalized.save(output, format="PNG", optimize=False, compress_level=9)
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
]
