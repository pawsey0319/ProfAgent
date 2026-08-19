from __future__ import annotations

import base64
import binascii
import asyncio
import ipaddress
import json
import re
import socket
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .asset_service import AssetError, AssetService, MAX_ASSET_BYTES
from .config import CPA_IMAGE_MODEL, Settings
from .providers import ProviderUnavailable


class GrokImageProvider:
    """Independent CPA adapter for one validated, static 2D image."""

    requested_model = CPA_IMAGE_MODEL
    transport_model = CPA_IMAGE_MODEL
    _MAX_ENVELOPE_BYTES = 8 * 1024 * 1024
    _CPA_TRACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if settings.cpa_image_model != CPA_IMAGE_MODEL:
            raise ValueError("image model must match the frozen CPA image contract")
        self.settings = settings
        self.transport = transport
        self.resolved_model: str | None = None
        self._attempted = False
        self._available = False
        self._request_model_pinned = False
        self._cpa_trace_verified = False
        self._model_reported = False
        self._model_verified = False
        self._verification_basis: str | None = None
        self._last_reason: str | None = None
        self._unavailable_until = 0.0

    def set_transport(self, transport: httpx.AsyncBaseTransport | None) -> None:
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.cpa_api_key:
            headers["Authorization"] = f"Bearer {self.settings.cpa_api_key}"
        return headers

    def _record_failure(
        self,
        reason_code: str,
        *,
        opens_circuit: bool,
        request_model_pinned: bool = False,
        cpa_trace_verified: bool = False,
        model_reported: bool = False,
    ) -> None:
        self._attempted = True
        self._available = False
        self._request_model_pinned = request_model_pinned
        self._cpa_trace_verified = cpa_trace_verified
        self._model_reported = model_reported
        self._model_verified = False
        self._verification_basis = None
        self.resolved_model = None
        self._last_reason = reason_code
        if opens_circuit:
            self._unavailable_until = time.monotonic() + 30.0

    def mark_timeout(self) -> None:
        self._record_failure(
            "CPA_IMAGE_BUDGET_EXCEEDED",
            opens_circuit=True,
            request_model_pinned=True,
        )

    @classmethod
    def _verified_cpa_trace_header(cls, headers: httpx.Headers) -> bool:
        values = headers.get_list("x-cpa-trace-id")
        return (
            len(values) == 1
            and isinstance(values[0], str)
            and cls._CPA_TRACE_ID.fullmatch(values[0]) is not None
        )

    @staticmethod
    def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("image response contains duplicate JSON keys")
            result[key] = value
        return result

    def health(self) -> dict[str, Any]:
        enabled = self.settings.cpa_image_enabled
        circuit_open = time.monotonic() < self._unavailable_until
        available = bool(enabled and self._available and not circuit_open)
        return {
            "enabled": enabled,
            "available": available,
            "status": "ok" if available else "fallback",
            "degraded": bool(enabled and not available),
            "attempted": self._attempted,
            "requested_model": self.requested_model,
            "transport_model": self.transport_model,
            "request_model_pinned": self._request_model_pinned,
            "cpa_trace_verified": self._cpa_trace_verified,
            "model_reported": self._model_reported,
            "resolved_model": self.resolved_model,
            "model_verified": self._model_verified,
            "verification_basis": self._verification_basis,
            "endpoint": f"{self.settings.cpa_base_url}/images/generations",
            "circuit_open": circuit_open,
            "reason": (
                "disabled"
                if not enabled
                else (
                    "CPA_IMAGE_CIRCUIT_OPEN"
                    if circuit_open
                    else self._last_reason
                    or (None if available else "image_model_unverified")
                )
            ),
        }

    @staticmethod
    def _public_https_url(value: Any) -> str:
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError("image URL is invalid")
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.port not in {None, 443}
        ):
            raise ValueError("image URL is not an allowed public HTTPS URL")
        return value

    @staticmethod
    def _resolve_public_host(host: str) -> None:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            }
        except OSError as exc:
            raise ValueError("image URL host could not be resolved") from exc
        if not addresses:
            raise ValueError("image URL host did not resolve")
        for raw in addresses:
            address = ipaddress.ip_address(raw)
            if not address.is_global:
                raise ValueError("image URL resolved to a non-public address")

    async def _download(self, client: httpx.AsyncClient, value: Any) -> bytes:
        url = self._public_https_url(value)
        host = urlsplit(url).hostname
        assert host is not None
        normalized_host = host.rstrip(".").lower()
        # Remote URL bodies are disabled unless an operator has explicitly
        # frozen the expected CDN hostname.  This is the trust boundary that
        # prevents a response-controlled host from winning a second DNS lookup
        # with a private address after validation (DNS rebinding/TOCTOU).
        if normalized_host not in self.settings.cpa_image_download_hosts:
            raise ValueError("image URL host is not allowlisted")
        await asyncio.to_thread(self._resolve_public_host, normalized_host)
        allowed_content_types = {"image/png", "image/jpeg", "image/webp"}
        body = bytearray()
        async with client.stream("GET", url, follow_redirects=False) as response:
            if 300 <= response.status_code < 400:
                raise ValueError("image URL redirects are rejected")
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").strip().lower()
            if content_type not in allowed_content_types:
                raise ValueError("image URL Content-Type is not an allowed static image")
            declared_length = response.headers.get("content-length")
            if declared_length:
                try:
                    parsed_length = int(declared_length)
                except ValueError as exc:
                    raise ValueError("invalid image Content-Length") from exc
                if parsed_length < 0 or parsed_length > MAX_ASSET_BYTES:
                    raise ValueError("downloaded image exceeds 5MB")
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_ASSET_BYTES:
                    raise ValueError("downloaded image exceeds 5MB")
        # Bind the response header, signature and decoder to one exact type.
        AssetService.validate_static_image(bytes(body), content_type)
        return bytes(body)

    async def generate_static_2d(
        self, prompt: str
    ) -> tuple[bytes, str, dict[str, Any]]:
        if not self.settings.cpa_image_enabled:
            raise ProviderUnavailable(
                "CPA image provider is disabled",
                reason_code="CPA_IMAGE_PROVIDER_DISABLED",
            )
        if time.monotonic() < self._unavailable_until:
            raise ProviderUnavailable(
                "CPA image circuit breaker is open",
                reason_code="CPA_IMAGE_CIRCUIT_OPEN",
            )
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
            raise ProviderUnavailable(
                "static 2D prompt rejected",
                reason_code="CPA_IMAGE_INPUT_REJECTED",
                opens_circuit=False,
            )
        request_body = {
            "model": self.transport_model,
            "prompt": prompt.strip(),
            "n": 1,
            "size": "1024x1024",
            "response_format": "b64_json",
        }
        request_model_pinned = request_body["model"] == self.transport_model
        request_started = False
        cpa_trace_verified = False
        model_reported = False
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.effective_cpa_image_timeout_seconds,
                trust_env=False,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                envelope_bytes = bytearray()
                request_started = True
                async with client.stream(
                    "POST",
                    f"{self.settings.cpa_base_url}/images/generations",
                    headers=self._headers(),
                    json=request_body,
                ) as response:
                    response.raise_for_status()
                    cpa_trace_verified = self._verified_cpa_trace_header(
                        response.headers
                    )
                    content_type = response.headers.get("content-type", "")
                    if content_type.split(";", 1)[0].strip().lower() != "application/json":
                        raise ValueError("image response envelope must be JSON")
                    declared_length = response.headers.get("content-length")
                    if declared_length:
                        try:
                            parsed_length = int(declared_length)
                        except ValueError as exc:
                            raise ValueError("invalid image envelope Content-Length") from exc
                        if parsed_length < 0 or parsed_length > self._MAX_ENVELOPE_BYTES:
                            raise ValueError("image response envelope exceeds 8MB")
                    async for chunk in response.aiter_bytes():
                        envelope_bytes.extend(chunk)
                        if len(envelope_bytes) > self._MAX_ENVELOPE_BYTES:
                            raise ValueError("image response envelope exceeds 8MB")
                envelope = json.loads(
                    envelope_bytes,
                    object_pairs_hook=self._reject_duplicate_json_keys,
                )
                if not isinstance(envelope, dict):
                    raise TypeError("image response must be an object")
                model_reported = "model" in envelope
                if not request_model_pinned or not cpa_trace_verified:
                    raise ProviderUnavailable(
                        "CPA image response did not include a verified receipt",
                        reason_code="CPA_IMAGE_TRACE_VERIFICATION_FAILED",
                        opens_circuit=False,
                    )
                if model_reported:
                    reported = envelope["model"]
                    if reported != self.transport_model:
                        raise ProviderUnavailable(
                            "CPA image response model is not allowlisted",
                            reason_code="CPA_IMAGE_MODEL_VERIFICATION_FAILED",
                        )
                    model_verified = True
                    resolved_model: str | None = self.transport_model
                    verification_basis = "reported_model_exact"
                else:
                    model_verified = False
                    resolved_model = None
                    verification_basis = "exact_request_with_cpa_trace"
                data = envelope.get("data")
                if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                    raise TypeError("image response data must contain exactly one item")
                item = data[0]
                has_b64 = "b64_json" in item
                has_url = "url" in item
                if has_b64 == has_url:
                    raise ValueError("image response must contain exactly one body source")
                if has_b64:
                    encoded = item["b64_json"]
                    if not isinstance(encoded, str):
                        raise TypeError("image base64 must be text")
                    image_bytes = base64.b64decode(encoded, validate=True)
                    source = "b64_json"
                else:
                    image_bytes = await self._download(client, item["url"])
                    source = "url"
            media_type, width, height = AssetService.validate_static_image(image_bytes)
            self._attempted = True
            self._available = True
            self._request_model_pinned = True
            self._cpa_trace_verified = cpa_trace_verified
            self._model_reported = model_reported
            self._model_verified = model_verified
            self._verification_basis = verification_basis
            self.resolved_model = resolved_model
            self._last_reason = None
            self._unavailable_until = 0.0
            return image_bytes, media_type, {
                "attempted": True,
                "status": "ok",
                "requested_model": self.requested_model,
                "transport_model": self.transport_model,
                "request_model_pinned": True,
                "cpa_trace_verified": cpa_trace_verified,
                "model_reported": model_reported,
                "resolved_model": self.resolved_model,
                "model_verified": model_verified,
                "verification_basis": verification_basis,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "width": width,
                "height": height,
                "size_bytes": len(image_bytes),
                "response_source": source,
                "input_contract": "static_2d_look_prompt_v2",
                "output_contract": "single_frame_static_image_v2",
            }
        except ProviderUnavailable as exc:
            self._record_failure(
                exc.reason_code,
                opens_circuit=exc.opens_circuit,
                request_model_pinned=request_started and request_model_pinned,
                cpa_trace_verified=cpa_trace_verified,
                model_reported=model_reported,
            )
            raise
        except Exception as exc:
            if isinstance(exc, httpx.TimeoutException):
                reason_code = "CPA_IMAGE_PROVIDER_TIMEOUT"
                opens_circuit = True
            elif isinstance(
                exc,
                (
                    AssetError,
                    binascii.Error,
                    json.JSONDecodeError,
                    KeyError,
                    IndexError,
                    TypeError,
                    ValueError,
                ),
            ):
                reason_code = "CPA_IMAGE_OUTPUT_REJECTED"
                opens_circuit = False
            else:
                reason_code = "CPA_IMAGE_PROVIDER_UNAVAILABLE"
                opens_circuit = True
            self._record_failure(
                reason_code,
                opens_circuit=opens_circuit,
                request_model_pinned=request_started and request_model_pinned,
                cpa_trace_verified=cpa_trace_verified,
                model_reported=model_reported,
            )
            raise ProviderUnavailable(
                type(exc).__name__,
                reason_code=reason_code,
                opens_circuit=opens_circuit,
            ) from exc
