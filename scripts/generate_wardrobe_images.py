from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, PngImagePlugin


REQUESTED_MODEL = "grok-imagine-image-quality"
ASPECT_RATIO = "1:1"
CONTRACT_VERSION = "wardrobe_catalog_single_garment_v1"
VERIFICATION_BASIS = "batch_exact_request_contract"
ASSET_VERSION = "wardrobe_generated_v1_s12r2"

COLOR_NAMES = {
    "beige": "beige",
    "black": "black",
    "blue": "medium blue",
    "brown": "brown",
    "gray": "neutral gray",
    "green": "muted green",
    "khaki": "khaki",
    "navy": "deep navy blue",
    "orange": "muted orange",
    "pink": "muted pink",
    "purple": "muted purple",
    "red": "muted red",
    "white": "clean white",
    "yellow": "muted yellow",
}

SLOT_NAMES = {
    "accessory": "fashion accessory",
    "bag": "bag",
    "bottom": "bottom garment",
    "dress": "dress",
    "outer": "outerwear garment",
    "shoes": "single matching pair of shoes",
    "top": "top garment",
}

GARMENT_DESCRIPTIONS = {
    "短款风衣": "a cropped double-breasted trench coat with lapels, storm flap and waist belt",
    "轻薄夹克": "a lightweight casual zip-front jacket",
    "针织外套": "a knitted button-front cardigan",
    "简洁大衣": "a minimalist full-length wool overcoat",
    "基础衬衫": "a classic collared button-up shirt",
    "圆领针织衫": "a crew-neck knitted sweater",
    "纯色短袖": "a plain short-sleeve crew-neck T-shirt",
    "利落长袖": "a smart long-sleeve top with full-length sleeves",
    "简约上衣": "a minimalist casual top",
    "轻便卫衣": "a lightweight long-sleeve crew-neck sweatshirt with ribbed cuffs",
    "直筒西裤": "full-length straight-leg tailored trousers with two clearly separated trouser legs",
    "宽松牛仔裤": "full-length loose-fit denim jeans with two clearly separated legs",
    "垂感长裤": "full-length draped straight-leg trousers with two clearly separated trouser legs",
    "休闲半裙": "a casual knee-to-midi-length skirt with one continuous skirt hem",
    "保暖长裤": "full-length warm trousers with two clearly separated trouser legs",
    "通勤衬衫裙": "an office-ready collared shirt dress",
    "简洁连衣裙": "a minimalist one-piece dress",
    "针织长裙": "a long one-piece knitted dress",
    "轻便运动鞋": "one matching pair of lightweight athletic sneakers",
    "简洁乐福鞋": "one matching pair of classic penny loafers without laces",
    "低跟单鞋": "one matching pair of closed-toe low-heeled pumps without laces",
    "保暖短靴": "one matching pair of warm ankle boots with visible ankle-height shafts",
    "通勤托特包": "one structured open-top commuting tote bag with two short top handles and no crossbody strap",
    "轻便斜挎包": "one compact crossbody bag with a single long shoulder strap",
    "小号手提包": "one small top-handle handbag",
    "素色围巾": "exactly one continuous long rectangular plain woven scarf laid out flat with both ends visible",
    "简约腰带": "exactly one single continuous long narrow waist belt laid flat with one buckle",
    "轻便帽子": "one lightweight bucket hat",
}

