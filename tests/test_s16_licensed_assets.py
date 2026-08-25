from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import importlib
import inspect
import ipaddress
import json
import logging
import math
import os
import ssl
import sys
import traceback
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import ModuleType
from typing import Any, get_args

import httpx
import httpcore
import pytest
from PIL import Image, ImageCms, PngImagePlugin
from pydantic import ValidationError

from profagent.models import Slot


PUBLIC_LICENSES = {
    "CC0",
    "PDM",
    "CC-BY-2.0",
    "CC-BY-3.0",
    "CC-BY-4.0",
}
LICENSE_CODES = PUBLIC_LICENSES | {"user-owned", "ai-generated"}
SOURCE_KINDS = {
    "licensed_photo",
    "user_owned_photo",
    "ai_generated_reference",
}
ASSET_STATUSES = {"ready", "missing", "failed", "quarantined", "takedown"}
PROVIDERS = {"openverse", "user_upload", "partner_api", "cpa_generated"}
APPROVED_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
CANONICAL_LICENSES = {
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
HASH_A = "a" * 64
HASH_B = "b" * 64
IMPORTED_AT = datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def licensed_assets() -> ModuleType:
    """Import lazily so collection is healthy while Task 2 is still RED."""

    try:
        return importlib.import_module("profagent.licensed_assets")
    except ModuleNotFoundError as exc:
        if exc.name == "profagent.licensed_assets":
            pytest.fail(
                "Task 2 RED: profagent.licensed_assets is not implemented",
                pytrace=False,
            )
        raise


def _literal_values(annotation: Any) -> set[str]:
    return {str(value) for value in get_args(annotation)}


def _source_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "provider": "openverse",
        "source_url": "https://images.example/source/asset-1",
        "creator": "Example Creator",
        "license_code": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "Example Creator / CC BY 4.0",
        "imported_at": IMPORTED_AT,
    }
    payload.update(overrides)
    return payload


def _license_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": "CC-BY-4.0",
        "name": "Creative Commons Attribution 4.0",
        "url": "https://creativecommons.org/licenses/by/4.0/",
        "author": "Example Creator",
        "attribution": "Example Creator / CC BY 4.0",
    }
    payload.update(overrides)
    return payload


def _asset_payload(module: ModuleType, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "garment_id": "g051",
        "user_id": "u01",
        "source_kind": "licensed_photo",
        "status": "ready",
        "original_sha256": HASH_A,
        "processed_sha256": HASH_B,
        "relative_path": "wardrobe_licensed_v1/g051.png",
        "license": module.PublicLicenseReceipt.model_validate(_license_payload()),
    }
    payload.update(overrides)
    return payload


def _make_image(
    image_format: str,
    *,
    size: tuple[int, int] = (32, 24),
    exif_orientation: int | None = None,
    include_metadata: bool = False,
) -> bytes:
    image = Image.new("RGB", size, (32, 96, 160))
    output = BytesIO()
    save_kwargs: dict[str, Any] = {}
    if image_format == "PNG" and include_metadata:
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Comment", "must be stripped")
        save_kwargs["pnginfo"] = metadata
    if image_format == "JPEG" and exif_orientation is not None:
        exif = Image.Exif()
        exif[274] = exif_orientation
        exif[315] = "must be stripped"
        save_kwargs["exif"] = exif
    if image_format == "WEBP" and include_metadata:
        save_kwargs["exif"] = b"Exif\x00\x00forbidden metadata"
    image.save(output, format=image_format, **save_kwargs)
    return output.getvalue()


class ResolverSpy:
    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self.mapping = mapping
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, hostname: str, port: int) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        return self.mapping[hostname]


class WireResponse:
    def __init__(
        self,
        status: int,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        extra_headers: tuple[tuple[str, str], ...] = (),
        chunks: tuple[bytes, ...] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = dict(headers or {})
        self.extra_headers = extra_headers
        self.chunks = chunks

    def to_wire_chunks(self) -> list[bytes]:
        headers = {key.lower(): value for key, value in self.headers.items()}
        body_parts = self.chunks if self.chunks is not None else (self.body,)
        if "content-length" not in headers and "transfer-encoding" not in headers:
            if self.chunks is None:
                headers["content-length"] = str(len(self.body))
            else:
                headers["transfer-encoding"] = "chunked"
        reason = {200: "OK", 301: "Moved", 302: "Found", 307: "Redirect", 308: "Redirect"}.get(
            self.status, "Response"
        )
        head = [f"HTTP/1.1 {self.status} {reason}\r\n".encode("ascii")]
        head.extend(f"{key}: {value}\r\n".encode("ascii") for key, value in headers.items())
        head.extend(
            f"{key}: {value}\r\n".encode("ascii")
            for key, value in self.extra_headers
        )
        head.append(b"\r\n")
        if headers.get("transfer-encoding", "").lower() == "chunked":
            encoded_body = [
                f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n"
                for chunk in body_parts
            ]
            return [b"".join(head), *encoded_body, b"0\r\n\r\n"]
        return [b"".join(head), *body_parts]


class FakeNetworkStream(httpcore.AsyncNetworkStream):
    def __init__(self, backend: "TransportSpy", target_host: str, target_port: int) -> None:
        self.backend = backend
        self.target_host = target_host
        self.target_port = target_port
        self._request = bytearray()
        self._response_chunks: list[bytes] = []
        self._closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self.backend.read_timeouts.append(timeout)
        if self.backend.stall_read:
            await asyncio.wait_for(self.backend.read_gate.wait(), timeout=timeout)
        self.backend.read_calls += 1
        if not self._response_chunks:
            return b""
        chunk = self._response_chunks[0]
        result = chunk[:max_bytes]
        remainder = chunk[max_bytes:]
        if remainder:
            self._response_chunks[0] = remainder
        else:
            self._response_chunks.pop(0)
        return result

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.backend.write_timeouts.append(timeout)
        self._request.extend(buffer)
        if b"\r\n\r\n" not in self._request or self._response_chunks:
            return
        head = bytes(self._request).split(b"\r\n\r\n", 1)[0]
        lines = head.split(b"\r\n")
        target = lines[0].split(b" ", 2)[1].decode("ascii")
        headers = {}
        for line in lines[1:]:
            key, value = line.split(b":", 1)
            headers[key.decode("ascii").lower()] = value.strip().decode("ascii")
        host = headers["host"]
        logical_url = f"https://{host}{target}"
        network_host = (
            f"[{self.target_host}]" if ":" in self.target_host else self.target_host
        )
        network_authority = (
            network_host
            if self.target_port == 443
            else f"{network_host}:{self.target_port}"
        )
        self.backend.calls.append(logical_url)
        self.backend.network_calls.append(f"https://{network_authority}{target}")
        self.backend.request_headers.append(headers)
        route = self.backend.routes[logical_url]
        if isinstance(route, Exception):
            raise route
        self._response_chunks = route.to_wire_chunks()

    async def aclose(self) -> None:
        self._closed = True
        self.backend.closed_streams += 1
        if self.backend.close_failure:
            raise RuntimeError("secret-close-failure")

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> "FakeNetworkStream":
        self.backend.tls_calls.append((server_hostname, timeout, ssl_context.verify_mode))
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class TransportSpy(httpcore.AsyncNetworkBackend):
    def __init__(self, routes: dict[str, WireResponse | Exception]) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.network_calls: list[str] = []
        self.connect_calls: list[tuple[str, int, float | None]] = []
        self.tls_calls: list[tuple[str | None, float | None, ssl.VerifyMode]] = []
        self.request_headers: list[dict[str, str]] = []
        self.read_timeouts: list[float | None] = []
        self.write_timeouts: list[float | None] = []
        self.factory_calls = 0
        self.closed_streams = 0
        self.read_calls = 0
        self.stall_connect = False
        self.stall_read = False
        self.close_failure = False
        self.connect_gate = asyncio.Event()
        self.read_gate = asyncio.Event()

    def factory(self) -> "TransportSpy":
        self.factory_calls += 1
        return self

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> FakeNetworkStream:
        self.connect_calls.append((host, port, timeout))
        if self.stall_connect:
            await asyncio.wait_for(self.connect_gate.wait(), timeout=timeout)
        return FakeNetworkStream(self, host, port)

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unix sockets are forbidden")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ChunkedBody:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks

def _response(
    status: int,
    *,
    body: bytes = b"",
    content_type: str | None = None,
    headers: dict[str, str] | None = None,
    stream: ChunkedBody | None = None,
) -> WireResponse:
    response_headers = dict(headers or {})
    if content_type is not None:
        response_headers["content-type"] = content_type
    return WireResponse(
        status,
        body=body,
        headers=response_headers,
        chunks=None if stream is None else stream._chunks,
    )


async def _fetch(
    module: ModuleType,
    *,
    url: str,
    resolver: ResolverSpy,
    transport: TransportSpy,
    ready_dir: Path,
    quarantine_dir: Path,
    expected_sha256: str | None = None,
    config: Any | None = None,
) -> Any:
    fetcher = module.SafeImageFetcher(
        resolver=resolver,
        config=config or module.SafeImageConfig(),
        ready_dir=ready_dir,
        quarantine_dir=quarantine_dir,
        _network_backend_factory=transport.factory,
    )
    return await fetcher.fetch(url, expected_sha256=expected_sha256)


def _assert_content_free_error(
    module: ModuleType,
    exc: BaseException,
    expected_reason: str,
    *forbidden_fragments: str,
) -> None:
    assert isinstance(exc, module.ImageFetchError)
    assert exc.reason_code == expected_reason
    assert str(exc) == expected_reason
    assert set(vars(exc)) <= {"reason_code"}
    rendered = repr(exc) + str(exc)
    for fragment in forbidden_fragments:
        assert fragment not in rendered


def _run_fetch_failure(**kwargs: Any) -> BaseException:
    with pytest.raises(Exception) as caught:
        asyncio.run(_fetch(**kwargs))
    return caught.value


def _assert_no_rejected_payload(
    ready_dir: Path,
    quarantine_dir: Path,
    *,
    payload: bytes,
    url: str,
) -> None:
    assert list(ready_dir.rglob("*")) == []
    for path in quarantine_dir.rglob("*"):
        if not path.is_file():
            continue
        receipt = path.read_bytes()
        assert payload not in receipt
        assert url.encode("utf-8") not in receipt


def test_license_source_and_asset_literals_are_exact(licensed_assets: ModuleType) -> None:
    assert _literal_values(licensed_assets.LicenseCode) == LICENSE_CODES
    assert _literal_values(licensed_assets.SourceKind) == SOURCE_KINDS
    assert _literal_values(licensed_assets.AssetStatus) == ASSET_STATUSES
    provider_annotation = licensed_assets.LicensedSourceReceipt.model_fields[
        "provider"
    ].annotation
    assert _literal_values(provider_annotation) == PROVIDERS


@pytest.mark.parametrize(
    ("provider", "license_code", "source_url", "creator", "license_url", "attribution"),
    [
        (
            "openverse",
            "CC0",
            "https://images.example/cc0",
            None,
            CANONICAL_LICENSES["CC0"][1],
            CANONICAL_LICENSES["CC0"][2],
        ),
        (
            "openverse",
            "PDM",
            "https://images.example/pdm",
            None,
            CANONICAL_LICENSES["PDM"][1],
            CANONICAL_LICENSES["PDM"][2],
        ),
        (
            "partner_api",
            "CC-BY-2.0",
            "https://partner.example/asset",
            "Creator",
            "https://creativecommons.org/licenses/by/2.0/",
            "Creator / CC BY 2.0",
        ),
        (
            "partner_api",
            "CC-BY-3.0",
            "https://partner.example/asset",
            "Creator",
            "https://creativecommons.org/licenses/by/3.0/",
            "Creator / CC BY 3.0",
        ),
        (
            "openverse",
            "CC-BY-4.0",
            "https://images.example/by4",
            "Creator",
            "https://creativecommons.org/licenses/by/4.0/",
            "Creator / CC BY 4.0",
        ),
        ("user_upload", "user-owned", None, None, None, "用户自有图片"),
        ("cpa_generated", "ai-generated", None, None, None, "AI 生成参考"),
    ],
)
def test_license_receipt_accepts_only_coherent_allowlisted_authority(
    licensed_assets: ModuleType,
    provider: str,
    license_code: str,
    source_url: str | None,
    creator: str | None,
    license_url: str | None,
    attribution: str,
) -> None:
    receipt = licensed_assets.LicensedSourceReceipt.model_validate(
        _source_payload(
            provider=provider,
            license_code=license_code,
            source_url=source_url,
            creator=creator,
            license_url=license_url,
            attribution=attribution,
        )
    )
    assert receipt.provider == provider
    assert receipt.license_code == license_code


@pytest.mark.parametrize(
    ("provider", "license_code", "source_url"),
    [
        ("openverse", "user-owned", "https://images.example/asset"),
        ("openverse", "ai-generated", "https://images.example/asset"),
        ("partner_api", "user-owned", "https://partner.example/asset"),
        ("user_upload", "CC0", None),
        ("user_upload", "ai-generated", None),
        ("cpa_generated", "CC-BY-4.0", None),
        ("cpa_generated", "user-owned", None),
        ("user_upload", "user-owned", "https://images.example/not-owned"),
        ("cpa_generated", "ai-generated", "https://images.example/not-cpa"),
    ],
)
def test_license_receipt_rejects_incoherent_provider_source_combinations(
    licensed_assets: ModuleType,
    provider: str,
    license_code: str,
    source_url: str | None,
) -> None:
    with pytest.raises(ValidationError):
        licensed_assets.LicensedSourceReceipt.model_validate(
            _source_payload(
                provider=provider,
                license_code=license_code,
                source_url=source_url,
            )
        )


@pytest.mark.parametrize(
    "license_code",
    [
        "CC-BY-NC-4.0",
        "CC-BY-ND-4.0",
        "CC-BY-NC-ND-4.0",
        "CC-BY-SA-4.0",
        "all-rights-reserved",
        "unknown",
        "",
    ],
)
def test_license_receipt_rejects_nc_nd_sharealike_and_unclear_licenses(
    licensed_assets: ModuleType,
    license_code: str,
) -> None:
    with pytest.raises(ValidationError):
        licensed_assets.LicensedSourceReceipt.model_validate(
            _source_payload(license_code=license_code)
        )


@pytest.mark.parametrize("missing", ["creator", "license_url", "attribution"])
def test_attribution_is_complete_for_every_cc_by_receipt(
    licensed_assets: ModuleType,
    missing: str,
) -> None:
    overrides = {missing: None if missing != "attribution" else "   "}
    with pytest.raises(ValidationError):
        licensed_assets.LicensedSourceReceipt.model_validate(
            _source_payload(**overrides)
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_url": "http://images.example/asset"},
        {"source_url": "https://user:secret@images.example/asset"},
        {"license_url": "http://creativecommons.org/licenses/by/4.0/"},
        {"attribution": "   "},
    ],
)
def test_license_receipt_requires_safe_https_metadata_and_nonblank_attribution(
    licensed_assets: ModuleType,
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        licensed_assets.LicensedSourceReceipt.model_validate(
            _source_payload(**overrides)
        )


@pytest.mark.parametrize("missing", ["author", "url", "attribution"])
def test_attribution_is_complete_for_public_cc_by_license(
    licensed_assets: ModuleType,
    missing: str,
) -> None:
    overrides = {missing: None if missing != "attribution" else ""}
    with pytest.raises(ValidationError):
        licensed_assets.PublicLicenseReceipt.model_validate(
            _license_payload(**overrides)
        )


@pytest.mark.parametrize(
    ("source_kind", "license_code", "url", "author", "attribution"),
    [
        (
            "licensed_photo",
            "CC0",
            CANONICAL_LICENSES["CC0"][1],
            None,
            CANONICAL_LICENSES["CC0"][2],
        ),
        (
            "licensed_photo",
            "PDM",
            CANONICAL_LICENSES["PDM"][1],
            None,
            CANONICAL_LICENSES["PDM"][2],
        ),
        (
            "licensed_photo",
            "CC-BY-4.0",
            "https://creativecommons.org/licenses/by/4.0/",
            "Example Creator",
            "Example Creator / CC BY 4.0",
        ),
        ("user_owned_photo", "user-owned", None, None, "用户自有图片"),
        ("ai_generated_reference", "ai-generated", None, None, "AI 生成参考"),
    ],
)
def test_license_asset_accepts_coherent_public_owned_and_generated_sources(
    licensed_assets: ModuleType,
    source_kind: str,
    license_code: str,
    url: str | None,
    author: str | None,
    attribution: str,
) -> None:
    controlled_name = (
        CANONICAL_LICENSES[license_code][0]
        if license_code in CANONICAL_LICENSES
        else attribution
    )
    receipt = licensed_assets.PublicLicenseReceipt.model_validate(
        _license_payload(
            code=license_code,
            name=controlled_name,
            url=url,
            author=author,
            attribution=attribution,
        )
    )
    asset = licensed_assets.ProcessedWardrobeAsset.model_validate(
        _asset_payload(
            licensed_assets,
            source_kind=source_kind,
            license=receipt,
        )
    )
    assert asset.source_kind == source_kind
    assert asset.license.code == license_code


@pytest.mark.parametrize(
    ("source_kind", "license_code"),
    [
        ("licensed_photo", "user-owned"),
        ("licensed_photo", "ai-generated"),
        ("user_owned_photo", "CC0"),
        ("user_owned_photo", "ai-generated"),
        ("ai_generated_reference", "CC-BY-4.0"),
        ("ai_generated_reference", "user-owned"),
    ],
)
def test_license_asset_rejects_incoherent_source_kind(
    licensed_assets: ModuleType,
    source_kind: str,
    license_code: str,
) -> None:
    controlled_name = {
        "user-owned": "用户自有图片",
        "ai-generated": "AI 生成参考",
    }.get(license_code, CANONICAL_LICENSES.get(license_code, (license_code,))[0])
    controlled_attribution = {
        "user-owned": "用户自有图片",
        "ai-generated": "AI 生成参考",
    }.get(license_code)
    if controlled_attribution is None:
        label = CANONICAL_LICENSES[license_code][2]
        controlled_attribution = f"Example Creator / {label}"
    license_payload = _license_payload(
        code=license_code,
        name=controlled_name,
        url=(
            None
            if license_code in {"user-owned", "ai-generated"}
            else CANONICAL_LICENSES[license_code][1]
        ),
        author=None if license_code in {"user-owned", "ai-generated"} else "Example Creator",
        attribution=controlled_attribution,
    )
    with pytest.raises(ValidationError):
        licensed_assets.ProcessedWardrobeAsset.model_validate(
            _asset_payload(
                licensed_assets,
                source_kind=source_kind,
                license=licensed_assets.PublicLicenseReceipt.model_validate(
                    license_payload
                ),
            )
        )


def test_license_models_are_frozen_extra_forbid_and_type_strict(
    licensed_assets: ModuleType,
) -> None:
    model_payloads = [
        (licensed_assets.LicensedSourceReceipt, _source_payload()),
        (licensed_assets.PublicLicenseReceipt, _license_payload()),
        (
            licensed_assets.ProcessedWardrobeAsset,
            _asset_payload(licensed_assets),
        ),
    ]
    for model, payload in model_payloads:
        assert model.model_config.get("extra") == "forbid"
        assert model.model_config.get("frozen") is True
        instance = model.model_validate(payload)
        with pytest.raises(ValidationError):
            instance.__setattr__(next(iter(model.model_fields)), "changed")
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "provider_payload": "forbidden"})
        missing = dict(payload)
        missing.pop(next(iter(model.model_fields)))
        with pytest.raises(ValidationError):
            model.model_validate(missing)

    with pytest.raises(ValidationError):
        licensed_assets.LicensedSourceReceipt.model_validate(
            _source_payload(imported_at="2026-08-23")
        )
    with pytest.raises(ValidationError):
        licensed_assets.ProcessedWardrobeAsset.model_validate(
            _asset_payload(licensed_assets, garment_id=51)
        )


@pytest.mark.parametrize("status", ["missing", "failed", "quarantined", "takedown"])
def test_takedown_and_nonready_assets_have_no_serveable_path_or_processed_hash(
    licensed_assets: ModuleType,
    status: str,
) -> None:
    asset = licensed_assets.ProcessedWardrobeAsset.model_validate(
        _asset_payload(
            licensed_assets,
            status=status,
            processed_sha256=None,
            relative_path=None,
        )
    )
    assert asset.status == status
    assert asset.processed_sha256 is None
    assert asset.relative_path is None

    with pytest.raises(ValidationError):
        licensed_assets.ProcessedWardrobeAsset.model_validate(
            _asset_payload(licensed_assets, status=status)
        )


@pytest.mark.parametrize(
    ("processed_sha256", "relative_path"),
    [(None, "wardrobe_licensed_v1/g051.png"), (HASH_B, None), (None, None)],
)
def test_ready_asset_requires_processed_hash_safe_path_and_license(
    licensed_assets: ModuleType,
    processed_sha256: str | None,
    relative_path: str | None,
) -> None:
    with pytest.raises(ValidationError):
        licensed_assets.ProcessedWardrobeAsset.model_validate(
            _asset_payload(
                licensed_assets,
                processed_sha256=processed_sha256,
                relative_path=relative_path,
            )
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "https://cdn.example/g051.png",
        "//cdn.example/g051.png",
        "/absolute/g051.png",
        "../outside/g051.png",
        "wardrobe_licensed_v1/../../outside.png",
        "C:/outside/g051.png",
        "C:\\outside\\g051.png",
    ],
)
def test_license_asset_never_exposes_arbitrary_external_browser_url(
    licensed_assets: ModuleType,
    relative_path: str,
) -> None:
    with pytest.raises(ValidationError):
        licensed_assets.ProcessedWardrobeAsset.model_validate(
            _asset_payload(licensed_assets, relative_path=relative_path)
        )
    with pytest.raises(ValidationError):
        licensed_assets.ProcessedWardrobeAsset.model_validate(
            {**_asset_payload(licensed_assets), "image_url": relative_path}
        )


def test_license_fetch_result_cannot_assign_server_authority(
    licensed_assets: ModuleType,
) -> None:
    forbidden = {
        "garment_id",
        "user_id",
        "license",
        "license_code",
        "source_kind",
        "authorization",
        "image_url",
        "source_url",
    }
    assert forbidden.isdisjoint(licensed_assets.FetchedImage.model_fields)
    assert licensed_assets.FetchedImage.model_config.get("extra") == "forbid"
    assert licensed_assets.FetchedImage.model_config.get("frozen") is True
    signature = inspect.signature(licensed_assets.SafeImageFetcher.fetch)
    assert set(signature.parameters) == {"self", "url", "expected_sha256"}


@pytest.mark.parametrize("url", ["http://public.example/a.png", "file:///etc/passwd"])
def test_ssrf_fetch_requires_https_before_dns_or_transport(
    licensed_assets: ModuleType,
    tmp_path: Path,
    url: str,
) -> None:
    resolver = ResolverSpy({})
    transport = TransportSpy({})
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=resolver,
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
    )
    _assert_content_free_error(licensed_assets, exc, "invalid_url", url)
    assert resolver.calls == []
    assert transport.calls == []


def test_ssrf_fetch_rejects_userinfo_before_dns_or_transport(
    licensed_assets: ModuleType,
    tmp_path: Path,
) -> None:
    url = "https://user:secret@public.example/a.png"
    resolver = ResolverSpy({})
    transport = TransportSpy({})
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=resolver,
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
    )
    _assert_content_free_error(licensed_assets, exc, "invalid_url", url, "secret")
    assert resolver.calls == []
    assert transport.calls == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.1.2",
        "192.168.2.3",
        "169.254.10.2",
        "224.0.0.1",
        "192.0.2.1",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "2001:db8::1",
        "::",
    ],
)
def test_ssrf_private_reserved_and_non_global_addresses_never_reach_transport(
    licensed_assets: ModuleType,
    tmp_path: Path,
    address: str,
) -> None:
    parsed_address = ipaddress.ip_address(address)
    assert not parsed_address.is_global or parsed_address.is_multicast
    url = "https://private.example/a.png"
    resolver = ResolverSpy({"private.example": (address,)})
    transport = TransportSpy({})
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=resolver,
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
    )
    _assert_content_free_error(licensed_assets, exc, "unsafe_address", url, address)
    assert resolver.calls == [("private.example", 443)]
    assert transport.calls == []


def test_ssrf_mixed_global_and_private_dns_answer_fails_closed(
    licensed_assets: ModuleType,
    tmp_path: Path,
) -> None:
    url = "https://mixed.example/a.png"
    resolver = ResolverSpy({"mixed.example": ("93.184.216.34", "127.0.0.1")})
    transport = TransportSpy({})
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=resolver,
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
    )
    _assert_content_free_error(licensed_assets, exc, "unsafe_address", url)
    assert resolver.calls == [("mixed.example", 443)]
    assert transport.calls == []


def test_ssrf_redirect_to_private_is_blocked_before_second_request(
    licensed_assets: ModuleType,
    tmp_path: Path,
) -> None:
    original = "https://public.example/start"
    private = "https://private.example/secret.png"
    resolver = ResolverSpy(
        {
            "public.example": ("93.184.216.34",),
            "private.example": ("10.0.0.7",),
        }
    )
    transport = TransportSpy(
        {original: _response(302, headers={"location": private})}
    )
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=original,
        resolver=resolver,
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
    )
    _assert_content_free_error(licensed_assets, exc, "unsafe_address", private)
    assert resolver.calls == [
        ("public.example", 443),
        ("private.example", 443),
    ]
    assert transport.calls == [original]


def test_ssrf_validates_every_redirect_hop_and_honors_explicit_port(
    licensed_assets: ModuleType,
    tmp_path: Path,
) -> None:
    png = _make_image("PNG")
    first = "https://one.example/start"
    second = "https://two.example:8443/next"
    final = "https://three.example/final.png"
    resolver = ResolverSpy(
        {
            "one.example": ("93.184.216.34",),
            "two.example": ("8.8.8.8",),
            "three.example": ("1.1.1.1",),
        }
    )
    transport = TransportSpy(
        {
            first: _response(302, headers={"location": second}),
            second: _response(307, headers={"location": final}),
            final: _response(200, body=png, content_type="image/png"),
        }
    )
    result = asyncio.run(
        _fetch(
            module=licensed_assets,
            url=first,
            resolver=resolver,
            transport=transport,
            ready_dir=tmp_path / "ready",
            quarantine_dir=tmp_path / "quarantine",
        )
    )
    assert isinstance(result, licensed_assets.FetchedImage)
    assert resolver.calls == [
        ("one.example", 443),
        ("two.example", 8443),
        ("three.example", 443),
    ]
    assert transport.calls == [first, second, final]
    assert [httpx.URL(url).host for url in transport.network_calls] == [
        "93.184.216.34",
        "8.8.8.8",
        "1.1.1.1",
    ]


def test_ssrf_transport_is_pinned_to_validated_ip_to_prevent_dns_rebinding(
    licensed_assets: ModuleType,
    tmp_path: Path,
) -> None:
    body = _make_image("PNG")
    logical_url = "https://rebind.example/image.png"
    resolver = ResolverSpy({"rebind.example": ("93.184.216.34",)})
    transport = TransportSpy(
        {logical_url: _response(200, body=body, content_type="image/png")}
    )
    result = asyncio.run(
        _fetch(
            module=licensed_assets,
            url=logical_url,
            resolver=resolver,
            transport=transport,
            ready_dir=tmp_path / "ready",
            quarantine_dir=tmp_path / "quarantine",
        )
    )
    assert isinstance(result, licensed_assets.FetchedImage)
    assert resolver.calls == [("rebind.example", 443)]
    assert transport.calls == [logical_url]
    assert httpx.URL(transport.network_calls[0]).host == "93.184.216.34"


def test_ssrf_redirect_count_is_bounded(licensed_assets: ModuleType, tmp_path: Path) -> None:
    urls = [f"https://hop{i}.example/image" for i in range(4)]
    resolver = ResolverSpy(
        {f"hop{i}.example": ("93.184.216.34",) for i in range(4)}
    )
    transport = TransportSpy(
        {
            urls[0]: _response(302, headers={"location": urls[1]}),
            urls[1]: _response(302, headers={"location": urls[2]}),
            urls[2]: _response(302, headers={"location": urls[3]}),
        }
    )
    config = licensed_assets.SafeImageConfig(max_redirects=2)
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=urls[0],
        resolver=resolver,
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
        config=config,
    )
    _assert_content_free_error(licensed_assets, exc, "too_many_redirects", *urls)
    assert transport.calls == urls[:3]
    assert resolver.calls == [(f"hop{i}.example", 443) for i in range(3)]


@pytest.mark.parametrize(
    ("content_type", "body", "reason"),
    [
        (None, _make_image("PNG"), "missing_mime"),
        ("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "unsupported_mime"),
        ("image/gif", b"GIF89a", "unsupported_mime"),
        ("text/html", b"<html>not an image</html>", "unsupported_mime"),
        ("image/png", _make_image("JPEG"), "mime_mismatch"),
        ("image/jpeg", _make_image("WEBP"), "mime_mismatch"),
        ("image/png", b"<html>spoofed</html>", "decode_failed"),
    ],
    ids=[
        "missing-content-type",
        "svg",
        "gif",
        "html",
        "jpeg-declared-png",
        "webp-declared-jpeg",
        "html-declared-png",
    ],
)
def test_mime_missing_unsupported_spoofed_or_mismatched_is_rejected(
    licensed_assets: ModuleType,
    tmp_path: Path,
    content_type: str | None,
    body: bytes,
    reason: str,
) -> None:
    url = "https://public.example/image"
    resolver = ResolverSpy({"public.example": ("93.184.216.34",)})
    transport = TransportSpy(
        {url: _response(200, body=body, content_type=content_type)}
    )
    ready = tmp_path / "ready"
    quarantine = tmp_path / "quarantine"
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=resolver,
        transport=transport,
        ready_dir=ready,
        quarantine_dir=quarantine,
    )
    _assert_content_free_error(licensed_assets, exc, reason, url, "spoofed")
    _assert_no_rejected_payload(ready, quarantine, payload=body, url=url)


@pytest.mark.parametrize(
    ("image_format", "content_type"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_mime_controlled_png_jpeg_and_webp_are_accepted_and_hashed(
    licensed_assets: ModuleType,
    tmp_path: Path,
    image_format: str,
    content_type: str,
) -> None:
    body = _make_image(image_format)
    url = f"https://public.example/image.{image_format.lower()}"
    resolver = ResolverSpy({"public.example": ("93.184.216.34",)})
    transport = TransportSpy(
        {url: _response(200, body=body, content_type=content_type)}
    )
    result = asyncio.run(
        _fetch(
            module=licensed_assets,
            url=url,
            resolver=resolver,
            transport=transport,
            ready_dir=tmp_path / "ready",
            quarantine_dir=tmp_path / "quarantine",
        )
    )
    assert result.source_mime_type == content_type
    assert result.processed_mime_type in APPROVED_MIME_TYPES
    assert result.original_sha256 == hashlib.sha256(body).hexdigest()
    assert result.processed_sha256 == hashlib.sha256(result.processed_bytes).hexdigest()
    assert result.original_bytes == body
    assert result.width == 32
    assert result.height == 24


def test_content_length_and_stream_overflow_are_rejected_without_ready_bytes(
    licensed_assets: ModuleType,
    tmp_path: Path,
) -> None:
    config = licensed_assets.SafeImageConfig(max_response_bytes=128)
    url = "https://public.example/oversize.png"
    resolver = ResolverSpy({"public.example": ("93.184.216.34",)})
    ready = tmp_path / "ready"
    quarantine = tmp_path / "quarantine"

    header_transport = TransportSpy(
        {
            url: _response(
                200,
                body=b"x",
                content_type="image/png",
                headers={"content-length": "129"},
            )
        }
    )
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=resolver,
        transport=header_transport,
        ready_dir=ready,
        quarantine_dir=quarantine,
        config=config,
    )
    _assert_content_free_error(licensed_assets, exc, "response_too_large", url)

    resolver.calls.clear()
    streamed = b"a" * 80 + b"b" * 80
    stream_transport = TransportSpy(
        {
            url: _response(
                200,
                content_type="image/png",
                stream=ChunkedBody((b"a" * 80, b"b" * 80)),
            )
        }
    )
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=resolver,
        transport=stream_transport,
        ready_dir=ready,
        quarantine_dir=quarantine,
        config=config,
    )
    _assert_content_free_error(licensed_assets, exc, "response_too_large", url)
    _assert_no_rejected_payload(ready, quarantine, payload=streamed, url=url)


