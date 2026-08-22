from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from .models import CatalogItem, Garment, OutfitFixture, User, WardrobePatch


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
            base_ids=set(base_garments),
            user_ids=set(users),
        )
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
        base_ids: set[str],
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
            if garment.garment_id in base_ids or garment.garment_id in overlay:
                raise RuntimeError(
                    f"duplicate fixture identifier: {garment.garment_id}"
                )
            if garment.user_id not in user_ids:
                raise RuntimeError(f"unknown fixture user: {garment.user_id}")
            overlay[garment.garment_id] = garment
        return overlay

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
