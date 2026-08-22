from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator

from generate_s16_wardrobe import (
    BASE_HASHES,
    DATA_VERSION,
    EXPECTED_ADDED_OWNER_COUNTS,
    EXPECTED_ADDED_SLOT_COUNTS,
    EXPECTED_FINAL_OWNER_COUNTS,
    EXPECTED_FINAL_SLOT_COUNTS,
    EXTENSION_PATH,
    MANIFEST_PATH,
    ROOT,
    SCHEMA_PATH,
    SOURCE_ID,
    _assert_contract,
    _build_rows,
    _manifest,
    _schema,
    _sha256,
)


EXPECTED_OWNER_SLOT_COUNTS = {
    "u01": {
        "top": 14,
        "bottom": 14,
        "dress": 10,
        "outer": 11,
        "shoes": 11,
        "bag": 6,
        "accessory": 6,
    },
    "u02": {
        "top": 5,
        "bottom": 5,
        "dress": 3,
        "outer": 4,
        "shoes": 4,
        "bag": 2,
        "accessory": 1,
    },
    "u03": {
        "top": 5,
        "bottom": 5,
        "dress": 3,
        "outer": 3,
        "shoes": 3,
        "bag": 2,
        "accessory": 3,
    },
}


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid JSONL: {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise RuntimeError(f"JSONL row must be an object: {path}:{line_number}")
            rows.append(value)
    return rows


def _assert_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise RuntimeError(f"{label} mismatch: expected={expected!r} actual={actual!r}")


def main() -> None:
    for relative, expected in BASE_HASHES.items():
        path = ROOT / relative
        if not path.is_file() or _sha256(path) != expected:
            raise RuntimeError(f"immutable baseline mismatch: {relative}")

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    _assert_equal(schema, _schema(), "extension schema contract")
    validator = Draft202012Validator(schema)
    extension = _read_jsonl(EXTENSION_PATH)
    for line_number, row in enumerate(extension, 1):
        errors = list(validator.iter_errors(row))
        if errors:
            raise RuntimeError(f"invalid S16 garment row: {line_number}")
    _assert_contract(extension)
    _assert_equal(extension, _build_rows(), "declarative garment profiles")
    _assert_equal(len(extension), 70, "extension garment count")
    _assert_equal(
        [row["garment_id"] for row in extension],
        [f"g{index:03d}" for index in range(51, 121)],
        "extension garment IDs",
    )
    _assert_equal(
        dict(Counter(row["user_id"] for row in extension)),
        EXPECTED_ADDED_OWNER_COUNTS,
        "extension owner counts",
    )
    _assert_equal(
        dict(Counter(row["slot"] for row in extension)),
        EXPECTED_ADDED_SLOT_COUNTS,
        "extension slot counts",
    )

    base = _read_jsonl(ROOT / "data" / "fixtures" / "garments.jsonl")
    combined = [*base, *extension]
    identifiers = [row["garment_id"] for row in combined]
    _assert_equal(len(identifiers), len(set(identifiers)), "combined garment IDs")
    _assert_equal(len(combined), 120, "combined garment count")
    _assert_equal(
        dict(Counter(row["user_id"] for row in combined)),
        EXPECTED_FINAL_OWNER_COUNTS,
        "final owner counts",
    )
    _assert_equal(
        dict(Counter(row["slot"] for row in combined)),
        EXPECTED_FINAL_SLOT_COUNTS,
        "final slot counts",
    )
    owner_slot_counts = {
        user_id: dict(
            Counter(row["slot"] for row in combined if row["user_id"] == user_id)
        )
        for user_id in EXPECTED_FINAL_OWNER_COUNTS
    }
    _assert_equal(
        owner_slot_counts,
        EXPECTED_OWNER_SLOT_COUNTS,
        "final owner-slot matrix",
    )

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    _assert_equal(manifest, _manifest(extension), "closed manifest contract")
    _assert_equal(manifest.get("schema_version"), 1, "manifest schema version")
    _assert_equal(manifest.get("data_version"), DATA_VERSION, "manifest data version")
    _assert_equal(manifest.get("source_id"), SOURCE_ID, "manifest source")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise RuntimeError("manifest files contract is invalid")
    garments_receipt = files.get("garments_extension")
    schema_receipt = files.get("schema")
    if not isinstance(garments_receipt, dict) or not isinstance(schema_receipt, dict):
        raise RuntimeError("manifest artifact receipts are invalid")
    _assert_equal(garments_receipt.get("count"), 70, "manifest extension count")
    _assert_equal(
        garments_receipt.get("sha256"), _sha256(EXTENSION_PATH), "manifest garment hash"
    )
    _assert_equal(
        schema_receipt.get("sha256"), _sha256(SCHEMA_PATH), "manifest schema hash"
    )
    manifest_base = manifest.get("base")
    if not isinstance(manifest_base, dict) or not isinstance(
        manifest_base.get("files"), dict
    ):
        raise RuntimeError("manifest baseline binding is invalid")
    for relative, expected in BASE_HASHES.items():
        receipt = manifest_base["files"].get(relative)
        if not isinstance(receipt, dict):
            raise RuntimeError(f"manifest baseline receipt missing: {relative}")
        _assert_equal(receipt.get("sha256"), expected, f"manifest baseline {relative}")

    print(
        "S16 wardrobe validation passed: "
        "garments=120 owners=u01:72,u02:24,u03:24 "
        "slots=top:24,bottom:24,dress:16,outer:18,shoes:18,bag:10,accessory:10"
    )


if __name__ == "__main__":
    main()
