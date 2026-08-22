from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import ipaddress
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import ModuleType
from typing import Any, get_args

import httpx
import pytest
from PIL import Image, PngImagePlugin
from pydantic import ValidationError


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


class TransportSpy:
    def __init__(self, routes: dict[str, httpx.Response | Exception]) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.network_calls: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        # A validated hostname must never be resolved a second time by the HTTP
        # stack: the network target is the already-validated IP, while Host and
        # TLS SNI retain the original hostname. This is the Task 2 anti-rebinding
        # boundary and is observable even through MockTransport.
        network_url = str(request.url)
        network_host = request.url.host
        assert network_host is not None
        assert ipaddress.ip_address(network_host).is_global
        host_header = request.headers.get("host")
        assert host_header
        logical_hostname = host_header.rsplit(":", 1)[0]
        sni_hostname = request.extensions.get("sni_hostname")
        assert sni_hostname in {logical_hostname, logical_hostname.encode("ascii")}
        path_and_query = request.url.raw_path.decode("ascii")
        logical_url = f"https://{host_header}{path_and_query}"
        self.calls.append(logical_url)
        self.network_calls.append(network_url)
        response = self.routes[logical_url]
        if isinstance(response, Exception):
            raise response
        return response


class ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def _response(
    status: int,
    *,
    body: bytes = b"",
    content_type: str | None = None,
    headers: dict[str, str] | None = None,
    stream: httpx.AsyncByteStream | None = None,
) -> httpx.Response:
    response_headers = dict(headers or {})
    if content_type is not None:
        response_headers["content-type"] = content_type
    kwargs: dict[str, Any] = {"status_code": status, "headers": response_headers}
    if stream is None:
        kwargs["content"] = body
    else:
        kwargs["stream"] = stream
    return httpx.Response(**kwargs)


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
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport),
        follow_redirects=False,
    ) as client:
        fetcher = module.SafeImageFetcher(
            client=client,
            resolver=resolver,
            config=config or module.SafeImageConfig(),
            ready_dir=ready_dir,
            quarantine_dir=quarantine_dir,
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
        ("openverse", "CC0", "https://images.example/cc0", None, None, "CC0"),
        ("openverse", "PDM", "https://images.example/pdm", None, None, "Public Domain"),
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
        ("licensed_photo", "CC0", None, None, "CC0"),
        ("licensed_photo", "PDM", None, None, "Public Domain"),
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
    receipt = licensed_assets.PublicLicenseReceipt.model_validate(
        _license_payload(
            code=license_code,
            name=license_code,
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
    license_payload = _license_payload(
        code=license_code,
        name=license_code,
        url=None if license_code in {"user-owned", "ai-generated"} else _license_payload()["url"],
        author=None if license_code in {"user-owned", "ai-generated"} else "Example Creator",
        attribution=license_code,
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
    assert not ipaddress.ip_address(address).is_global
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
    }
    assert _literal_values(licensed_assets.ImageFetchReasonCode) == allowed_reasons
    error = licensed_assets.ImageFetchError("unsafe_address")
    _assert_content_free_error(licensed_assets, error, "unsafe_address")
    with pytest.raises((TypeError, ValueError, ValidationError)):
        licensed_assets.ImageFetchError("https://private.example/secret")
