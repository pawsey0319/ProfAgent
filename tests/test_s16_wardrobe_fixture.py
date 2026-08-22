from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from profagent.repository import FixtureRepository


DATA_VERSION = "fixtures_s16_womenswear_v1"
SOURCE_ID = "fixtures_s16"
ALLOWED_AUDIENCES = {"womenswear", "unisex_womenswear_compatible"}

BASE_FILE_SHA256 = {
    "data/fixtures/users.jsonl": "a47a1225037834c85b890fa06011e43ba69d485c9fcf419935fd027d5eaff466",
    "data/fixtures/garments.jsonl": "b9f6d85ace1dc31079b304d342c37a29ba9f6362d63ca3cd548e62f7226be001",
    "data/fixtures/outfits.jsonl": "cf2a439194d4bf209a3fa2289903271c95cb36424dcdb5b772d22e37550045a2",
    "data/fixtures/catalog.jsonl": "c56e47f700b38be3053aeb476d116c3ecfcac0c0784e6c2a6c92a3eae0ce33af",
    "data/eval/eval.jsonl": "a534e04346a60b837528248e38891d9e17a1024895f36aea9433e209409bff66",
}
BASE_SCHEMA_SHA256 = {
    "data/schemas/catalog.schema.json": "6bc49498b7121bc0645d7ad60f72b6309bd257152965f5151891eb9b230d8d06",
    "data/schemas/eval.schema.json": "9b617f016212ce002cf15da862ff76bdb5f069687fd6725f68b758bf61c8fd61",
    "data/schemas/garment.schema.json": "66b545a717b39f7636c28e84e26f14a80916a614a8d81793dca7174211e344b3",
    "data/schemas/outfit.schema.json": "2929fce70ed5684c521e7700cbb4dc9c303f466ac1ee12e24c79696661d8dbbb",
    "data/schemas/user.schema.json": "2af5482bab6320e6cec29f7897a9df93b2834359e9f16e4a85b7fc7e7c03db3a",
}
BASE_SCRIPT_SHA256 = {
    "scripts/generate.py": "9a9bd87047bc34fdea3c9ea05b63d9803e9d4ca1b3737a6ad11fd34ad7dfe71a",
    "scripts/validate.py": "c711f22e640a7d34c2c821d35038732bc747411c9a37d88b896c856376b3e0ee",
}

EXPECTED_OWNER_COUNTS = {"u01": 72, "u02": 24, "u03": 24}
EXPECTED_SLOT_COUNTS = {
    "top": 24,
    "bottom": 24,
    "dress": 16,
    "outer": 18,
    "shoes": 18,
    "bag": 10,
    "accessory": 10,
}
ADDED_OWNER_COUNTS = {"u01": 44, "u02": 12, "u03": 14}
ADDED_SLOT_COUNTS = {
    "top": 14,
    "bottom": 15,
    "dress": 11,
    "outer": 11,
    "shoes": 10,
    "bag": 4,
    "accessory": 5,
}
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

GENERATOR = Path("scripts/generate_s16_wardrobe.py")
EXTENSION = Path("data/fixtures/garments_s16_womenswear.jsonl")
EXTENSION_SCHEMA = Path("data/schemas/garment_s16.schema.json")
EXTENSION_MANIFEST = Path("data/manifests/fixtures_s16_womenswear_v1.json")
EXPECTED_GENERATED_ARTIFACTS = {
    EXTENSION.as_posix(),
    EXTENSION_SCHEMA.as_posix(),
    EXTENSION_MANIFEST.as_posix(),
}
EXTENSION_ROW_KEYS = {
    "schema_version",
    "data_version",
    "source_id",
    "synthetic",
    "garment_id",
    "user_id",
    "name",
    "slot",
    "color",
    "seasons",
    "styles",
    "occasions",
    "status",
    "formal",
    "warmth",
    "material",
    "fit",
    "search_text",
    "audience",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:  # pragma: no cover - contract message
                raise AssertionError(f"invalid JSONL at {path}:{line_number}") from exc
            assert isinstance(value, dict), f"row {line_number} must be an object"
            rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    )
    path.write_text(payload, encoding="utf-8", newline="\n")


def _extension_rows(project_root: Path) -> list[dict[str, object]]:
    path = project_root / EXTENSION
    assert path.is_file(), f"missing S16 extension fixture: {EXTENSION.as_posix()}"
    return _read_jsonl(path)


def _copy_file(project_root: Path, temp_root: Path, relative: str | Path) -> None:
    relative_path = Path(relative)
    destination = temp_root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project_root / relative_path, destination)


def _prepare_generation_root(project_root: Path, temp_root: Path) -> None:
    generator = project_root / GENERATOR
    assert generator.is_file(), f"missing S16 generator: {GENERATOR.as_posix()}"
    _copy_file(project_root, temp_root, GENERATOR)
    for relative in BASE_FILE_SHA256:
        _copy_file(project_root, temp_root, relative)
    for relative in BASE_SCHEMA_SHA256:
        _copy_file(project_root, temp_root, relative)
    for relative in BASE_SCRIPT_SHA256:
        _copy_file(project_root, temp_root, relative)
    _copy_file(project_root, temp_root, "data/manifests/fixtures_v1.0.json")


