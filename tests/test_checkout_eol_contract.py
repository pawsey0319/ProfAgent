from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
S16_MANIFEST = ROOT / "data" / "manifests" / "fixtures_s16_womenswear_v1.json"
HASH_BOUND_TEXT_INPUTS = (
    "data/eval/eval.jsonl",
    "data/fixtures/catalog.jsonl",
    "data/fixtures/garments.jsonl",
    "data/fixtures/garments_s16_womenswear.jsonl",
    "data/fixtures/outfits.jsonl",
    "data/fixtures/users.jsonl",
    "data/schemas/garment_s16.schema.json",
)
DETERMINISTIC_LF_OUTPUTS = (
    "data/manifests/fixtures_s16_womenswear_v1.json",
)
LF_CHECKOUT_CONTRACT_PATHS = HASH_BOUND_TEXT_INPUTS + DETERMINISTIC_LF_OUTPUTS


def _check_attr(path: str) -> dict[str, str]:
    result = subprocess.run(
        ["git", "check-attr", "text", "eol", "--", path],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    attributes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        _, name, value = line.split(": ", 2)
        attributes[name] = value
    return attributes


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_every_byte_bound_input_and_deterministic_output_forces_lf_checkout() -> None:
    manifest = json.loads(S16_MANIFEST.read_text(encoding="utf-8"))
    manifest_paths = {
        receipt["path"] for receipt in manifest["base"]["files"].values()
    } | {receipt["path"] for receipt in manifest["files"].values()}

    assert manifest_paths == set(HASH_BOUND_TEXT_INPUTS)

    missing_lf_contract = {
        path: _check_attr(path)
        for path in LF_CHECKOUT_CONTRACT_PATHS
        if _check_attr(path) != {"text": "set", "eol": "lf"}
    }
    assert missing_lf_contract == {}, (
        "byte-hash-bound inputs and deterministic outputs must force "
        "`text eol=lf` so a clean "
        f"Windows checkout cannot change their SHA-256: {missing_lf_contract}"
    )


def test_s16_schema_hash_survives_windows_autocrlf_checkout() -> None:
    path = "data/schemas/garment_s16.schema.json"
    manifest = json.loads(S16_MANIFEST.read_text(encoding="utf-8"))
    expected = manifest["files"]["schema"]["sha256"]
    blob = subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout

    assert b"\r\n" not in blob
    assert _sha256(blob) == expected

    attributes = _check_attr(path)
    simulated_windows_checkout = (
        blob if attributes == {"text": "set", "eol": "lf"} else blob.replace(b"\n", b"\r\n")
    )
    assert _sha256(simulated_windows_checkout) == expected, (
        "Windows core.autocrlf checkout changes the S16 schema hash unless "
        f"`.gitattributes` forces LF; attributes={attributes}, "
        f"checkout_sha256={_sha256(simulated_windows_checkout)}, expected={expected}"
    )