@pytest.mark.parametrize(
    "body",
    [b"not an image", _make_image("PNG")[:-12]],
    ids=["undecodable", "truncated-png"],
)
def test_truncated_or_undecodable_images_fail_closed(
    licensed_assets: ModuleType,
    tmp_path: Path,
    body: bytes,
) -> None:
    url = "https://public.example/broken.png"
    resolver = ResolverSpy({"public.example": ("93.184.216.34",)})
    transport = TransportSpy(
        {url: _response(200, body=body, content_type="image/png")}
    )
    ready = tmp_path / "ready"
    quarantine = tmp_path / "quarantine"
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=resolver,
        transport=transport,
        ready_dir=ready,
        quarantine_dir=quarantine,
    )
    _assert_content_free_error(licensed_assets, exc, "decode_failed", url)
    _assert_no_rejected_payload(ready, quarantine, payload=body, url=url)


@pytest.mark.parametrize(
    ("size", "config_overrides", "reason"),
    [
        ((8, 8), {"min_dimension": 16}, "dimension_out_of_range"),
        ((65, 32), {"max_dimension": 64}, "dimension_out_of_range"),
        ((100, 100), {"max_pixels": 9_999}, "pixel_limit_exceeded"),
    ],
)
def test_decompression_pixel_and_dimension_bounds_are_enforced(
    licensed_assets: ModuleType,
    tmp_path: Path,
    size: tuple[int, int],
    config_overrides: dict[str, int],
    reason: str,
) -> None:
    body = _make_image("PNG", size=size)
    url = "https://public.example/bounds.png"
    resolver = ResolverSpy({"public.example": ("93.184.216.34",)})
    transport = TransportSpy(
        {url: _response(200, body=body, content_type="image/png")}
    )
    config = licensed_assets.SafeImageConfig(**config_overrides)
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=resolver,
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
        config=config,
    )
    _assert_content_free_error(licensed_assets, exc, reason, url)


def test_hash_mismatch_is_quarantined_without_writing_rejected_bytes_ready(
    licensed_assets: ModuleType,
    tmp_path: Path,
) -> None:
    body = _make_image("PNG")
    url = "https://public.example/hash.png"
    resolver = ResolverSpy({"public.example": ("93.184.216.34",)})
    transport = TransportSpy(
        {url: _response(200, body=body, content_type="image/png")}
    )
    ready = tmp_path / "ready"
    quarantine = tmp_path / "quarantine"
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=resolver,
        transport=transport,
        ready_dir=ready,
        quarantine_dir=quarantine,
        expected_sha256="0" * 64,
    )
    _assert_content_free_error(licensed_assets, exc, "hash_mismatch", url)
    _assert_no_rejected_payload(ready, quarantine, payload=body, url=url)


def test_exif_orientation_is_normalized_metadata_stripped_and_bytes_deterministic(
    licensed_assets: ModuleType,
    tmp_path: Path,
) -> None:
    body = _make_image(
        "JPEG", size=(12, 20), exif_orientation=6, include_metadata=True
    )
    url = "https://public.example/oriented.jpg"

    def fetch_once(suffix: str) -> Any:
        return asyncio.run(
            _fetch(
                module=licensed_assets,
                url=url,
                resolver=ResolverSpy({"public.example": ("93.184.216.34",)}),
                transport=TransportSpy(
                    {url: _response(200, body=body, content_type="image/jpeg")}
                ),
                ready_dir=tmp_path / f"ready-{suffix}",
                quarantine_dir=tmp_path / f"quarantine-{suffix}",
            )
        )

    first = fetch_once("first")
    second = fetch_once("second")
    assert first.processed_bytes == second.processed_bytes
    assert first.processed_sha256 == second.processed_sha256
    assert (first.width, first.height) == (20, 12)
    with Image.open(BytesIO(first.processed_bytes)) as processed:
        processed.load()
        assert processed.size == (20, 12)
        assert len(processed.getexif()) == 0
        assert "comment" not in {key.lower() for key in processed.info}
        assert "exif" not in {key.lower() for key in processed.info}


def test_png_text_metadata_is_stripped(licensed_assets: ModuleType, tmp_path: Path) -> None:
    body = _make_image("PNG", include_metadata=True)
    url = "https://public.example/metadata.png"
    result = asyncio.run(
        _fetch(
            module=licensed_assets,
            url=url,
            resolver=ResolverSpy({"public.example": ("93.184.216.34",)}),
            transport=TransportSpy(
                {url: _response(200, body=body, content_type="image/png")}
            ),
            ready_dir=tmp_path / "ready",
            quarantine_dir=tmp_path / "quarantine",
        )
    )
    with Image.open(BytesIO(result.processed_bytes)) as processed:
        processed.load()
        assert "Comment" not in processed.info


def test_network_exception_reason_is_content_free_and_no_payload_is_persisted(
    licensed_assets: ModuleType,
    tmp_path: Path,
) -> None:
    url = "https://public.example/secret-token/image.png"
    secret = "upstream-secret-response"
    resolver = ResolverSpy({"public.example": ("93.184.216.34",)})
    transport = TransportSpy(
        {
            url: httpx.ConnectError(
                f"connect failed {secret}", request=httpx.Request("GET", url)
            )
        }
    )
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=resolver,
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
    )
    _assert_content_free_error(licensed_assets, exc, "network_error", url, secret)
    _assert_no_rejected_payload(
        tmp_path / "ready",
        tmp_path / "quarantine",
        payload=secret.encode(),
        url=url,
    )


def test_safe_image_config_defaults_are_positive_and_bounded(
    licensed_assets: ModuleType,
) -> None:
    config = licensed_assets.SafeImageConfig()
    assert config.model_config.get("extra") == "forbid"
    assert config.model_config.get("frozen") is True
    assert 0 < config.max_response_bytes <= 25 * 1024 * 1024
    assert 0 < config.max_pixels <= 40_000_000
    assert 0 < config.min_dimension <= config.max_dimension <= 8192
    assert 0 <= config.max_redirects <= 5

    for field, value in {
        "max_response_bytes": 0,
        "max_pixels": 0,
        "min_dimension": 0,
        "max_dimension": 0,
        "max_redirects": -1,
    }.items():
        with pytest.raises(ValidationError):
            licensed_assets.SafeImageConfig(**{field: value})


def test_error_reason_code_is_closed_and_contains_no_provider_content(
    licensed_assets: ModuleType,
) -> None:
    allowed_reasons = {
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
    assert _literal_values(licensed_assets.ImageFetchReasonCode) == allowed_reasons
    error = licensed_assets.ImageFetchError("unsafe_address")
    _assert_content_free_error(licensed_assets, error, "unsafe_address")
    with pytest.raises((TypeError, ValueError, ValidationError)):
        licensed_assets.ImageFetchError("https://private.example/secret")


@pytest.mark.parametrize(
    "overrides",
    [
        {"license_url": CANONICAL_LICENSES["CC-BY-3.0"][1]},
        {"license_url": "https://license.example/by/4.0/"},
        {"attribution": "Unrelated person / CC BY 4.0"},
        {"creator": "x" * 201, "attribution": f"{'x' * 201} / CC BY 4.0"},
    ],
)
def test_source_receipt_rejects_noncanonical_license_authority(
    licensed_assets: ModuleType, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        licensed_assets.LicensedSourceReceipt.model_validate(
            _source_payload(**overrides)
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "CC BY 4.0"},
        {"url": CANONICAL_LICENSES["CC-BY-3.0"][1]},
        {"url": "https://license.example/by/4.0/"},
        {"attribution": "Unrelated person / CC BY 4.0"},
        {"author": "x" * 201, "attribution": f"{'x' * 201} / CC BY 4.0"},
    ],
)
def test_public_receipt_rejects_noncanonical_license_authority(
    licensed_assets: ModuleType, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        licensed_assets.PublicLicenseReceipt.model_validate(
            _license_payload(**overrides)
        )


@pytest.mark.parametrize(
    ("code", "name", "attribution", "url", "author"),
    [
        ("user-owned", "user-owned", "用户自有图片", None, None),
        ("user-owned", "用户自有图片", "not controlled", None, None),
        (
            "ai-generated",
            "AI 生成参考",
            "AI 生成参考",
            "https://images.example/not-authority",
            None,
        ),
        ("ai-generated", "ai-generated", "AI 生成参考", None, None),
    ],
)
def test_owned_and_generated_receipts_use_exact_server_owned_text(
    licensed_assets: ModuleType,
    code: str,
    name: str,
    attribution: str,
    url: str | None,
    author: str | None,
) -> None:
    with pytest.raises(ValidationError):
        licensed_assets.PublicLicenseReceipt.model_validate(
            _license_payload(
                code=code,
                name=name,
                attribution=attribution,
                url=url,
                author=author,
            )
        )


def test_fetcher_public_api_has_no_arbitrary_http_client(
    licensed_assets: ModuleType,
) -> None:
    parameters = inspect.signature(licensed_assets.SafeImageFetcher).parameters
    assert "client" not in parameters
    assert "_network_backend_factory" in parameters


def test_httpcore_transport_pins_ip_preserves_sni_and_separates_redirect_pools(
    licensed_assets: ModuleType, tmp_path: Path
) -> None:
    body = _make_image("PNG")
    first = "https://one.example/start"
    final = "https://two.example/final.png"
    transport = TransportSpy(
        {
            first: _response(302, headers={"location": final}),
            final: _response(200, body=body, content_type="image/png"),
        }
    )
    result = asyncio.run(
        _fetch(
            module=licensed_assets,
            url=first,
            resolver=ResolverSpy(
                {
                    "one.example": ("93.184.216.34",),
                    "two.example": ("93.184.216.34",),
                }
            ),
            transport=transport,
            ready_dir=tmp_path / "ready",
            quarantine_dir=tmp_path / "quarantine",
        )
    )
    assert isinstance(result, licensed_assets.FetchedImage)
    assert transport.factory_calls == 2
    assert [(host, port) for host, port, _ in transport.connect_calls] == [
        ("93.184.216.34", 443),
        ("93.184.216.34", 443),
    ]
    assert [call[0] for call in transport.tls_calls] == ["one.example", "two.example"]
    assert all(call[2] == ssl.CERT_REQUIRED for call in transport.tls_calls)
    assert all(
        set(headers) == {"host", "accept", "accept-encoding", "connection"}
        for headers in transport.request_headers
    )


def test_pool_configuration_is_direct_bounded_and_proxy_free(
    licensed_assets: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_pool = licensed_assets.httpcore.AsyncConnectionPool
    observed: list[dict[str, Any]] = []

    def recording_pool(*args: Any, **kwargs: Any) -> Any:
        observed.append(dict(kwargs))
        return real_pool(*args, **kwargs)

    monkeypatch.setattr(
        licensed_assets.httpcore, "AsyncConnectionPool", recording_pool
    )
    url = "https://public.example/pool.png"
    result = asyncio.run(
        _fetch(
            module=licensed_assets,
            url=url,
            resolver=ResolverSpy({"public.example": ("93.184.216.34",)}),
            transport=TransportSpy(
                {url: _response(200, body=_make_image("PNG"), content_type="image/png")}
            ),
            ready_dir=tmp_path / "ready",
            quarantine_dir=tmp_path / "quarantine",
        )
    )
    assert isinstance(result, licensed_assets.FetchedImage)
    assert len(observed) == 1
    assert observed[0]["proxy"] is None
    assert observed[0]["max_connections"] == 1
    assert observed[0]["max_keepalive_connections"] == 0
    assert observed[0]["keepalive_expiry"] == 0.0
    assert observed[0]["http1"] is True
    assert observed[0]["http2"] is False
    assert observed[0]["retries"] == 0


def test_httpcore_transport_pins_ipv6_and_preserves_logical_sni(
    licensed_assets: ModuleType, tmp_path: Path
) -> None:
    url = "https://ipv6.example:8443/image.png"
    transport = TransportSpy(
        {url: _response(200, body=_make_image("PNG"), content_type="image/png")}
    )
    result = asyncio.run(
        _fetch(
            module=licensed_assets,
            url=url,
            resolver=ResolverSpy({"ipv6.example": ("2606:4700:4700::1111",)}),
            transport=transport,
            ready_dir=tmp_path / "ready",
            quarantine_dir=tmp_path / "quarantine",
        )
    )
    assert isinstance(result, licensed_assets.FetchedImage)
    assert transport.connect_calls[0][:2] == ("2606:4700:4700::1111", 8443)
    assert transport.tls_calls[0][0] == "ipv6.example"


@pytest.mark.parametrize(
    ("url", "mapping", "resolver_calls"),
    [
        ("https://[fec0::1]/image.png", {}, []),
        (
            "https://site-local.example/image.png",
            {"site-local.example": ("fec0::1",)},
            [("site-local.example", 443)],
        ),
    ],
)
def test_ipv6_site_local_is_rejected_before_transport(
    licensed_assets: ModuleType,
    tmp_path: Path,
    url: str,
    mapping: dict[str, tuple[str, ...]],
    resolver_calls: list[tuple[str, int]],
) -> None:
    resolver = ResolverSpy(mapping)
    transport = TransportSpy({})
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=resolver,
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
    )
    _assert_content_free_error(licensed_assets, exc, "unsafe_address", url)
    assert resolver.calls == resolver_calls
    assert transport.connect_calls == []


def test_accept_encoding_identity_and_compressed_response_rejected_before_body(
    licensed_assets: ModuleType, tmp_path: Path
) -> None:
    url = "https://public.example/compressed.png"
    compressed = gzip.compress(b"x" * 2_000_000)
    transport = TransportSpy(
        {
            url: _response(
                200,
                body=compressed,
                content_type="image/png",
                headers={"content-encoding": "gzip"},
            )
        }
    )
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=ResolverSpy({"public.example": ("93.184.216.34",)}),
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
        config=licensed_assets.SafeImageConfig(max_response_bytes=4096),
    )
    _assert_content_free_error(
        licensed_assets, exc, "unsupported_content_encoding", url
    )
    assert transport.request_headers[0]["accept-encoding"] == "identity"
    assert transport.read_calls == 1


@pytest.mark.parametrize(
    "content_encoding",
    ["br", "deflate", "identity, gzip", "identity, identity", ""],
)
def test_nonidentity_or_multiple_content_encoding_fails_closed(
    licensed_assets: ModuleType,
    tmp_path: Path,
    content_encoding: str,
) -> None:
    url = "https://public.example/encoded.png"
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=ResolverSpy({"public.example": ("93.184.216.34",)}),
        transport=TransportSpy(
            {
                url: _response(
                    200,
                    body=_make_image("PNG"),
                    content_type="image/png",
                    headers={"content-encoding": content_encoding},
                )
            }
        ),
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
    )
    _assert_content_free_error(
        licensed_assets, exc, "unsupported_content_encoding", url
    )


def test_duplicate_identity_content_encoding_headers_fail_closed(
    licensed_assets: ModuleType,
    tmp_path: Path,
) -> None:
    url = "https://public.example/duplicate-encoding.png"
    transport = TransportSpy(
        {
            url: WireResponse(
                200,
                body=_make_image("PNG"),
                headers={
                    "content-type": "image/png",
                    "content-encoding": "identity",
                },
                extra_headers=(("content-encoding", "identity"),),
            )
        }
    )
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=ResolverSpy({"public.example": ("93.184.216.34",)}),
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
    )
    _assert_content_free_error(
        licensed_assets, exc, "unsupported_content_encoding", url
    )


def test_safe_image_config_has_bounded_server_owned_timeouts(
    licensed_assets: ModuleType,
) -> None:
    config = licensed_assets.SafeImageConfig()
    for field in (
        "dns_timeout_seconds",
        "connect_timeout_seconds",
        "read_timeout_seconds",
        "write_timeout_seconds",
        "pool_timeout_seconds",
        "total_timeout_seconds",
    ):
        assert 0 < getattr(config, field) <= 30
        with pytest.raises(ValidationError):
            licensed_assets.SafeImageConfig(**{field: 0.0})
    assert config.total_timeout_seconds >= max(
        config.dns_timeout_seconds,
        config.connect_timeout_seconds,
        config.read_timeout_seconds,
    )


def test_safe_image_config_reads_only_bounded_server_settings(
    licensed_assets: ModuleType, tmp_path: Path
) -> None:
    from profagent.config import Settings

    settings = Settings(
        root_dir=tmp_path,
        licensed_asset_dns_timeout_seconds=0.25,
        licensed_asset_connect_timeout_seconds=0.5,
        licensed_asset_read_timeout_seconds=0.75,
        licensed_asset_write_timeout_seconds=1.0,
        licensed_asset_pool_timeout_seconds=1.25,
        licensed_asset_total_timeout_seconds=1.5,
    )
    config = licensed_assets.SafeImageConfig.from_settings(settings)
    assert (
        config.dns_timeout_seconds,
        config.connect_timeout_seconds,
        config.read_timeout_seconds,
        config.write_timeout_seconds,
        config.pool_timeout_seconds,
        config.total_timeout_seconds,
    ) == (0.25, 0.5, 0.75, 1.0, 1.25, 1.5)

    bounded = licensed_assets.SafeImageConfig.from_settings(
        Settings(
            root_dir=tmp_path,
            licensed_asset_dns_timeout_seconds=999.0,
            licensed_asset_connect_timeout_seconds=999.0,
            licensed_asset_read_timeout_seconds=999.0,
            licensed_asset_write_timeout_seconds=999.0,
            licensed_asset_pool_timeout_seconds=999.0,
            licensed_asset_total_timeout_seconds=999.0,
        )
    )
    assert all(
        getattr(bounded, field) == 30.0
        for field in (
            "dns_timeout_seconds",
            "connect_timeout_seconds",
            "read_timeout_seconds",
            "write_timeout_seconds",
            "pool_timeout_seconds",
            "total_timeout_seconds",
        )
    )


def test_stalled_resolver_uses_controlled_dns_timeout(
    licensed_assets: ModuleType, tmp_path: Path
) -> None:
    calls: list[tuple[str, int]] = []

    async def stalled(hostname: str, port: int) -> tuple[str, ...]:
        calls.append((hostname, port))
        await asyncio.Event().wait()
        return ()

    transport = TransportSpy({})

    async def run() -> BaseException:
        fetcher = licensed_assets.SafeImageFetcher(
            resolver=stalled,
            config=licensed_assets.SafeImageConfig(
                dns_timeout_seconds=0.01, total_timeout_seconds=0.2
            ),
            ready_dir=tmp_path / "ready",
            quarantine_dir=tmp_path / "quarantine",
            _network_backend_factory=transport.factory,
        )
        with pytest.raises(Exception) as caught:
            await fetcher.fetch("https://public.example/image.png")
        return caught.value

    exc = asyncio.run(run())
    _assert_content_free_error(licensed_assets, exc, "timeout")
    assert calls == [("public.example", 443)]
    assert transport.connect_calls == []


@pytest.mark.parametrize("stall_kind", ["connect", "read"])
def test_stalled_connect_and_read_are_bounded_and_cleanup(
    licensed_assets: ModuleType, tmp_path: Path, stall_kind: str
) -> None:
    url = "https://public.example/stall.png"
    transport = TransportSpy(
        {url: _response(200, body=_make_image("PNG"), content_type="image/png")}
    )
    setattr(transport, f"stall_{stall_kind}", True)
    config = licensed_assets.SafeImageConfig(
        connect_timeout_seconds=0.01,
        read_timeout_seconds=0.01,
        total_timeout_seconds=0.2,
    )
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=ResolverSpy({"public.example": ("93.184.216.34",)}),
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
        config=config,
    )
    _assert_content_free_error(licensed_assets, exc, "timeout")
    if stall_kind == "connect":
        assert transport.connect_calls[0][2] == 0.01
    else:
        assert transport.read_timeouts[0] == 0.01
        assert transport.closed_streams >= 1


def test_total_timeout_is_independent_and_server_owned(
    licensed_assets: ModuleType, tmp_path: Path
) -> None:
    url = "https://public.example/total.png"
    transport = TransportSpy(
        {url: _response(200, body=_make_image("PNG"), content_type="image/png")}
    )
    transport.stall_read = True
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=ResolverSpy({"public.example": ("93.184.216.34",)}),
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
        config=licensed_assets.SafeImageConfig(
            read_timeout_seconds=0.2, total_timeout_seconds=0.01
        ),
    )
    _assert_content_free_error(licensed_assets, exc, "timeout")
    assert transport.closed_streams >= 1


def _png_chunk_types(payload: bytes) -> list[bytes]:
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    chunks: list[bytes] = []
    while offset < len(payload):
        length = int.from_bytes(payload[offset : offset + 4], "big")
        chunk_type = payload[offset + 4 : offset + 8]
        chunks.append(chunk_type)
        offset += 12 + length
    assert offset == len(payload)
    return chunks


def _make_image_with_authoritative_metadata(image_format: str) -> bytes:
    image = Image.new("RGB", (32, 24), (32, 96, 160))
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    exif = Image.Exif()
    exif[315] = "private author"
    output = BytesIO()
    kwargs: dict[str, Any] = {"icc_profile": profile}
    if image_format == "PNG":
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Comment", "private text")
        metadata.add_itxt("XML:com.adobe.xmp", "<xmp>private</xmp>")
        kwargs["pnginfo"] = metadata
        kwargs["exif"] = exif
    elif image_format == "JPEG":
        kwargs["exif"] = exif
        kwargs["xmp"] = b"<xmp>private</xmp>"
        kwargs["comment"] = b"private comment"
    else:
        kwargs["exif"] = exif
        kwargs["xmp"] = b"<xmp>private</xmp>"
    image.save(output, format=image_format, **kwargs)
    return output.getvalue()


@pytest.mark.parametrize(
    ("image_format", "mime_type"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_processed_png_is_reconstructed_from_pixels_and_has_no_metadata_chunks(
    licensed_assets: ModuleType,
    tmp_path: Path,
    image_format: str,
    mime_type: str,
) -> None:
    body = _make_image_with_authoritative_metadata(image_format)
    url = f"https://public.example/metadata.{image_format.lower()}"
    result = asyncio.run(
        _fetch(
            module=licensed_assets,
            url=url,
            resolver=ResolverSpy({"public.example": ("93.184.216.34",)}),
            transport=TransportSpy(
                {url: _response(200, body=body, content_type=mime_type)}
            ),
            ready_dir=tmp_path / "ready",
            quarantine_dir=tmp_path / "quarantine",
        )
    )
    chunks = _png_chunk_types(result.processed_bytes)
    assert set(chunks) <= {b"IHDR", b"IDAT", b"IEND"}
    with Image.open(BytesIO(result.processed_bytes)) as processed:
        processed.load()
        assert processed.info == {}
        assert len(processed.getexif()) == 0


def test_upstream_exception_has_no_retained_cause_context_or_secret_traceback(
    licensed_assets: ModuleType, tmp_path: Path
) -> None:
    url = "https://public.example/secret-url/image.png"
    secret = "upstream-secret-body"
    transport = TransportSpy({url: RuntimeError(secret)})
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=ResolverSpy({"public.example": ("93.184.216.34",)}),
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
    )
    _assert_content_free_error(licensed_assets, exc, "network_error", url, secret)
    assert exc.__cause__ is None
    assert exc.__context__ is None
    rendered = "".join(traceback.format_exception(exc))
    assert secret not in rendered
    assert url not in rendered


def test_close_failures_are_suppressed_and_do_not_replace_success(
    licensed_assets: ModuleType, tmp_path: Path
) -> None:
    url = "https://public.example/close.png"
    transport = TransportSpy(
        {url: _response(200, body=_make_image("PNG"), content_type="image/png")}
    )
    transport.close_failure = True
    result = asyncio.run(
        _fetch(
            module=licensed_assets,
            url=url,
            resolver=ResolverSpy({"public.example": ("93.184.216.34",)}),
            transport=transport,
            ready_dir=tmp_path / "ready",
            quarantine_dir=tmp_path / "quarantine",
        )
    )
    assert isinstance(result, licensed_assets.FetchedImage)
    assert transport.closed_streams >= 1


def test_cancellation_propagates_after_response_and_pool_cleanup(
    licensed_assets: ModuleType, tmp_path: Path
) -> None:
    async def run() -> int:
        url = "https://public.example/cancel.png"
        transport = TransportSpy(
            {url: _response(200, body=_make_image("PNG"), content_type="image/png")}
        )
        transport.stall_read = True
        fetcher = licensed_assets.SafeImageFetcher(
            resolver=ResolverSpy({"public.example": ("93.184.216.34",)}),
            config=licensed_assets.SafeImageConfig(
                read_timeout_seconds=1.0, total_timeout_seconds=2.0
            ),
            ready_dir=tmp_path / "ready",
            quarantine_dir=tmp_path / "quarantine",
            _network_backend_factory=transport.factory,
        )
        task = asyncio.create_task(fetcher.fetch(url))
        for _ in range(100):
            if transport.read_timeouts:
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return transport.closed_streams

    assert asyncio.run(run()) >= 1


@pytest.mark.parametrize(
    "url",
    [
        "https://@public.example/image.png",
        "https://user@public.example/image.png",
        "https://public.example/image.png#",
        "https://public.example:0/image.png",
        "https://public.example:65536/image.png",
        "https://public.example:/image.png",
        "https://[fe80::1%25eth0]/image.png",
        "https://2130706433/image.png",
        "https://0177.0.0.1/image.png",
        "https://0x7f000001/image.png",
    ],
)
def test_raw_initial_url_authority_edges_fail_before_dns_or_transport(
    licensed_assets: ModuleType, tmp_path: Path, url: str
) -> None:
    resolver = ResolverSpy({})
    transport = TransportSpy({})
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=resolver,
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
    )
    _assert_content_free_error(licensed_assets, exc, "invalid_url", url)
    assert resolver.calls == []
    assert transport.connect_calls == []


@pytest.mark.parametrize(
    "location",
    [
        "https://@other.example/image.png",
        "https://other.example/image.png#",
        "https://other.example:0/image.png",
        "https://other.example:/image.png",
        "https://[fe80::1%25eth0]/image.png",
    ],
)
def test_raw_redirect_authority_edges_fail_before_redirect_dns_or_transport(
    licensed_assets: ModuleType, tmp_path: Path, location: str
) -> None:
    start = "https://public.example/start"
    resolver = ResolverSpy({"public.example": ("93.184.216.34",)})
    transport = TransportSpy(
        {start: _response(302, headers={"location": location})}
    )
    exc = _run_fetch_failure(
        module=licensed_assets,
        url=start,
        resolver=resolver,
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
    )
    _assert_content_free_error(licensed_assets, exc, "invalid_redirect", location)
    assert resolver.calls == [("public.example", 443)]
    assert len(transport.connect_calls) == 1


# S16B Task 3: the CPA catalog operation is classification/crop advice only.
# Garment identity, ownership, provenance, authorization, persistence, and the
# final accept/quarantine transition stay server-owned outside the Provider.
CATALOG_AUDIENCES = {"womenswear", "unisex_womenswear_compatible"}
CATALOG_CONFIDENCE_BANDS = {"high", "medium", "low"}
CATALOG_QUALITY_ISSUES = {
    "logo_or_watermark",
    "text_overlay",
    "multiple_items",
    "severe_occlusion",
    "low_resolution",
}
CATALOG_FAILURE_REASONS = {
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
CATALOG_TRACE_KEYS = {
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
CATALOG_IMAGE = _make_image("PNG", size=(24, 32))


@pytest.fixture(scope="module")
def vision_module() -> ModuleType:
    return importlib.import_module("profagent.vision")


def _catalog_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "slot": "top",
        "product_type": "tie-neck blouse",
        "audience": "womenswear",
        "contains_identifiable_person": False,
        "object_region": [0.1, 0.05, 0.9, 0.95],
        "confidence_band": "high",
        "quality_issues": [],
    }
    payload.update(overrides)
    return payload


def _catalog_completion_bytes(
    payload: dict[str, Any] | str,
    *,
    model: str = "grok-4.6-high",
    outer_overrides: dict[str, Any] | None = None,
) -> bytes:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    outer: dict[str, Any] = {
        "model": model,
        "choices": [{"message": {"content": content}}],
    }
    outer.update(outer_overrides or {})
    return json.dumps(outer).encode("utf-8")


class CatalogVisionTransportSpy:
    def __init__(
        self,
        response_body: bytes,
        *,
        status_code: int = 200,
        stall: bool = False,
    ) -> None:
        self.response_body = response_body
        self.status_code = status_code
        self.stall = stall
        self.calls: list[httpx.Request] = []
        self.request_json: list[dict[str, Any]] = []
        self.cancelled = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        self.request_json.append(json.loads(request.content))
        if self.stall:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return httpx.Response(
            self.status_code,
            stream=httpx.ByteStream(self.response_body),
            headers={"content-type": "application/json"},
            request=request,
        )

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _vision_adapter(
    module: ModuleType,
    offline_settings: Any,
    transport: CatalogVisionTransportSpy,
    *,
    budget: float = 0.5,
) -> Any:
    settings = replace(
        offline_settings,
        cpa_text_enabled=True,
        cpa_base_url="https://cpa.invalid/v1",
        cpa_api_key="test-only-key",
        cpa_timeout_seconds=1.0,
        vision_interaction_budget_seconds=budget,
    )
    return module.VisionAdapter(settings, transport=transport.transport)


def _inspect_catalog(
    adapter: Any,
    *,
    image_bytes: bytes = CATALOG_IMAGE,
    mime_type: str = "image/png",
    allowed_slot: str = "top",
    expected_product_type: str = "tie-neck blouse",
) -> tuple[Any, dict[str, Any]]:
    return asyncio.run(
        adapter.inspect_catalog_asset(
            image_bytes=image_bytes,
            mime_type=mime_type,
            allowed_slot=allowed_slot,
            expected_product_type=expected_product_type,
        )
    )


def _assert_catalog_trace_is_minimized(
    trace: dict[str, Any],
    *,
    success: bool,
    expected_reason: str | None = None,
) -> None:
    assert set(trace) <= CATALOG_TRACE_KEYS
    assert trace["component"] == "vision"
    assert trace["operation"] == "catalog_asset_assessment"
    assert trace["schema"] == "catalog_asset_assessment_v2"
    assert trace["requested_model"] == "grok4.6"
    assert trace["transport_model"] == "grok-4.6-high"
    assert trace["image_logged"] is False
    assert isinstance(trace["latency_ms"], (int, float))
    assert trace["latency_ms"] >= 0
    assert 0 < trace["interaction_budget_seconds"] <= 10
    assert isinstance(trace["assessment_count"], int)
    assert isinstance(trace["quality_issue_count"], int)
    if success:
        assert trace["status"] == "ok"
        assert trace["reason_code"] is None
        assert trace["resolved_model"] in {"grok-4.6-high", "grok-4.6-build"}
        assert trace["model_verified"] is True
        assert trace["assessment_count"] == 1
    else:
        assert trace["status"] == "quarantined"
        assert expected_reason in CATALOG_FAILURE_REASONS
        assert trace["reason_code"] == expected_reason
        assert trace["model_verified"] is False or expected_reason not in {
            "model_mismatch",
            "response_schema_invalid",
            "http_error",
            "provider_unavailable",
            "timeout",
        }
        assert trace["assessment_count"] == 0

    rendered = json.dumps(trace, ensure_ascii=False, sort_keys=True)
    encoded = base64.b64encode(CATALOG_IMAGE).decode("ascii").lower()
    for forbidden in (
        encoded,
        "data:image",
        "base64,",
        "garment_id",
        "g051",
        "user_id",
        "u01",
        "source_url",
        "creativecommons",
        "license",
        "authorization",
        "provider secret prose",
        "prompt",
    ):
        assert forbidden not in rendered.lower()


def _assert_catalog_failure(
    module: ModuleType,
    caught: pytest.ExceptionInfo[BaseException],
    expected_reason: str,
) -> dict[str, Any]:
    assert isinstance(caught.value, module.VisionUnavailable)
    assert caught.value.reason == "catalog_asset_quarantined"
    assert str(caught.value) == "catalog_asset_quarantined"
    trace = caught.value.provider_trace
    _assert_catalog_trace_is_minimized(
        trace, success=False, expected_reason=expected_reason
    )
    return trace


def test_vision_catalog_assessment_model_is_exact_frozen_and_closed(
    vision_module: ModuleType,
) -> None:
    model = vision_module.CatalogAssetAssessment
    assert set(model.model_fields) == {
        "slot",
        "product_type",
        "audience",
        "contains_identifiable_person",
        "object_region",
        "confidence_band",
        "quality_issues",
    }
    assert model.model_config.get("extra") == "forbid"
    assert model.model_config.get("frozen") is True
    assert _literal_values(model.model_fields["slot"].annotation) == set(
        get_args(Slot)
    )
    assert _literal_values(model.model_fields["audience"].annotation) == (
        CATALOG_AUDIENCES
    )
    assert _literal_values(model.model_fields["confidence_band"].annotation) == (
        CATALOG_CONFIDENCE_BANDS
    )
    assert _literal_values(vision_module.CatalogQualityIssueCode) == (
        CATALOG_QUALITY_ISSUES
    )

    assessment = model.model_validate(_catalog_payload())
    assert assessment.object_region == (0.1, 0.05, 0.9, 0.95)
    assert assessment.quality_issues == ()
    with pytest.raises(ValidationError):
        assessment.slot = "bottom"
    with pytest.raises(ValidationError):
        model.model_validate({**_catalog_payload(), "provider_note": "forbidden"})


@pytest.mark.parametrize("person_value", [0, 1, "false", "true", None])
def test_vision_catalog_person_flag_is_strict_bool(
    vision_module: ModuleType,
    person_value: Any,
) -> None:
    with pytest.raises(ValidationError):
        vision_module.CatalogAssetAssessment.model_validate(
            _catalog_payload(contains_identifiable_person=person_value)
        )


@pytest.mark.parametrize(
    "region",
    [
        (-0.1, 0.0, 0.8, 0.8),
        (0.0, -0.1, 0.8, 0.8),
        (0.0, 0.0, 1.1, 0.8),
        (0.0, 0.0, 0.8, 1.1),
        (0.5, 0.0, 0.5, 0.8),
        (0.6, 0.0, 0.5, 0.8),
        (0.0, 0.5, 0.8, 0.5),
        (0.0, 0.6, 0.8, 0.5),
        (math.nan, 0.0, 0.8, 0.8),
        (0.0, math.inf, 0.8, 0.8),
        (0.0, 0.0, -math.inf, 0.8),
        (0.0, 0.0, 0.8),
        "0,0,1,1",
    ],
)
def test_vision_catalog_crop_is_finite_normalized_and_ordered(
    vision_module: ModuleType,
    region: Any,
) -> None:
    with pytest.raises(ValidationError):
        vision_module.CatalogAssetAssessment.model_validate(
            _catalog_payload(object_region=region)
        )


@pytest.mark.parametrize(
    "quality_issues",
    [
        ["logo_or_watermark", "logo_or_watermark"],
        ["provider_free_text"],
        ["logo_or_watermark", "provider_free_text"],
        "logo_or_watermark",
    ],
)
def test_vision_catalog_quality_issue_codes_are_closed_and_unique(
    vision_module: ModuleType,
    quality_issues: Any,
) -> None:
    with pytest.raises(ValidationError):
        vision_module.CatalogAssetAssessment.model_validate(
            _catalog_payload(quality_issues=quality_issues)
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("slot", "overall"),
        ("slot", "unknown"),
        ("audience", "menswear"),
        ("audience", "unknown"),
        ("product_type", "unknown garment"),
        ("confidence_band", "very_high"),
        ("confidence_band", 1),
    ],
)
def test_vision_catalog_model_rejects_unknown_enum_values_and_types(
    vision_module: ModuleType,
    field: str,
    value: Any,
) -> None:
    with pytest.raises(ValidationError):
        vision_module.CatalogAssetAssessment.model_validate(
            _catalog_payload(**{field: value})
        )


