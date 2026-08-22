from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from .models import CatalogItem, Garment, OutfitFixture, User, WardrobePatch


S16_DATA_VERSION = "fixtures_s16_womenswear_v1"
S16_SOURCE_ID = "fixtures_s16"
S16_SEED = 20260729
S16_AUDIENCES = ["womenswear", "unisex_womenswear_compatible"]
S16_EXTENSION_RELATIVE = "data/fixtures/garments_s16_womenswear.jsonl"
S16_SCHEMA_RELATIVE = "data/schemas/garment_s16.schema.json"
S16_MANIFEST_RELATIVE = "data/manifests/fixtures_s16_womenswear_v1.json"
S16_BASE_HASHES = {
    "data/fixtures/users.jsonl": "a47a1225037834c85b890fa06011e43ba69d485c9fcf419935fd027d5eaff466",
    "data/fixtures/garments.jsonl": "b9f6d85ace1dc31079b304d342c37a29ba9f6362d63ca3cd548e62f7226be001",
    "data/fixtures/outfits.jsonl": "cf2a439194d4bf209a3fa2289903271c95cb36424dcdb5b772d22e37550045a2",
    "data/fixtures/catalog.jsonl": "c56e47f700b38be3053aeb476d116c3ecfcac0c0784e6c2a6c92a3eae0ce33af",
    "data/eval/eval.jsonl": "a534e04346a60b837528248e38891d9e17a1024895f36aea9433e209409bff66",
}
S16_ADDED_OWNER_COUNTS = {"u01": 44, "u02": 12, "u03": 14}
S16_ADDED_SLOT_COUNTS = {
    "top": 14,
    "bottom": 15,
    "dress": 11,
    "outer": 11,
    "shoes": 10,
    "bag": 4,
    "accessory": 5,
}
S16_ADDED_OWNER_SLOT_COUNTS = {
    "u01": {"top": 8, "bottom": 9, "dress": 7, "outer": 7, "shoes": 7, "bag": 3, "accessory": 3},
    "u02": {"top": 3, "bottom": 3, "dress": 2, "outer": 2, "shoes": 2, "bag": 0, "accessory": 0},
    "u03": {"top": 3, "bottom": 3, "dress": 2, "outer": 2, "shoes": 1, "bag": 1, "accessory": 2},
}
S16_FINAL_OWNER_COUNTS = {"u01": 72, "u02": 24, "u03": 24}
S16_FINAL_SLOT_COUNTS = {
    "top": 24,
    "bottom": 24,
    "dress": 16,
    "outer": 18,
    "shoes": 18,
    "bag": 10,
    "accessory": 10,
}
S16_FINAL_OWNER_SLOT_COUNTS = {
    "u01": {"top": 14, "bottom": 14, "dress": 10, "outer": 11, "shoes": 11, "bag": 6, "accessory": 6},
    "u02": {"top": 5, "bottom": 5, "dress": 3, "outer": 4, "shoes": 4, "bag": 2, "accessory": 1},
    "u03": {"top": 5, "bottom": 5, "dress": 3, "outer": 3, "shoes": 3, "bag": 2, "accessory": 3},
}


