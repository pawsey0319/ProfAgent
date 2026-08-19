from __future__ import annotations

import hashlib
import json
import re
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, model_validator
from PIL import Image, UnidentifiedImageError

from .asset_service import AssetError, AssetService
from .config import CPA_IMAGE_MODEL, Settings
from .models import API_VERSION
from .repository import FixtureRepository


class WardrobeCatalogAssetNotFound(KeyError):
    pass


class CatalogAssetProviderStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "fallback"]
    attempted: bool
    requested_model: Literal["grok-imagine-image-quality"] = CPA_IMAGE_MODEL
    transport_model: Literal["grok-imagine-image-quality"] = CPA_IMAGE_MODEL
    request_model_pinned: bool
    provenance_verified: bool
    model_reported: bool
    resolved_model: Literal["grok-imagine-image-quality"] | None = None
    model_verified: bool
    verification_basis: Literal["batch_exact_request_contract"] | None = None
    degraded: bool
    reason_code: str | None = None

    @model_validator(mode="after")
    def verify_batch_evidence_contract(self) -> "CatalogAssetProviderStatus":
        if self.status == "ok":
            if (
                not self.attempted
                or not self.request_model_pinned
                or not self.provenance_verified
                or self.model_reported
                or self.model_verified
                or self.resolved_model is not None
                or self.verification_basis != "batch_exact_request_contract"
                or self.degraded
                or self.reason_code is not None
            ):
                raise ValueError("ready catalog provenance is inconsistent")
        elif (
            not self.degraded
            or self.reason_code is None
            or self.provenance_verified
            or self.model_verified
            or self.resolved_model is not None
            or self.verification_basis is not None
        ):
            raise ValueError("fallback catalog provenance is inconsistent")
        return self


class CatalogAssetAILabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated: Literal[True] = True
    display_label: Literal["AI生成目录参考"] = "AI生成目录参考"
    disclaimer: Literal[
        "仅供品类、风格与配色参考，不代表真实尺码、面料或垂坠"
    ] = "仅供品类、风格与配色参考，不代表真实尺码、面料或垂坠"


class WardrobeCatalogAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    garment_id: str
    status: Literal["ready", "missing", "failed"]
    asset_version: str
    image_url: str | None
    media_type: Literal["image/png"] | None
    provider: CatalogAssetProviderStatus
    prompt_sha256: str | None
    prompt_hash_status: Literal["recorded_at_generation", "not_preserved"] | None
    content_sha256: str | None
    ai_label: CatalogAssetAILabel = Field(default_factory=CatalogAssetAILabel)


class WardrobeCatalogAssetsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: Literal["r1_demo_v1"] = API_VERSION
    user_id: str
    manifest_version: str
    assets: list[WardrobeCatalogAsset]