@pytest.mark.parametrize(
    ("reported_model", "confidence", "audience"),
    [
        ("grok-4.6-high", "high", "womenswear"),
        ("grok-4.6-build", "medium", "unisex_womenswear_compatible"),
    ],
)
def test_vision_catalog_accepts_only_verified_high_or_medium_assessment(
    vision_module: ModuleType,
    offline_settings: Any,
    reported_model: str,
    confidence: str,
    audience: str,
) -> None:
    payload = _catalog_payload(
        audience=audience,
        confidence_band=confidence,
        object_region=[0.0, 0.0, 1.0, 1.0],
    )
    transport = CatalogVisionTransportSpy(
        _catalog_completion_bytes(payload, model=reported_model)
    )
    adapter = _vision_adapter(vision_module, offline_settings, transport)
    assessment, trace = _inspect_catalog(adapter)
    assert isinstance(assessment, vision_module.CatalogAssetAssessment)
    assert assessment.model_dump(mode="json") == payload
    assert assessment.slot == "top"
    assert assessment.confidence_band == confidence
    assert trace["quality_issue_count"] == 0
    _assert_catalog_trace_is_minimized(trace, success=True)
    assert len(transport.calls) == 1


def test_vision_catalog_request_is_minimized_and_contains_no_authority(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    transport = CatalogVisionTransportSpy(
        _catalog_completion_bytes(_catalog_payload())
    )
    adapter = _vision_adapter(vision_module, offline_settings, transport)
    _inspect_catalog(adapter)
    assert len(transport.request_json) == 1
    request = transport.request_json[0]
    assert set(request) == {
        "model",
        "messages",
        "response_format",
        "temperature",
        "max_tokens",
    }
    assert request["model"] == "grok-4.6-high"
    assert request["response_format"] == {"type": "json_object"}
    assert request["temperature"] == 0
    serialized = json.dumps(request, ensure_ascii=False).lower()
    assert base64.b64encode(CATALOG_IMAGE).decode("ascii").lower() in serialized
    for required in (
        "image/png",
        "top",
        "tie-neck blouse",
        "womenswear",
        "unisex_womenswear_compatible",
        *sorted(CATALOG_QUALITY_ISSUES),
    ):
        assert required in serialized
    for forbidden in (
        "garment_id",
        "g051",
        "user_id",
        "u01",
        "source_url",
        "license",
        "authorization",
        "receipt",
        "owner_id",
        "raw_name",
        "search_text",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("payload_overrides", "allowed_slot", "reason"),
    [
        ({"slot": "bottom"}, "top", "slot_mismatch"),
        ({"audience": "menswear"}, "top", "audience_rejected"),
        ({"contains_identifiable_person": True}, "top", "identifiable_person"),
        ({"confidence_band": "low"}, "top", "low_confidence"),
        ({"object_region": [0.8, 0.1, 0.2, 0.9]}, "top", "invalid_region"),
        ({"object_region": [math.nan, 0.1, 0.9, 0.9]}, "top", "invalid_region"),
        ({"quality_issues": ["logo_or_watermark"]}, "top", "quality_rejected"),
        ({"quality_issues": ["text_overlay"]}, "top", "quality_rejected"),
        ({"quality_issues": ["multiple_items"]}, "top", "quality_rejected"),
        ({"quality_issues": ["severe_occlusion"]}, "top", "quality_rejected"),
        ({"quality_issues": ["low_resolution"]}, "top", "quality_rejected"),
    ],
)
def test_vision_catalog_server_rejects_unsafe_assessment_with_quarantine_reason(
    vision_module: ModuleType,
    offline_settings: Any,
    payload_overrides: dict[str, Any],
    allowed_slot: str,
    reason: str,
) -> None:
    transport = CatalogVisionTransportSpy(
        _catalog_completion_bytes(_catalog_payload(**payload_overrides))
    )
    adapter = _vision_adapter(vision_module, offline_settings, transport)
    with pytest.raises(vision_module.VisionUnavailable) as caught:
        _inspect_catalog(adapter, allowed_slot=allowed_slot)
    _assert_catalog_failure(vision_module, caught, reason)
    assert len(transport.calls) == 1


def _catalog_malformed_completion_cases() -> list[tuple[str, bytes]]:
    valid = _catalog_completion_bytes(_catalog_payload())
    valid_outer = json.loads(valid)
    cases: list[tuple[str, bytes]] = [
        ("not-json", b"provider secret prose"),
        (
            "outer-extra",
            json.dumps({**valid_outer, "provider_note": "secret"}).encode(),
        ),
        (
            "outer-missing-model",
            json.dumps({"choices": valid_outer["choices"]}).encode(),
        ),
        (
            "outer-missing-choices",
            json.dumps({"model": "grok-4.6-high"}).encode(),
        ),
        (
            "choices-not-list",
            json.dumps(
                {"model": "grok-4.6-high", "choices": {"message": {}}}
            ).encode(),
        ),
        (
            "choices-empty",
            json.dumps({"model": "grok-4.6-high", "choices": []}).encode(),
        ),
        (
            "choices-two",
            json.dumps(
                {
                    "model": "grok-4.6-high",
                    "choices": valid_outer["choices"] * 2,
                }
            ).encode(),
        ),
        (
            "choice-missing-message",
            json.dumps(
                {"model": "grok-4.6-high", "choices": [{}]}
            ).encode(),
        ),
        (
            "choice-not-object",
            json.dumps(
                {"model": "grok-4.6-high", "choices": ["message"]}
            ).encode(),
        ),
        (
            "message-missing-content",
            json.dumps(
                {
                    "model": "grok-4.6-high",
                    "choices": [{"message": {}}],
                }
            ).encode(),
        ),
        (
            "message-not-object",
            json.dumps(
                {
                    "model": "grok-4.6-high",
                    "choices": [{"message": "content"}],
                }
            ).encode(),
        ),
        (
            "content-not-string",
            json.dumps(
                {
                    "model": "grok-4.6-high",
                    "choices": [{"message": {"content": _catalog_payload()}}],
                }
            ).encode(),
        ),
        (
            "inner-not-json",
            _catalog_completion_bytes("provider secret prose"),
        ),
        (
            "inner-extra",
            _catalog_completion_bytes(
                {**_catalog_payload(), "garment_id": "g999"}
            ),
        ),
        (
            "inner-missing",
            _catalog_completion_bytes(
                {
                    key: value
                    for key, value in _catalog_payload().items()
                    if key != "audience"
                }
            ),
        ),
        (
            "inner-type",
            _catalog_completion_bytes(
                _catalog_payload(contains_identifiable_person=1)
            ),
        ),
    ]
    duplicate_outer = valid.replace(
        b'{"model":', b'{"model":"grok-4.6-high","model":', 1
    )
    duplicate_choice = valid.replace(
        b'{"message":',
        b'{"message":{"content":"{}"},"message":',
        1,
    )
    duplicate_message = valid.replace(
        b'{"content":', b'{"content":"{}","content":', 1
    )
    duplicate_inner = _catalog_completion_bytes(
        '{"slot":"top","slot":"bottom","audience":"womenswear",'
        '"contains_identifiable_person":false,'
        '"object_region":[0.1,0.1,0.9,0.9],'
        '"confidence_band":"high","quality_issues":[]}'
    )
    cases.extend(
        [
            ("outer-duplicate", duplicate_outer),
            ("choice-duplicate", duplicate_choice),
            ("message-duplicate", duplicate_message),
            ("inner-duplicate", duplicate_inner),
        ]
    )
    return cases


@pytest.mark.parametrize(
    ("_label", "response_body"),
    _catalog_malformed_completion_cases(),
    ids=[label for label, _ in _catalog_malformed_completion_cases()],
)
def test_vision_catalog_rejects_closed_outer_inner_and_duplicate_key_violations(
    vision_module: ModuleType,
    offline_settings: Any,
    _label: str,
    response_body: bytes,
) -> None:
    transport = CatalogVisionTransportSpy(response_body)
    adapter = _vision_adapter(vision_module, offline_settings, transport)
    with pytest.raises(vision_module.VisionUnavailable) as caught:
        _inspect_catalog(adapter)
    _assert_catalog_failure(vision_module, caught, "response_schema_invalid")
    assert len(transport.calls) == 1


@pytest.mark.parametrize("reported_model", ["grok-4.5", "grok-4.6-high-preview", "", None])
def test_vision_catalog_wrong_model_fails_without_mislabeling_fallback_source(
    vision_module: ModuleType,
    offline_settings: Any,
    reported_model: Any,
) -> None:
    outer = {
        "model": reported_model,
        "choices": [{"message": {"content": json.dumps(_catalog_payload())}}],
    }
    transport = CatalogVisionTransportSpy(json.dumps(outer).encode())
    adapter = _vision_adapter(vision_module, offline_settings, transport)
    with pytest.raises(vision_module.VisionUnavailable) as caught:
        _inspect_catalog(adapter)
    trace = _assert_catalog_failure(vision_module, caught, "model_mismatch")
    assert trace["resolved_model"] is None
    assert trace["model_verified"] is False
    health = adapter.health()
    assert health["requested_model"] == "grok4.6"
    assert health["transport_model"] == "grok-4.6-high"
    assert health["resolved_model"] is None
    assert health["model_verified"] is False


@pytest.mark.parametrize(
    ("image_bytes", "mime_type", "allowed_slot"),
    [
        (b"", "image/png", "top"),
        ("not-bytes", "image/png", "top"),
        (bytearray(b"not-immutable"), "image/png", "top"),
        (CATALOG_IMAGE, "", "top"),
        (CATALOG_IMAGE, "image/gif", "top"),
        (CATALOG_IMAGE, "text/html", "top"),
        (CATALOG_IMAGE, "image/png", "overall"),
        (CATALOG_IMAGE, "image/png", "unknown"),
    ],
    ids=[
        "empty-bytes",
        "text-image",
        "mutable-image",
        "empty-mime",
        "gif-mime",
        "html-mime",
        "non-catalog-slot",
        "unknown-slot",
    ],
)
def test_vision_catalog_invalid_input_is_blocked_before_transport(
    vision_module: ModuleType,
    offline_settings: Any,
    image_bytes: Any,
    mime_type: str,
    allowed_slot: str,
) -> None:
    transport = CatalogVisionTransportSpy(
        _catalog_completion_bytes(_catalog_payload())
    )
    adapter = _vision_adapter(vision_module, offline_settings, transport)
    with pytest.raises(vision_module.VisionUnavailable) as caught:
        _inspect_catalog(
            adapter,
            image_bytes=image_bytes,
            mime_type=mime_type,
            allowed_slot=allowed_slot,
        )
    _assert_catalog_failure(vision_module, caught, "input_rejected")
    assert transport.calls == []


def test_vision_catalog_oversize_input_is_blocked_before_transport(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    assert 0 < vision_module.CATALOG_VISION_MAX_IMAGE_BYTES <= 25 * 1024 * 1024
    transport = CatalogVisionTransportSpy(
        _catalog_completion_bytes(_catalog_payload())
    )
    adapter = _vision_adapter(vision_module, offline_settings, transport)
    with pytest.raises(vision_module.VisionUnavailable) as caught:
        _inspect_catalog(
            adapter,
            image_bytes=b"x" * (vision_module.CATALOG_VISION_MAX_IMAGE_BYTES + 1),
        )
    _assert_catalog_failure(vision_module, caught, "input_rejected")
    assert transport.calls == []


def test_vision_catalog_timeout_is_controlled_cancels_transport_and_mutates_nothing(
    vision_module: ModuleType,
    licensed_assets: ModuleType,
    offline_settings: Any,
) -> None:
    existing_asset = licensed_assets.ProcessedWardrobeAsset.model_validate(
        _asset_payload(licensed_assets)
    )
    manifest = {
        "asset_version": "wardrobe_assets_v2",
        "items": [existing_asset.model_dump(mode="json")],
    }
    before_asset = existing_asset.model_dump(mode="json")
    before_manifest = deepcopy(manifest)
    transport = CatalogVisionTransportSpy(
        _catalog_completion_bytes(_catalog_payload()), stall=True
    )
    adapter = _vision_adapter(
        vision_module, offline_settings, transport, budget=0.01
    )
    with pytest.raises(vision_module.VisionUnavailable) as caught:
        _inspect_catalog(adapter)
    _assert_catalog_failure(vision_module, caught, "timeout")
    assert transport.cancelled is True
    assert len(transport.calls) == 1
    assert existing_asset.model_dump(mode="json") == before_asset
    assert manifest == before_manifest


def test_vision_catalog_invalid_assessment_has_zero_manifest_or_authority_mutation(
    vision_module: ModuleType,
    licensed_assets: ModuleType,
    offline_settings: Any,
) -> None:
    existing_asset = licensed_assets.ProcessedWardrobeAsset.model_validate(
        _asset_payload(licensed_assets)
    )
    manifest = {
        "asset_version": "wardrobe_assets_v2",
        "items": [existing_asset.model_dump(mode="json")],
    }
    before_asset = existing_asset.model_dump(mode="json")
    before_manifest = deepcopy(manifest)
    payload = _catalog_payload(
        confidence_band="low",
        garment_id="g999",
        user_id="u99",
        source_url="https://evil.example/source",
        license="all-rights-reserved",
        authorization=True,
    )
    transport = CatalogVisionTransportSpy(_catalog_completion_bytes(payload))
    adapter = _vision_adapter(vision_module, offline_settings, transport)
    with pytest.raises(vision_module.VisionUnavailable) as caught:
        _inspect_catalog(adapter)
    _assert_catalog_failure(
        vision_module, caught, "response_schema_invalid"
    )
    assert existing_asset.model_dump(mode="json") == before_asset
    assert manifest == before_manifest


def test_vision_catalog_external_cancellation_propagates_and_cancels_mock_transport(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    async def exercise() -> tuple[bool, int]:
        transport = CatalogVisionTransportSpy(
            _catalog_completion_bytes(_catalog_payload()), stall=True
        )
        adapter = _vision_adapter(
            vision_module, offline_settings, transport, budget=0.5
        )
        task = asyncio.create_task(
            adapter.inspect_catalog_asset(
                image_bytes=CATALOG_IMAGE,
                mime_type="image/png",
                allowed_slot="top",
                expected_product_type="tie-neck blouse",
            )
        )
        for _ in range(100):
            if transport.calls:
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return transport.cancelled, len(transport.calls)

    assert asyncio.run(exercise()) == (True, 1)


def test_vision_catalog_http_and_provider_errors_are_controlled_and_content_free(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    transport = CatalogVisionTransportSpy(
        b'{"error":"provider secret prose"}', status_code=503
    )
    adapter = _vision_adapter(vision_module, offline_settings, transport)
    with pytest.raises(vision_module.VisionUnavailable) as caught:
        _inspect_catalog(adapter)
    trace = _assert_catalog_failure(vision_module, caught, "http_error")
    assert "provider secret prose" not in json.dumps(trace)
    assert len(transport.calls) == 1


def test_vision_catalog_signature_and_result_exclude_provider_authority_fields(
    vision_module: ModuleType,
) -> None:
    signature = inspect.signature(vision_module.VisionAdapter.inspect_catalog_asset)
    assert set(signature.parameters) == {
        "self",
        "image_bytes",
        "mime_type",
        "allowed_slot",
        "expected_product_type",
    }
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for name, parameter in signature.parameters.items()
        if name != "self"
    )
    assert {
        "garment_id",
        "user_id",
        "source_url",
        "license",
        "authorization",
        "receipt",
        "relative_path",
    }.isdisjoint(vision_module.CatalogAssetAssessment.model_fields)


def _fetched_png(licensed_assets: ModuleType, *, size: tuple[int, int]) -> Any:
    payload = _make_image("PNG", size=size)
    digest = hashlib.sha256(payload).hexdigest()
    return licensed_assets.FetchedImage(
        source_mime_type="image/png",
        processed_mime_type="image/png",
        original_sha256=digest,
        processed_sha256=digest,
        original_bytes=payload,
        processed_bytes=payload,
        width=size[0],
        height=size[1],
    )


def test_catalog_asset_server_bridge_crops_fetched_image_without_authority_or_metadata(
    vision_module: ModuleType,
    licensed_assets: ModuleType,
    offline_settings: Any,
) -> None:
    fetched = _fetched_png(licensed_assets, size=(10, 8))
    before = fetched.model_dump(mode="python")
    payload = _catalog_payload(object_region=[0.2, 0.25, 0.8, 0.75])
    transport = CatalogVisionTransportSpy(_catalog_completion_bytes(payload))
    adapter = _vision_adapter(vision_module, offline_settings, transport)

    cropped, assessment, trace = asyncio.run(
        licensed_assets.assess_and_crop_catalog_asset(
            vision=adapter,
            fetched_image=fetched,
            allowed_slot="top",
            expected_product_type="tie-neck blouse",
        )
    )

    assert isinstance(cropped, licensed_assets.CatalogCroppedImage)
    assert set(type(cropped).model_fields) == {
        "input_sha256",
        "processed_sha256",
        "processed_mime_type",
        "processed_bytes",
        "width",
        "height",
        "object_region",
    }
    assert {
        "garment_id",
        "user_id",
        "owner_id",
        "source_url",
        "license",
        "authorization",
        "receipt",
        "relative_path",
        "status",
    }.isdisjoint(type(cropped).model_fields)
    assert assessment.object_region == (0.2, 0.25, 0.8, 0.75)
    assert cropped.object_region == assessment.object_region
    assert cropped.input_sha256 == fetched.processed_sha256
    assert cropped.processed_mime_type == "image/png"
    assert (cropped.width, cropped.height) == (6, 4)
    assert hashlib.sha256(cropped.processed_bytes).hexdigest() == (
        cropped.processed_sha256
    )
    with Image.open(BytesIO(cropped.processed_bytes)) as image:
        assert image.format == "PNG"
        assert image.size == (6, 4)
        assert image.info == {}
    assert fetched.model_dump(mode="python") == before
    _assert_catalog_trace_is_minimized(trace, success=True)
    assert len(transport.calls) == 1


def test_catalog_asset_server_bridge_quarantines_local_processing_failure(
    vision_module: ModuleType,
    licensed_assets: ModuleType,
    offline_settings: Any,
) -> None:
    invalid = b"not-a-decoded-image"
    digest = hashlib.sha256(invalid).hexdigest()
    fetched = licensed_assets.FetchedImage(
        source_mime_type="image/png",
        processed_mime_type="image/png",
        original_sha256=digest,
        processed_sha256=digest,
        original_bytes=invalid,
        processed_bytes=invalid,
        width=10,
        height=8,
    )
    transport = CatalogVisionTransportSpy(
        _catalog_completion_bytes(_catalog_payload())
    )
    adapter = _vision_adapter(vision_module, offline_settings, transport)

    with pytest.raises(vision_module.VisionUnavailable) as caught:
        asyncio.run(
            licensed_assets.assess_and_crop_catalog_asset(
                vision=adapter,
                fetched_image=fetched,
                allowed_slot="top",
                expected_product_type="tie-neck blouse",
            )
        )

    _assert_catalog_failure(vision_module, caught, "input_rejected")
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "response_body",
    [
        b"[" * 2_000 + b"]" * 2_000,
        _catalog_completion_bytes("[" * 2_000 + "]" * 2_000),
    ],
    ids=["outer-recursion", "inner-recursion"],
)
def test_vision_catalog_deeply_nested_json_is_controlled_quarantine(
    vision_module: ModuleType,
    offline_settings: Any,
    response_body: bytes,
) -> None:
    transport = CatalogVisionTransportSpy(response_body)
    adapter = _vision_adapter(vision_module, offline_settings, transport)
    with pytest.raises(vision_module.VisionUnavailable) as caught:
        _inspect_catalog(adapter)
    _assert_catalog_failure(
        vision_module, caught, "response_schema_invalid"
    )
    assert len(transport.calls) == 1


def test_vision_catalog_oversize_provider_response_is_bounded_and_quarantined(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    assert 0 < vision_module.VISION_MAX_RESPONSE_BYTES <= 1024 * 1024
    transport = CatalogVisionTransportSpy(
        b"x" * (vision_module.VISION_MAX_RESPONSE_BYTES + 1)
    )
    adapter = _vision_adapter(vision_module, offline_settings, transport)
    with pytest.raises(vision_module.VisionUnavailable) as caught:
        _inspect_catalog(adapter)
    _assert_catalog_failure(
        vision_module, caught, "response_schema_invalid"
    )
    assert len(transport.calls) == 1


def test_vision_catalog_unhashable_slot_is_input_rejected_before_transport(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    transport = CatalogVisionTransportSpy(
        _catalog_completion_bytes(_catalog_payload())
    )
    adapter = _vision_adapter(vision_module, offline_settings, transport)
    with pytest.raises(vision_module.VisionUnavailable) as caught:
        _inspect_catalog(adapter, allowed_slot=["top"])  # type: ignore[arg-type]
    _assert_catalog_failure(vision_module, caught, "input_rejected")
    assert transport.calls == []


# Task 4 contract: the CLI owns persistence but exposes an injectable
# ``run_ingestion`` boundary.  The production command must wire its official
# Openverse client, SafeImageFetcher, and Vision adapter into this boundary;
# these tests deliberately use MockTransport and local doubles only.
TASK4_CLI_PATH = Path(__file__).parents[1] / "scripts" / "ingest_licensed_wardrobe_assets.py"
TASK4_OPENVERSE_BASE = "https://api.openverse.org/v1/images/"


class _LazyLicensedIngestionCli:
    """Make a missing CLI a RED assertion in a test body, not collection error."""

    def __init__(self) -> None:
        self._module: ModuleType | None = None

    def _load(self) -> ModuleType:
        if self._module is None:
            if not TASK4_CLI_PATH.is_file():
                pytest.fail(
                    "Task 4 RED: scripts/ingest_licensed_wardrobe_assets.py is not implemented",
                    pytrace=False,
                )
            module_name = "_task4_licensed_ingestion_cli"
            spec = importlib.util.spec_from_file_location(module_name, TASK4_CLI_PATH)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            self._module = module
        return self._module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


@pytest.fixture
def licensed_ingestion_cli() -> Any:
    return _LazyLicensedIngestionCli()


def _task4_garment_rows(*garment_ids: str) -> list[dict[str, Any]]:
    fixture = (
        Path(__file__).parents[1]
        / "data"
        / "fixtures"
        / "garments_s16_womenswear.jsonl"
    )
    wanted = set(garment_ids)
    rows = [
        json.loads(line)
        for line in fixture.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["garment_id"] in wanted
    ]
    assert {row["garment_id"] for row in rows} == wanted
    return sorted(rows, key=lambda row: row["garment_id"])


def _write_task4_garment_file(tmp_path: Path, *garment_ids: str) -> Path:
    path = tmp_path / "controlled-garments.jsonl"
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in _task4_garment_rows(*garment_ids)
        ),
        encoding="utf-8",
    )
    return path


def _openverse_candidate(
    *,
    creator: str = "Openverse Creator",
    source_url: str = "https://museum.example/object/1",
    image_url: str = "https://images.example/object-1.png",
    license_url: str = "https://creativecommons.org/licenses/by/4.0/",
    **overrides: Any,
) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "id": _RULING_F_UUID,
        "license": "by",
        "license_version": "4.0",
        "license_url": license_url,
        "creator": creator,
        "foreign_landing_url": source_url,
        "url": image_url,
    }
    candidate.update(overrides)
    return candidate


def _openverse_response(*candidates: dict[str, Any]) -> dict[str, Any]:
    return {"result_count": len(candidates), "results": list(candidates)}


class Task4SafeFetcher:
    def __init__(self, result: Any | Exception, *, wait: bool = False) -> None:
        self.result = result
        self.wait = wait
        self.urls: list[str] = []
        self.active = 0
        self.max_active = 0

    async def fetch(self, url: str, **_kwargs: Any) -> Any:
        self.urls.append(url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.wait:
                await asyncio.sleep(0)
            if isinstance(self.result, Exception):
                raise self.result
            return self.result
        finally:
            self.active -= 1


class Task4Vision:
    def __init__(self, assessment: Any | Exception) -> None:
        self.assessment = assessment
        self.calls: list[dict[str, Any]] = []

    async def inspect_catalog_asset(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        allowed_slot: str,
        expected_product_type: str,
    ) -> tuple[Any, dict[str, object]]:
        self.calls.append(
            {
                "image_bytes": image_bytes,
                "mime_type": mime_type,
                "allowed_slot": allowed_slot,
                "expected_product_type": expected_product_type,
            }
        )
        if isinstance(self.assessment, Exception):
            raise self.assessment
        assessment = self.assessment
        if callable(assessment):
            assessment = assessment(
                allowed_slot=allowed_slot,
                expected_product_type=expected_product_type,
            )
        return assessment, {"operation": "catalog_asset_assessment"}


def _task4_fetched_image(licensed_assets: ModuleType) -> Any:
    payload = _make_image("PNG", size=(16, 12))
    digest = hashlib.sha256(payload).hexdigest()
    return licensed_assets.FetchedImage(
        source_mime_type="image/png",
        processed_mime_type="image/png",
        original_sha256=digest,
        processed_sha256=digest,
        original_bytes=payload,
        processed_bytes=payload,
        width=16,
        height=12,
    )


def _task4_assessment(
    vision_module: ModuleType,
    *,
    slot: str = "top",
    product_type: str = "tie-neck blouse",
) -> Any:
    return vision_module.CatalogAssetAssessment(
        slot=slot,
        product_type=product_type,
        audience="womenswear",
        contains_identifiable_person=False,
        object_region=(0.0, 0.0, 1.0, 1.0),
        confidence_band="high",
        quality_issues=(),
    )


def _task4_config(
    cli: ModuleType,
    *,
    garment_file: Path,
    state_dir: Path,
    dry_run: bool = False,
    resume: bool = False,
    candidate_budget: int = 2,
    retry_budget: int = 1,
    concurrency: int = 1,
    total_budget: int = 2,
    search_budget: int | None = None,
    diagnostic_canary: bool = False,
) -> Any:
    kwargs: dict[str, Any] = {
        "provider": "openverse",
        "license_allowlist": ("CC0", "PDM", "CC-BY-2.0", "CC-BY-3.0", "CC-BY-4.0"),
        "garment_file": garment_file,
        "manifest_path": state_dir / "wardrobe_assets_v2.json",
        "sources_path": state_dir / "wardrobe_s16_sources.jsonl",
        "asset_directory": state_dir / "wardrobe_licensed_v1",
        "dry_run": dry_run,
        "resume": resume,
        "candidate_budget": candidate_budget,
        "retry_budget": retry_budget,
        "concurrency": concurrency,
        "total_budget": total_budget,
    }
    if search_budget is not None:
        kwargs["search_budget"] = search_budget
    if diagnostic_canary:
        kwargs["diagnostic_canary"] = True
    return cli.IngestionConfig(**kwargs)


async def _run_task4_ingestion(
    cli: ModuleType,
    *,
    config: Any,
    response_handler: Any,
    fetcher: Task4SafeFetcher,
    vision: Task4Vision,
) -> Any:
    transport = httpx.MockTransport(response_handler)
    async with httpx.AsyncClient(
        transport=transport, base_url=TASK4_OPENVERSE_BASE
    ) as api_client:
        return await cli.run_ingestion(
            config=config,
            api_client=api_client,
            safe_fetcher=fetcher,
            vision=vision,
        )


def _official_openverse_handler(
    payload: dict[str, Any],
    *,
    thumbnail_response: httpx.Response | Exception | None = None,
    thumbnail_requests: list[httpx.Request] | None = None,
) -> Any:
    expected_thumbnail_paths = {
        f"/v1/images/{candidate['id']}/thumb/"
        for candidate in payload.get("results", [])
        if isinstance(candidate, dict) and isinstance(candidate.get("id"), str)
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "api.openverse.org"
        if request.url.path == "/v1/images/":
            return httpx.Response(200, json=payload)
        assert request.url.path in expected_thumbnail_paths
        if thumbnail_requests is not None:
            thumbnail_requests.append(request)
        if isinstance(thumbnail_response, Exception):
            raise thumbnail_response
        if thumbnail_response is not None:
            return thumbnail_response
        return httpx.Response(
            200,
            content=_make_image("PNG", size=(16, 12)),
            headers={"content-type": "image/png"},
        )

    return handler


def _task4_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _task4_manifest_item(manifest: dict[str, Any], garment_id: str) -> dict[str, Any]:
    return next(
        item for item in manifest["items"] if item["garment_id"] == garment_id
    )


def test_ingestion_accepts_only_complete_machine_readable_openverse_receipts(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    garment_file = _write_task4_garment_file(tmp_path, "g051")
    state_dir = tmp_path / "state"
    fetched = _task4_fetched_image(licensed_assets)
    fetcher = Task4SafeFetcher(fetched)
    vision = Task4Vision(_task4_assessment(vision_module))
    config = _task4_config(
        licensed_ingestion_cli, garment_file=garment_file, state_dir=state_dir
    )

    result = asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=config,
            response_handler=_official_openverse_handler(
                _openverse_response(_openverse_candidate())
            ),
            fetcher=fetcher,
            vision=vision,
        )
    )

    assert result.ready_ids == ("g051",)
    assert result.quarantined_ids == ()
    assert result.remaining_ids == ()
    manifest = _task4_manifest(config.manifest_path)
    item = _task4_manifest_item(manifest, "g051")
    assert {
        "garment_id": item["garment_id"],
        "user_id": item["user_id"],
        "slot": item["slot"],
        "audience": item["audience"],
        "status": item["status"],
        "source_kind": item["source_kind"],
    } == {
        "garment_id": "g051",
        "user_id": "u01",
        "slot": "top",
        "audience": "unisex_womenswear_compatible",
        "status": "ready",
        "source_kind": "licensed_photo",
    }
    receipt = item["receipt_history"][-1]
    assert receipt["provider"] == "openverse"
    assert receipt["creator"] == "Openverse Creator"
    assert receipt["source_url"] == "https://museum.example/object/1"
    assert receipt["image_url"] == (
        f"https://api.openverse.org/v1/images/{_RULING_F_UUID}/thumb/"
    )
    assert receipt["license_code"] == "CC-BY-4.0"
    assert receipt["license_url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert fetcher.urls == []
    assert len(vision.calls) == 1
    assert vision.calls[0]["image_bytes"].startswith(b"\x89PNG\r\n\x1a\n")
    assert vision.calls[0]["mime_type"] == "image/png"
    assert vision.calls[0]["allowed_slot"] == "top"


@pytest.mark.parametrize(
    "missing_field",
    [
        "license",
        "license_version",
        "creator",
        "license_url",
        "foreign_landing_url",
        "url",
    ],
)
def test_ingestion_rejects_incomplete_openverse_metadata_before_image_or_vision(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
    missing_field: str,
) -> None:
    garment_file = _write_task4_garment_file(tmp_path, "g051")
    state_dir = tmp_path / "state"
    fetcher = Task4SafeFetcher(_task4_fetched_image(licensed_assets))
    vision = Task4Vision(_task4_assessment(vision_module))
    candidate = _openverse_candidate()
    candidate.pop(missing_field)

    result = asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=_task4_config(
                licensed_ingestion_cli,
                garment_file=garment_file,
                state_dir=state_dir,
                candidate_budget=1,
            ),
            response_handler=_official_openverse_handler(_openverse_response(candidate)),
            fetcher=fetcher,
            vision=vision,
        )
    )

    assert result.ready_ids == ()
    assert result.quarantined_ids == ("g051",)
    assert result.remaining_ids == ("g051",)
    assert fetcher.urls == []
    assert vision.calls == []


def test_ingestion_rejects_noncanonical_license_url_before_image_fetch(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    garment_file = _write_task4_garment_file(tmp_path, "g051")
    state_dir = tmp_path / "state"
    fetcher = Task4SafeFetcher(_task4_fetched_image(licensed_assets))
    vision = Task4Vision(_task4_assessment(vision_module))
    candidate = _openverse_candidate(
        license_url="https://creativecommons.org/licenses/by/3.0/"
    )

    result = asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=_task4_config(
                licensed_ingestion_cli, garment_file=garment_file, state_dir=state_dir
            ),
            response_handler=_official_openverse_handler(_openverse_response(candidate)),
            fetcher=fetcher,
            vision=vision,
        )
    )

    assert result.ready_ids == ()
    assert result.quarantined_ids == ("g051",)
    assert result.remaining_ids == ("g051",)
    assert fetcher.urls == []
    assert vision.calls == []


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("user_id", "u99"),
        ("slot", "dress"),
        ("audience", "womenswear"),
    ],
)
def test_ingestion_rejects_tampered_extension_identity_before_provider_access(
    licensed_ingestion_cli: ModuleType,
    tmp_path: Path,
    field: str,
    tampered_value: str,
) -> None:
    garment_file = tmp_path / "tampered-garments.jsonl"
    row = _task4_garment_rows("g051")[0]
    row[field] = tampered_value
    garment_file.write_text(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    calls: list[httpx.Request] = []

    def no_provider_access(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    with pytest.raises((ValueError, ValidationError)):
        asyncio.run(
            _run_task4_ingestion(
                licensed_ingestion_cli,
                config=_task4_config(
                    licensed_ingestion_cli,
                    garment_file=garment_file,
                    state_dir=tmp_path / "state",
                ),
                response_handler=no_provider_access,
                fetcher=Task4SafeFetcher(RuntimeError("must_not_fetch")),
                vision=Task4Vision(RuntimeError("must_not_assess")),
            )
        )
    assert calls == []


def test_ingestion_dry_run_leaves_assets_manifest_and_receipts_unmodified(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    garment_file = _write_task4_garment_file(tmp_path, "g051")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    manifest_path = state_dir / "wardrobe_assets_v2.json"
    sources_path = state_dir / "wardrobe_s16_sources.jsonl"
    manifest_path.write_text('{"items": []}\n', encoding="utf-8")
    sources_path.write_text('{"existing": true}\n', encoding="utf-8")
    before = {
        path: path.read_bytes()
        for path in (manifest_path, sources_path)
    }
    fetcher = Task4SafeFetcher(_task4_fetched_image(licensed_assets))
    vision = Task4Vision(_task4_assessment(vision_module))
    config = _task4_config(
        licensed_ingestion_cli,
        garment_file=garment_file,
        state_dir=state_dir,
        dry_run=True,
    )

    result = asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=config,
            response_handler=_official_openverse_handler(
                _openverse_response(_openverse_candidate())
            ),
            fetcher=fetcher,
            vision=vision,
        )
    )

    assert result.dry_run is True
    assert {path: path.read_bytes() for path in before} == before
    assert not config.asset_directory.exists()


def test_ingestion_resume_reuses_exact_existing_content_hash_without_new_receipt(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    garment_file = _write_task4_garment_file(tmp_path, "g051")
    state_dir = tmp_path / "state"
    fetched = _task4_fetched_image(licensed_assets)
    first = asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=_task4_config(
                licensed_ingestion_cli, garment_file=garment_file, state_dir=state_dir
            ),
            response_handler=_official_openverse_handler(
                _openverse_response(_openverse_candidate())
            ),
            fetcher=Task4SafeFetcher(fetched),
            vision=Task4Vision(_task4_assessment(vision_module)),
        )
    )
    assert first.ready_ids == ("g051",)
    before = _task4_manifest(state_dir / "wardrobe_assets_v2.json")

    second = asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=_task4_config(
                licensed_ingestion_cli,
                garment_file=garment_file,
                state_dir=state_dir,
                resume=True,
            ),
            response_handler=_official_openverse_handler(
                _openverse_response(_openverse_candidate())
            ),
            fetcher=Task4SafeFetcher(RuntimeError("fetch_must_not_run")),
            vision=Task4Vision(RuntimeError("vision_must_not_run")),
        )
    )

    after = _task4_manifest(state_dir / "wardrobe_assets_v2.json")
    assert second.ready_ids == ("g051",)
    assert second.reused_ids == ("g051",)
    assert after == before
    item = _task4_manifest_item(after, "g051")
    asset_path = state_dir / "wardrobe_licensed_v1" / item["relative_path"]
    assert item["processed_sha256"] == hashlib.sha256(asset_path.read_bytes()).hexdigest()