def _run_generator(project_root: Path, temp_root: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(project_root), existing_pythonpath) if value
    )
    return subprocess.run(
        [sys.executable, str(temp_root / GENERATOR)],
        cwd=temp_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _relative_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def _copy_repository_root(project_root: Path, temp_root: Path) -> Path:
    for directory in ("data/fixtures", "data/schemas", "data/manifests"):
        source = project_root / directory
        destination = temp_root / directory
        shutil.copytree(source, destination)
    return temp_root


def _repository_rows(repository: FixtureRepository) -> list[object]:
    return [
        garment
        for user_id in EXPECTED_OWNER_COUNTS
        for garment in repository.list_garments(user_id)
    ]


def test_fixtures_v1_bytes_schemas_and_baseline_scripts_are_frozen(
    project_root: Path,
) -> None:
    for relative, expected in {
        **BASE_FILE_SHA256,
        **BASE_SCHEMA_SHA256,
        **BASE_SCRIPT_SHA256,
    }.items():
        path = project_root / relative
        assert path.is_file(), f"missing immutable baseline file: {relative}"
        assert _sha256(path) == expected, f"immutable baseline bytes changed: {relative}"

    manifest = json.loads(
        (project_root / "data/manifests/fixtures_v1.0.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["data_version"] == "fixtures_v1.0"
    assert {
        item["path"]: item["sha256"] for item in manifest["files"].values()
    } == BASE_FILE_SHA256
    base_rows = _read_jsonl(project_root / "data/fixtures/garments.jsonl")
    assert len(base_rows) == 50
    assert [row["garment_id"] for row in base_rows] == [
        f"g{index:03d}" for index in range(1, 51)
    ]


def test_generator_is_repeatable_and_writes_only_extension_artifacts(
    project_root: Path, tmp_path: Path
) -> None:
    generation_root = tmp_path / "generation-root"
    _prepare_generation_root(project_root, generation_root)
    base_bytes = {
        relative: (generation_root / relative).read_bytes()
        for relative in BASE_FILE_SHA256
    }
    before_files = _relative_files(generation_root)
    before_bytes = {
        relative: (generation_root / relative).read_bytes()
        for relative in before_files
    }

    first_run = _run_generator(project_root, generation_root)
    assert first_run.returncode == 0, first_run.stdout + first_run.stderr
    first_files = _relative_files(generation_root)
    assert first_files - before_files == EXPECTED_GENERATED_ARTIFACTS
    assert not any(
        Path(relative).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        for relative in first_files - before_files
    ), "Task 1 generator must not create image assets"
    assert {
        relative: (generation_root / relative).read_bytes()
        for relative in before_files
    } == before_bytes, "generator modified a pre-existing baseline file"
    first_artifacts = {
        relative: (generation_root / relative).read_bytes()
        for relative in EXPECTED_GENERATED_ARTIFACTS
    }

    second_run = _run_generator(project_root, generation_root)
    assert second_run.returncode == 0, second_run.stdout + second_run.stderr
    assert _relative_files(generation_root) == first_files
    assert {
        relative: (generation_root / relative).read_bytes()
        for relative in EXPECTED_GENERATED_ARTIFACTS
    } == first_artifacts
    assert {
        relative: (generation_root / relative).read_bytes()
        for relative in BASE_FILE_SHA256
    } == base_bytes
    assert {
        relative: (generation_root / relative).read_bytes()
        for relative in before_files
    } == before_bytes

    for relative, generated_bytes in first_artifacts.items():
        assert (project_root / relative).read_bytes() == generated_bytes, (
            f"committed extension artifact differs from deterministic output: {relative}"
        )


def test_extension_rows_use_closed_metadata_and_exact_additions(
    project_root: Path,
) -> None:
    rows = _extension_rows(project_root)
    assert len(rows) == 70
    assert [row["garment_id"] for row in rows] == [
        f"g{index:03d}" for index in range(51, 121)
    ]
    assert Counter(row["user_id"] for row in rows) == Counter(ADDED_OWNER_COUNTS)
    assert Counter(row["slot"] for row in rows) == Counter(ADDED_SLOT_COUNTS)

    names: set[str] = set()
    for row in rows:
        assert set(row) == EXTENSION_ROW_KEYS
        assert row["schema_version"] == 1
        assert row["data_version"] == DATA_VERSION
        assert row["source_id"] == SOURCE_ID
        assert row["synthetic"] is True
        assert row["audience"] in ALLOWED_AUDIENCES
        assert {"gender", "user_gender", "sex"}.isdisjoint(row)
        name = row["name"]
        assert isinstance(name, str) and re.search(r"[\u4e00-\u9fff]", name)
        assert name not in names, f"extension garment name is not distinct: {name}"
        names.add(name)
        assert name in str(row["search_text"])

    occasions = {
        occasion for row in rows for occasion in row.get("occasions", [])
    }
    assert {
        "commute",
        "meeting",
        "interview",
        "date",
        "daily",
        "travel",
        "party",
    }.issubset(occasions)
    assert "sports" in occasions
    assert any("sporty" in row.get("styles", []) for row in rows)


def test_extension_schema_is_closed_and_rejects_out_of_contract_rows(
    project_root: Path,
) -> None:
    schema_path = project_root / EXTENSION_SCHEMA
    assert schema_path.is_file(), f"missing S16 extension schema: {EXTENSION_SCHEMA}"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert schema.get("additionalProperties") is False
    assert set(schema.get("required", [])) == EXTENSION_ROW_KEYS
    properties = schema.get("properties", {})
    assert properties["data_version"].get("const") == DATA_VERSION
    assert properties["source_id"].get("const") == SOURCE_ID
    assert properties["synthetic"].get("const") is True
    assert set(properties["audience"].get("enum", [])) == ALLOWED_AUDIENCES
    validator = Draft202012Validator(schema)
    rows = _extension_rows(project_root)
    assert all(not list(validator.iter_errors(row)) for row in rows)

    invalid_cases = []
    invalid_audience = dict(rows[0], audience="menswear")
    invalid_cases.append(invalid_audience)
    invalid_id = dict(rows[0], garment_id="g050")
    invalid_cases.append(invalid_id)
    invalid_high_id = dict(rows[0], garment_id="g121")
    invalid_cases.append(invalid_high_id)
    invalid_version = dict(rows[0], data_version="fixtures_v1.0")
    invalid_cases.append(invalid_version)
    invalid_source = dict(rows[0], source_id="fixtures")
    invalid_cases.append(invalid_source)
    invalid_synthetic = dict(rows[0], synthetic=False)
    invalid_cases.append(invalid_synthetic)
    extra_field = dict(rows[0], user_gender="female")
    invalid_cases.append(extra_field)
    missing_name = dict(rows[0])
    missing_name.pop("name")
    invalid_cases.append(missing_name)
    assert all(list(validator.iter_errors(row)) for row in invalid_cases)


def test_extension_manifest_binds_base_and_generated_bytes(project_root: Path) -> None:
    manifest_path = project_root / EXTENSION_MANIFEST
    assert manifest_path.is_file(), f"missing S16 extension manifest: {EXTENSION_MANIFEST}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    serialized = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    assert manifest.get("schema_version") == 1
    assert manifest.get("data_version") == DATA_VERSION
    assert manifest.get("source_id") == SOURCE_ID
    assert manifest.get("seed") == 20260729
    for relative, expected_hash in BASE_FILE_SHA256.items():
        assert relative in serialized
        assert expected_hash in serialized
    assert EXTENSION.as_posix() in serialized
    assert _sha256(project_root / EXTENSION) in serialized
    assert "70" in serialized


def test_repository_loads_exact_s16_distribution_and_preserves_base_records(
    project_root: Path,
) -> None:
    extension_rows = _extension_rows(project_root)
    repository = FixtureRepository(project_root)
    garments = _repository_rows(repository)
    assert repository.counts["garments"] == 120
    assert len(garments) == 120
    assert Counter(item.user_id for item in garments) == Counter(EXPECTED_OWNER_COUNTS)
    assert Counter(item.slot for item in garments) == Counter(EXPECTED_SLOT_COUNTS)
    assert {
        user_id: dict(Counter(item.slot for item in garments if item.user_id == user_id))
        for user_id in EXPECTED_OWNER_COUNTS
    } == EXPECTED_OWNER_SLOT_COUNTS
    assert {item.garment_id for item in garments if int(item.garment_id[1:]) > 50} == {
        row["garment_id"] for row in extension_rows
    }
    assert {item.audience for item in garments}.issubset(ALLOWED_AUDIENCES)

    for row in _read_jsonl(project_root / "data/fixtures/garments.jsonl"):
        loaded = repository.get_garment(str(row["garment_id"]))
        assert loaded is not None
        loaded_data = loaded.model_dump()
        assert {key: loaded_data[key] for key in row} == row


@pytest.mark.parametrize(
    "case",
    ["invalid_audience", "duplicate_id", "unknown_owner", "schema_error"],
)
def test_repository_rejects_entire_invalid_overlay_without_partial_exposure(
    project_root: Path, tmp_path: Path, case: str
) -> None:
    rows = _extension_rows(project_root)
    invalid_rows = [dict(row) for row in rows]
    if case == "invalid_audience":
        invalid_rows[0]["audience"] = "menswear"
    elif case == "duplicate_id":
        invalid_rows[0]["garment_id"] = "g001"
    elif case == "unknown_owner":
        invalid_rows[0]["user_id"] = "u99"
    else:
        invalid_rows[0].pop("name")

    repository_root = _copy_repository_root(project_root, tmp_path / case)
    _write_jsonl(repository_root / EXTENSION, invalid_rows)
    with pytest.raises((RuntimeError, ValueError)):
        FixtureRepository(repository_root)
