from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from .models import CatalogItem, Garment, OutfitFixture, User, WardrobePatch


class FixtureRepository:
    """Validated, in-memory view over the immutable fixture baseline."""

    def __init__(self, root_dir: Path):
        self.root_dir = root_dir
        fixture_dir = root_dir / "data" / "fixtures"
        self._users = {
            row["user_id"]: User.model_validate(row)
            for row in self._read_jsonl(fixture_dir / "users.jsonl")
        }
        self._garments = {
            row["garment_id"]: Garment.model_validate(row)
            for row in self._read_jsonl(fixture_dir / "garments.jsonl")
        }
        self._outfits = {
            row["outfit_id"]: OutfitFixture.model_validate(row)
            for row in self._read_jsonl(fixture_dir / "outfits.jsonl")
        }
        self._catalog = {
            row["item_id"]: CatalogItem.model_validate(row)
            for row in self._read_jsonl(fixture_dir / "catalog.jsonl")
        }
        self._lock = RLock()
        self._validate_references()

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