def test_ingestion_source_change_appends_receipt_history_instead_of_overwriting(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    garment_file = _write_task4_garment_file(tmp_path, "g051")
    state_dir = tmp_path / "state"
    fetched = _task4_fetched_image(licensed_assets)
    first_candidate = _openverse_candidate()
    second_candidate = _openverse_candidate(
        source_url="https://museum.example/object/2",
        image_url="https://images.example/object-2.png",
    )
    for candidate in (first_candidate, second_candidate):
        asyncio.run(
            _run_task4_ingestion(
                licensed_ingestion_cli,
                config=_task4_config(
                    licensed_ingestion_cli,
                    garment_file=garment_file,
                    state_dir=state_dir,
                    resume=True,
                ),
                response_handler=_official_openverse_handler(_openverse_response(candidate)),
                fetcher=Task4SafeFetcher(fetched),
                vision=Task4Vision(_task4_assessment(vision_module)),
            )
        )

    item = _task4_manifest_item(
        _task4_manifest(state_dir / "wardrobe_assets_v2.json"), "g051"
    )
    assert [receipt["source_url"] for receipt in item["receipt_history"]] == [
        "https://museum.example/object/1",
        "https://museum.example/object/2",
    ]
    assert [receipt["image_url"] for receipt in item["receipt_history"]] == [
        f"https://api.openverse.org/v1/images/{_RULING_F_UUID}/thumb/",
        f"https://api.openverse.org/v1/images/{_RULING_F_UUID}/thumb/",
    ]
    source_history = [
        json.loads(line)
        for line in (state_dir / "wardrobe_s16_sources.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    openverse_history = [
        record
        for record in source_history
        if record["receipt"]["provider"] == "openverse"
    ]
    assert [record["receipt"]["source_url"] for record in openverse_history] == [
        "https://museum.example/object/1",
        "https://museum.example/object/2",
    ]


@pytest.mark.parametrize("failure_kind", ["source", "safety", "vision"])
def test_ingestion_failure_never_returns_false_ready_and_reports_exact_ids(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
    failure_kind: str,
) -> None:
    garment_file = _write_task4_garment_file(tmp_path, "g051")
    state_dir = tmp_path / "state"
    candidate = _openverse_candidate()
    fetch_outcome: Any = _task4_fetched_image(licensed_assets)
    vision_outcome: Any = _task4_assessment(vision_module)
    if failure_kind == "source":
        candidate = _openverse_candidate(license="nc", license_version="4.0")
    elif failure_kind == "safety":
        fetch_outcome = licensed_assets.ImageFetchError("decode_failed")
    else:
        vision_outcome = vision_module.VisionUnavailable(
            "catalog_asset_quarantined", {"reason_code": "low_confidence"}
        )

    result = asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=_task4_config(
                licensed_ingestion_cli, garment_file=garment_file, state_dir=state_dir
            ),
            response_handler=_official_openverse_handler(
                _openverse_response(candidate),
                thumbnail_response=(
                    httpx.Response(
                        200,
                        content=b"not-an-image",
                        headers={"content-type": "image/png"},
                    )
                    if failure_kind == "safety"
                    else None
                ),
            ),
            fetcher=Task4SafeFetcher(fetch_outcome),
            vision=Task4Vision(vision_outcome),
        )
    )

    assert result.ready_ids == ()
    assert result.quarantined_ids == ("g051",)
    assert result.remaining_ids == ("g051",)
    if config := getattr(result, "manifest_path", None):
        manifest = _task4_manifest(config)
        assert _task4_manifest_item(manifest, "g051")["status"] != "ready"


def test_ingestion_enforces_candidate_retry_concurrency_and_total_budgets(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    garment_file = _write_task4_garment_file(tmp_path, "g051", "g052")
    state_dir = tmp_path / "state"
    fetched = _task4_fetched_image(licensed_assets)
    fetcher = Task4SafeFetcher(fetched, wait=True)
    vision = Task4Vision(
        lambda *, allowed_slot, expected_product_type: _task4_assessment(
            vision_module,
            slot=allowed_slot,
            product_type=expected_product_type,
        )
    )
    search_responses = [
        httpx.Response(503, json={"detail": "bounded retry"}),
        httpx.Response(
            200,
            json=_openverse_response(
                _openverse_candidate(),
                _openverse_candidate(
                    source_url="https://museum.example/object/2",
                    image_url="https://images.example/object-2.png",
                ),
            ),
        ),
        httpx.Response(
            200,
            json=_openverse_response(_openverse_candidate()),
        ),
    ]

    def retrying_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.openverse.org"
        if request.url.path == "/v1/images/":
            return search_responses.pop(0)
        assert request.url.path == _RULING_F_THUMB_PATH
        return httpx.Response(
            200,
            content=_make_image("PNG", size=(16, 12)),
            headers={"content-type": "image/png"},
        )

    config = _task4_config(
        licensed_ingestion_cli,
        garment_file=garment_file,
        state_dir=state_dir,
        candidate_budget=1,
        retry_budget=1,
        concurrency=1,
        total_budget=2,
    )
    result = asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=config,
            response_handler=retrying_handler,
            fetcher=fetcher,
            vision=vision,
        )
    )

    assert result.ready_ids == ("g051", "g052")
    assert result.remaining_ids == ()
    assert result.api_attempts == 3
    assert result.candidate_attempts == 2
    assert fetcher.urls == []
    assert fetcher.max_active == 0
    assert len(search_responses) == 0


def test_ingestion_total_budget_leaves_unattempted_controlled_ids_remaining(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    garment_file = _write_task4_garment_file(tmp_path, "g051", "g052")
    state_dir = tmp_path / "state"
    result = asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=_task4_config(
                licensed_ingestion_cli,
                garment_file=garment_file,
                state_dir=state_dir,
                total_budget=1,
            ),
            response_handler=_official_openverse_handler(
                _openverse_response(_openverse_candidate())
            ),
            fetcher=Task4SafeFetcher(_task4_fetched_image(licensed_assets)),
            vision=Task4Vision(_task4_assessment(vision_module)),
        )
    )

    assert result.ready_ids == ("g051",)
    assert result.quarantined_ids == ()
    assert result.remaining_ids == ("g052",)


@pytest.mark.parametrize(
    "field,value",
    [
        ("candidate_budget", 0),
        ("retry_budget", -1),
        ("concurrency", 0),
        ("total_budget", 0),
    ],
)
def test_ingestion_rejects_unbounded_or_nonpositive_budget_configuration(
    licensed_ingestion_cli: ModuleType,
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    kwargs = {field: value}
    with pytest.raises((ValueError, ValidationError)):
        _task4_config(
            licensed_ingestion_cli,
            garment_file=_write_task4_garment_file(tmp_path, "g051"),
            state_dir=tmp_path / "state",
            **kwargs,
        )


def _task4_mark_takedown(manifest_path: Path) -> bytes:
    manifest = _task4_manifest(manifest_path)
    item = _task4_manifest_item(manifest, "g051")
    item["status"] = "takedown"
    item["processed_sha256"] = None
    item["relative_path"] = None
    item["failure_reason"] = "source_takedown"
    payload = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    manifest_path.write_bytes(payload)
    return payload


def test_ingestion_same_source_takedown_is_preserved_without_fetch_or_vision(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    garment_file = _write_task4_garment_file(tmp_path, "g051")
    state_dir = tmp_path / "state"
    config = _task4_config(
        licensed_ingestion_cli,
        garment_file=garment_file,
        state_dir=state_dir,
    )
    asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=config,
            response_handler=_official_openverse_handler(
                _openverse_response(_openverse_candidate())
            ),
            fetcher=Task4SafeFetcher(_task4_fetched_image(licensed_assets)),
            vision=Task4Vision(_task4_assessment(vision_module)),
        )
    )
    before = _task4_mark_takedown(config.manifest_path)
    fetcher = Task4SafeFetcher(RuntimeError("takedown_must_not_refetch"))
    vision = Task4Vision(RuntimeError("takedown_must_not_reassess"))

    result = asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=config,
            response_handler=_official_openverse_handler(
                _openverse_response(_openverse_candidate())
            ),
            fetcher=fetcher,
            vision=vision,
        )
    )

    assert result.ready_ids == ()
    assert result.quarantined_ids == ("g051",)
    assert result.remaining_ids == ("g051",)
    assert fetcher.urls == []
    assert vision.calls == []
    assert config.manifest_path.read_bytes() == before


def test_same_openverse_uuid_with_different_raw_url_cannot_bypass_takedown(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _task4_config(
        cli,
        garment_file=_write_task4_garment_file(tmp_path, "g051"),
        state_dir=tmp_path / "state",
        resume=True,
    )
    first_candidate = _openverse_candidate(
        image_url="https://images.example/provider-raw-a.png"
    )
    first = asyncio.run(
        _run_task4_ingestion(
            cli,
            config=config,
            response_handler=_official_openverse_handler(
                _openverse_response(first_candidate)
            ),
            fetcher=Task4SafeFetcher(_task4_fetched_image(licensed_assets)),
            vision=Task4Vision(_task4_assessment(vision_module)),
        )
    )
    assert first.ready_ids == ("g051",)
    before = _task4_mark_takedown(config.manifest_path)
    thumbnail_requests: list[httpx.Request] = []

    result = asyncio.run(
        _run_task4_ingestion(
            cli,
            config=config,
            response_handler=_official_openverse_handler(
                _openverse_response(
                    _openverse_candidate(
                        image_url="https://attacker.example/provider-raw-b.png"
                    )
                ),
                thumbnail_requests=thumbnail_requests,
            ),
            fetcher=Task4SafeFetcher(RuntimeError("takedown_must_not_refetch")),
            vision=Task4Vision(RuntimeError("takedown_must_not_reassess")),
        )
    )

    assert result.ready_ids == ()
    assert result.quarantined_ids == ("g051",)
    assert result.item_outcomes[0].reason_code == "source_takedown"
    assert thumbnail_requests == []
    assert config.manifest_path.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("provider_item_id", "123E4567-E89B-12D3-A456-426614174000"),
        (
            "image_url",
            "https://api.openverse.org/v1/images/"
            "123e4567-e89b-12d3-a456-426614174001/thumb/",
        ),
    ],
    ids=["noncanonical_uuid", "endpoint_uuid_mismatch"],
)
def test_preserved_openverse_receipt_requires_canonical_uuid_bound_thumbnail_endpoint(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
    field: str,
    forged_value: str,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _task4_config(
        cli,
        garment_file=_write_task4_garment_file(tmp_path, "g051"),
        state_dir=tmp_path / "state",
        resume=True,
    )
    asyncio.run(
        _run_task4_ingestion(
            cli,
            config=config,
            response_handler=_official_openverse_handler(
                _openverse_response(_openverse_candidate())
            ),
            fetcher=Task4SafeFetcher(_task4_fetched_image(licensed_assets)),
            vision=Task4Vision(_task4_assessment(vision_module)),
        )
    )

    manifest = _task4_manifest(config.manifest_path)
    _task4_manifest_item(manifest, "g051")["receipt_history"][-1][field] = (
        forged_value
    )
    config.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    source_records = [
        json.loads(line)
        for line in config.sources_path.read_text(encoding="utf-8").splitlines()
    ]
    source_record = next(
        record for record in source_records if record["garment_id"] == "g051"
    )
    source_record["receipt"][field] = forged_value
    config.sources_path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in source_records
        ),
        encoding="utf-8",
    )
    provider_calls: list[httpx.Request] = []

    def no_provider_access(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request)
        return httpx.Response(500)

    with pytest.raises(ValueError, match="invalid_source_receipt"):
        asyncio.run(
            _run_task4_ingestion(
                cli,
                config=config,
                response_handler=no_provider_access,
                fetcher=Task4SafeFetcher(RuntimeError("must_not_fetch")),
                vision=Task4Vision(RuntimeError("must_not_assess")),
            )
        )
    assert provider_calls == []


def test_ingestion_takedown_allows_separately_provenanced_replacement(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    garment_file = _write_task4_garment_file(tmp_path, "g051")
    state_dir = tmp_path / "state"
    config = _task4_config(
        licensed_ingestion_cli,
        garment_file=garment_file,
        state_dir=state_dir,
        resume=True,
    )
    fetched = _task4_fetched_image(licensed_assets)
    asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=config,
            response_handler=_official_openverse_handler(
                _openverse_response(_openverse_candidate())
            ),
            fetcher=Task4SafeFetcher(fetched),
            vision=Task4Vision(_task4_assessment(vision_module)),
        )
    )
    _task4_mark_takedown(config.manifest_path)

    result = asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=config,
            response_handler=_official_openverse_handler(
                _openverse_response(
                    _openverse_candidate(
                        source_url="https://museum.example/object/2",
                        image_url="https://images.example/object-2.png",
                    )
                )
            ),
            fetcher=Task4SafeFetcher(fetched),
            vision=Task4Vision(_task4_assessment(vision_module)),
        )
    )

    assert result.ready_ids == ("g051",)
    item = _task4_manifest_item(_task4_manifest(config.manifest_path), "g051")
    assert item["status"] == "ready"
    assert [receipt["source_url"] for receipt in item["receipt_history"]] == [
        "https://museum.example/object/1",
        "https://museum.example/object/2",
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_id",
        "wrong_owner",
        "wrong_slot",
        "wrong_audience",
        "unsafe_ready_path",
        "nonready_serveable",
        "unknown_field",
        "receipt_hash_mismatch",
        "license_authority_mismatch",
    ],
)
def test_ingestion_rejects_invalid_prior_manifest_before_provider_access(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
    mutation: str,
) -> None:
    garment_file = _write_task4_garment_file(tmp_path, "g051")
    state_dir = tmp_path / "state"
    config = _task4_config(
        licensed_ingestion_cli,
        garment_file=garment_file,
        state_dir=state_dir,
        resume=True,
    )
    asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=config,
            response_handler=_official_openverse_handler(
                _openverse_response(_openverse_candidate())
            ),
            fetcher=Task4SafeFetcher(_task4_fetched_image(licensed_assets)),
            vision=Task4Vision(_task4_assessment(vision_module)),
        )
    )
    manifest = _task4_manifest(config.manifest_path)
    item = _task4_manifest_item(manifest, "g051")
    if mutation == "unknown_id":
        item["garment_id"] = "g999"
    elif mutation == "wrong_owner":
        item["user_id"] = "u02"
    elif mutation == "wrong_slot":
        item["slot"] = "dress"
    elif mutation == "wrong_audience":
        item["audience"] = "womenswear"
    elif mutation == "unsafe_ready_path":
        item["relative_path"] = "../escape.png"
    elif mutation == "nonready_serveable":
        item["status"] = "takedown"
    elif mutation == "unknown_field":
        item["unexpected"] = True
    elif mutation == "receipt_hash_mismatch":
        item["receipt_history"][-1]["processed_sha256"] = "0" * 64
    else:
        item["license"]["code"] = "CC-BY-3.0"
    config.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    provider_calls: list[httpx.Request] = []

    def no_provider_access(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request)
        return httpx.Response(500, json={"detail": "must_not_call"})

    with pytest.raises(ValueError):
        asyncio.run(
            _run_task4_ingestion(
                licensed_ingestion_cli,
                config=config,
                response_handler=no_provider_access,
                fetcher=Task4SafeFetcher(RuntimeError("must_not_fetch")),
                vision=Task4Vision(RuntimeError("must_not_assess")),
            )
        )
    assert provider_calls == []