class WardrobeCatalogAssetService:
    """Fail-closed, owner-bound view over a versioned generated-image manifest."""

    _ASSET_SET = "wardrobe_generated_v1"
    _ASSET_VERSION = "wardrobe_generated_v1_s12r2"
    _HASH = re.compile(r"^[0-9a-f]{64}$")
    _MEDIA_TYPES = {"image/png"}

    def __init__(self, settings: Settings, repository: FixtureRepository) -> None:
        self.settings = settings
        self.repository = repository
        self.root_dir = settings.root_dir.resolve()
        self.manifest_path = (
            settings.wardrobe_catalog_manifest
            or self.root_dir / "data" / "manifests" / "wardrobe_generated_v1.json"
        ).resolve()
        self.asset_root = (
            self.root_dir / "data" / "assets" / self._ASSET_SET
        ).resolve()

    @staticmethod
    def _fallback_provider(reason_code: str, *, attempted: bool) -> CatalogAssetProviderStatus:
        return CatalogAssetProviderStatus(
            status="fallback",
            attempted=attempted,
            request_model_pinned=False,
            provenance_verified=False,
            model_reported=False,
            resolved_model=None,
            model_verified=False,
            verification_basis=None,
            degraded=True,
            reason_code=reason_code,
        )

    @staticmethod
    def _ready_provider() -> CatalogAssetProviderStatus:
        return CatalogAssetProviderStatus(
            status="ok",
            attempted=True,
            request_model_pinned=True,
            provenance_verified=True,
            model_reported=False,
            resolved_model=None,
            model_verified=False,
            verification_basis="batch_exact_request_contract",
            degraded=False,
        )

    def _read_manifest(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            # A configured manifest must remain inside this repository. This
            # avoids turning the read API into an arbitrary local-file reader.
            self.manifest_path.relative_to(self.root_dir)
            if self.manifest_path.stat().st_size > 2 * 1024 * 1024:
                return None, "WARDROBE_ASSET_MANIFEST_REJECTED"
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None, "WARDROBE_ASSET_MANIFEST_UNAVAILABLE"
        if not isinstance(payload, dict):
            return None, "WARDROBE_ASSET_MANIFEST_REJECTED"
        if (
            payload.get("schema_version") != 2
            or payload.get("asset_set") != self._ASSET_SET
            or payload.get("asset_version") != self._ASSET_VERSION
            or payload.get("generation_contract")
            != "wardrobe_catalog_single_garment_v1"
            or payload.get("requested_model") != CPA_IMAGE_MODEL
            or payload.get("request_model_pinned") is not True
            or payload.get("model_reported") is not False
            or payload.get("model_verified") is not False
            or payload.get("resolved_model") is not None
            or payload.get("verification_basis")
            != "batch_exact_request_contract"
            or payload.get("aspect_ratio") != "1:1"
            or payload.get("ai_generated") is not True
            or payload.get("metadata_status")
            != "embedded_png_and_sidecar_manifest"
            or not isinstance(payload.get("items"), list)
        ):
            return None, "WARDROBE_ASSET_MANIFEST_REJECTED"
        return payload, None

    def _path_for(self, item: dict[str, Any], garment_id: str) -> Path | None:
        raw_path = item.get("relative_path")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        candidate = (self.root_dir / raw_path).resolve()
        try:
            candidate.relative_to(self.asset_root)
        except ValueError:
            return None
        if candidate.name != f"{garment_id}{candidate.suffix.lower()}":
            return None
        if candidate.suffix.lower() != ".png":
            return None
        return candidate

    def _validate_ready(
        self, item: dict[str, Any], garment_id: str, user_id: str
    ) -> tuple[bytes, str, str | None, str, str] | None:
        prompt_hash = item.get("prompt_sha256")
        prompt_hash_status = item.get("prompt_hash_status")
        content_hash = item.get("sha256")
        media_type = item.get("media_type")
        prompt_evidence_valid = (
            prompt_hash_status == "recorded_at_generation"
            and isinstance(prompt_hash, str)
            and self._HASH.fullmatch(prompt_hash) is not None
        ) or (
            prompt_hash_status == "not_preserved" and prompt_hash is None
        )
        if (
            item.get("garment_id") != garment_id
            or item.get("user_id") != user_id
            or item.get("source_data_version") != self.settings.data_version
            or item.get("status") != "succeeded"
            or item.get("requested_model") != CPA_IMAGE_MODEL
            or item.get("request_model_pinned") is not True
            or item.get("model_reported") is not False
            or item.get("model_verified") is not False
            or item.get("resolved_model") is not None
            or item.get("verification_basis")
            != "batch_exact_request_contract"
            or item.get("aspect_ratio") != "1:1"
            or item.get("ai_generated") is not True
            or not prompt_evidence_valid
            or not isinstance(content_hash, str)
            or not self._HASH.fullmatch(content_hash)
            or media_type not in self._MEDIA_TYPES
        ):
            return None
        path = self._path_for(item, garment_id)
        if path is None:
            return None
        try:
            body = path.read_bytes()
            validated_type, width, height = AssetService.validate_static_image(
                body, media_type
            )
            with Image.open(BytesIO(body)) as image:
                image.load()
                expected_provenance = {
                    "AI-Generated": "true",
                    "Requested-Model": CPA_IMAGE_MODEL,
                    "Model-Reported": "false",
                    "Verification-Basis": "batch_exact_request_contract",
                    "Garment-ID": garment_id,
                    "Generation-Contract": "wardrobe_catalog_single_garment_v1",
                }
                if any(
                    image.info.get(key) != value
                    for key, value in expected_provenance.items()
                ):
                    return None
        except (OSError, AssetError, UnidentifiedImageError, ValueError):
            return None
        if (
            validated_type != media_type
            or item.get("bytes") != len(body)
            or item.get("width") != width
            or item.get("height") != height
            or hashlib.sha256(body).hexdigest() != content_hash
        ):
            return None
        return body, media_type, prompt_hash, prompt_hash_status, content_hash

    def list_for_user(self, user_id: str) -> WardrobeCatalogAssetsResponse:
        if self.repository.get_user(user_id) is None:
            raise WardrobeCatalogAssetNotFound(user_id)
        manifest, manifest_reason = self._read_manifest()
        indexed: dict[str, dict[str, Any]] = {}
        duplicate_ids: set[str] = set()
        if manifest is not None:
            for raw in manifest["items"]:
                if not isinstance(raw, dict) or not isinstance(raw.get("garment_id"), str):
                    continue
                garment_id = raw["garment_id"]
                if garment_id in indexed:
                    duplicate_ids.add(garment_id)
                else:
                    indexed[garment_id] = raw

        assets: list[WardrobeCatalogAsset] = []
        for garment in self.repository.list_garments(user_id):
            item = indexed.get(garment.garment_id)
            if item is None:
                reason = manifest_reason or "WARDROBE_CATALOG_IMAGE_MISSING"
                assets.append(
                    WardrobeCatalogAsset(
                        garment_id=garment.garment_id,
                        status="missing",
                        asset_version=self._ASSET_VERSION,
                        image_url=None,
                        media_type=None,
                        provider=self._fallback_provider(reason, attempted=False),
                        prompt_sha256=None,
                        prompt_hash_status=None,
                        content_sha256=None,
                    )
                )
                continue
            ready = None if garment.garment_id in duplicate_ids else self._validate_ready(
                item, garment.garment_id, user_id
            )
            if ready is None:
                assets.append(
                    WardrobeCatalogAsset(
                        garment_id=garment.garment_id,
                        status="failed",
                        asset_version=self._ASSET_VERSION,
                        image_url=None,
                        media_type=None,
                        provider=self._fallback_provider(
                            "WARDROBE_CATALOG_IMAGE_REJECTED", attempted=True
                        ),
                        prompt_sha256=None,
                        prompt_hash_status=None,
                        content_sha256=None,
                    )
                )
                continue
            _body, media_type, prompt_hash, prompt_hash_status, content_hash = ready
            assets.append(
                WardrobeCatalogAsset(
                    garment_id=garment.garment_id,
                    status="ready",
                    asset_version=self._ASSET_VERSION,
                    image_url=(
                        f"/wardrobe/{garment.garment_id}/catalog-image?"
                        + urlencode(
                            {
                                "user_id": user_id,
                                "asset_version": self._ASSET_VERSION,
                                "content_sha256": content_hash,
                            }
                        )
                    ),
                    media_type=media_type,
                    provider=self._ready_provider(),
                    prompt_sha256=prompt_hash,
                    prompt_hash_status=prompt_hash_status,
                    content_sha256=content_hash,
                )
            )
        return WardrobeCatalogAssetsResponse(
            user_id=user_id,
            manifest_version=self._ASSET_VERSION,
            assets=assets,
        )

    def read_owned(
        self,
        garment_id: str,
        user_id: str,
        asset_version: str,
        content_sha256: str,
    ) -> tuple[bytes, str]:
        if (
            asset_version != self._ASSET_VERSION
            or self._HASH.fullmatch(content_sha256) is None
        ):
            raise WardrobeCatalogAssetNotFound(garment_id)
        if garment_id not in self.repository.garment_ids(user_id):
            raise WardrobeCatalogAssetNotFound(garment_id)
        response = self.list_for_user(user_id)
        asset = next(
            (candidate for candidate in response.assets if candidate.garment_id == garment_id),
            None,
        )
        if (
            asset is None
            or asset.status != "ready"
            or asset.media_type is None
            or asset.content_sha256 != content_sha256
        ):
            raise WardrobeCatalogAssetNotFound(garment_id)
        manifest, reason = self._read_manifest()
        if manifest is None or reason is not None:
            raise WardrobeCatalogAssetNotFound(garment_id)
        raw_items = [
            raw
            for raw in manifest["items"]
            if isinstance(raw, dict) and raw.get("garment_id") == garment_id
        ]
        if len(raw_items) != 1:
            raise WardrobeCatalogAssetNotFound(garment_id)
        ready = self._validate_ready(raw_items[0], garment_id, user_id)
        if ready is None:
            raise WardrobeCatalogAssetNotFound(garment_id)
        body, media_type, _prompt_hash, _prompt_status, verified_content_hash = ready
        if verified_content_hash != content_sha256:
            raise WardrobeCatalogAssetNotFound(garment_id)
        return body, media_type