class FixtureRepository:
    """Validated, in-memory view over the immutable fixture baseline."""

    def __init__(self, root_dir: Path):
        self.root_dir = root_dir
        fixture_dir = root_dir / "data" / "fixtures"
        users = self._validated_unique_rows(
            self._read_jsonl(fixture_dir / "users.jsonl"),
            key="user_id",
            model=User,
        )
        base_garments = self._validated_unique_rows(
            self._read_jsonl(fixture_dir / "garments.jsonl"),
            key="garment_id",
            model=Garment,
        )
        overlay_garments = self._load_s16_garment_overlay(
            base_garments=base_garments,
            user_ids=set(users),
        )
        if overlay_garments:
            self._validate_s16_combined_contract(base_garments, overlay_garments)
        outfits = self._validated_unique_rows(
            self._read_jsonl(fixture_dir / "outfits.jsonl"),
            key="outfit_id",
            model=OutfitFixture,
        )
        catalog = self._validated_unique_rows(
            self._read_jsonl(fixture_dir / "catalog.jsonl"),
            key="item_id",
            model=CatalogItem,
        )

        # Publish the fully validated snapshot only after the overlay passed as
        # one unit. A malformed extension can never expose a partial wardrobe.
        self._users = users
        self._garments = {**base_garments, **overlay_garments}
        self._outfits = outfits
        self._catalog = catalog
        self._lock = RLock()
        self._validate_references()

    @staticmethod
    def _validated_unique_rows(
        rows: Iterable[dict[str, Any]],
        *,
        key: str,
        model: type[User] | type[Garment] | type[OutfitFixture] | type[CatalogItem],
    ) -> dict[str, Any]:
        validated: dict[str, Any] = {}
        for raw in rows:
            value = model.model_validate(raw)
            identifier = getattr(value, key)
            if identifier in validated:
                raise RuntimeError(f"duplicate fixture identifier: {identifier}")
            validated[identifier] = value
        return validated

    def _load_s16_garment_overlay(
        self,
        *,
        base_garments: dict[str, Garment],
        user_ids: set[str],
    ) -> dict[str, Garment]:
        fixture_path = (
            self.root_dir
            / "data"
            / "fixtures"
            / "garments_s16_womenswear.jsonl"
        )
        if not fixture_path.exists():
            return {}
        schema_path = (
            self.root_dir / "data" / "schemas" / "garment_s16.schema.json"
        )
        if not schema_path.is_file():
            raise RuntimeError("S16 garment overlay schema is missing")
        manifest_path = self.root_dir / S16_MANIFEST_RELATIVE
        if not manifest_path.is_file():
            raise RuntimeError("S16 garment overlay manifest is missing")
        self._validate_s16_manifest(
            manifest_path=manifest_path,
            fixture_path=fixture_path,
            schema_path=schema_path,
        )
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("S16 garment overlay schema is invalid") from exc
        validator = Draft202012Validator(schema)
        rows = list(self._read_jsonl(fixture_path))
        overlay: dict[str, Garment] = {}
        for line_number, raw in enumerate(rows, 1):
            errors = sorted(validator.iter_errors(raw), key=lambda item: list(item.path))
            if errors:
                raise RuntimeError(
                    f"invalid S16 garment overlay row: {line_number}"
                )
            garment = Garment.model_validate(raw)
            if garment.garment_id in base_garments or garment.garment_id in overlay:
                raise RuntimeError(
                    f"duplicate fixture identifier: {garment.garment_id}"
                )
            if garment.user_id not in user_ids:
                raise RuntimeError(f"unknown fixture user: {garment.user_id}")
            overlay[garment.garment_id] = garment
        self._validate_s16_overlay_contract(overlay)
        return overlay

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _closed_json_object(path: Path) -> dict[str, Any]:
        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise RuntimeError("S16 manifest contains duplicate keys")
                value[key] = item
            return value

        try:
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=reject_duplicates,
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("S16 garment overlay manifest is invalid") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("S16 garment overlay manifest must be an object")
        return payload

    @staticmethod
    def _require_exact_keys(
        value: Any,
        expected: set[str],
        label: str,
    ) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != expected:
            raise RuntimeError(f"S16 manifest {label} fields drifted")
        return value

    def _validate_s16_manifest(
        self,
        *,
        manifest_path: Path,
        fixture_path: Path,
        schema_path: Path,
    ) -> None:
        manifest = self._closed_json_object(manifest_path)
        self._require_exact_keys(
            manifest,
            {
                "schema_version",
                "data_version",
                "source_id",
                "seed",
                "base",
                "files",
                "counts",
                "audiences",
            },
            "root",
        )
        if (
            manifest["schema_version"] != 1
            or manifest["data_version"] != S16_DATA_VERSION
            or manifest["source_id"] != S16_SOURCE_ID
            or manifest["seed"] != S16_SEED
            or manifest["audiences"] != S16_AUDIENCES
        ):
            raise RuntimeError("S16 manifest identity drifted")

        expected_counts = {
            "added_owners": S16_ADDED_OWNER_COUNTS,
            "added_slots": S16_ADDED_SLOT_COUNTS,
            "added_owner_slots": S16_ADDED_OWNER_SLOT_COUNTS,
            "final_owners": S16_FINAL_OWNER_COUNTS,
            "final_slots": S16_FINAL_SLOT_COUNTS,
            "final_owner_slots": S16_FINAL_OWNER_SLOT_COUNTS,
            "total_added": 70,
            "total_garments": 120,
        }
        if manifest["counts"] != expected_counts:
            raise RuntimeError("S16 manifest count receipts drifted")

        base = self._require_exact_keys(
            manifest["base"], {"data_version", "files"}, "base"
        )
        if base["data_version"] != "fixtures_v1.0":
            raise RuntimeError("S16 manifest base version drifted")
        base_files = self._require_exact_keys(
            base["files"], set(S16_BASE_HASHES), "base files"
        )
        for relative, expected_hash in S16_BASE_HASHES.items():
            receipt = self._require_exact_keys(
                base_files[relative], {"path", "sha256"}, f"base receipt {relative}"
            )
            path = self.root_dir / relative
            if (
                receipt != {"path": relative, "sha256": expected_hash}
                or not path.is_file()
                or self._sha256(path) != expected_hash
            ):
                raise RuntimeError(f"S16 manifest base receipt drifted: {relative}")

        files = self._require_exact_keys(
            manifest["files"], {"garments_extension", "schema"}, "files"
        )
        garment_receipt = self._require_exact_keys(
            files["garments_extension"],
            {"path", "count", "sha256"},
            "garment receipt",
        )
        schema_receipt = self._require_exact_keys(
            files["schema"], {"path", "sha256"}, "schema receipt"
        )
        if garment_receipt != {
            "path": S16_EXTENSION_RELATIVE,
            "count": 70,
            "sha256": self._sha256(fixture_path),
        }:
            raise RuntimeError("S16 garment receipt drifted")
        if schema_receipt != {
            "path": S16_SCHEMA_RELATIVE,
            "sha256": self._sha256(schema_path),
        }:
            raise RuntimeError("S16 schema receipt drifted")

    @staticmethod
    def _owner_slot_counts(garments: Iterable[Garment]) -> dict[str, dict[str, int]]:
        rows = list(garments)
        return {
            user_id: {
                slot: sum(
                    item.user_id == user_id and item.slot == slot for item in rows
                )
                for slot in S16_ADDED_SLOT_COUNTS
            }
            for user_id in S16_ADDED_OWNER_COUNTS
        }

    @classmethod
    def _validate_s16_overlay_contract(cls, overlay: dict[str, Garment]) -> None:
        garments = list(overlay.values())
        if list(overlay) != [f"g{index:03d}" for index in range(51, 121)]:
            raise RuntimeError("S16 garment overlay ID sequence drifted")
        if Counter(item.user_id for item in garments) != Counter(
            S16_ADDED_OWNER_COUNTS
        ):
            raise RuntimeError("S16 garment overlay owner counts drifted")
        if Counter(item.slot for item in garments) != Counter(S16_ADDED_SLOT_COUNTS):
            raise RuntimeError("S16 garment overlay slot counts drifted")
        if cls._owner_slot_counts(garments) != S16_ADDED_OWNER_SLOT_COUNTS:
            raise RuntimeError("S16 garment overlay owner-slot counts drifted")

    @classmethod
    def _validate_s16_combined_contract(
        cls,
        base: dict[str, Garment],
        overlay: dict[str, Garment],
    ) -> None:
        garments = [*base.values(), *overlay.values()]
        if len(garments) != 120:
            raise RuntimeError("S16 combined garment count drifted")
        if Counter(item.user_id for item in garments) != Counter(
            S16_FINAL_OWNER_COUNTS
        ):
            raise RuntimeError("S16 combined owner counts drifted")
        if Counter(item.slot for item in garments) != Counter(S16_FINAL_SLOT_COUNTS):
            raise RuntimeError("S16 combined slot counts drifted")
        if cls._owner_slot_counts(garments) != S16_FINAL_OWNER_SLOT_COUNTS:
            raise RuntimeError("S16 combined owner-slot counts drifted")

    @staticmethod
    def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if line.strip():
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(f"invalid JSONL: {path}:{line_number}") from exc

    def _validate_references(self) -> None:
        for garment in self._garments.values():
            if garment.user_id not in self._users:
                raise RuntimeError(f"unknown fixture user: {garment.user_id}")
        for outfit in self._outfits.values():
            if outfit.user_id not in self._users:
                raise RuntimeError(f"unknown fixture user: {outfit.user_id}")
            allowed = self.garment_ids(outfit.user_id)
            if not set(outfit.items).issubset(allowed):
                raise RuntimeError(f"ungrounded fixture outfit: {outfit.outfit_id}")

    @property
    def counts(self) -> dict[str, int]:
        return {
            "users": len(self._users),
            "garments": len(self._garments),
            "outfits": len(self._outfits),
            "catalog": len(self._catalog),
        }

    def get_user(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    def list_users(self) -> list[User]:
        return list(self._users.values())

    def garment_ids(self, user_id: str) -> set[str]:
        return {
            item.garment_id
            for item in self._garments.values()
            if item.user_id == user_id
        }

    def list_garments(
        self,
        user_id: str,
        *,
        slot: str | None = None,
        status: str | None = None,
        season: str | None = None,
        color: str | None = None,
        occasion: str | None = None,
    ) -> list[Garment]:
        items = [item for item in self._garments.values() if item.user_id == user_id]
        if slot:
            items = [item for item in items if item.slot == slot]
        if status:
            items = [item for item in items if item.status == status]
        if season:
            items = [
                item for item in items if season in item.seasons or "all" in item.seasons
            ]
        if color:
            items = [item for item in items if item.color == color]
        if occasion:
            items = [item for item in items if occasion in item.occasions]
        return sorted(items, key=lambda item: item.garment_id)

    def get_garment(self, garment_id: str) -> Garment | None:
        return self._garments.get(garment_id)

    def update_garment(
        self, garment_id: str, user_id: str, patch: WardrobePatch
    ) -> Garment | None:
        with self._lock:
            current = self._garments.get(garment_id)
            if current is None or current.user_id != user_id:
                return None
            updated = current.model_copy(update=patch.model_dump(exclude_none=True))
            # Revalidate after model_copy because Pydantic does not validate updates.
            updated = Garment.model_validate(updated.model_dump())
            self._garments[garment_id] = updated
            return updated

    def list_outfits(self, user_id: str) -> list[OutfitFixture]:
        return sorted(
            [item for item in self._outfits.values() if item.user_id == user_id],
            key=lambda item: item.outfit_id,
        )

    def list_catalog(self) -> list[CatalogItem]:
        return sorted(self._catalog.values(), key=lambda item: item.item_id)