def test_ingestion_invalid_source_history_cannot_publish_ready_manifest_or_bytes(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    garment_file = _write_task4_garment_file(tmp_path, "g051")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = _task4_config(
        licensed_ingestion_cli,
        garment_file=garment_file,
        state_dir=state_dir,
    )
    invalid_history = b'{"existing":true}'
    config.sources_path.write_bytes(invalid_history)

    with pytest.raises(ValueError):
        asyncio.run(
            _run_task4_ingestion(
                licensed_ingestion_cli,
                config=config,
                response_handler=_official_openverse_handler(
                    _openverse_response(_openverse_candidate())
                ),
                fetcher=Task4SafeFetcher(_task4_fetched_image(licensed_assets)),
                vision=Task4Vision(_task4_assessment(vision_module)),
            )
        )

    assert config.sources_path.read_bytes() == invalid_history
    assert not config.manifest_path.exists()
    assert not config.asset_directory.exists()


def test_ingestion_receipt_write_failure_cannot_publish_ready_manifest(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    garment_file = _write_task4_garment_file(tmp_path, "g051")
    config = _task4_config(
        cli,
        garment_file=garment_file,
        state_dir=tmp_path / "state",
    )
    original_atomic_write = cli._atomic_write_bytes

    def fail_receipt_write(path: Path, payload: bytes) -> None:
        if Path(path) == config.sources_path:
            raise OSError("simulated_receipt_write_failure")
        original_atomic_write(path, payload)

    monkeypatch.setattr(cli, "_atomic_write_bytes", fail_receipt_write)

    with pytest.raises(OSError, match="simulated_receipt_write_failure"):
        asyncio.run(
            _run_task4_ingestion(
                cli,
                config=config,
                response_handler=_official_openverse_handler(
                    _openverse_response(_openverse_candidate())
                ),
                fetcher=Task4SafeFetcher(_task4_fetched_image(licensed_assets)),
                vision=Task4Vision(_task4_assessment(vision_module)),
            )
        )

    assert not config.sources_path.exists()
    assert not config.manifest_path.exists()


def test_ingestion_cli_uses_settings_bound_safe_image_config(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    offline_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    settings = replace(
        offline_settings,
        licensed_asset_max_response_bytes=123_456,
    )
    captured: dict[str, Any] = {}

    def capture_fetcher(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    async def capture_run(config: Any, *_args: Any, **_kwargs: Any) -> Any:
        return cli.IngestionResult(
            dry_run=True,
            ready_ids=(),
            reused_ids=(),
            quarantined_ids=(),
            remaining_ids=(),
            api_attempts=0,
            candidate_attempts=0,
            manifest_path=config.manifest_path,
        )

    class TestSettings:
        @classmethod
        def from_env(cls) -> Any:
            return settings

    monkeypatch.setattr(cli, "Settings", TestSettings)
    monkeypatch.setattr(cli, "SafeImageFetcher", capture_fetcher)
    monkeypatch.setattr(cli, "VisionAdapter", lambda _settings: object())
    monkeypatch.setattr(cli, "run_ingestion", capture_run)
    config = cli.IngestionConfig(
        provider="user-owned",
        garment_file=_write_task4_garment_file(tmp_path, "g051"),
        source_map=tmp_path / "sources.jsonl",
        dry_run=True,
    )

    asyncio.run(cli._run_cli(config))

    assert captured["config"] == licensed_assets.SafeImageConfig.from_settings(settings)


def _task4_ai_reference_state(
    tmp_path: Path,
    *,
    mutation: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(__file__).parents[1]
    v1_manifest = json.loads(
        (root / "data" / "manifests" / "wardrobe_generated_v1.json").read_text(
            encoding="utf-8"
        )
    )
    v1_item = next(
        item for item in v1_manifest["items"] if item["garment_id"] == "g001"
    )
    garment = json.loads(
        next(
            line
            for line in (root / "data" / "fixtures" / "garments.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if json.loads(line)["garment_id"] == "g001"
        )
    )
    receipt = {
        "provider": "cpa_generated",
        "provider_item_id": "g001",
        "provider_license": "ai-generated",
        "provider_license_version": "wardrobe_generated_v1_s12r2",
        "license_code": "ai-generated",
        "license_url": None,
        "creator": None,
        "source_url": None,
        "image_url": None,
        "source_ref": "data/manifests/wardrobe_generated_v1.json#g001",
        "expected_sha256": v1_item["sha256"],
        "attribution": "AI 生成参考",
        "imported_at": v1_item["provenance_normalized_at"],
        "original_sha256": v1_item["sha256"],
        "processed_sha256": v1_item["sha256"],
        "processing": "verified_s12r_ai_reference_passthrough",
    }
    item = {
        "garment_id": "g001",
        "user_id": garment["user_id"],
        "slot": garment["slot"],
        "audience": garment.get("audience", "womenswear"),
        "source_kind": "ai_generated_reference",
        "status": "ready",
        "original_sha256": v1_item["sha256"],
        "processed_sha256": v1_item["sha256"],
        "relative_path": v1_item["relative_path"],
        "license": {
            "code": "ai-generated",
            "name": "AI 生成参考",
            "url": None,
            "author": None,
            "attribution": "AI 生成参考",
        },
        "receipt_history": [receipt],
    }
    if mutation == "forged_hash":
        item["original_sha256"] = "0" * 64
        item["processed_sha256"] = "0" * 64
        receipt["expected_sha256"] = "0" * 64
        receipt["original_sha256"] = "0" * 64
        receipt["processed_sha256"] = "0" * 64
    elif mutation == "wrong_source_ref":
        receipt["source_ref"] = "data/manifests/wardrobe_generated_v1.json#g002"
    elif mutation == "wrong_processing":
        receipt["processing"] = "safe_decode_normalize_then_server_crop"
    elif mutation == "wrong_contract_version":
        receipt["provider_license_version"] = "wardrobe_generated_v1"
    elif mutation == "wrong_relative_path":
        item["relative_path"] = "data/assets/wardrobe_generated_v1/g002.png"
    elif mutation == "forged_imported_at":
        receipt["imported_at"] = "2026-08-23T00:00:00+00:00"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "wardrobe_s16_sources.jsonl").write_text(
        json.dumps({"garment_id": "g001", "receipt": receipt}, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    (state_dir / "wardrobe_assets_v2.json").write_text(
        json.dumps(
            {"manifest_version": "wardrobe_assets_v2", "items": [item]},
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return item, receipt


def test_ingestion_resume_strictly_preserves_verified_v1_ai_reference_subset(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    expected_item, _ = _task4_ai_reference_state(tmp_path)
    config = _task4_config(
        licensed_ingestion_cli,
        garment_file=_write_task4_garment_file(tmp_path, "g051"),
        state_dir=tmp_path / "state",
        resume=True,
    )

    result = asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=config,
            response_handler=_official_openverse_handler(_openverse_response()),
            fetcher=Task4SafeFetcher(RuntimeError("must_not_fetch")),
            vision=Task4Vision(RuntimeError("must_not_assess")),
        )
    )

    assert result.quarantined_ids == ("g051",)
    persisted = _task4_manifest(config.manifest_path)
    assert next(
        item for item in persisted["items"] if item["garment_id"] == "g001"
    ) == expected_item


@pytest.mark.parametrize(
    "mutation",
    [
        "forged_hash",
        "wrong_source_ref",
        "wrong_processing",
        "wrong_contract_version",
        "wrong_relative_path",
        "forged_imported_at",
    ],
)
def test_ingestion_rejects_forged_ai_reference_before_provider_access(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
    mutation: str,
) -> None:
    _task4_ai_reference_state(tmp_path, mutation=mutation)
    config = _task4_config(
        licensed_ingestion_cli,
        garment_file=_write_task4_garment_file(tmp_path, "g051"),
        state_dir=tmp_path / "state",
        resume=True,
    )
    provider_calls: list[httpx.Request] = []

    def no_provider_access(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request)
        return httpx.Response(500)

    with pytest.raises(ValueError):
        asyncio.run(
            _run_task4_ingestion(
                licensed_ingestion_cli,
                config=config,
                response_handler=no_provider_access,
                fetcher=Task4SafeFetcher(RuntimeError("must_not_fetch")),
                vision=Task4Vision(RuntimeError("must_not_assess")),
            )
        )
    assert provider_calls == []


def test_ingestion_blank_state_composes_50_verified_ai_references_and_mock_extension(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    config = _task4_config(
        licensed_ingestion_cli,
        garment_file=_write_task4_garment_file(tmp_path, "g051"),
        state_dir=tmp_path / "state",
    )

    result = asyncio.run(
        _run_task4_ingestion(
            licensed_ingestion_cli,
            config=config,
            response_handler=_official_openverse_handler(
                _openverse_response(_openverse_candidate())
            ),
            fetcher=Task4SafeFetcher(_task4_fetched_image(licensed_assets)),
            vision=Task4Vision(_task4_assessment(vision_module)),
        )
    )

    assert result.ready_ids == ("g051",)
    items = _task4_manifest(config.manifest_path)["items"]
    assert len(items) == 51
    ai_items = [
        item for item in items if item["source_kind"] == "ai_generated_reference"
    ]
    assert [item["garment_id"] for item in ai_items] == [
        f"g{number:03d}" for number in range(1, 51)
    ]
    assert all(item["status"] == "ready" for item in ai_items)
    assert all(item["license"]["code"] == "ai-generated" for item in ai_items)
    assert all(
        item["original_sha256"]
        == item["processed_sha256"]
        == item["receipt_history"][-1]["expected_sha256"]
        for item in ai_items
    )
    assert all(
        item["receipt_history"][-1]["provider"] == "cpa_generated"
        for item in ai_items
    )
    source_records = [
        json.loads(line)
        for line in config.sources_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(source_records) == 51
    assert sum(
        record["receipt"]["provider"] == "cpa_generated"
        for record in source_records
    ) == 50
    extension = next(item for item in items if item["garment_id"] == "g051")
    assert extension["source_kind"] == "licensed_photo"
    assert extension["status"] == "ready"


def test_ingestion_blank_state_rejects_mutated_v1_reference_before_provider_access(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    root = Path(__file__).parents[1]
    v1_manifest = json.loads(
        (root / "data" / "manifests" / "wardrobe_generated_v1.json").read_text(
            encoding="utf-8"
        )
    )
    v1_manifest["items"][-1]["sha256"] = "0" * 64
    mutated_manifest = tmp_path / "mutated-wardrobe-generated-v1.json"
    mutated_manifest.write_text(
        json.dumps(v1_manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "AI_REFERENCE_MANIFEST", mutated_manifest)
    cli._verified_ai_reference.cache_clear()
    provider_calls: list[httpx.Request] = []

    def no_provider_access(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request)
        return httpx.Response(500)

    config = _task4_config(
        cli,
        garment_file=_write_task4_garment_file(tmp_path, "g051"),
        state_dir=tmp_path / "state",
    )
    with pytest.raises(ValueError):
        asyncio.run(
            _run_task4_ingestion(
                cli,
                config=config,
                response_handler=no_provider_access,
                fetcher=Task4SafeFetcher(RuntimeError("must_not_fetch")),
                vision=Task4Vision(RuntimeError("must_not_assess")),
            )
        )

    assert provider_calls == []
    assert not config.manifest_path.exists()
    assert not config.sources_path.exists()
    assert not config.asset_directory.exists()


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    [
        (
            httpx.Response(200, json=_openverse_response()),
            "no_results",
        ),
        (
            httpx.Response(
                429,
                content=b"provider prose: wait at https://provider.example/private",
            ),
            "provider_rate_limited",
        ),
        (
            httpx.Response(
                401,
                content=b"provider prose: account denied https://provider.example/private",
            ),
            "provider_auth_rejected",
        ),
        (
            httpx.Response(
                503,
                content=b"provider prose: maintenance https://provider.example/private",
            ),
            "provider_unavailable",
        ),
        (
            httpx.Response(
                200,
                content=b'{"results":"not a list"}',
                headers={"content-type": "application/json"},
            ),
            "provider_schema_invalid",
        ),
        (
            httpx.Response(
                200,
                content=b"x" * (1024 * 1024 + 1),
                headers={"content-type": "application/json"},
            ),
            "provider_response_too_large",
        ),
    ],
)
def test_openverse_search_exposes_only_bounded_controlled_failure_reasons(
    licensed_ingestion_cli: ModuleType,
    tmp_path: Path,
    response: httpx.Response,
    expected_reason: str,
) -> None:
    """A changed status/schema branch must not leak provider-controlled details."""

    cli = licensed_ingestion_cli._load()
    config = _task4_config(
        cli,
        garment_file=_write_task4_garment_file(tmp_path, "g051"),
        state_dir=tmp_path / "state",
        candidate_budget=3,
        retry_budget=0,
        total_budget=1,
    )
    garment = cli._controlled_garments(config.garment_file)[0]

    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    async def search() -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=TASK4_OPENVERSE_BASE,
        ) as api_client:
            return await cli._search_openverse(
                api_client=api_client,
                garment=garment,
                config=config,
                counters=cli._Counters(),
            )

    outcome = asyncio.run(search())

    assert outcome.candidates == ()
    assert outcome.reason_code == expected_reason
    assert outcome.reason_code in {
        "no_results",
        "provider_rate_limited",
        "provider_auth_rejected",
        "provider_unavailable",
        "provider_schema_invalid",
        "provider_response_too_large",
    }
    public_outcome = repr(outcome)
    assert "provider prose" not in public_outcome
    assert "https://provider.example/private" not in public_outcome


def test_g051_search_uses_frozen_monotonic_plan_and_shared_search_budget_without_persistence(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Changing name/search prose must not change the closed diagnostic query plan."""

    cli = licensed_ingestion_cli._load()
    garment_file = _write_task4_garment_file(tmp_path, "g051")
    config = _task4_config(
        cli,
        garment_file=garment_file,
        state_dir=tmp_path / "state",
        dry_run=True,
        candidate_budget=3,
        retry_budget=0,
        concurrency=1,
        total_budget=1,
        search_budget=3,
    )
    queries: list[str] = []

    def empty_results(request: httpx.Request) -> httpx.Response:
        queries.append(str(request.url.params["q"]))
        return httpx.Response(200, json=_openverse_response())

    result = asyncio.run(
        _run_task4_ingestion(
            cli,
            config=config,
            response_handler=empty_results,
            fetcher=Task4SafeFetcher(_task4_fetched_image(licensed_assets)),
            vision=Task4Vision(_task4_assessment(vision_module)),
        )
    )

    assert queries == [
        "blouse",
        "navy blouse",
        "women's blouse flat lay",
    ]
    assert result.api_attempts == 3
    assert result.candidate_attempts == 0
    assert result.remaining_ids == ("g051",)
    forbidden_query_text = {
        "飘带衬衫",
        "synthetic",
        "藏青色",
        "合成材质",
        "约会",
        "会议",
        "soft",
        "smart",
        "u01",
        "g051",
    }
    query_surfaces = "\n".join(queries) + capsys.readouterr().out + caplog.text
    assert not any(value in query_surfaces for value in forbidden_query_text)
    assert not config.manifest_path.exists()
    assert not config.sources_path.exists()
    assert not config.asset_directory.exists()


def test_frozen_query_plan_is_closed_monotonic_and_free_of_raw_garment_text_for_every_slot(
    licensed_ingestion_cli: ModuleType,
) -> None:
    """Any query derived from name/search_text/material/style/occasion is a data leak."""

    cli = licensed_ingestion_cli._load()
    expected_product = {
        "top": "blouse",
        "bottom": "trousers",
        "dress": "dress",
        "outer": "jacket",
        "shoes": "shoes",
        "bag": "handbag",
        "accessory": "accessory",
    }
    garments = cli._controlled_garments(cli.CONTROLLED_GARMENT_FILE)
    representative_by_slot: dict[str, Any] = {}
    for garment in garments:
        representative_by_slot.setdefault(garment.slot, garment)

    assert set(representative_by_slot) == set(expected_product)
    for slot, garment in representative_by_slot.items():
        queries = cli._frozen_openverse_query_plan(garment)
        product = expected_product[slot]
        assert queries == (
            product,
            f"{garment.color} {product}",
            f"women's {product} flat lay",
        )
        assert 1 <= len(queries) <= 3
        assert all(query.isascii() for query in queries)
        assert all(query == query.strip() and len(query) <= 100 for query in queries)
        raw_inputs = {
            garment.garment_id,
            garment.user_id,
            garment.name,
            garment.search_text,
            garment.material,
            *garment.occasions,
            *garment.styles,
        }
        public_queries = "\n".join(queries)
        assert not any(value and value in public_queries for value in raw_inputs)


def test_cli_openverse_client_ignores_malformed_proxy_environment_without_auth_state(
    licensed_ingestion_cli: ModuleType,
    offline_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Removing trust_env or adding proxy/auth/hooks must fail this constructor spy."""

    cli = licensed_ingestion_cli._load()
    monkeypatch.setenv("NO_PROXY", "%%%malformed-no-proxy%%%")
    environment_before = dict(os.environ)
    constructed: dict[str, Any] = {}
    received: dict[str, Any] = {}

    class OfficialClientSpy:
        def __init__(self, **kwargs: Any) -> None:
            constructed.update(kwargs)

        async def __aenter__(self) -> "OfficialClientSpy":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    class TestSettings:
        @classmethod
        def from_env(cls) -> Any:
            return offline_settings

    async def capture_run(
        config: Any,
        api_client: Any,
        safe_fetcher: Any,
        vision: Any,
    ) -> Any:
        received.update(
            {
                "config": config,
                "api_client": api_client,
                "safe_fetcher": safe_fetcher,
                "vision": vision,
            }
        )
        return cli.IngestionResult(
            dry_run=True,
            ready_ids=(),
            reused_ids=(),
            quarantined_ids=(),
            remaining_ids=(),
            api_attempts=0,
            candidate_attempts=0,
            manifest_path=config.manifest_path,
        )

    monkeypatch.setattr(cli, "Settings", TestSettings)
    monkeypatch.setattr(cli, "SafeImageFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(cli, "VisionAdapter", lambda _settings: object())
    monkeypatch.setattr(cli.httpx, "AsyncClient", OfficialClientSpy)
    monkeypatch.setattr(cli, "run_ingestion", capture_run)
    config = _task4_config(
        cli,
        garment_file=_write_task4_garment_file(tmp_path, "g051"),
        state_dir=tmp_path / "state",
        dry_run=True,
        total_budget=1,
    )

    asyncio.run(cli._run_cli(config))

    assert received["api_client"].__class__ is OfficialClientSpy
    assert constructed["trust_env"] is False
    assert constructed["follow_redirects"] is False
    assert constructed["base_url"] == cli.OPENVERSE_API_BASE
    assert not {"proxy", "proxies", "auth", "cookies", "event_hooks"} & set(constructed)
    assert constructed.get("verify", True) is True
    assert constructed["headers"]["Accept-Encoding"] == "identity"
    assert constructed["headers"]["Accept"] == "image/png,image/jpeg,image/webp"
    timeout = constructed["timeout"]
    assert 0 < timeout.connect <= 5.0
    assert 0 < timeout.read <= 15.0
    assert 0 < timeout.write <= 5.0
    assert 0 < timeout.pool <= 5.0
    assert dict(os.environ) == environment_before


def test_g051_diagnostic_canary_config_limits_search_candidates_fetches_and_persistence(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    """Raising any diagnostic canary bound must be rejected before an external run."""

    cli = licensed_ingestion_cli._load()
    config = _task4_config(
        cli,
        garment_file=_write_task4_garment_file(tmp_path, "g051"),
        state_dir=tmp_path / "state",
        dry_run=True,
        candidate_budget=3,
        retry_budget=0,
        concurrency=1,
        total_budget=1,
        search_budget=3,
        diagnostic_canary=True,
    )
    candidates = [
        _openverse_candidate(
            id=f"123e4567-e89b-12d3-a456-42661417400{number}",
            source_url=f"https://museum.example/object/{number}",
            image_url=f"https://images.example/object-{number}.png",
        )
        for number in range(1, 4)
    ]
    fetcher = Task4SafeFetcher(licensed_assets.ImageFetchError("decode_failed"))
    vision = Task4Vision(_task4_assessment(vision_module))
    thumbnail_requests: list[httpx.Request] = []

    result = asyncio.run(
        _run_task4_ingestion(
            cli,
            config=config,
            response_handler=_official_openverse_handler(
                _openverse_response(*candidates),
                thumbnail_response=httpx.Response(
                    200,
                    content=b"not-an-image",
                    headers={"content-type": "image/png"},
                ),
                thumbnail_requests=thumbnail_requests,
            ),
            fetcher=fetcher,
            vision=vision,
        )
    )

    assert result.dry_run is True
    assert result.api_attempts <= 3
    assert result.candidate_attempts == 3
    assert fetcher.urls == []
    assert len(thumbnail_requests) == 3
    assert vision.calls == []
    assert result.ready_ids == ()
    assert result.quarantined_ids == ("g051",)
    assert result.remaining_ids == ("g051",)
    assert not config.resume
    assert not config.manifest_path.exists()
    assert not config.sources_path.exists()
    assert not config.asset_directory.exists()


def test_ingestion_vision_boundary_reports_only_call_count_and_model_provenance(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    """A future authority argument or raw Vision trace must not cross the result boundary."""

    cli = licensed_ingestion_cli._load()

    class AuthorityFreeVision(Task4Vision):
        model_provenance = "mock-catalog-model-v1"

        async def inspect_catalog_asset(
            self,
            *,
            image_bytes: bytes,
            mime_type: str,
            allowed_slot: str,
            expected_product_type: str,
        ) -> tuple[Any, dict[str, object]]:
            assert image_bytes
            assert mime_type == "image/png"
            assert allowed_slot == "top"
            assert expected_product_type == "tie-neck blouse"
            return await super().inspect_catalog_asset(
                image_bytes=image_bytes,
                mime_type=mime_type,
                allowed_slot=allowed_slot,
                expected_product_type=expected_product_type,
            )

    vision = AuthorityFreeVision(_task4_assessment(vision_module))
    result = asyncio.run(
        _run_task4_ingestion(
            cli,
            config=_task4_config(
                cli,
                garment_file=_write_task4_garment_file(tmp_path, "g051"),
                state_dir=tmp_path / "state",
                dry_run=True,
                total_budget=1,
            ),
            response_handler=_official_openverse_handler(
                _openverse_response(_openverse_candidate())
            ),
            fetcher=Task4SafeFetcher(_task4_fetched_image(licensed_assets)),
            vision=vision,
        )
    )

    assert result.vision_call_count == 1
    assert result.vision_model_provenance == "mock-catalog-model-v1"
    result_fields = result.model_dump()
    assert not {"garment_id", "user_id", "source_url", "image_url", "license", "vision_trace"} & set(result_fields)


def test_ingestion_result_exposes_only_closed_per_id_failure_reason(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    provider_prose = "private provider prose https://provider.example/private"

    result = asyncio.run(
        _run_task4_ingestion(
            cli,
            config=_task4_config(
                cli,
                garment_file=_write_task4_garment_file(tmp_path, "g051"),
                state_dir=tmp_path / "state",
                dry_run=True,
                candidate_budget=3,
                retry_budget=0,
                concurrency=1,
                total_budget=1,
                search_budget=1,
            ),
            response_handler=lambda _request: httpx.Response(
                429,
                json={"detail": provider_prose},
            ),
            fetcher=Task4SafeFetcher(_task4_fetched_image(licensed_assets)),
            vision=Task4Vision(_task4_assessment(vision_module)),
        )
    )

    assert result.item_outcomes == (
        cli.ItemIngestionOutcome(
            garment_id="g051",
            status="quarantined",
            reason_code="provider_rate_limited",
        ),
    )
    public_json = result.model_dump_json()
    assert provider_prose not in public_json
    assert "provider.example" not in public_json


def test_openverse_query_is_not_exposed_by_httpx_info_transport_logging(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _task4_config(
        cli,
        garment_file=_write_task4_garment_file(tmp_path, "g051"),
        state_dir=tmp_path / "state",
        dry_run=True,
        candidate_budget=3,
        retry_budget=0,
        concurrency=1,
        total_budget=1,
        search_budget=1,
    )

    with caplog.at_level(logging.INFO, logger="httpx"):
        asyncio.run(
            _run_task4_ingestion(
                cli,
                config=config,
                response_handler=lambda _request: httpx.Response(
                    200, json=_openverse_response()
                ),
                fetcher=Task4SafeFetcher(_task4_fetched_image(licensed_assets)),
                vision=Task4Vision(_task4_assessment(vision_module)),
            )
        )

    assert "navy synthetic blouse product flat lay" not in caplog.text
    assert "api.openverse.org/v1/images/?q=" not in caplog.text


def test_diagnostic_canary_cli_forces_closed_g051_dry_run_bounds(
    licensed_ingestion_cli: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    garment_file = _write_task4_garment_file(tmp_path, "g051")
    received: dict[str, Any] = {}

    async def capture_run(config: Any) -> Any:
        received["config"] = config
        return cli.IngestionResult(
            dry_run=True,
            ready_ids=(),
            reused_ids=(),
            quarantined_ids=(),
            remaining_ids=(),
            api_attempts=0,
            candidate_attempts=0,
            manifest_path=config.manifest_path,
        )

    monkeypatch.setattr(cli, "_run_cli", capture_run)

    exit_code = cli.main(
        [
            "--provider",
            "openverse",
            "--license",
            "CC0",
            "--garment-file",
            str(garment_file),
            "--diagnostic-canary",
        ]
    )

    config = received["config"]
    assert exit_code == 0
    assert config.diagnostic_canary is True
    assert config.dry_run is True
    assert config.resume is False
    assert config.candidate_budget == 3
    assert config.retry_budget == 0
    assert config.concurrency == 1
    assert config.total_budget == 1
    assert config.search_budget == 3
    assert tuple(
        garment.garment_id for garment in cli._controlled_garments(config.garment_file)
    ) == ("g051",)


def test_diagnostic_canary_cli_rejects_non_g051_before_run(
    licensed_ingestion_cli: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    called = False

    async def should_not_run(_config: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("diagnostic canary must fail before dependency setup")

    monkeypatch.setattr(cli, "_run_cli", should_not_run)

    with pytest.raises(SystemExit, match="g051"):
        cli.main(
            [
                "--provider",
                "openverse",
                "--license",
                "CC0",
                "--garment-file",
                str(_write_task4_garment_file(tmp_path, "g052")),
                "--diagnostic-canary",
            ]
        )

    assert called is False


# Ruling F: a fake/non-global DNS answer for api.openverse.org must not relax
# Task 2's generic fetcher.  The only contemplated exception is a separate,
# exact UUID-to-official-thumbnail path which remains inside the official
# Openverse client boundary and then reuses Task 2 decoding/normalization.
_RULING_F_UUID = "123e4567-e89b-12d3-a456-426614174000"
_RULING_F_OTHER_UUID = "123e4567-e89b-12d3-a456-426614174001"
_RULING_F_THUMB_PATH = f"/v1/images/{_RULING_F_UUID}/thumb/"


def test_trusted_openverse_thumbnail_uses_only_same_canonical_uuid_path_and_task2_normalization(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    """Using metadata url/host instead of the validated UUID would be an SSRF bypass."""

    cli = licensed_ingestion_cli._load()
    thumbnail_requests: list[httpx.Request] = []
    source_url = (
        "https://api.openverse.org/v1/images/"
        f"{_RULING_F_OTHER_UUID}/thumb/"
    )
    jpeg = _make_image("JPEG", size=(16, 12))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/images/":
            return httpx.Response(
                200,
                headers={"set-cookie": "untrusted_search_state=private"},
                json=_openverse_response(
                    _openverse_candidate(id=_RULING_F_UUID, image_url=source_url)
                ),
            )
        thumbnail_requests.append(request)
        assert request.method == "GET"
        assert request.url == httpx.URL(
            f"https://api.openverse.org{_RULING_F_THUMB_PATH}"
        )
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["accept"] == "image/png,image/jpeg,image/webp"
        assert "cookie" not in request.headers
        return httpx.Response(200, content=jpeg, headers={"content-type": "image/jpeg"})

    generic_fetcher = Task4SafeFetcher(
        licensed_assets.ImageFetchError("unsafe_address")
    )
    vision = Task4Vision(_task4_assessment(vision_module))
    result = asyncio.run(
        _run_task4_ingestion(
            cli,
            config=_task4_config(
                cli,
                garment_file=_write_task4_garment_file(tmp_path, "g051"),
                state_dir=tmp_path / "state",
                dry_run=True,
                candidate_budget=1,
                retry_budget=0,
                concurrency=1,
                total_budget=1,
                search_budget=1,
            ),
            response_handler=handler,
            fetcher=generic_fetcher,
            vision=vision,
        )
    )

    assert result.ready_ids == ("g051",)
    assert len(thumbnail_requests) == 1
    assert generic_fetcher.urls == []
    assert len(vision.calls) == 1
    # JPEG input is normalized locally by the already accepted Task 2 proof
    # before the authority-free Vision bridge sees it.
    assert set(vision.calls[0]) == {
        "image_bytes",
        "mime_type",
        "allowed_slot",
        "expected_product_type",
    }
    assert vision.calls[0]["mime_type"] == "image/png"
    assert vision.calls[0]["allowed_slot"] == "top"
    assert vision.calls[0]["expected_product_type"] == "tie-neck blouse"
    assert vision.calls[0]["image_bytes"].startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.parametrize(
    "provider_item_id",
    [
        "123e4567-e89b-12d3-a456-426614174000/../other",
        "123e4567-e89b-12d3-a456-426614174000%2fother",
        "123e4567-e89b-12d3-a456-426614174000?other",
        "123e4567-e89b-12d3-a456-426614174000#other",
        "123e4567-e89b-12d3-a456-426614174000@api.openverse.org",
        "123e4567-e89b-12d3-a456-426614174000:443",
        "123E4567-E89B-12D3-A456-426614174000",
        "not-a-canonical-uuid",
    ],
)
def test_trusted_thumbnail_rejects_noncanonical_or_url_shaped_provider_id_before_any_thumbnail_request(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
    provider_item_id: str,
) -> None:
    """A malformed Openverse ID must never become a path, URL, or generic fetch."""

    cli = licensed_ingestion_cli._load()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/v1/images/"
        return httpx.Response(
            200,
            json=_openverse_response(_openverse_candidate(id=provider_item_id)),
        )

    generic_fetcher = Task4SafeFetcher(
        licensed_assets.ImageFetchError("unsafe_address")
    )
    vision = Task4Vision(_task4_assessment(vision_module))
    result = asyncio.run(
        _run_task4_ingestion(
            cli,
            config=_task4_config(
                cli,
                garment_file=_write_task4_garment_file(tmp_path, "g051"),
                state_dir=tmp_path / "state",
                dry_run=True,
                candidate_budget=1,
                retry_budget=0,
                concurrency=1,
                total_budget=1,
                search_budget=1,
            ),
            response_handler=handler,
            fetcher=generic_fetcher,
            vision=vision,
        )
    )

    assert result.quarantined_ids == ("g051",)
    assert result.item_outcomes[0].reason_code == "candidate_budget_exhausted"
    assert len(calls) == 1
    assert generic_fetcher.urls == []
    assert vision.calls == []


@pytest.mark.parametrize(
    ("thumbnail_response", "secret"),
    [
        (httpx.Response(302, headers={"location": "https://evil.example/image.png"}), "evil.example"),
        (
            httpx.Response(
                200,
                content=gzip.compress(_make_image("PNG")),
                headers={"content-type": "image/png", "content-encoding": "gzip"},
            ),
            "gzip",
        ),
        (
            httpx.Response(
                200,
                content=b"x" * (26 * 1024 * 1024),
                headers={"content-type": "image/png"},
            ),
            "x" * 32,
        ),
        (
            httpx.Response(
                200,
                content=b"<private-html-body>",
                headers={"content-type": "text/html"},
            ),
            "private-html-body",
        ),
        (
            httpx.Response(
                200,
                content=b"not-a-real-image",
                headers={"content-type": "image/png"},
            ),
            "not-a-real-image",
        ),
    ],
    ids=["redirect", "nonidentity", "oversize", "mime", "decode"],
)
def test_trusted_thumbnail_rejects_unsafe_response_before_vision_without_leaking_content(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    thumbnail_response: httpx.Response,
    secret: str,
) -> None:
    """Redirect/body/MIME failures must stop before Vision and expose only a closed outcome."""

    cli = licensed_ingestion_cli._load()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/images/":
            return httpx.Response(
                200,
                json=_openverse_response(_openverse_candidate(id=_RULING_F_UUID)),
            )
        assert request.url.path == _RULING_F_THUMB_PATH
        return thumbnail_response

    generic_fetcher = Task4SafeFetcher(
        licensed_assets.ImageFetchError("unsafe_address")
    )
    vision = Task4Vision(_task4_assessment(vision_module))
    with caplog.at_level(logging.INFO):
        result = asyncio.run(
            _run_task4_ingestion(
                cli,
                config=_task4_config(
                    cli,
                    garment_file=_write_task4_garment_file(tmp_path, "g051"),
                    state_dir=tmp_path / "state",
                    dry_run=True,
                    candidate_budget=1,
                    retry_budget=0,
                    concurrency=1,
                    total_budget=1,
                    search_budget=1,
                ),
                response_handler=handler,
                fetcher=generic_fetcher,
                vision=vision,
            )
        )

    assert result.ready_ids == ()
    assert result.quarantined_ids == ("g051",)
    assert result.item_outcomes[0].reason_code == "candidate_budget_exhausted"
    assert [request.url.path for request in requests] == [
        "/v1/images/",
        _RULING_F_THUMB_PATH,
    ]
    assert generic_fetcher.urls == []
    assert vision.calls == []
    public_surface = result.model_dump_json() + caplog.text
    assert secret not in public_surface
    assert "evil.example" not in public_surface


def test_trusted_thumbnail_timeout_is_content_free_and_does_not_fall_back_to_generic_fetch(
    licensed_ingestion_cli: ModuleType,
    licensed_assets: ModuleType,
    vision_module: ModuleType,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    private_error = "upstream thumbnail timeout with private receipt"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/images/":
            return httpx.Response(
                200,
                json=_openverse_response(_openverse_candidate(id=_RULING_F_UUID)),
            )
        raise httpx.ReadTimeout(private_error, request=request)

    generic_fetcher = Task4SafeFetcher(licensed_assets.ImageFetchError("unsafe_address"))
    vision = Task4Vision(_task4_assessment(vision_module))
    with caplog.at_level(logging.INFO):
        result = asyncio.run(
            _run_task4_ingestion(
                cli,
                config=_task4_config(
                    cli,
                    garment_file=_write_task4_garment_file(tmp_path, "g051"),
                    state_dir=tmp_path / "state",
                    dry_run=True,
                    candidate_budget=1,
                    retry_budget=0,
                    concurrency=1,
                    total_budget=1,
                    search_budget=1,
                ),
                response_handler=handler,
                fetcher=generic_fetcher,
                vision=vision,
            )
        )

    assert result.quarantined_ids == ("g051",)
    assert result.item_outcomes[0].reason_code == "candidate_budget_exhausted"
    assert [request.url.path for request in requests] == [
        "/v1/images/",
        _RULING_F_THUMB_PATH,
    ]
    assert generic_fetcher.urls == []
    assert vision.calls == []
    assert private_error not in result.model_dump_json() + caplog.text


def test_generic_fetcher_still_rejects_arbitrary_openverse_thumbnail_host_as_unsafe_address(
    licensed_assets: ModuleType,
    tmp_path: Path,
) -> None:
    """The trusted exception is not a general api.openverse.org DNS exception."""

    url = f"https://api.openverse.org{_RULING_F_THUMB_PATH}"
    resolver = ResolverSpy({"api.openverse.org": ("10.0.0.7",)})
    transport = TransportSpy({})
    error = _run_fetch_failure(
        module=licensed_assets,
        url=url,
        resolver=resolver,
        transport=transport,
        ready_dir=tmp_path / "ready",
        quarantine_dir=tmp_path / "quarantine",
    )

    _assert_content_free_error(licensed_assets, error, "unsafe_address", url)
    assert resolver.calls == [("api.openverse.org", 443)]
    assert transport.calls == []


# Task 4 / Ruling G: bounded, in-process CPA-generated asset contract.
_RG_MODEL = "grok-imagine-image-quality"
_RG_PROMPT_VERSION = "womenswear_catalog_reference_prompt_v1"

_RG_EXPECTED_PRODUCT_NOUNS = {
    "g051": "tie-neck blouse", "g052": "square-neck knit top",
    "g053": "draped button-up shirt", "g054": "fitted base-layer top",
    "g055": "puff-sleeve blouse", "g056": "sleeveless knit vest",
    "g057": "collared dress shirt", "g058": "linen shirt",
    "g059": "tailored straight-leg trousers", "g060": "wide-leg trousers",
    "g061": "pleated midi skirt", "g062": "pencil skirt",
    "g063": "straight-leg jeans", "g064": "knit midi skirt",
    "g065": "jogger trousers", "g066": "athletic trousers",
    "g067": "a-line midi skirt", "g068": "waist-fitted work dress",
    "g069": "shirt dress", "g070": "tea dress", "g071": "sheath work dress",
    "g072": "linen travel dress", "g073": "knit dress",
    "g074": "satin evening gown", "g075": "double-breasted blazer",
    "g076": "windbreaker jacket", "g077": "cropped boucle jacket",
    "g078": "trench coat", "g079": "oversized wool overcoat",
    "g080": "hooded sports jacket", "g081": "evening shawl",
    "g082": "low-heel loafers", "g083": "pointed-toe pumps",
    "g084": "ballet flats", "g085": "travel sneakers",
    "g086": "strappy sandals", "g087": "mary jane shoes",
    "g088": "ankle boots", "g089": "structured tote bag",
    "g090": "travel crossbody bag", "g091": "chain clutch bag",
    "g092": "square neck scarf", "g093": "resin earrings",
    "g094": "slim leather belt", "g095": "zip-up crop top",
    "g096": "crew-neck cardigan", "g097": "satin evening blouse",
    "g098": "cigarette trousers", "g099": "linen wide-leg trousers",
    "g100": "satin midi skirt", "g101": "square-neck dress",
    "g102": "polo sports dress", "g103": "linen blazer",
    "g104": "denim jacket", "g105": "running shoes",
    "g106": "low-heel mule shoes", "g107": "sun-protection shirt",
    "g108": "knit sweater", "g109": "mock-neck knit top",
    "g110": "cargo trousers", "g111": "slit midi skirt",
    "g112": "tapered casual trousers", "g113": "a-line day dress",
    "g114": "tailored blazer dress", "g115": "long knit cardigan",
    "g116": "formal blazer", "g117": "leather derby shoes",
    "g118": "soft leather shoulder bag", "g119": "sun hat",
    "g120": "faux pearl necklace",
}


class RulingGImageProvider:
    requested_model = _RG_MODEL
    transport_model = _RG_MODEL

    def __init__(self, payload: bytes, **metadata: Any) -> None:
        self.payload, self.prompts = payload, []
        self.error = metadata.pop("error", None)
        self.metadata = {
            "requested_model": _RG_MODEL, "transport_model": _RG_MODEL,
            "request_model_pinned": True, "cpa_trace_verified": True,
            "model_reported": True, "resolved_model": _RG_MODEL,
            "model_verified": True, "verification_basis": "reported_model_exact",
        }
        self.metadata.update(metadata)

    async def generate_static_2d(self, prompt: str) -> tuple[bytes, str, dict[str, Any]]:
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.payload, "image/png", dict(self.metadata)


class RulingGNoFetch:
    async def fetch(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("generated bytes must not use URL fetch")

    async def load_user_owned(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("generated bytes must not use upload loader")


def _rg_config(cli: ModuleType, tmp_path: Path, *, resume: bool = False) -> Any:
    return cli.IngestionConfig(
        provider="cpa-generated", garment_file=_write_task4_garment_file(tmp_path, "g051"),
        manifest_path=tmp_path / "wardrobe_assets_v2.json",
        sources_path=tmp_path / "wardrobe_s16_sources.jsonl",
        asset_directory=tmp_path / "wardrobe_catalog_reference_v1",
        resume=resume, candidate_budget=1, retry_budget=0, concurrency=1, total_budget=1,
    )


def _rg_run(cli: ModuleType, config: Any, provider: Any, vision: Any) -> Any:
    return asyncio.run(cli.run_ingestion(
        config=config, api_client=None, safe_fetcher=RulingGNoFetch(), vision=vision,
        image_provider=provider, image_model_allowlist=(_RG_MODEL,),
    ))


def _rg_records(config: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    item = _task4_manifest_item(_task4_manifest(config.manifest_path), "g051")
    rows = map(json.loads, config.sources_path.read_text(encoding="utf-8").splitlines())
    return item, next(row["receipt"] for row in rows if row["garment_id"] == "g051")


def test_cpa_generated_prompt_receipt_processing_and_truth_are_bound(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType, tmp_path: Path,
) -> None:
    raw = _make_image_with_authoritative_metadata("PNG")
    provider, vision = RulingGImageProvider(raw), Task4Vision(_task4_assessment(vision_module))
    config = _rg_config(licensed_ingestion_cli, tmp_path)
    assert _rg_run(licensed_ingestion_cli, config, provider, vision).ready_ids == ("g051",)
    prompt, row = provider.prompts[0], _task4_garment_rows("g051")[0]
    lower = prompt.lower()
    assert all(value in lower for value in (
        "women", "blouse", "navy", "synthetic", "soft", "smart", "single", "neutral"))
    assert all(value.lower() not in lower for value in (
        row["garment_id"], row["user_id"], row["name"], row["search_text"],
        "garment_id", "user_id", "owner", "source", "license"))
    assert len(vision.calls) == 1 and vision.calls[0]["image_bytes"] != raw
    with Image.open(BytesIO(vision.calls[0]["image_bytes"])) as normalized:
        normalized.load()
        assert normalized.getexif() == {} and not normalized.info
    item, receipt = _rg_records(config)
    assert receipt == item["receipt_history"][-1]
    assert (receipt["bound_garment_id"], receipt["bound_user_id"]) == ("g051", "u01")
    assert receipt["prompt_template_version"] == _RG_PROMPT_VERSION
    assert (receipt["requested_model"], receipt["transport_model"], receipt["resolved_model"]) == (_RG_MODEL,) * 3
    assert receipt["prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    assert receipt["original_sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["processed_sha256"] == item["processed_sha256"]
    assert item["source_kind"] == "ai_generated_reference"
    assert item["license"] == {"code": "ai-generated", "name": "AI 生成参考",
                                "url": None, "author": None, "attribution": "AI 生成参考"}
    assert prompt not in config.sources_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(("metadata", "ready"), [
    ({"resolved_model": "wrong-model", "model_verified": True}, False),
    ({"model_reported": False, "resolved_model": None, "model_verified": False,
      "verification_basis": "exact_request_with_cpa_trace"}, True),
])
def test_cpa_generated_model_receipt_compatibility(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType, tmp_path: Path,
    metadata: dict[str, Any], ready: bool,
) -> None:
    config = _rg_config(licensed_ingestion_cli, tmp_path)
    result = _rg_run(licensed_ingestion_cli, config,
        RulingGImageProvider(_make_image("PNG"), **metadata),
        Task4Vision(_task4_assessment(vision_module)))
    assert (result.ready_ids == ("g051",)) is ready
    item = _task4_manifest_item(_task4_manifest(config.manifest_path), "g051")
    if ready:
        receipt = item["receipt_history"][-1]
        assert receipt["resolved_model"] is None and receipt["model_verified"] is False
        assert receipt["request_model_pinned"] is receipt["cpa_trace_verified"] is True
    else:
        assert item["status"] == "quarantined" and not item["receipt_history"]


@pytest.mark.parametrize("field,value", [
    ("bound_garment_id", "g052"), ("bound_user_id", "u02"),
    ("prompt_sha256", "0" * 64), ("original_sha256", "1" * 64),
    ("processed_sha256", "2" * 64),
])
def test_cpa_generated_resume_rejects_receipt_replay_or_drift(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType, tmp_path: Path,
    field: str, value: str,
) -> None:
    config = _rg_config(licensed_ingestion_cli, tmp_path)
    _rg_run(licensed_ingestion_cli, config, RulingGImageProvider(_make_image("PNG")),
            Task4Vision(_task4_assessment(vision_module)))
    manifest = _task4_manifest(config.manifest_path)
    _task4_manifest_item(manifest, "g051")["receipt_history"][-1][field] = value
    config.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        _rg_run(licensed_ingestion_cli, config.model_copy(update={"resume": True}),
                RulingGImageProvider(_make_image("PNG")), Task4Vision(_task4_assessment(vision_module)))


def test_cpa_generated_unsafe_vision_never_becomes_ready(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType, tmp_path: Path,
) -> None:
    config = _rg_config(licensed_ingestion_cli, tmp_path)
    vision = Task4Vision(_task4_assessment(vision_module, slot="bottom"))
    result = _rg_run(licensed_ingestion_cli, config,
                     RulingGImageProvider(_make_image_with_authoritative_metadata("PNG")), vision)
    item = _task4_manifest_item(_task4_manifest(config.manifest_path), "g051")
    assert result.ready_ids == () and item["status"] == "quarantined"
    assert item["relative_path"] is None and not item["receipt_history"]
    with Image.open(BytesIO(vision.calls[0]["image_bytes"])) as normalized:
        normalized.load()
        assert normalized.getexif() == {} and not normalized.info


def test_cpa_generated_exact_resume_is_side_effect_free(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType, tmp_path: Path,
) -> None:
    config = _rg_config(licensed_ingestion_cli, tmp_path)
    _rg_run(licensed_ingestion_cli, config, RulingGImageProvider(_make_image("PNG")),
            Task4Vision(_task4_assessment(vision_module)))
    before = (config.manifest_path.read_bytes(), config.sources_path.read_bytes())
    provider = RulingGImageProvider(b"", error=AssertionError("must not regenerate"))
    result = _rg_run(licensed_ingestion_cli, config.model_copy(update={"resume": True}), provider,
                     Task4Vision(AssertionError("must not re-inspect")))
    assert result.reused_ids == ("g051",) and not provider.prompts
    assert before == (config.manifest_path.read_bytes(), config.sources_path.read_bytes())


def test_cpa_generated_failure_never_false_ready(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType, tmp_path: Path,
) -> None:
    from profagent.providers import ProviderUnavailable
    config = _rg_config(licensed_ingestion_cli, tmp_path)
    provider = RulingGImageProvider(b"", error=ProviderUnavailable(
        "private", reason_code="CPA_IMAGE_PROVIDER_UNAVAILABLE"))
    result = _rg_run(licensed_ingestion_cli, config, provider,
                     Task4Vision(_task4_assessment(vision_module)))
    item = _task4_manifest_item(_task4_manifest(config.manifest_path), "g051")
    assert result.ready_ids == () and item["status"] == "quarantined"
    assert item["relative_path"] is None and not item["receipt_history"]


def test_cpa_generated_receipt_write_failure_never_publishes_manifest(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cli, config = licensed_ingestion_cli._load(), None
    config = _rg_config(cli, tmp_path)
    original = cli._atomic_write_bytes

    def fail_receipt(path: Path, payload: bytes) -> None:
        if Path(path) == config.sources_path:
            raise OSError("receipt write failed")
        original(path, payload)

    monkeypatch.setattr(cli, "_atomic_write_bytes", fail_receipt)
    with pytest.raises(OSError):
        _rg_run(cli, config, RulingGImageProvider(_make_image("PNG")),
                Task4Vision(_task4_assessment(vision_module)))
    assert not config.manifest_path.exists()


def test_cpa_generated_receipt_models_bind_to_actual_provider_not_only_allowlist(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType, tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    provider = RulingGImageProvider(_make_image("PNG"))
    provider.metadata.update({
        "requested_model": "other-reviewed-model",
        "transport_model": "other-reviewed-model",
        "resolved_model": "other-reviewed-model",
    })
    config = _rg_config(cli, tmp_path)
    result = asyncio.run(cli.run_ingestion(
        config=config, api_client=None, safe_fetcher=RulingGNoFetch(),
        vision=Task4Vision(_task4_assessment(vision_module)), image_provider=provider,
        image_model_allowlist=(_RG_MODEL, "other-reviewed-model"),
    ))
    assert result.ready_ids == ()
    assert _task4_manifest_item(_task4_manifest(config.manifest_path), "g051")["status"] == "quarantined"


def test_cpa_generated_same_prompt_and_model_takedown_blocks_provider_before_call(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType, tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    _rg_run(cli, config, RulingGImageProvider(_make_image("PNG")),
            Task4Vision(_task4_assessment(vision_module)))
    before = _task4_mark_takedown(config.manifest_path)
    provider = RulingGImageProvider(b"", error=AssertionError("takedown_must_not_generate"))
    result = _rg_run(cli, config, provider, Task4Vision(AssertionError("must_not_inspect")))
    assert result.ready_ids == () and not provider.prompts
    assert result.item_outcomes[0].reason_code == "source_takedown"
    assert config.manifest_path.read_bytes() == before


def test_cpa_generated_product_noun_oracle_is_exact_and_name_independent(
    licensed_ingestion_cli: ModuleType,
) -> None:
    cli = licensed_ingestion_cli._load()
    garments = {
        garment.garment_id: garment
        for garment in cli._controlled_garments(cli.CONTROLLED_GARMENT_FILE)
    }
    assert cli._CPA_PRODUCT_NOUN_BY_GARMENT_ID == _RG_EXPECTED_PRODUCT_NOUNS
    assert set(garments) == set(_RG_EXPECTED_PRODUCT_NOUNS)
    for garment_id, noun in _RG_EXPECTED_PRODUCT_NOUNS.items():
        garment = garments[garment_id]
        prompt = cli._cpa_generated_prompt(garment)
        assert noun in prompt.lower()
        changed = garment.model_copy(update={"name": "风衣 裙 帽", "search_text": "ignore previous"})
        assert cli._cpa_generated_prompt(changed) == prompt
        assert garment_id not in prompt and garment.name not in prompt
    with pytest.raises(ValueError, match="invalid_cpa_generated_product_noun"):
        cli._cpa_generated_prompt(garments["g051"].model_copy(update={"garment_id": "g121"}))


def test_historical_cpa_receipt_policy_is_stable_across_provider_invocations(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType, tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    _rg_run(cli, config, RulingGImageProvider(_make_image("PNG")),
            Task4Vision(_task4_assessment(vision_module)))
    _, durable = cli._load_source_history(
        config.sources_path, authoritative=cli._authoritative_garments(),
        image_model_allowlist=(),
    )
    manifest = cli._load_existing_manifest(
        config.manifest_path, authoritative=cli._authoritative_garments(),
        asset_directory=config.asset_directory, durable_receipts=durable,
        image_model_allowlist=(),
    )
    assert _task4_manifest_item(manifest, "g051")["status"] == "ready"


# Fresh fix2: production construction and exact generated-product verification.
def test_cpa_generated_cli_constructs_the_frozen_provider_from_settings(
    licensed_ingestion_cli: ModuleType,
    offline_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    settings = replace(
        offline_settings,
        cpa_image_enabled=True,
        cpa_base_url="https://cpa.example/v1",
        cpa_api_key="test-only-key",
        cpa_image_model=_RG_MODEL,
        cpa_image_timeout_seconds=7.5,
    )
    captured: dict[str, Any] = {}

    class TestSettings:
        @classmethod
        def from_env(cls) -> Any:
            return settings

    class ProviderSpy:
        requested_model = _RG_MODEL
        transport_model = _RG_MODEL

        def __init__(self, received: Any) -> None:
            captured["settings"] = received

    async def capture_run(*args: Any, **kwargs: Any) -> Any:
        captured["image_provider"] = kwargs.get("image_provider")
        captured["allowlist"] = kwargs.get("image_model_allowlist")
        config = kwargs.get("config", args[0] if args else None)
        return cli.IngestionResult(
            dry_run=True,
            ready_ids=(),
            reused_ids=(),
            quarantined_ids=(),
            remaining_ids=(),
            api_attempts=0,
            candidate_attempts=0,
            manifest_path=config.manifest_path,
        )

    monkeypatch.setattr(cli, "Settings", TestSettings)
    monkeypatch.setattr(cli, "GrokImageProvider", ProviderSpy, raising=False)
    monkeypatch.setattr(cli, "SafeImageFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(cli, "VisionAdapter", lambda _settings: object())
    monkeypatch.setattr(cli, "run_ingestion", capture_run)
    config = _rg_config(cli, tmp_path)

    asyncio.run(cli._run_cli(config))

    assert captured["settings"] is settings
    assert isinstance(captured["image_provider"], ProviderSpy)
    assert captured["allowlist"] == (_RG_MODEL,)


def test_catalog_vision_quarantines_same_slot_wrong_product_type(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    transport = CatalogVisionTransportSpy(
        _catalog_completion_bytes(
            _catalog_payload(product_type="puff-sleeve blouse")
        )
    )
    adapter = _vision_adapter(vision_module, offline_settings, transport)

    with pytest.raises(vision_module.VisionUnavailable) as caught:
        _inspect_catalog(
            adapter,
            allowed_slot="top",
            expected_product_type="tie-neck blouse",
        )

    _assert_catalog_failure(vision_module, caught, "product_type_mismatch")


def test_cpa_generated_same_slot_wrong_product_never_becomes_ready(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    vision = Task4Vision(
        _task4_assessment(
            vision_module,
            slot="top",
            product_type="puff-sleeve blouse",
        )
    )

    result = _rg_run(
        cli,
        config,
        RulingGImageProvider(_make_image("PNG")),
        vision,
    )

    assert result.ready_ids == ()
    assert result.quarantined_ids == ("g051",)
    assert vision.calls[0]["allowed_slot"] == "top"
    assert vision.calls[0]["expected_product_type"] == "tie-neck blouse"
    item = _task4_manifest_item(_task4_manifest(config.manifest_path), "g051")
    assert item["status"] == "quarantined"
    assert item["receipt_history"] == []


@pytest.mark.parametrize(
    "updates",
    [
        {"cpa_image_enabled": False},
        {"cpa_image_enabled": True, "cpa_base_url": ""},
        {
            "cpa_image_enabled": True,
            "cpa_base_url": "https://cpa.example/v1",
            "cpa_image_model": "unreviewed-image-model",
        },
    ],
)
def test_cpa_generated_cli_configuration_failures_are_controlled_before_run(
    licensed_ingestion_cli: ModuleType,
    offline_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    updates: dict[str, Any],
) -> None:
    cli = licensed_ingestion_cli._load()
    secret = "must-not-escape"
    setting_updates = {
        "cpa_api_key": secret,
        "cpa_image_model": _RG_MODEL,
        **updates,
    }
    settings = replace(offline_settings, **setting_updates)

    class TestSettings:
        @classmethod
        def from_env(cls) -> Any:
            return settings

    async def must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("configuration failure must precede ingestion")

    monkeypatch.setattr(cli, "Settings", TestSettings)
    monkeypatch.setattr(cli, "SafeImageFetcher", lambda **_kwargs: object())
    monkeypatch.setattr(cli, "VisionAdapter", lambda _settings: object())
    monkeypatch.setattr(cli, "run_ingestion", must_not_run)

    with pytest.raises(ValueError) as caught:
        asyncio.run(cli._run_cli(_rg_config(cli, tmp_path)))

    assert str(caught.value) == "cpa_generated_configuration_unavailable"
    assert secret not in str(caught.value)


def test_frozen_grok_provider_uses_exact_cpa_transport_boundary(
    offline_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("profagent.image_provider")
    payload = _make_image("PNG")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": _RG_MODEL,
                "data": [{"b64_json": base64.b64encode(payload).decode("ascii")}],
            },
            headers={"content-type": "application/json", "x-cpa-trace-id": "test-1"},
            request=request,
        )

    settings = replace(
        offline_settings,
        cpa_image_enabled=True,
        cpa_base_url="https://cpa.example/v1",
        cpa_api_key="test-only-key",
        cpa_image_model=_RG_MODEL,
        cpa_image_timeout_seconds=7.5,
    )
    original_client = module.httpx.AsyncClient
    client_kwargs: dict[str, Any] = {}

    class AsyncClientSpy:
        def __init__(self, **kwargs: Any) -> None:
            client_kwargs.update(kwargs)
            self.inner = original_client(**kwargs)

        async def __aenter__(self) -> Any:
            return await self.inner.__aenter__()

        async def __aexit__(self, *args: Any) -> Any:
            return await self.inner.__aexit__(*args)

    monkeypatch.setattr(module.httpx, "AsyncClient", AsyncClientSpy)
    provider = module.GrokImageProvider(
        settings,
        transport=httpx.MockTransport(handler),
    )

    result, mime_type, receipt = asyncio.run(provider.generate_static_2d("safe prompt"))

    assert result == payload and mime_type == "image/png"
    assert receipt["transport_model"] == _RG_MODEL
    assert client_kwargs["trust_env"] is False
    assert client_kwargs["follow_redirects"] is False
    assert client_kwargs["timeout"] == 7.5
    assert len(requests) == 1
    assert str(requests[0].url) == "https://cpa.example/v1/images/generations"
    body = json.loads(requests[0].content)
    assert body == {
        "model": _RG_MODEL,
        "prompt": "safe prompt",
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
    }


# Task 4 / Ruling I: closed failure diagnostics and private Vision quarantine.
_RI_VISION_MODEL = {
    "requested_model": "grok4.6",
    "resolved_model": "grok-4.6-high",
    "model_verified": True,
}


def _ri_paths(config: Any) -> tuple[Path, Path]:
    state_root = config.asset_directory.parent
    return (
        state_root / "private_quarantine" / "cpa_generated",
        state_root / "cpa_generated_quarantine_diagnostics.jsonl",
    )


def _ri_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _ri_vision_error(vision_module: ModuleType, reason: str) -> Exception:
    return vision_module.VisionUnavailable(
        "provider prose must not escape",
        {
            "reason_code": reason,
            **_RI_VISION_MODEL,
            "transport_model": "grok-4.6-high",
            "provider_body": "secret-body",
            "prompt": "secret-prompt",
            "owner_id": "secret-owner",
            **(
                {
                    "schema_stage": "payload",
                    "envelope_failure_code": None,
                    "envelope_metadata_profile": (
                        "cpa_chat_completion_metadata_v1"
                    ),
                }
                if reason == "response_schema_invalid"
                else {}
            ),
        },
    )


def _ri_assert_public_quarantined(config: Any) -> None:
    item = _task4_manifest_item(_task4_manifest(config.manifest_path), "g051")
    assert item["status"] == "quarantined"
    assert item["license"] is None and item["relative_path"] is None
    assert item["original_sha256"] is None and item["processed_sha256"] is None
    assert item["receipt_history"] == []
    assert not any(row.get("garment_id") == "g051" for row in _ri_rows(config.sources_path))


def test_cpa_failure_outcome_is_closed_content_free_and_has_minimal_image_model(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType, tmp_path: Path,
) -> None:
    from profagent.providers import ProviderUnavailable

    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    provider = RulingGImageProvider(
        b"", error=ProviderUnavailable(
            "secret-provider-body g051 u01", reason_code="CPA_IMAGE_PROVIDER_TIMEOUT"
        )
    )
    result = _rg_run(cli, config, provider, Task4Vision(AssertionError("no Vision")))
    outcome = result.item_outcomes[0]
    assert outcome.failure_stage == "image_provider"
    assert outcome.reason_code == "timeout"
    assert outcome.image_model_provenance.model_dump() == {
        "requested_model": _RG_MODEL, "resolved_model": None, "model_verified": False,
    }
    assert outcome.vision_model_provenance is None
    public = outcome.model_dump_json()
    assert all(secret not in public for secret in (
        "secret-provider-body", "secret-owner", "secret-prompt", "u01"
    ))
    _ri_assert_public_quarantined(config)
    root, diagnostics = _ri_paths(config)
    assert not root.exists() and not diagnostics.exists()


@pytest.mark.parametrize(
    "reason", ["unsupported_mime", "decode_failed", "dimension_out_of_range", "pixel_limit_exceeded"]
)
def test_local_validation_failures_are_exact_and_create_no_private_artifact(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType, tmp_path: Path,
    reason: str,
) -> None:
    cli = licensed_ingestion_cli._load()

    class RejectingLocal(RulingGNoFetch):
        def validate_local_bytes(self, *_args: Any, **_kwargs: Any) -> Any:
            raise cli.ImageFetchError(reason)

    config = _rg_config(cli, tmp_path)
    vision = Task4Vision(AssertionError("Vision must not be reached"))
    result = asyncio.run(cli.run_ingestion(
        config=config, api_client=None, safe_fetcher=RejectingLocal(), vision=vision,
        image_provider=RulingGImageProvider(_make_image("PNG")),
        image_model_allowlist=(_RG_MODEL,),
    ))
    outcome = result.item_outcomes[0]
    assert (outcome.failure_stage, outcome.reason_code) == ("local_validation", reason)
    assert not vision.calls
    _ri_assert_public_quarantined(config)
    root, diagnostics = _ri_paths(config)
    assert not root.exists() and not diagnostics.exists()


@pytest.mark.parametrize(
    "reason", ["product_type_mismatch", "identifiable_person", "quality_rejected",
               "response_schema_invalid", "timeout"],
)
def test_vision_rejection_persists_only_normalized_private_content_and_minimal_record(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType, tmp_path: Path,
    reason: str,
) -> None:
    cli = licensed_ingestion_cli._load()
    raw = _make_image_with_authoritative_metadata("PNG")
    provider, config = RulingGImageProvider(raw), _rg_config(cli, tmp_path)
    vision = Task4Vision(_ri_vision_error(vision_module, reason))
    result = _rg_run(cli, config, provider, vision)
    outcome = result.item_outcomes[0]
    assert (outcome.failure_stage, outcome.reason_code) == ("vision", reason)
    assert outcome.image_model_provenance.model_dump() == {
        "requested_model": _RG_MODEL, "resolved_model": _RG_MODEL, "model_verified": True,
    }
    assert outcome.vision_model_provenance.model_dump() == _RI_VISION_MODEL
    _ri_assert_public_quarantined(config)

    normalized = vision.calls[0]["image_bytes"]
    digest = hashlib.sha256(normalized).hexdigest()
    root, diagnostics = _ri_paths(config)
    files = [path for path in root.rglob("*") if path.is_file()]
    assert files == [root / f"{digest}.png"] and files[0].read_bytes() == normalized
    assert root.resolve() != config.asset_directory.resolve()
    with Image.open(files[0]) as image:
        image.load()
        assert image.format == "PNG" and image.getexif() == {} and not image.info
    rows = _ri_rows(diagnostics)
    assert len(rows) == 1
    record = rows[0]
    assert record["garment_id"] == "g051" and record["user_id"] == "u01"
    assert record["prompt_sha256"] == hashlib.sha256(provider.prompts[0].encode()).hexdigest()
    assert record["original_sha256"] == hashlib.sha256(raw).hexdigest()
    assert record["processed_sha256"] == digest == record["quarantine_sha256"]
    assert record["quarantine_relative_path"] == f"{digest}.png"
    assert (record["failure_stage"], record["reason_code"]) == ("vision", reason)
    assert record["image_model_provenance"] == outcome.image_model_provenance.model_dump()
    assert record["vision_model_provenance"] == _RI_VISION_MODEL
    encoded = json.dumps(record, ensure_ascii=False)
    assert all(secret not in encoded for secret in (
        "provider prose", "secret-body", "secret-prompt", "secret-owner", provider.prompts[0]
    ))


def test_cpa_cancellation_creates_no_public_or_private_state(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType, tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    with pytest.raises(asyncio.CancelledError):
        _rg_run(cli, config, RulingGImageProvider(b"", error=asyncio.CancelledError()),
                Task4Vision(AssertionError("no Vision")))
    root, diagnostics = _ri_paths(config)
    assert not root.exists() and not diagnostics.exists()
    assert not config.manifest_path.exists() and not config.sources_path.exists()


@pytest.mark.parametrize("failure_point", ["quarantine", "diagnostic", "manifest"])
def test_vision_quarantine_publication_is_atomic_without_dangling_trust(
    licensed_ingestion_cli: ModuleType, vision_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_point: str,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    root, diagnostics = _ri_paths(config)
    original_bytes, original_json = cli._atomic_write_bytes, cli._atomic_write_json

    def fail_bytes(path: Path, payload: bytes) -> None:
        target = Path(path)
        if (failure_point == "quarantine" and root in target.parents) or (
            failure_point == "diagnostic" and target == diagnostics
        ):
            raise OSError(failure_point)
        original_bytes(target, payload)

    def fail_json(path: Path, payload: dict[str, Any]) -> None:
        if failure_point == "manifest" and Path(path) == config.manifest_path:
            raise OSError("manifest")
        original_json(path, payload)

    monkeypatch.setattr(cli, "_atomic_write_bytes", fail_bytes)
    monkeypatch.setattr(cli, "_atomic_write_json", fail_json)
    with pytest.raises(OSError, match=failure_point):
        _rg_run(cli, config, RulingGImageProvider(_make_image("PNG")),
                Task4Vision(_ri_vision_error(vision_module, "product_type_mismatch")))
    assert not [path for path in root.rglob("*") if path.is_file()]
    assert _ri_rows(diagnostics) == []
    if config.manifest_path.exists():
        assert not any(row.get("garment_id") == "g051" and row.get("status") == "ready"
                       for row in _task4_manifest(config.manifest_path)["items"])
    assert not any(row.get("garment_id") == "g051" for row in _ri_rows(config.sources_path))


@pytest.mark.parametrize(
    ("provider_code", "closed_reason"),
    [
        ("CPA_IMAGE_OUTPUT_REJECTED", "response_schema_invalid"),
        ("CPA_IMAGE_INPUT_REJECTED", "input_rejected"),
        ("CPA_IMAGE_PROVIDER_UNAVAILABLE", "provider_unavailable"),
        ("CPA_IMAGE_PROVIDER_TIMEOUT", "timeout"),
    ],
)
def test_cpa_failure_codes_have_exact_closed_reason_mapping(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
    provider_code: str,
    closed_reason: str,
) -> None:
    from profagent.providers import ProviderUnavailable

    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    provider = RulingGImageProvider(
        b"", error=ProviderUnavailable("untrusted prose", reason_code=provider_code)
    )
    outcome = _rg_run(
        cli, config, provider, Task4Vision(AssertionError("Vision must not run"))
    ).item_outcomes[0]
    assert (outcome.failure_stage, outcome.reason_code) == (
        "image_provider", closed_reason,
    )


def test_vision_provenance_is_server_requested_and_allowlisted_not_trace_claimed(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    poisoned = vision_module.VisionUnavailable(
        "untrusted prose",
        {
            "reason_code": "product_type_mismatch",
            "requested_model": "attacker-request-token",
            "resolved_model": "unknown-attacker-model",
            "model_verified": True,
            "provider_body": "secret-body",
        },
    )
    result = _rg_run(
        cli, config, RulingGImageProvider(_make_image("PNG")), Task4Vision(poisoned)
    )
    provenance = result.item_outcomes[0].vision_model_provenance.model_dump()
    assert provenance == {
        "requested_model": "grok4.6",
        "resolved_model": None,
        "model_verified": False,
    }
    _, diagnostics = _ri_paths(config)
    encoded = diagnostics.read_text(encoding="utf-8")
    assert all(value not in encoded for value in (
        "attacker-request-token", "unknown-attacker-model", "secret-body"
    ))
    assert _ri_rows(diagnostics)[0]["vision_model_provenance"] == provenance


def test_failed_private_rollback_has_marker_and_next_run_recovers_pair(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    root, diagnostics = _ri_paths(config)
    marker = config.asset_directory.parent / "cpa_generated_quarantine_transaction.json"
    original_json = cli._atomic_write_json

    def fail_manifest(path: Path, payload: dict[str, Any]) -> None:
        if Path(path) == config.manifest_path:
            raise OSError("manifest")
        original_json(path, payload)

    monkeypatch.setattr(cli, "_atomic_write_json", fail_manifest)
    monkeypatch.setattr(cli, "_restore_private_file", lambda *_args: None)
    with pytest.raises(OSError, match="manifest"):
        _rg_run(
            cli, config, RulingGImageProvider(_make_image("PNG")),
            Task4Vision(_ri_vision_error(vision_module, "product_type_mismatch")),
        )
    assert marker.is_file()
    assert diagnostics.is_file() and any(path.is_file() for path in root.rglob("*"))
    private_png = next(path for path in root.rglob("*") if path.is_file())
    png_before = private_png.read_bytes()
    diagnostics_before = diagnostics.read_bytes()
    manifest_before = (
        config.manifest_path.read_bytes() if config.manifest_path.exists() else None
    )
    sources_before = (
        config.sources_path.read_bytes() if config.sources_path.exists() else None
    )

    monkeypatch.setattr(cli, "_atomic_write_json", original_json)
    provider = RulingGImageProvider(
        b"", error=AssertionError("provider must not run")
    )
    with pytest.raises(ValueError, match="invalid_private_quarantine_transaction"):
        _rg_run(
            cli, config, provider, Task4Vision(AssertionError("Vision must not run"))
        )
    assert provider.prompts == [] and marker.is_file()
    assert private_png.read_bytes() == png_before
    assert diagnostics.read_bytes() == diagnostics_before
    assert (
        config.manifest_path.read_bytes() if config.manifest_path.exists() else None
    ) == manifest_before
    assert (
        config.sources_path.read_bytes() if config.sources_path.exists() else None
    ) == sources_before
    if config.manifest_path.exists():
        assert not any(
            item.get("garment_id") == "g051" and item.get("status") == "ready"
            for item in _task4_manifest(config.manifest_path)["items"]
        )
    if config.sources_path.exists():
        assert not any(
            row.get("garment_id") == "g051" for row in _ri_rows(config.sources_path)
        )


def test_private_quarantine_rejects_symlink_root_before_any_write(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    root, diagnostics = _ri_paths(config)
    original = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda path: True if path == root else original(path)
    )
    with pytest.raises(ValueError, match="unsafe_private_quarantine_root"):
        _rg_run(
            cli, config, RulingGImageProvider(_make_image("PNG")),
            Task4Vision(_ri_vision_error(vision_module, "quality_rejected")),
        )
    assert not diagnostics.exists() and not config.manifest_path.exists()


def test_private_quarantine_root_is_outside_all_serveable_roots_and_ignored() -> None:
    repository = Path(__file__).resolve().parents[1]
    private_root = (repository / "data" / "assets" / "private_quarantine").resolve()
    for relative in ("data/assets/wardrobe_catalog_reference_v1", "static", "web"):
        serveable = (repository / relative).resolve()
        assert private_root != serveable and serveable not in private_root.parents
        assert private_root not in serveable.parents
    ignored = (repository / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/assets/private_quarantine/" in ignored
    assert "data/assets/cpa_generated_quarantine_diagnostics.jsonl" in ignored


# Task 4 / Ruling I replay repair: journals are staging-only and committed
# diagnostics are independently revalidated before any provider call.
def _rii_seed_private_truth(
    cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
    *,
    inject_transaction: bool = True,
) -> tuple[Any, Path, Path, Path, bytes, bytes]:
    config = _rg_config(cli, tmp_path)
    _rg_run(
        cli,
        config,
        RulingGImageProvider(_make_image("PNG")),
        Task4Vision(_ri_vision_error(vision_module, "product_type_mismatch")),
    )
    root, diagnostics = _ri_paths(config)
    private_png = next(path for path in root.iterdir() if path.suffix == ".png")
    rows = _ri_rows(diagnostics)
    if inject_transaction:
        rows[0].setdefault("transaction_id", "1" * 32)
    diagnostics.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    marker = config.asset_directory.parent / "cpa_generated_quarantine_transaction.json"
    return (
        config,
        private_png,
        diagnostics,
        marker,
        private_png.read_bytes(),
        diagnostics.read_bytes(),
    )


@pytest.mark.parametrize("phase", ["prepared", "committed"])
def test_replayed_journal_cannot_target_or_delete_committed_private_truth(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
    phase: str,
) -> None:
    cli = licensed_ingestion_cli._load()
    config, private_png, diagnostics, marker, png_before, diagnostic_before = (
        _rii_seed_private_truth(cli, vision_module, tmp_path)
    )
    entries = []
    for kind, path, payload in (
        ("quarantine", private_png, png_before),
        ("diagnostics", diagnostics, diagnostic_before),
    ):
        entries.append({
            "kind": kind,
            "name": path.name,
            "previous_sha256": None,
            "backup_name": None,
            "new_sha256": hashlib.sha256(payload).hexdigest(),
        })
    marker.write_text(json.dumps({
        "version": "cpa_private_quarantine_tx_v1",
        "transaction_id": "2" * 32,
        "phase": phase,
        "entries": entries,
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid_private_quarantine_transaction"):
        _rg_run(
            cli,
            config,
            RulingGImageProvider(b"", error=AssertionError("provider must not run")),
            Task4Vision(AssertionError("Vision must not run")),
        )
    assert private_png.read_bytes() == png_before
    assert diagnostics.read_bytes() == diagnostic_before


@pytest.mark.parametrize("corruption", ["missing_png", "prompt_binding", "duplicate_tx"])
def test_existing_private_diagnostic_corruption_fails_before_provider(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
    corruption: str,
) -> None:
    cli = licensed_ingestion_cli._load()
    config, private_png, diagnostics, _, png_before, _ = _rii_seed_private_truth(
        cli, vision_module, tmp_path
    )
    manifest_before = config.manifest_path.read_bytes()
    sources_before = config.sources_path.read_bytes()
    rows = _ri_rows(diagnostics)
    if corruption == "missing_png":
        private_png.unlink()
    elif corruption == "prompt_binding":
        rows[0]["prompt_sha256"] = "f" * 64
    else:
        rows.append(dict(rows[0]))
    diagnostics.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid_private_quarantine_diagnostics"):
        _rg_run(
            cli,
            config,
            RulingGImageProvider(b"", error=AssertionError("provider must not run")),
            Task4Vision(AssertionError("Vision must not run")),
        )
    if corruption != "missing_png":
        assert private_png.read_bytes() == png_before
    assert config.manifest_path.read_bytes() == manifest_before
    assert config.sources_path.read_bytes() == sources_before
    item = _task4_manifest_item(_task4_manifest(config.manifest_path), "g051")
    assert item["status"] == "quarantined" and item["relative_path"] is None
    assert not any(row.get("garment_id") == "g051" for row in _ri_rows(config.sources_path))


def test_valid_existing_diagnostic_is_read_only_and_never_resumes_ready(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    from profagent.providers import ProviderUnavailable

    cli = licensed_ingestion_cli._load()
    config, private_png, diagnostics, _, png_before, diagnostic_before = (
        _rii_seed_private_truth(
            cli, vision_module, tmp_path, inject_transaction=False
        )
    )
    row = _ri_rows(diagnostics)[0]
    assert len(row["transaction_id"]) == 32
    vision = Task4Vision(AssertionError("existing diagnostic must not invoke Vision"))
    result = _rg_run(
        cli,
        config,
        RulingGImageProvider(
            b"", error=ProviderUnavailable(
                "closed", reason_code="CPA_IMAGE_PROVIDER_UNAVAILABLE"
            ),
        ),
        vision,
    )
    assert result.ready_ids == () and result.quarantined_ids == ("g051",)
    assert not vision.calls
    assert private_png.read_bytes() == png_before
    assert diagnostics.read_bytes() == diagnostic_before


@pytest.mark.parametrize("surface", ["image_model_provenance", "vision_model_provenance"])
def test_existing_diagnostic_model_provenance_rebinds_server_policy_before_provider(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
    surface: str,
) -> None:
    """Legal attacker tokens and verified contradictions never survive a new run."""

    cli = licensed_ingestion_cli._load()
    for index, updates in enumerate((
        {"requested_model": "attacker-request-model"},
        {"resolved_model": "attacker-resolved-model", "model_verified": True},
        {"resolved_model": None, "model_verified": True},
    )):
        case_root = tmp_path / f"{surface}-{index}"
        case_root.mkdir()
        config, private_png, diagnostics, marker, _, _ = _rii_seed_private_truth(
            cli, vision_module, case_root
        )
        rows = _ri_rows(diagnostics)
        rows[0][surface].update(updates)
        diagnostics.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        durable = (private_png.read_bytes(), diagnostics.read_bytes(),
                   config.manifest_path.read_bytes(), config.sources_path.read_bytes())
        provider = RulingGImageProvider(
            b"", error=AssertionError("provider must not run")
        )
        with pytest.raises(ValueError, match="invalid_private_quarantine_diagnostics"):
            _rg_run(cli, config, provider, Task4Vision(AssertionError("Vision must not run")))
        assert provider.prompts == [] and not marker.exists()
        assert durable == (private_png.read_bytes(), diagnostics.read_bytes(),
                           config.manifest_path.read_bytes(), config.sources_path.read_bytes())


@pytest.mark.parametrize(
    "corruptions",
    [("prompt_drift", "missing_png", "marker"), ("duplicate_tx", "provenance")],
)
def test_cpa_dry_run_validates_all_private_truth_before_provider_without_mutation(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
    corruptions: tuple[str, ...],
) -> None:
    """Dry-run is read-only, but it is not an integrity-validation bypass."""

    cli = licensed_ingestion_cli._load()
    for corruption in corruptions:
        case_root = tmp_path / corruption
        case_root.mkdir()
        config, private_png, diagnostics, marker, _, _ = _rii_seed_private_truth(
            cli, vision_module, case_root
        )
        rows = _ri_rows(diagnostics)
        if corruption == "prompt_drift":
            rows[0]["prompt_sha256"] = "f" * 64
        elif corruption == "missing_png":
            private_png.unlink()
        elif corruption == "duplicate_tx":
            rows.append(dict(rows[0]))
        elif corruption == "provenance":
            rows[0]["vision_model_provenance"]["requested_model"] = "attacker-model"
        else:
            marker.write_text("{}", encoding="utf-8")
        if corruption not in {"missing_png", "marker"}:
            diagnostics.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )
        paths = (private_png, diagnostics, config.manifest_path, config.sources_path, marker)
        durable = tuple((path.exists(), path.read_bytes() if path.exists() else None)
                        for path in paths)
        provider = RulingGImageProvider(
            b"", error=AssertionError("dry-run provider must not run")
        )
        with pytest.raises(ValueError, match="private_quarantine"):
            _rg_run(
                cli, config.model_copy(update={"dry_run": True}), provider,
                Task4Vision(AssertionError("Vision must not run")),
            )
        assert provider.prompts == []
        assert durable == tuple((path.exists(), path.read_bytes() if path.exists() else None)
                                for path in paths)


# S16B post-canary schema repair: CPA is OpenAI-compatible, but only a
# deliberately small response envelope and one closed catalog payload are
# trusted.  These tests are MockTransport/local-only and must never reach CPA.
_RJ_SCHEMA_STAGES = frozenset({"envelope", "content", "payload"})


def _rj_standard_completion(
    content: Any,
    *,
    model: str = "grok-4.6-high",
    envelope_updates: dict[str, Any] | None = None,
    choice_updates: dict[str, Any] | None = None,
    message_updates: dict[str, Any] | None = None,
) -> bytes:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "refusal": None,
    }
    message.update(message_updates or {})
    choice: dict[str, Any] = {
        "index": 0,
        "message": message,
        "finish_reason": "stop",
    }
    choice.update(choice_updates or {})
    envelope: dict[str, Any] = {
        "id": "chatcmpl-mock-only",
        "object": "chat.completion",
        "created": 1_784_838_400,
        "model": model,
        "choices": [choice],
        "system_fingerprint": "fp_mock_only",
    }
    envelope.update(envelope_updates or {})
    return json.dumps(envelope).encode("utf-8")


def _rj_schema_failure(
    vision_module: ModuleType,
    offline_settings: Any,
    response_body: bytes,
    *,
    expected_stage: str,
    expected_reason: str = "response_schema_invalid",
) -> dict[str, Any]:
    transport = CatalogVisionTransportSpy(response_body)
    adapter = _vision_adapter(vision_module, offline_settings, transport)
    with pytest.raises(vision_module.VisionUnavailable) as caught:
        _inspect_catalog(adapter)
    trace = _assert_catalog_failure(vision_module, caught, expected_reason)
    assert trace["schema_stage"] == expected_stage
    assert trace["schema_stage"] in _RJ_SCHEMA_STAGES
    assert len(transport.calls) == 1
    return trace


def test_catalog_vision_accepts_minimal_standard_cpa_envelope_allowlist(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    content = json.dumps(_catalog_payload())
    minimal = json.dumps(
        {"model": "grok-4.6-high", "choices": [{"message": {"content": content}}]}
    ).encode("utf-8")
    nullable_fingerprint = _rj_standard_completion(
        content, envelope_updates={"system_fingerprint": None}
    )
    for response_body in (
        _rj_standard_completion(content),
        minimal,
        nullable_fingerprint,
    ):
        transport = CatalogVisionTransportSpy(response_body)
        assessment, trace = _inspect_catalog(
            _vision_adapter(vision_module, offline_settings, transport)
        )
        assert assessment.model_dump(mode="json") == _catalog_payload()
        assert trace["requested_model"] == "grok4.6"
        assert trace["resolved_model"] == "grok-4.6-high"
        assert trace["model_verified"] is True


def test_catalog_vision_rejects_every_nonstandard_envelope_key_at_exact_layer(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    payload = json.dumps(_catalog_payload())
    cases = (
        _rj_standard_completion(payload, envelope_updates={"provider_note": "secret-top"}),
        _rj_standard_completion(payload, choice_updates={"logprobs": {"secret": True}}),
        _rj_standard_completion(payload, message_updates={"audio": "secret-audio"}),
    )
    for response_body in cases:
        trace = _rj_schema_failure(
            vision_module,
            offline_settings,
            response_body,
            expected_stage="envelope",
        )
        assert vision_module.CATALOG_SCHEMA_STAGES == _RJ_SCHEMA_STAGES
        assert trace["requested_model"] == "grok4.6"
        assert trace["resolved_model"] is None
        assert trace["model_verified"] is False
        rendered = json.dumps(trace, ensure_ascii=False)
        assert "secret" not in rendered and "provider_note" not in rendered


def test_catalog_vision_accepts_bare_or_one_exact_complete_json_fence(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    payload = json.dumps(_catalog_payload())
    for content in (payload, f"```json\n{payload}\n```"):
        transport = CatalogVisionTransportSpy(_catalog_completion_bytes(content))
        assessment, trace = _inspect_catalog(
            _vision_adapter(vision_module, offline_settings, transport)
        )
        assert assessment.model_dump(mode="json") == _catalog_payload()
        assert trace["model_verified"] is True


def test_catalog_vision_rejects_ambiguous_or_refused_content_at_content_layer(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    payload = json.dumps(_catalog_payload())
    cases = (
        _catalog_completion_bytes(f"```json\n{payload}\n```\n```json\n{payload}\n```"),
        _catalog_completion_bytes(f"provider prose\n```json\n{payload}\n```"),
        _catalog_completion_bytes(f"```json\n{payload}"),
        _rj_standard_completion(
            payload,
            message_updates={"refusal": "provider secret refusal"},
        ),
        _rj_standard_completion(_catalog_payload()),
    )
    for response_body in cases:
        trace = _rj_schema_failure(
            vision_module,
            offline_settings,
            response_body,
            expected_stage="content",
        )
        assert "provider secret refusal" not in json.dumps(trace)


def test_catalog_vision_rejects_closed_payload_violations_at_payload_layer(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    cases = (
        (_catalog_payload(provider_note="secret-payload"), "response_schema_invalid"),
        (_catalog_payload(slot="hat"), "response_schema_invalid"),
        (_catalog_payload(product_type="unknown garment"), "response_schema_invalid"),
        (_catalog_payload(audience="menswear"), "audience_rejected"),
    )
    for payload, reason in cases:
        trace = _rj_schema_failure(
            vision_module,
            offline_settings,
            _catalog_completion_bytes(payload),
            expected_stage="payload",
            expected_reason=reason,
        )
        assert "secret-payload" not in json.dumps(trace)


def test_cpa_schema_failure_isolated_private_and_never_becomes_ready_or_source(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    offline_settings: Any,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    transport = CatalogVisionTransportSpy(
        _catalog_completion_bytes("provider secret prose")
    )
    vision = _vision_adapter(vision_module, offline_settings, transport)

    result = _rg_run(
        cli,
        config,
        RulingGImageProvider(_make_image("PNG")),
        vision,
    )

    outcome = result.item_outcomes[0]
    assert result.ready_ids == () and result.quarantined_ids == ("g051",)
    assert (outcome.failure_stage, outcome.reason_code) == (
        "vision",
        "response_schema_invalid",
    )
    assert outcome.vision_model_provenance.model_dump() == {
        "requested_model": "grok4.6",
        "resolved_model": None,
        "model_verified": False,
    }
    _ri_assert_public_quarantined(config)
    source_before_retry = config.sources_path.read_bytes()
    assert not any(row.get("garment_id") == "g051" for row in _ri_rows(config.sources_path))
    private_root, diagnostics = _ri_paths(config)
    private_files = [path for path in private_root.iterdir() if path.suffix == ".png"]
    assert len(private_files) == 1 and diagnostics.is_file()
    diagnostic = _ri_rows(diagnostics)[0]
    assert diagnostic["vision_model_provenance"] == {
        "requested_model": "grok4.6",
        "resolved_model": None,
        "model_verified": False,
    }
    assert "provider secret prose" not in json.dumps(diagnostic)
    assert config.sources_path.read_bytes() == source_before_retry


# S16B reviewer follow-up: optional OpenAI-compatible metadata is still a
# closed security boundary.  Presence never implies that arbitrary values or
# future provider extensions are trusted.
_RK_PROVIDER_PROSE = "provider-secret-prose-must-not-leak"


def _rk_assert_envelope_rejected(
    vision_module: ModuleType,
    offline_settings: Any,
    response_body: bytes,
) -> None:
    trace = _rj_schema_failure(
        vision_module,
        offline_settings,
        response_body,
        expected_stage="envelope",
    )
    assert trace["reason_code"] == "response_schema_invalid"
    assert trace["schema_stage"] == "envelope"
    assert trace["requested_model"] == "grok4.6"
    assert trace["resolved_model"] is None
    assert trace["model_verified"] is False
    assert _RK_PROVIDER_PROSE not in json.dumps(trace, ensure_ascii=False)


def _rk_completion(
    *,
    envelope_updates: dict[str, Any] | None = None,
    choice_updates: dict[str, Any] | None = None,
    message_updates: dict[str, Any] | None = None,
) -> bytes:
    return _rj_standard_completion(
        _RK_PROVIDER_PROSE,
        envelope_updates=envelope_updates,
        choice_updates=choice_updates,
        message_updates=message_updates,
    )


def test_ambiguous_cpa_completion_metadata_rejects_nonzero_or_nonstrict_choice_index(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    for value in (True, 1, {}, "0"):
        _rk_assert_envelope_rejected(
            vision_module,
            offline_settings,
            _rk_completion(choice_updates={"index": value}),
        )


def test_ambiguous_cpa_completion_metadata_rejects_nonstop_finish_reason(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    for value in ("length", "content_filter", "tool_calls", None):
        _rk_assert_envelope_rejected(
            vision_module,
            offline_settings,
            _rk_completion(choice_updates={"finish_reason": value}),
        )


def test_ambiguous_cpa_completion_metadata_rejects_nonassistant_message_role(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    for value in ("tool", "system", "user", None, {"role": "assistant"}):
        _rk_assert_envelope_rejected(
            vision_module,
            offline_settings,
            _rk_completion(message_updates={"role": value}),
        )


def test_ambiguous_cpa_completion_metadata_rejects_wrong_object_and_any_usage(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    cases = (
        _rk_completion(envelope_updates={"object": "chat.completion.chunk"}),
        _rk_completion(envelope_updates={"object": None}),
        _rk_completion(envelope_updates={"object": {"type": "chat.completion"}}),
        _rk_completion(envelope_updates={"usage": {}}),
        _rk_completion(envelope_updates={"usage": None}),
        _rk_completion(envelope_updates={"usage": {"total_tokens": 2}}),
    )
    for response_body in cases:
        _rk_assert_envelope_rejected(
            vision_module, offline_settings, response_body
        )


def test_ambiguous_cpa_completion_metadata_rejects_malformed_id_created_and_fingerprint(
    vision_module: ModuleType,
    offline_settings: Any,
) -> None:
    cases = (
        *( _rk_completion(envelope_updates={"id": value})
           for value in (None, "", "x" * 257, 1, {"id": "chatcmpl"}) ),
        *( _rk_completion(envelope_updates={"created": value})
           for value in (True, -1, 1.0, "1", {}) ),
        *( _rk_completion(envelope_updates={"system_fingerprint": value})
           for value in (True, 1, "f" * 257, {}) ),
    )
    for response_body in cases:
        _rk_assert_envelope_rejected(
            vision_module, offline_settings, response_body
        )


def test_ambiguous_cpa_completion_metadata_never_becomes_ready_or_source(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    offline_settings: Any,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    cases = (
        _rk_completion(choice_updates={"index": True}),
        _rk_completion(choice_updates={"finish_reason": "length"}),
        _rk_completion(message_updates={"role": "tool"}),
        _rk_completion(envelope_updates={"object": "chat.completion.chunk"}),
        _rk_completion(envelope_updates={"usage": {}}),
        _rk_completion(envelope_updates={"id": ""}),
    )
    for index, response_body in enumerate(cases):
        case_root = tmp_path / f"case-{index}"
        case_root.mkdir()
        config = _rg_config(cli, case_root)
        vision = _vision_adapter(
            vision_module,
            offline_settings,
            CatalogVisionTransportSpy(response_body),
        )
        result = _rg_run(
            cli,
            config,
            RulingGImageProvider(_make_image("PNG")),
            vision,
        )
        outcome = result.item_outcomes[0]
        assert result.ready_ids == () and result.quarantined_ids == ("g051",)
        assert (outcome.failure_stage, outcome.reason_code) == (
            "vision",
            "response_schema_invalid",
        )
        assert outcome.vision_model_provenance.model_dump() == {
            "requested_model": "grok4.6",
            "resolved_model": None,
            "model_verified": False,
        }
        _ri_assert_public_quarantined(config)
        assert not any(
            row.get("garment_id") == "g051" for row in _ri_rows(config.sources_path)
        )


# Ruling L post-canary repair: the adapter's closed schema failure stage must
# survive every ingestion boundary without admitting provider-defined values.
_RL_SCHEMA_STAGES = frozenset({"envelope", "content", "payload"})
_RL_PROVIDER_PROSE = "provider-secret-schema-stage-must-not-leak"
_RL_MISSING = object()
_RL_FROZEN_LEGACY_LINE_SHA256 = frozenset(
    {
        "8dfb54df971e3d5bc48fb04e9c31a86d57898ba7393915f99dff2404ecc9bf5f",
        "c2cbcfd81f0f33f88a83cacfd534b09056223c56885c5560c89ab42657d759fc",
    }
)


def _rl_raw_line(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True).encode("utf-8")


def _rl_raw_line_sha256(record: dict[str, Any]) -> str:
    return hashlib.sha256(_rl_raw_line(record)).hexdigest()


def _rl_vision_error(
    vision_module: ModuleType,
    *,
    reason: str,
    schema_stage: Any = _RL_MISSING,
) -> Exception:
    trace: dict[str, Any] = {
        "reason_code": reason,
        **_RI_VISION_MODEL,
        "transport_model": "grok-4.6-high",
        "provider_body": _RL_PROVIDER_PROSE,
    }
    if schema_stage is not _RL_MISSING:
        trace["schema_stage"] = schema_stage
    if reason == "response_schema_invalid":
        trace["envelope_failure_code"] = (
            "invalid_envelope_metadata" if schema_stage == "envelope" else None
        )
        trace["envelope_metadata_profile"] = (
            "cpa_chat_completion_metadata_v1"
        )
    return vision_module.VisionUnavailable(_RL_PROVIDER_PROSE, trace)


def _rl_attempt(
    cli: ModuleType,
    vision_module: ModuleType,
    config: Any,
    *,
    reason: str,
    schema_stage: Any = _RL_MISSING,
) -> Any:
    garment = tuple(cli._controlled_garments(config.garment_file))[0]
    return asyncio.run(
        cli._attempt_cpa_generated_garment(
            garment=garment,
            config=config,
            safe_fetcher=RulingGNoFetch(),
            vision=Task4Vision(
                _rl_vision_error(
                    vision_module,
                    reason=reason,
                    schema_stage=schema_stage,
                )
            ),
            image_provider=RulingGImageProvider(_make_image("PNG")),
            image_model_allowlist=(_RG_MODEL,),
            existing_item=None,
            counters=cli._Counters(),
        )
    )


def test_vision_schema_stage_contract_is_closed_on_public_outcomes(
    licensed_ingestion_cli: ModuleType,
) -> None:
    cli = licensed_ingestion_cli._load()
    assert frozenset(get_args(cli.SchemaStage)) == _RL_SCHEMA_STAGES
    for stage in _RL_SCHEMA_STAGES:
        failure_code = (
            "invalid_envelope_metadata" if stage == "envelope" else None
        )
        outcome = cli.ItemIngestionOutcome(
            garment_id="g051",
            status="quarantined",
            reason_code="response_schema_invalid",
            failure_stage="vision",
            schema_stage=stage,
            envelope_failure_code=failure_code,
            envelope_metadata_profile="cpa_chat_completion_metadata_v1",
        )
        assert outcome.schema_stage == stage
        assert outcome.envelope_failure_code == failure_code
        assert (
            outcome.envelope_metadata_profile
            == "cpa_chat_completion_metadata_v1"
        )
        assert outcome.model_dump(mode="json")["schema_stage"] == stage
    for stage in ("future", _RL_PROVIDER_PROSE, None, True, {"stage": "envelope"}):
        with pytest.raises(ValidationError):
            cli.ItemIngestionOutcome(
                garment_id="g051",
                status="quarantined",
                reason_code="response_schema_invalid",
                failure_stage="vision",
                schema_stage=stage,
            )
    for reason, stage in (
        ("model_mismatch", "envelope"),
        ("slot_mismatch", "payload"),
        ("timeout", None),
        ("provider_unavailable", None),
        ("http_error", None),
    ):
        outcome = cli.ItemIngestionOutcome(
            garment_id="g051",
            status="quarantined",
            reason_code=reason,
            failure_stage="vision",
            schema_stage=stage,
        )
        assert outcome.schema_stage == stage
    with pytest.raises(ValidationError):
        cli.ItemIngestionOutcome(
            garment_id="g051",
            status="quarantined",
            reason_code="response_schema_invalid",
            failure_stage="image_provider",
            schema_stage="envelope",
        )


def test_vision_schema_stage_survives_attempt_public_json_and_private_v3_diagnostic(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    offline_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    captured: list[Any] = []
    original_prepare = cli._prepare_private_quarantine

    def capture_prepare(config: Any, outcomes: list[Any]) -> Any:
        captured.extend(outcomes)
        return original_prepare(config, outcomes)

    monkeypatch.setattr(cli, "_prepare_private_quarantine", capture_prepare)
    response_bodies = {
        "envelope": _rk_completion(
            envelope_updates={"usage": {"provider": _RL_PROVIDER_PROSE}}
        ),
        "content": _rj_standard_completion(_RL_PROVIDER_PROSE),
        "payload": _catalog_completion_bytes(
            _catalog_payload(provider_note=_RL_PROVIDER_PROSE)
        ),
    }
    expected_codes = {
        "envelope": "unsupported_envelope_fields",
        "content": None,
        "payload": None,
    }
    for stage, response_body in response_bodies.items():
        case_root = tmp_path / stage
        case_root.mkdir()
        config = _rg_config(cli, case_root)
        result = _rg_run(
            cli,
            config,
            RulingGImageProvider(_make_image("PNG")),
            _vision_adapter(
                vision_module,
                offline_settings,
                CatalogVisionTransportSpy(response_body),
            ),
        )
        internal = captured[-1]
        public = result.item_outcomes[0]
        public_json = json.loads(result.model_dump_json())["item_outcomes"][0]
        private = _ri_rows(_ri_paths(config)[1])[0]
        assert internal.schema_stage == stage
        assert public.schema_stage == stage
        assert public_json["schema_stage"] == stage
        assert private["schema_version"] == 3
        assert private["schema_stage"] == stage
        for record in (public_json, private):
            assert record["envelope_failure_code"] == expected_codes[stage]
            assert (
                record["envelope_metadata_profile"]
                == "cpa_chat_completion_metadata_v1"
            )
        assert (private["failure_stage"], private["reason_code"]) == (
            "vision",
            "response_schema_invalid",
        )
        assert result.ready_ids == () and result.quarantined_ids == ("g051",)
        assert not any(
            row.get("garment_id") == "g051" for row in _ri_rows(config.sources_path)
        )
        rendered = json.dumps(
            {"public": public_json, "private": private}, ensure_ascii=False
        )
        assert _RL_PROVIDER_PROSE not in rendered


def test_non_schema_vision_failure_preserves_honest_closed_stage_at_every_boundary(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    captured: list[Any] = []
    original_prepare = cli._prepare_private_quarantine

    def capture_prepare(config: Any, outcomes: list[Any]) -> Any:
        captured.extend(outcomes)
        return original_prepare(config, outcomes)

    monkeypatch.setattr(cli, "_prepare_private_quarantine", capture_prepare)
    staged_cases = (
        ("model_mismatch", "envelope"),
        ("slot_mismatch", "payload"),
        ("product_type_mismatch", "payload"),
        ("audience_rejected", "payload"),
        ("identifiable_person", "payload"),
        ("low_confidence", "payload"),
        ("invalid_region", "payload"),
        ("quality_rejected", "payload"),
    )
    nullable_cases = (
        ("timeout", None),
        ("provider_unavailable", None),
        ("http_error", None),
    )
    for index, (reason, stage) in enumerate((*staged_cases, *nullable_cases)):
        case_root = tmp_path / f"case-{index}"
        case_root.mkdir()
        config = _rg_config(cli, case_root)
        result = _rg_run(
            cli,
            config,
            RulingGImageProvider(_make_image("PNG", size=(32 + index, 24))),
            Task4Vision(
                _rl_vision_error(
                    vision_module,
                    reason=reason,
                    schema_stage=stage if stage is not None else _RL_MISSING,
                )
            ),
        )
        internal = captured[-1]
        public = result.item_outcomes[0]
        public_json = json.loads(result.model_dump_json())["item_outcomes"][0]
        private = _ri_rows(_ri_paths(config)[1])[0]
        assert internal.schema_stage == public.schema_stage == stage
        assert public_json["schema_stage"] == private["schema_stage"] == stage
        assert private["schema_version"] == 3
        for record in (public_json, private):
            assert record["envelope_failure_code"] is None
            assert record["envelope_metadata_profile"] is None
        assert (private["failure_stage"], private["reason_code"]) == (
            "vision",
            reason,
        )
        assert result.ready_ids == () and result.quarantined_ids == ("g051",)
        assert not any(
            row.get("garment_id") == "g051" for row in _ri_rows(config.sources_path)
        )
        rendered = json.dumps(
            {"public": public_json, "private": private}, ensure_ascii=False
        )
        assert _RL_PROVIDER_PROSE not in rendered

    image_schema_failure = cli.ItemIngestionOutcome(
        garment_id="g051",
        status="quarantined",
        reason_code="response_schema_invalid",
        failure_stage="image_provider",
        schema_stage=None,
    )
    assert image_schema_failure.schema_stage is None


def test_invalid_or_missing_vision_schema_stage_fails_before_persistence(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    cases = (
        ("response_schema_invalid", _RL_MISSING),
        ("response_schema_invalid", None),
        ("response_schema_invalid", "future"),
        ("response_schema_invalid", _RL_PROVIDER_PROSE),
        ("timeout", "future"),
        ("provider_unavailable", _RL_PROVIDER_PROSE),
        ("future_reason", "payload"),
    )
    for index, (reason, stage) in enumerate(cases):
        case_root = tmp_path / f"case-{index}"
        case_root.mkdir()
        config = _rg_config(cli, case_root)
        provider = RulingGImageProvider(_make_image("PNG"))
        vision = Task4Vision(
            _rl_vision_error(
                vision_module,
                reason=reason,
                schema_stage=stage,
            )
        )
        with pytest.raises(ValueError, match="invalid_vision_schema_stage") as caught:
            _rg_run(cli, config, provider, vision)
        assert _RL_PROVIDER_PROSE not in str(caught.value)
        root, diagnostics = _ri_paths(config)
        assert not root.exists() and not diagnostics.exists()
        assert not config.manifest_path.exists() and not config.sources_path.exists()
        assert not config.asset_directory.exists()


def test_legacy_v1_private_diagnostic_without_stage_is_read_only_compatible(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    assert (
        cli._LEGACY_PRIVATE_QUARANTINE_DIAGNOSTIC_SHA256_ALLOWLIST
        == _RL_FROZEN_LEGACY_LINE_SHA256
    )
    config, private_png, diagnostics, _, png_before, _ = _rii_seed_private_truth(
        cli, vision_module, tmp_path, inject_transaction=False
    )
    legacy = _ri_rows(diagnostics)[0]
    legacy.pop("schema_version", None)
    legacy.pop("schema_stage", None)
    legacy.pop("envelope_failure_code", None)
    legacy.pop("envelope_metadata_profile", None)
    legacy_bytes = _rl_raw_line(legacy) + b"\n"
    diagnostics.write_bytes(legacy_bytes)
    monkeypatch.setattr(
        cli,
        "_LEGACY_PRIVATE_QUARANTINE_DIAGNOSTIC_SHA256_ALLOWLIST",
        frozenset({_rl_raw_line_sha256(legacy)}),
    )
    authoritative = {
        garment.garment_id: garment
        for garment in cli._controlled_garments(config.garment_file)
    }
    records = cli._validate_existing_private_quarantine(
        config,
        authoritative=authoritative,
        requested_image_model=_RG_MODEL,
        image_resolved_allowlist=(_RG_MODEL,),
    )
    assert len(records) == 1
    assert getattr(records[0], "schema_stage", None) is None
    assert "schema_stage" not in records[0].model_dump(mode="json", exclude_unset=True)
    assert diagnostics.read_bytes() == legacy_bytes
    assert private_png.read_bytes() == png_before

    result = _rg_run(
        cli,
        config,
        RulingGImageProvider(_make_image("PNG", size=(41, 29))),
        Task4Vision(
            _rl_vision_error(
                vision_module,
                reason="product_type_mismatch",
                schema_stage="payload",
            )
        ),
    )
    appended = diagnostics.read_bytes()
    assert appended.startswith(legacy_bytes)
    assert appended[: len(legacy_bytes)] == legacy_bytes
    rows = _ri_rows(diagnostics)
    assert len(rows) == 2
    assert "schema_version" not in rows[0] and "schema_stage" not in rows[0]
    assert rows[1]["schema_version"] == 3 and rows[1]["schema_stage"] == "payload"
    assert rows[1]["envelope_failure_code"] is None
    assert rows[1]["envelope_metadata_profile"] is None
    assert private_png.read_bytes() == png_before
    assert result.ready_ids == () and result.quarantined_ids == ("g051",)
    assert not any(
        row.get("garment_id") == "g051" for row in _ri_rows(config.sources_path)
    )


def test_unallowlisted_third_legacy_v1_diagnostic_fails_before_provider_or_write(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config, private_png, diagnostics, marker, _, _ = _rii_seed_private_truth(
        cli, vision_module, tmp_path, inject_transaction=False
    )
    base = _ri_rows(diagnostics)[0]
    base.pop("schema_version", None)
    base.pop("schema_stage", None)
    base.pop("envelope_failure_code", None)
    base.pop("envelope_metadata_profile", None)
    rows: list[dict[str, Any]] = []
    for digit in ("1", "2", "3"):
        row = dict(base)
        row["transaction_id"] = digit * 32
        rows.append(row)
    diagnostics.write_bytes(b"".join(_rl_raw_line(row) + b"\n" for row in rows))
    monkeypatch.setattr(
        cli,
        "_LEGACY_PRIVATE_QUARANTINE_DIAGNOSTIC_SHA256_ALLOWLIST",
        frozenset(_rl_raw_line_sha256(row) for row in rows[:2]),
        raising=False,
    )
    durable_paths = (
        private_png,
        diagnostics,
        config.manifest_path,
        config.sources_path,
        marker,
    )
    durable = tuple(
        (path.exists(), path.read_bytes() if path.exists() else None)
        for path in durable_paths
    )
    provider = RulingGImageProvider(
        b"", error=AssertionError("provider must not run for unallowlisted legacy")
    )
    with pytest.raises(ValueError, match="invalid_private_quarantine_diagnostics"):
        _rg_run(
            cli,
            config,
            provider,
            Task4Vision(AssertionError("Vision must not run")),
        )
    assert provider.prompts == []
    assert durable == tuple(
        (path.exists(), path.read_bytes() if path.exists() else None)
        for path in durable_paths
    )
    _ri_assert_public_quarantined(config)


def test_new_private_v3_diagnostic_cannot_use_legacy_missing_stage_path(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    outcome = _rl_attempt(
        cli,
        vision_module,
        config,
        reason="response_schema_invalid",
        schema_stage="envelope",
    )
    record = dict(outcome.private_quarantine_record)
    assert record["schema_version"] == 3 and record["schema_stage"] == "envelope"
    assert record["envelope_failure_code"] == "invalid_envelope_metadata"
    assert (
        record["envelope_metadata_profile"]
        == "cpa_chat_completion_metadata_v1"
    )
    for missing in (
        ("schema_stage",),
        ("schema_version",),
        ("envelope_failure_code",),
        ("envelope_metadata_profile",),
        ("schema_stage", "schema_version"),
        ("envelope_failure_code", "envelope_metadata_profile"),
    ):
        forged = dict(record)
        for key in missing:
            forged.pop(key)
        with pytest.raises((ValidationError, ValueError)):
            cli._prepare_private_quarantine(
                config,
                [replace(outcome, private_quarantine_record=forged)],
            )
    root, diagnostics = _ri_paths(config)
    assert not root.exists() and not diagnostics.exists()
    assert not config.manifest_path.exists() and not config.sources_path.exists()


# Ruling O: after the fourth real canary localized the rejection to the
# completion envelope, freeze a content-free diagnostic taxonomy before any
# further external call.  Every transport below is MockTransport/local-only.
_RO_ENVELOPE_FAILURE_CODES = frozenset(
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
_RO_METADATA_PROFILE = "cpa_chat_completion_metadata_v1"
_RO_PROVIDER_PROSE = "provider-secret-envelope-value-must-not-leak"


def _ro_vision_error(
    vision_module: ModuleType,
    *,
    reason: str = "response_schema_invalid",
    stage: str = "envelope",
    failure_code: Any = "invalid_envelope_metadata",
    profile: Any = _RO_METADATA_PROFILE,
) -> Exception:
    return vision_module.VisionUnavailable(
        _RO_PROVIDER_PROSE,
        {
            "reason_code": reason,
            "schema_stage": stage,
            "envelope_failure_code": failure_code,
            "envelope_metadata_profile": profile,
            **_RI_VISION_MODEL,
            "transport_model": "grok-4.6-high",
            "provider_body": _RO_PROVIDER_PROSE,
            "unknown_provider_key": _RO_PROVIDER_PROSE,
        },
    )


def _ro_private_v3_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 3,
        "transaction_id": "3" * 32,
        "garment_id": "g051",
        "user_id": "u01",
        "slot": "top",
        "product_type": "tie-neck blouse",
        "prompt_sha256": "a" * 64,
        "original_sha256": "b" * 64,
        "processed_sha256": "c" * 64,
        "quarantine_sha256": "c" * 64,
        "quarantine_relative_path": f"{'c' * 64}.png",
        "failure_stage": "vision",
        "reason_code": "response_schema_invalid",
        "schema_stage": "envelope",
        "envelope_failure_code": "invalid_envelope_metadata",
        "envelope_metadata_profile": _RO_METADATA_PROFILE,
        "image_model_provenance": {
            "requested_model": _RG_MODEL,
            "resolved_model": _RG_MODEL,
            "model_verified": True,
        },
        "vision_model_provenance": _RI_VISION_MODEL,
    }
    payload.update(overrides)
    return payload


def test_envelope_failure_code_and_small_metadata_profile_are_exactly_frozen(
    vision_module: ModuleType,
    licensed_ingestion_cli: ModuleType,
) -> None:
    cli = licensed_ingestion_cli._load()
    assert frozenset(get_args(vision_module.EnvelopeFailureCode)) == (
        _RO_ENVELOPE_FAILURE_CODES
    )
    assert (
        vision_module.CPA_CHAT_COMPLETION_METADATA_PROFILE
        == _RO_METADATA_PROFILE
    )
    assert cli.CPA_CHAT_COMPLETION_METADATA_PROFILE == _RO_METADATA_PROFILE


@pytest.mark.parametrize(
    ("response_body", "expected_code", "forbidden_tokens"),
    (
        (b'\xff', "envelope_json_invalid", ()),
        (b'{"model":', "envelope_json_invalid", ()),
        (
            b'{"model":"grok-4.6-high","model":"duplicate",'
            b'"choices":[]}',
            "envelope_json_invalid",
            ("duplicate",),
        ),
        (
            json.dumps({"model": "grok-4.6-high"}).encode(),
            "unsupported_envelope_fields",
            (),
        ),
        (
            _rk_completion(
                envelope_updates={"usage": {"secret": _RO_PROVIDER_PROSE}}
            ),
            "unsupported_envelope_fields",
            ("usage", "secret"),
        ),
        (
            _rk_completion(
                envelope_updates={"provider_note": _RO_PROVIDER_PROSE}
            ),
            "unsupported_envelope_fields",
            ("provider_note",),
        ),
        (
            _rk_completion(envelope_updates={"id": ""}),
            "invalid_envelope_metadata",
            (),
        ),
        (
            _rk_completion(envelope_updates={"object": "chat.completion.chunk"}),
            "invalid_envelope_metadata",
            ("chat.completion.chunk",),
        ),
        (
            _rk_completion(envelope_updates={"choices": []}),
            "invalid_choices",
            (),
        ),
        (
            _rk_completion(
                choice_updates={"logprobs": {"secret": _RO_PROVIDER_PROSE}}
            ),
            "invalid_choices",
            ("logprobs", "secret"),
        ),
        (
            _rk_completion(choice_updates={"index": 1}),
            "invalid_choice_metadata",
            (),
        ),
        (
            _rk_completion(choice_updates={"finish_reason": "length"}),
            "invalid_choice_metadata",
            ("length",),
        ),
        (
            _rk_completion(message_updates={"content": None}),
            "invalid_message",
            (),
        ),
        (
            _rk_completion(
                message_updates={"audio": {"secret": _RO_PROVIDER_PROSE}}
            ),
            "invalid_message",
            ("audio", "secret"),
        ),
        (
            _rk_completion(message_updates={"role": "tool"}),
            "invalid_message_metadata",
            ("tool",),
        ),
    ),
)
def test_mock_cpa_envelope_failures_map_to_content_free_closed_codes(
    vision_module: ModuleType,
    offline_settings: Any,
    response_body: bytes,
    expected_code: str,
    forbidden_tokens: tuple[str, ...],
) -> None:
    trace = _rj_schema_failure(
        vision_module,
        offline_settings,
        response_body,
        expected_stage="envelope",
    )
    assert trace["envelope_failure_code"] == expected_code
    assert trace["envelope_metadata_profile"] == _RO_METADATA_PROFILE
    rendered = json.dumps(trace, ensure_ascii=False)
    assert _RO_PROVIDER_PROSE not in rendered
    assert "unknown_provider_key" not in rendered
    for token in forbidden_tokens:
        assert token not in rendered


def test_envelope_code_and_profile_reach_public_cli_and_private_v3_only(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    offline_settings: Any,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    response = _rk_completion(
        envelope_updates={"provider_note": _RO_PROVIDER_PROSE}
    )
    result = _rg_run(
        cli,
        config,
        RulingGImageProvider(_make_image("PNG")),
        _vision_adapter(
            vision_module,
            offline_settings,
            CatalogVisionTransportSpy(response),
        ),
    )
    outcome = result.item_outcomes[0]
    public = json.loads(result.model_dump_json())["item_outcomes"][0]
    private = _ri_rows(_ri_paths(config)[1])[0]
    for record in (public, private):
        assert record["schema_stage"] == "envelope"
        assert record["envelope_failure_code"] == "unsupported_envelope_fields"
        assert record["envelope_metadata_profile"] == _RO_METADATA_PROFILE
    assert private["schema_version"] == 3
    assert result.ready_ids == () and result.quarantined_ids == ("g051",)
    _ri_assert_public_quarantined(config)
    assert not any(
        row.get("garment_id") == "g051" for row in _ri_rows(config.sources_path)
    )
    rendered = json.dumps({"public": public, "private": private})
    assert _RO_PROVIDER_PROSE not in rendered and "provider_note" not in rendered


def test_envelope_diagnostic_fields_reject_unknown_and_illegal_combinations(
    licensed_ingestion_cli: ModuleType,
) -> None:
    cli = licensed_ingestion_cli._load()
    valid = {
        "garment_id": "g051",
        "status": "quarantined",
        "reason_code": "response_schema_invalid",
        "failure_stage": "vision",
        "schema_stage": "envelope",
        "envelope_failure_code": "invalid_envelope_metadata",
        "envelope_metadata_profile": _RO_METADATA_PROFILE,
    }
    assert cli.ItemIngestionOutcome(**valid).envelope_failure_code == (
        "invalid_envelope_metadata"
    )
    invalid_public = (
        {**valid, "envelope_failure_code": "future_code"},
        {**valid, "envelope_metadata_profile": "future_profile"},
        {key: value for key, value in valid.items() if key != "envelope_failure_code"},
        {**valid, "schema_stage": "content"},
        {**valid, "schema_stage": "payload"},
        {**valid, "reason_code": "slot_mismatch"},
        {**valid, "failure_stage": "image_provider"},
    )
    for payload in invalid_public:
        with pytest.raises(ValidationError):
            cli.ItemIngestionOutcome(**payload)

    assert cli._PrivateQuarantineDiagnostic.model_validate(
        _ro_private_v3_payload()
    ).schema_version == 3
    for updates in (
        {"envelope_failure_code": "future_code"},
        {"envelope_metadata_profile": "future_profile"},
        {"envelope_failure_code": None},
        {"schema_stage": "content"},
        {"reason_code": "slot_mismatch"},
    ):
        with pytest.raises(ValidationError):
            cli._PrivateQuarantineDiagnostic.model_validate(
                _ro_private_v3_payload(**updates)
            )


@pytest.mark.parametrize(
    ("stage", "response_body"),
    (
        ("content", _rj_standard_completion(_RO_PROVIDER_PROSE)),
        (
            "payload",
            _catalog_completion_bytes(
                _catalog_payload(provider_note=_RO_PROVIDER_PROSE)
            ),
        ),
    ),
)
def test_content_and_payload_failures_have_profile_but_no_envelope_code(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    offline_settings: Any,
    tmp_path: Path,
    stage: str,
    response_body: bytes,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    result = _rg_run(
        cli,
        config,
        RulingGImageProvider(_make_image("PNG")),
        _vision_adapter(
            vision_module,
            offline_settings,
            CatalogVisionTransportSpy(response_body),
        ),
    )
    public = json.loads(result.model_dump_json())["item_outcomes"][0]
    private = _ri_rows(_ri_paths(config)[1])[0]
    for record in (public, private):
        assert record["schema_stage"] == stage
        assert record["envelope_failure_code"] is None
        assert record["envelope_metadata_profile"] == _RO_METADATA_PROFILE
    assert private["schema_version"] == 3
    assert _RO_PROVIDER_PROSE not in json.dumps(
        {"public": public, "private": private}
    )


def test_non_envelope_vision_failure_cannot_forge_envelope_code_before_write(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    with pytest.raises(ValueError, match="invalid_vision_envelope_diagnostic"):
        _rg_run(
            cli,
            config,
            RulingGImageProvider(_make_image("PNG")),
            Task4Vision(
                _ro_vision_error(
                    vision_module,
                    reason="slot_mismatch",
                    stage="payload",
                )
            ),
        )
    root, diagnostics = _ri_paths(config)
    assert not root.exists() and not diagnostics.exists()
    assert not config.manifest_path.exists() and not config.sources_path.exists()


def test_historical_v1_v2_prefix_is_byte_exact_when_private_v3_is_appended(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    repository = Path(__file__).resolve().parents[1]
    frozen_diagnostics = (
        repository / "data/assets/cpa_generated_quarantine_diagnostics.jsonl"
    )
    frozen_private = repository / "data/assets/private_quarantine/cpa_generated"
    frozen_bytes = frozen_diagnostics.read_bytes()
    assert hashlib.sha256(frozen_bytes).hexdigest() == (
        "0f981797ee2dc37d41a0563e1d5d34b8d31802ac5589beae4aa223309843dd28"
    )
    frozen_rows = [json.loads(line) for line in frozen_bytes.splitlines()]
    assert [row.get("schema_version") for row in frozen_rows] == [None, None, 2]
    assert [row.get("schema_stage") for row in frozen_rows] == [
        None,
        None,
        "envelope",
    ]

    config = _rg_config(cli, tmp_path)
    private_root, diagnostics = _ri_paths(config)
    private_root.mkdir(parents=True)
    diagnostics.write_bytes(frozen_bytes)
    for row in frozen_rows:
        name = row["quarantine_relative_path"]
        (private_root / name).write_bytes((frozen_private / name).read_bytes())

    result = _rg_run(
        cli,
        config,
        RulingGImageProvider(_make_image("PNG", size=(43, 31))),
        Task4Vision(_ro_vision_error(vision_module)),
    )
    appended = diagnostics.read_bytes()
    assert appended[: len(frozen_bytes)] == frozen_bytes
    rows = [json.loads(line) for line in appended.splitlines()]
    assert len(rows) == 4
    assert rows[-1]["schema_version"] == 3
    assert rows[-1]["schema_stage"] == "envelope"
    assert rows[-1]["envelope_failure_code"] == "invalid_envelope_metadata"
    assert rows[-1]["envelope_metadata_profile"] == _RO_METADATA_PROFILE
    assert result.ready_ids == () and result.quarantined_ids == ("g051",)


# Ruling O follow-up: schema v2 is historical evidence, never a generally
# writable compatibility version.  Only the frozen Ruling N raw line is valid.
_RP_FROZEN_V2_LINE_SHA256 = (
    "51bbd3a82208c66f7269ad8adade0517a2f8c2b07689e1dddacbe22d9d86397e"
)
_RP_FROZEN_DIAGNOSTIC_SHA256 = (
    "0f981797ee2dc37d41a0563e1d5d34b8d31802ac5589beae4aa223309843dd28"
)


def _rp_frozen_diagnostic_evidence() -> tuple[Path, bytes, list[bytes]]:
    repository = Path(__file__).resolve().parents[1]
    path = repository / "data/assets/cpa_generated_quarantine_diagnostics.jsonl"
    payload = path.read_bytes()
    lines = payload.splitlines()
    assert len(lines) == 3
    assert hashlib.sha256(payload).hexdigest() == _RP_FROZEN_DIAGNOSTIC_SHA256
    assert hashlib.sha256(lines[2]).hexdigest() == _RP_FROZEN_V2_LINE_SHA256
    return path, payload, lines


def _rp_forged_v2_line(**updates: Any) -> bytes:
    _, _, lines = _rp_frozen_diagnostic_evidence()
    row = json.loads(lines[2])
    row.update(updates)
    forged = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert hashlib.sha256(forged).hexdigest() != _RP_FROZEN_V2_LINE_SHA256
    return forged


def test_historical_private_v2_allowlist_contains_only_frozen_ruling_n_line(
    licensed_ingestion_cli: ModuleType,
) -> None:
    cli = licensed_ingestion_cli._load()
    assert cli._LEGACY_V2_PRIVATE_QUARANTINE_DIAGNOSTIC_SHA256_ALLOWLIST == (
        frozenset({_RP_FROZEN_V2_LINE_SHA256})
    )


@pytest.mark.parametrize(
    ("updates"),
    (
        {"transaction_id": "f" * 32},
        {"prompt_sha256": "f" * 64},
    ),
)
def test_any_mutated_private_v2_line_is_rejected_by_loader(
    licensed_ingestion_cli: ModuleType,
    updates: dict[str, Any],
) -> None:
    cli = licensed_ingestion_cli._load()
    forged = _rp_forged_v2_line(**updates)
    with pytest.raises(
        ValueError,
        match="unrecognized_legacy_v2_private_quarantine_diagnostic",
    ):
        cli._private_quarantine_diagnostic_from_raw_line(forged.decode("utf-8"))


def test_forged_private_v2_fails_before_provider_and_any_state_write(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    repository = Path(__file__).resolve().parents[1]
    _, _, lines = _rp_frozen_diagnostic_evidence()
    forged = _rp_forged_v2_line(transaction_id="f" * 32)
    config = _rg_config(cli, tmp_path)
    private_root, diagnostics = _ri_paths(config)
    private_root.mkdir(parents=True)
    diagnostics.write_bytes(b"\n".join((*lines[:2], forged)) + b"\n")
    frozen_private = repository / "data/assets/private_quarantine/cpa_generated"
    for raw_line in (*lines[:2], forged):
        row = json.loads(raw_line)
        name = row["quarantine_relative_path"]
        (private_root / name).write_bytes((frozen_private / name).read_bytes())

    marker = config.asset_directory.parent / "cpa_generated_quarantine_transaction.json"
    durable_paths = (private_root, diagnostics, config.manifest_path, config.sources_path, marker)
    durable = {
        path: (
            tuple(
                (child.relative_to(path), child.read_bytes())
                for child in sorted(path.rglob("*"))
                if child.is_file()
            )
            if path.is_dir()
            else (path.read_bytes() if path.exists() else None)
        )
        for path in durable_paths
    }
    provider = RulingGImageProvider(_make_image("PNG"))
    vision = Task4Vision(AssertionError("Vision must not run"))
    with pytest.raises(
        ValueError,
        match="unrecognized_legacy_v2_private_quarantine_diagnostic",
    ):
        _rg_run(cli, config, provider, vision)
    assert provider.prompts == [] and vision.calls == [] and not marker.exists()
    assert durable == {
        path: (
            tuple(
                (child.relative_to(path), child.read_bytes())
                for child in sorted(path.rglob("*"))
                if child.is_file()
            )
            if path.is_dir()
            else (path.read_bytes() if path.exists() else None)
        )
        for path in durable_paths
    }


def test_frozen_ruling_n_private_v2_is_read_compatible_and_byte_exact(
    licensed_ingestion_cli: ModuleType,
) -> None:
    cli = licensed_ingestion_cli._load()
    path, before, lines = _rp_frozen_diagnostic_evidence()
    record = cli._private_quarantine_diagnostic_from_raw_line(
        lines[2].decode("utf-8")
    )
    assert record.schema_version == 2 and record.schema_stage == "envelope"
    assert path.read_bytes() == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        _RP_FROZEN_DIAGNOSTIC_SHA256
    )


# Ruling U: CLIProxyAPI 7.2.97 commit 42f36b94's non-stream OpenAI serializer
# has a different, source-proven completion shape.  These are hand-written
# wire envelopes: the only double is the HTTP opener below VisionAdapter.
_RU_PROFILE = "cpa_chat_completion_metadata_v2"
_RU_FAILURE_PROFILES = frozenset(
    {"cpa_chat_completion_metadata_v1", "cpa_chat_completion_metadata_v2"}
)
_RU_METADATA_SENTINEL = "serializer-metadata-must-not-escape"


def _ru_tool_call() -> dict[str, Any]:
    return {
        "id": "call_mock_only",
        "type": "function",
        "function": {"name": "inspect_catalog_asset", "arguments": "{}"},
    }


def _ru_usage() -> dict[str, Any]:
    return {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "prompt_tokens_details": {
            "cached_tokens": 2,
            "cached_creation_tokens": 1,
        },
        "completion_tokens_details": {"reasoning_tokens": 3},
    }


def _ru_completion(
    *,
    native_finish_reason: str | None = "stop",
    reasoning_content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    envelope_updates: dict[str, Any] | None = None,
    choice_updates: dict[str, Any] | None = None,
    message_updates: dict[str, Any] | None = None,
) -> bytes:
    """One complete, independently-written CPA v2 non-stream response."""
    message: dict[str, Any] = {
        "role": "assistant",
        "content": json.dumps(_catalog_payload()),
        "reasoning_content": reasoning_content,
        "tool_calls": tool_calls,
    }
    message.update(message_updates or {})
    choice: dict[str, Any] = {
        "index": 0,
        "finish_reason": "stop",
        "native_finish_reason": native_finish_reason,
        "message": message,
    }
    choice.update(choice_updates or {})
    envelope: dict[str, Any] = {
        "id": "chatcmpl-cli-proxy-mock",
        "object": "chat.completion",
        "created": 1_784_838_400,
        "model": "grok-4.6-high",
        "choices": [choice],
    }
    if usage is not None:
        envelope["usage"] = usage
    envelope.update(envelope_updates or {})
    return json.dumps(envelope).encode("utf-8")


def _ru_assert_rejected(
    vision_module: ModuleType,
    offline_settings: Any,
    response_body: bytes,
    *,
    forbidden: tuple[str, ...],
) -> None:
    transport = CatalogVisionTransportSpy(response_body)
    adapter = _vision_adapter(vision_module, offline_settings, transport)
    with pytest.raises(vision_module.VisionUnavailable) as caught:
        _inspect_catalog(adapter)
    trace = _assert_catalog_failure(
        vision_module, caught, "response_schema_invalid"
    )
    assert trace["schema_stage"] == "envelope"
    assert trace["envelope_metadata_profile"] == _RU_PROFILE
    rendered = json.dumps(trace, ensure_ascii=False)
    assert _RU_METADATA_SENTINEL not in rendered
    for value in forbidden:
        assert value not in rendered
    # The real adapter performs exactly one fake HTTP round-trip.  It has no
    # ingestion or persistence collaborator on this direct boundary.
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("native_finish_reason", "reasoning_content", "tool_calls", "usage"),
    (
        ("stop", None, None, _ru_usage()),
        (None, _RU_METADATA_SENTINEL, None, {"total_tokens": 0}),
        ("stop", _RU_METADATA_SENTINEL, None, None),
    ),
)
def test_ruling_u_accepts_and_discards_complete_cliproxy_v2_metadata(
    vision_module: ModuleType,
    offline_settings: Any,
    native_finish_reason: str | None,
    reasoning_content: str | None,
    tool_calls: list[dict[str, Any]] | None,
    usage: dict[str, Any] | None,
) -> None:
    transport = CatalogVisionTransportSpy(
        _ru_completion(
            native_finish_reason=native_finish_reason,
            reasoning_content=reasoning_content,
            tool_calls=tool_calls,
            usage=usage,
        )
    )
    assessment, trace = _inspect_catalog(
        _vision_adapter(vision_module, offline_settings, transport)
    )
    assert assessment.model_dump(mode="json") == _catalog_payload()
    rendered = json.dumps({"assessment": assessment.model_dump(), "trace": trace})
    for metadata_key in (
        "native_finish_reason",
        "reasoning_content",
        "tool_calls",
        "usage",
        "cached_tokens",
        "cached_creation_tokens",
        "reasoning_tokens",
    ):
        assert metadata_key not in rendered
    assert _RU_METADATA_SENTINEL not in rendered
    assert len(transport.calls) == 1


def test_ruling_u_discards_v2_metadata_before_ready_output_or_persistence(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    offline_settings: Any,
    tmp_path: Path,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    transport = CatalogVisionTransportSpy(
        _ru_completion(
            reasoning_content=_RU_METADATA_SENTINEL,
            tool_calls=None,
            usage=_ru_usage(),
        )
    )
    result = _rg_run(
        cli,
        config,
        RulingGImageProvider(_make_image("PNG")),
        _vision_adapter(vision_module, offline_settings, transport),
    )
    persisted = json.dumps(
        {
            "output": json.loads(result.model_dump_json()),
            "manifest": _task4_manifest(config.manifest_path),
            "sources": _ri_rows(config.sources_path),
        },
        ensure_ascii=False,
    )
    assert result.ready_ids == ("g051",) and result.quarantined_ids == ()
    assert not _ri_paths(config)[1].exists()
    for metadata_key in (
        "native_finish_reason",
        "reasoning_content",
        "tool_calls",
        "usage",
        "cached_tokens",
        "cached_creation_tokens",
        "reasoning_tokens",
        _RU_METADATA_SENTINEL,
    ):
        assert metadata_key not in persisted
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("response_body", "forbidden"),
    (
        (_ru_completion(envelope_updates={"future_top": _RU_METADATA_SENTINEL}), ("future_top",)),
        (_ru_completion(choice_updates={"future_choice": _RU_METADATA_SENTINEL}), ("future_choice",)),
        (_ru_completion(message_updates={"future_message": _RU_METADATA_SENTINEL}), ("future_message",)),
        (_ru_completion(envelope_updates={"usage": {"future_usage": _RU_METADATA_SENTINEL}}), ("future_usage",)),
        (_ru_completion(envelope_updates={"usage": {"prompt_tokens_details": {"future_detail": _RU_METADATA_SENTINEL}}}), ("future_detail",)),
        (_ru_completion(message_updates={"tool_calls": [{**_ru_tool_call(), "future_tool": _RU_METADATA_SENTINEL}]}), ("future_tool",)),
        (_ru_completion(message_updates={"tool_calls": [{**_ru_tool_call(), "function": {"name": "inspect_catalog_asset", "arguments": "{}", "future_function": _RU_METADATA_SENTINEL}}]}), ("future_function",)),
        (_ru_completion(envelope_updates={"system_fingerprint": _RU_METADATA_SENTINEL}), ("system_fingerprint",)),
        (_ru_completion(message_updates={"refusal": _RU_METADATA_SENTINEL}), ("refusal",)),
        (_ru_completion(choice_updates={"finish_reason": None}), ()),
        (_ru_completion(choice_updates={"finish_reason": "length"}), ("length",)),
        (_ru_completion(choice_updates={"native_finish_reason": True}), ()),
        (_ru_completion(choice_updates={"native_finish_reason": "length"}), ("length",)),
        (_ru_completion(choice_updates={"native_finish_reason": []}), ()),
        (_ru_completion(choice_updates={"native_finish_reason": {"value": _RU_METADATA_SENTINEL}}), ()),
        (_ru_completion(choice_updates={"native_finish_reason": 1.0}), ()),
        (_ru_completion(message_updates={"content": None}), ()),
        (_ru_completion(message_updates={"reasoning_content": {"value": _RU_METADATA_SENTINEL}}), ()),
        (_ru_completion(message_updates={"reasoning_content": True}), ()),
        (_ru_completion(message_updates={"reasoning_content": 1.0}), ()),
        (_ru_completion(message_updates={"reasoning_content": []}), ()),
        (_ru_completion(message_updates={"tool_calls": []}), ()),
        (_ru_completion(message_updates={"tool_calls": [_ru_tool_call()]}), ()),
        (_ru_completion(message_updates={"tool_calls": {"value": _RU_METADATA_SENTINEL}}), ()),
        (_ru_completion(message_updates={"tool_calls": True}), ()),
        (_ru_completion(message_updates={"tool_calls": 1.0}), ()),
        (_ru_completion(message_updates={"tool_calls": "call"}), ("call",)),
        (_ru_completion(message_updates={"tool_calls": [{**_ru_tool_call(), "id": None}]}), ()),
        (_ru_completion(message_updates={"tool_calls": [None]}), ()),
        (_ru_completion(message_updates={"tool_calls": ["call"]}), ("call",)),
        (_ru_completion(message_updates={"tool_calls": [{**_ru_tool_call(), "type": "other"}]}), ("other",)),
        (_ru_completion(message_updates={"tool_calls": [{**_ru_tool_call(), "function": {"name": 1, "arguments": "{}"}}]}), ()),
        (_ru_completion(envelope_updates={"usage": {}}), ()),
        (_ru_completion(envelope_updates={"usage": None}), ()),
        (_ru_completion(envelope_updates={"usage": []}), ()),
        (_ru_completion(envelope_updates={"usage": "18"}), ("18",)),
        (_ru_completion(envelope_updates={"usage": True}), ()),
        (_ru_completion(envelope_updates={"usage": -1}), ()),
        (_ru_completion(envelope_updates={"usage": {"prompt_tokens": True}}), ()),
        (_ru_completion(envelope_updates={"usage": {"prompt_tokens": 1.5}}), ()),
        (_ru_completion(envelope_updates={"usage": {"prompt_tokens": -1}}), ()),
        (_ru_completion(envelope_updates={"usage": {"prompt_tokens_details": {"cached_tokens": False}}}), ()),
        (_ru_completion(envelope_updates={"usage": {"completion_tokens_details": {"reasoning_tokens": -1}}}), ()),
    ),
)
def test_ruling_u_rejects_unknown_or_noncanonical_v2_metadata_without_leaks(
    vision_module: ModuleType,
    offline_settings: Any,
    response_body: bytes,
    forbidden: tuple[str, ...],
) -> None:
    _ru_assert_rejected(
        vision_module, offline_settings, response_body, forbidden=forbidden
    )


def _ru_profiled_failure(
    vision_module: ModuleType,
    *,
    reason: str,
    stage: str,
    failure_code: str | None,
    profile: str,
) -> Exception:
    """A hand-written post-transport trace; metadata must never leave its boundary."""
    return vision_module.VisionUnavailable(
        _RU_METADATA_SENTINEL,
        {
            "reason_code": reason,
            "schema_stage": stage,
            "envelope_failure_code": failure_code,
            "envelope_metadata_profile": profile,
            **_RI_VISION_MODEL,
            "transport_model": "grok-4.6-high",
            "provider_body": _RU_METADATA_SENTINEL,
            "serializer_metadata": {"tool_calls": _RU_METADATA_SENTINEL},
        },
    )


def test_ruling_u_failure_profile_allowlist_is_exactly_v1_and_v2(
    licensed_ingestion_cli: ModuleType,
) -> None:
    cli = licensed_ingestion_cli._load()
    assert frozenset(get_args(cli.EnvelopeMetadataProfile)) == _RU_FAILURE_PROFILES


@pytest.mark.parametrize("profile", tuple(sorted(_RU_FAILURE_PROFILES)))
@pytest.mark.parametrize(
    ("reason", "stage", "failure_code"),
    (
        ("response_schema_invalid", "envelope", "invalid_envelope_metadata"),
        ("response_schema_invalid", "content", None),
        ("response_schema_invalid", "payload", None),
        ("model_mismatch", "envelope", None),
    ),
)
def test_ruling_u_profiled_failures_reach_public_and_private_v3_without_leaks(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
    profile: str,
    reason: str,
    stage: str,
    failure_code: str | None,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    provider = RulingGImageProvider(_make_image("PNG"))
    vision = Task4Vision(
        _ru_profiled_failure(
            vision_module,
            reason=reason,
            stage=stage,
            failure_code=failure_code,
            profile=profile,
        )
    )
    result = _rg_run(cli, config, provider, vision)
    public = json.loads(result.model_dump_json())["item_outcomes"][0]
    private = _ri_rows(_ri_paths(config)[1])[0]
    for record in (public, private):
        assert record["reason_code"] == reason
        assert record["schema_stage"] == stage
        assert record["envelope_failure_code"] == failure_code
        assert record["envelope_metadata_profile"] == profile
    assert private["schema_version"] == 3
    assert result.ready_ids == () and result.quarantined_ids == ("g051",)
    _ri_assert_public_quarantined(config)
    rendered = json.dumps({"public": public, "private": private}, ensure_ascii=False)
    assert _RU_METADATA_SENTINEL not in rendered
    assert "serializer_metadata" not in rendered and "tool_calls" not in rendered
    assert len(provider.prompts) == 1 and len(vision.calls) == 1


@pytest.mark.parametrize("profile", ("future_profile", _RU_METADATA_SENTINEL))
def test_ruling_u_unknown_profile_is_rejected_before_any_durable_write(
    licensed_ingestion_cli: ModuleType,
    vision_module: ModuleType,
    tmp_path: Path,
    profile: str,
) -> None:
    cli = licensed_ingestion_cli._load()
    config = _rg_config(cli, tmp_path)
    provider = RulingGImageProvider(_make_image("PNG"))
    vision = Task4Vision(
        _ru_profiled_failure(
            vision_module,
            reason="response_schema_invalid",
            stage="envelope",
            failure_code="invalid_envelope_metadata",
            profile=profile,
        )
    )
    with pytest.raises(ValueError, match="invalid_vision_envelope_diagnostic"):
        _rg_run(cli, config, provider, vision)
    private_root, diagnostics = _ri_paths(config)
    assert not private_root.exists() and not diagnostics.exists()
    assert not config.manifest_path.exists() and not config.sources_path.exists()
    assert len(provider.prompts) == 1 and len(vision.calls) == 1