NAME_NEGATIVE_RULES = {
    "基础衬衫": "It must have a shirt collar and button front; not a T-shirt or blouse.",
    "利落长袖": "Both sleeves must reach the wrists; absolutely no short sleeves.",
    "轻便卫衣": "It must be a sweatshirt with long sleeves; not a T-shirt, blouse or dress.",
    "直筒西裤": "Absolutely no skirt, shorts or dress; show two full-length trouser legs.",
    "垂感长裤": "Absolutely no skirt, shorts or dress; show two full-length trouser legs.",
    "休闲半裙": "Absolutely no trousers, shorts or separated legs; this is one skirt.",
    "保暖长裤": "Absolutely no skirt or shorts; show two full-length trouser legs.",
    "简洁乐福鞋": "No laces, no sneaker sole and no Oxford/Derby construction.",
    "低跟单鞋": "No laces, no sneaker and no boot shaft; show a modest low heel.",
    "保暖短靴": "No pumps, Mary Janes or low shoes; both boots must cover the ankle.",
    "通勤托特包": "No flap satchel, backpack, briefcase or long crossbody strap.",
    "轻便斜挎包": "No jacket or clothing, no backpack and no two-handle tote.",
    "素色围巾": "This is one continuous scarf only; do not duplicate it or split it into two pieces; absolutely no hat, cap, bag or clothing.",
    "简约腰带": "This is exactly one belt and one buckle only; do not duplicate it; absolutely no bag, strap assembly or clothing.",
    "短款风衣": "It must visibly be a trench coat, not a shirt jacket or utility overshirt.",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _prompt(item: dict[str, Any]) -> str:
    color = COLOR_NAMES.get(str(item["color"]), str(item["color"]))
    slot = SLOT_NAMES.get(str(item["slot"]), str(item["slot"]))
    name = str(item["name"])
    exact_description = GARMENT_DESCRIPTIONS.get(name, f"a {name}")
    negative_rule = NAME_NEGATIVE_RULES.get(name, "")
    return (
        f"Create exactly this product and category: {exact_description}, in {color}. "
        f"It is a {slot}, named {name}. {negative_rule} "
        f"The garment is {item['material']} material with a {item['fit']} fit, "
        f"described exactly as: {item['search_text']}. "
        "Front-facing symmetrical e-commerce catalog product photography, "
        "garment or product only, full item visible and centered, accurate color "
        "and construction, realistic textile texture, seamless warm light-gray "
        "studio background with a soft natural shadow. No person, no body, no "
        "mannequin, no hanger, no packaging, no props, no additional clothing, "
        "no extra accessories, no logo, no brand, no text, no watermark. "
        "Square composition, consistent neutral catalog lighting."
    )


def _sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _validate_image(path: Path, garment_id: str) -> dict[str, Any]:
    body = path.read_bytes()
    if len(body) > 5 * 1024 * 1024:
        raise ValueError("image exceeds 5 MiB")
    with Image.open(path) as image:
        image.load()
        if bool(getattr(image, "is_animated", False)) or int(
            getattr(image, "n_frames", 1)
        ) != 1:
            raise ValueError("animated image rejected")
        width, height = image.size
        if width <= 0 or height <= 0 or width > 8192 or height > 8192:
            raise ValueError("image edge outside contract")
        if width * height > 25_000_000:
            raise ValueError("image pixel count outside contract")
        media_type = {
            "PNG": "image/png",
            "JPEG": "image/jpeg",
            "WEBP": "image/webp",
        }.get(str(image.format).upper())
        if media_type is None:
            raise ValueError("unsupported static image format")
        expected_metadata = {
            "AI-Generated": "true",
            "Requested-Model": REQUESTED_MODEL,
            "Model-Reported": "false",
            "Verification-Basis": VERIFICATION_BASIS,
            "Garment-ID": garment_id,
            "Generation-Contract": CONTRACT_VERSION,
        }
        if media_type != "image/png" or any(
            image.info.get(key) != value for key, value in expected_metadata.items()
        ):
            raise ValueError("embedded AI provenance metadata is missing or invalid")
    return {
        "bytes": len(body),
        "sha256": _sha256_bytes(body),
        "width": width,
        "height": height,
        "media_type": media_type,
    }


def _normalize_png_with_metadata(path: Path, garment_id: str) -> None:
    """Write honest, non-sensitive AI provenance into the delivered PNG."""
    with Image.open(path) as source:
        source.load()
        if bool(getattr(source, "is_animated", False)) or int(
            getattr(source, "n_frames", 1)
        ) != 1:
            raise ValueError("animated image rejected")
        rendered = source.convert("RGBA" if "A" in source.getbands() else "RGB")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("AI-Generated", "true")
    metadata.add_text("Requested-Model", REQUESTED_MODEL)
    metadata.add_text("Model-Reported", "false")
    metadata.add_text("Verification-Basis", VERIFICATION_BASIS)
    metadata.add_text("Garment-ID", garment_id)
    metadata.add_text("Generation-Contract", CONTRACT_VERSION)
    temporary = path.with_suffix(".normalized.png")
    rendered.save(temporary, format="PNG", pnginfo=metadata, optimize=True)
    temporary.replace(path)


def _write_manifest(path: Path, rows: list[dict[str, Any]]) -> None:
    payload = {
        "schema_version": 2,
        "asset_set": "wardrobe_generated_v1",
        "asset_version": ASSET_VERSION,
        "generation_contract": CONTRACT_VERSION,
        "requested_model": REQUESTED_MODEL,
        "request_model_pinned": True,
        "model_reported": False,
        "model_verified": False,
        "resolved_model": None,
        "verification_basis": VERIFICATION_BASIS,
        "aspect_ratio": ASPECT_RATIO,
        "ai_generated": True,
        "metadata_status": "embedded_png_and_sidecar_manifest",
        "display_label": "AI 生成目录参考",
        "disclaimer": "仅用于合成衣橱的2D目录与风格参考，不代表真实购买、尺码、面料触感或垂坠。",
        "items": sorted(rows, key=lambda row: row["garment_id"]),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate one traceable CPA Grok catalog image per fixture garment."
    )
    parser.add_argument("--source", default="data/fixtures/garments.jsonl")
    parser.add_argument("--output-dir", default="data/assets/wardrobe_generated_v1")
    parser.add_argument(
        "--manifest", default="data/manifests/wardrobe_generated_v1.json"
    )
    parser.add_argument("--only-id", action="append")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    source = (root / args.source).resolve()
    output_dir = (root / args.output_dir).resolve()
    manifest_path = (root / args.manifest).resolve()
    skill_script = (
        Path.home()
        / ".codex"
        / "skills"
        / "grok-image"
        / "scripts"
        / "generate-grok-image.ps1"
    )
    if not skill_script.is_file():
        raise SystemExit("grok-image generation script is unavailable")

    items = _read_jsonl(source)
    if args.only_id:
        selected_ids = set(args.only_id)
        items = [item for item in items if item["garment_id"] in selected_ids]
    if args.limit is not None:
        items = items[: max(0, args.limit)]

    previous: dict[str, dict[str, Any]] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            previous = {
                row["garment_id"]: row
                for row in manifest.get("items", [])
                if isinstance(row, dict) and isinstance(row.get("garment_id"), str)
            }
        except (OSError, ValueError, TypeError):
            previous = {}

    output_dir.mkdir(parents=True, exist_ok=True)
    results = dict(previous)
    for index, item in enumerate(items, 1):
        garment_id = str(item["garment_id"])
        destination = output_dir / f"{garment_id}.png"
        prompt = _prompt(item)
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        relative_path = destination.relative_to(root).as_posix()
        print(f"[{index}/{len(items)}] {garment_id}", flush=True)

        try:
            if destination.is_file() and not args.force:
                _normalize_png_with_metadata(destination, garment_id)
                image_meta = _validate_image(destination, garment_id)
                generation_status = "reused"
                previous_row = previous.get(garment_id, {})
                if (
                    previous_row.get("prompt_hash_status")
                    == "recorded_at_generation"
                    and isinstance(previous_row.get("prompt_sha256"), str)
                ):
                    recorded_prompt_hash = previous_row["prompt_sha256"]
                    prompt_hash_status = "recorded_at_generation"
                else:
                    # The current template is not evidence of the prompt that made
                    # an already-existing image. Never overwrite missing provenance
                    # with a newly computed value.
                    recorded_prompt_hash = None
                    prompt_hash_status = "not_preserved"
            else:
                command = [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(skill_script),
                    "-Prompt",
                    prompt,
                    "-OutputPath",
                    str(destination),
                    "-Model",
                    REQUESTED_MODEL,
                    "-AspectRatio",
                    ASPECT_RATIO,
                ]
                completed = subprocess.run(
                    command,
                    cwd=root,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                if completed.returncode != 0:
                    raise RuntimeError("CPA_IMAGE_GENERATION_FAILED")
                _normalize_png_with_metadata(destination, garment_id)
                image_meta = _validate_image(destination, garment_id)
                generation_status = "generated"
                recorded_prompt_hash = prompt_hash
                prompt_hash_status = "recorded_at_generation"
            previous_row = previous.get(garment_id, {})
            results[garment_id] = {
                "garment_id": garment_id,
                "user_id": item["user_id"],
                "source_data_version": item["data_version"],
                "status": "succeeded",
                "generation_status": generation_status,
                "requested_model": REQUESTED_MODEL,
                "request_model_pinned": True,
                "model_reported": False,
                "model_verified": False,
                "resolved_model": None,
                "verification_basis": VERIFICATION_BASIS,
                "aspect_ratio": ASPECT_RATIO,
                "prompt_sha256": recorded_prompt_hash,
                "prompt_hash_status": prompt_hash_status,
                "relative_path": relative_path,
                "ai_generated": True,
                "generated_at": previous_row.get("generated_at")
                or datetime.now(timezone.utc).isoformat(),
                "provenance_normalized_at": datetime.now(timezone.utc).isoformat(),
                **image_meta,
            }
        except Exception:
            results[garment_id] = {
                "garment_id": garment_id,
                "user_id": item["user_id"],
                "source_data_version": item["data_version"],
                "status": "failed",
                "reason_code": "CPA_WARDROBE_IMAGE_FAILED",
                "requested_model": REQUESTED_MODEL,
                "request_model_pinned": True,
                "model_reported": False,
                "model_verified": False,
                "resolved_model": None,
                "verification_basis": VERIFICATION_BASIS,
                "aspect_ratio": ASPECT_RATIO,
                "prompt_sha256": prompt_hash,
                "prompt_hash_status": "recorded_at_attempt",
                "relative_path": relative_path,
                "ai_generated": True,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        _write_manifest(manifest_path, list(results.values()))

    failed = [row for row in results.values() if row.get("status") != "succeeded"]
    print(
        json.dumps(
            {
                "total": len(results),
                "succeeded": len(results) - len(failed),
                "failed": len(failed),
                "manifest": str(manifest_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
