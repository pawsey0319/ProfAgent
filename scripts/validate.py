from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Iterable

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
DATA_PATHS = {
    "users": ROOT / "data" / "fixtures" / "users.jsonl",
    "garments": ROOT / "data" / "fixtures" / "garments.jsonl",
    "outfits": ROOT / "data" / "fixtures" / "outfits.jsonl",
    "catalog": ROOT / "data" / "fixtures" / "catalog.jsonl",
    "eval": ROOT / "data" / "eval" / "eval.jsonl",
}
SCHEMA_PATHS = {
    "users": ROOT / "data" / "schemas" / "user.schema.json",
    "garments": ROOT / "data" / "schemas" / "garment.schema.json",
    "outfits": ROOT / "data" / "schemas" / "outfit.schema.json",
    "catalog": ROOT / "data" / "schemas" / "catalog.schema.json",
    "eval": ROOT / "data" / "schemas" / "eval.schema.json",
}
EXPECTED_COUNTS = {"users": 3, "garments": 50, "outfits": 20, "catalog": 50, "eval": 30}
EXPECTED_BUCKETS = {
    "urgent_no_shop": 8,
    "conditional_buy": 5,
    "weather_season_formality": 5,
    "taboo_stock_availability": 5,
    "grounding": 4,
    "support_safety": 3,
}
EXPECTED_VOCAB = {
    "persona": ["stylist"],
    "urgency": ["high", "medium", "low"],
    "horizon": ["now", "today", "soon", "planned", "unknown"],
    "budget": ["low", "mid", "high"],
    "slot": ["outer", "top", "bottom", "dress", "shoes", "bag", "accessory"],
    "season": ["spring", "summer", "autumn", "winter", "all"],
    "status": ["available", "laundry", "reserved", "unavailable"],
    "style": ["simple", "classic", "smart", "formal", "street", "sporty", "soft", "vintage", "business", "campus"],
    "occasion": ["daily", "commute", "interview", "meeting", "date", "party", "travel", "outdoor", "home", "sports"],
    "goal": ["reliable", "modern", "comfortable", "polished", "low_key", "confident", "cool"],
    "intent": ["recommend", "buy", "fill_gap", "browse", "vent"],
    "color": ["black", "white", "gray", "navy", "beige", "brown", "khaki", "red", "pink", "orange", "yellow", "green", "blue", "purple", "multi"],
    "material": ["cotton", "knit", "denim", "wool", "linen", "leather", "synthetic"],
    "fit": ["slim", "regular", "loose", "straight"],
}
HORIZON_URGENCY = {
    "now": "high",
    "today": "high",
    "soon": "medium",
    "planned": "low",
    "unknown": "high",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise AssertionError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return rows


def load_data() -> dict[str, list[dict]]:
    return {name: load_jsonl(path) for name, path in DATA_PATHS.items()}


def validate_vocab() -> None:
    with (ROOT / "data" / "vocab.yaml").open(encoding="utf-8") as handle:
        vocab = yaml.safe_load(handle)
    require(vocab["schema_version"] == 1, "vocab schema_version must be 1")
    require(vocab["data_version"] == "fixtures_v1.0", "vocab data_version mismatch")
    for name, expected in EXPECTED_VOCAB.items():
        require(vocab.get(name) == expected, f"vocab {name} must use the requested simple labels")

    with (ROOT / "data" / "sources.yaml").open(encoding="utf-8") as handle:
        sources = yaml.safe_load(handle)
    require(sources["schema_version"] == 1, "sources schema_version must be 1")
    require(sources["data_version"] == "fixtures_v1.0", "sources data_version mismatch")
    require(set(sources["sources"]) == {"fixtures", "catalog_mock", "eval_set"}, "unexpected source IDs")
    restrictions = "\n".join(sources.get("restrictions", []))
    for phrase in ["真实身份", "真实品牌", "模拟商品", "合成反馈"]:
        require(phrase in restrictions, f"sources restrictions missing policy: {phrase}")


def validate_schemas(data: dict[str, list[dict]]) -> None:
    for name, schema_path in SCHEMA_PATHS.items():
        with schema_path.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        require(schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema", f"{name} schema draft mismatch")
        require(schema.get("additionalProperties") is False, f"{name} schema must reject additional properties")
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        for index, row in enumerate(data[name], 1):
            errors = sorted(validator.iter_errors(row), key=lambda error: list(error.path))
            if errors:
                details = "; ".join(
                    f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}" for error in errors
                )
                raise AssertionError(f"{name} row {index} schema errors: {details}")


def validate_counts(data: dict[str, list[dict]]) -> None:
    actual = {name: len(rows) for name, rows in data.items()}
    require(actual == EXPECTED_COUNTS, f"record counts mismatch: {actual}")
    owner_counts = Counter(row["user_id"] for row in data["garments"])
    require(owner_counts == Counter({"u01": 28, "u02": 12, "u03": 10}), f"garment allocation mismatch: {owner_counts}")
    status_counts = Counter(row["status"] for row in data["garments"])
    require(status_counts["available"] >= 40, "at least 40 garments must be available")
    for status in ["laundry", "reserved", "unavailable"]:
        require(status_counts[status] > 0, f"missing negative garment status: {status}")
    require({row["slot"] for row in data["garments"]} == set(EXPECTED_VOCAB["slot"]), "garments do not cover all slots")
    garment_seasons = {season for row in data["garments"] for season in row["seasons"]}
    require(garment_seasons == set(EXPECTED_VOCAB["season"]), "garments do not cover all seasons")
    require({row["formal"] for row in data["garments"]} == set(range(5)), "garments do not cover formal 0-4")
    require({row["warmth"] for row in data["garments"]} == set(range(1, 6)), "garments do not cover warmth 1-5")
    outfit_counts = Counter(row["user_id"] for row in data["outfits"])
    require(outfit_counts == Counter({"u01": 10, "u02": 5, "u03": 5}), f"outfit allocation mismatch: {outfit_counts}")


def unique_index(rows: Iterable[dict], key: str, label: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in rows:
        value = row[key]
        require(value not in result, f"duplicate {label}: {value}")
        result[value] = row
    return result


def validate_references(data: dict[str, list[dict]]) -> None:
    users = unique_index(data["users"], "user_id", "user ID")
    garments = unique_index(data["garments"], "garment_id", "garment ID")
    outfits = unique_index(data["outfits"], "outfit_id", "outfit ID")
    catalog = unique_index(data["catalog"], "item_id", "catalog item ID")
    unique_index(data["eval"], "case_id", "eval case ID")

    for garment in data["garments"]:
        require(garment["user_id"] in users, f"garment has missing user: {garment['garment_id']}")
    for outfit in data["outfits"]:
        require(outfit["user_id"] in users, f"outfit has missing user: {outfit['outfit_id']}")
        for garment_id in outfit["items"]:
            require(garment_id in garments, f"outfit references missing garment: {garment_id}")
            require(garments[garment_id]["user_id"] == outfit["user_id"], f"outfit mixes users: {outfit['outfit_id']}")

    valid_ids = set(users) | set(garments) | set(outfits) | set(catalog)
    for case in data["eval"]:
        case_id = case["case_id"]
        user_id = case["user_id"]
        require(user_id in users, f"eval references missing user: {case_id}")
        for field in ["allowed_garments", "forbidden_garments"]:
            for garment_id in case[field]:
                require(garment_id in garments, f"{case_id} {field} has missing ID: {garment_id}")
                require(garments[garment_id]["user_id"] == user_id, f"{case_id} {field} crosses users")
        for field in ["allowed_catalog", "forbidden_catalog"]:
            for item_id in case[field]:
                require(item_id in catalog, f"{case_id} {field} has missing ID: {item_id}")
        for outfit_id in case["reference_outfits"]:
            require(outfit_id in outfits, f"{case_id} reference_outfits has missing ID: {outfit_id}")
            require(outfits[outfit_id]["user_id"] == user_id, f"{case_id} reference_outfits crosses users")
        for bad_id in case["bad_ids"]:
            require(bad_id not in valid_ids, f"{case_id} bad_ids unexpectedly contains a real ID: {bad_id}")


def validate_outfit_rules(data: dict[str, list[dict]]) -> None:
    users = {row["user_id"]: row for row in data["users"]}
    garments = {row["garment_id"]: row for row in data["garments"]}
    positive_count = 0
    negative_count = 0
    for outfit in data["outfits"]:
        selected = [garments[garment_id] for garment_id in outfit["items"]]
        slots = {garment["slot"] for garment in selected}
        complete = "shoes" in slots and ("dress" in slots or {"top", "bottom"}.issubset(slots))
        require(complete, f"incomplete outfit structure: {outfit['outfit_id']}")
        require(not ("dress" in slots and ({"top", "bottom"} & slots)), f"outfit mixes dress with top/bottom: {outfit['outfit_id']}")
        if outfit["positive"]:
            positive_count += 1
            require(outfit["grade"] in {"gold", "silver"}, f"positive outfit has negative grade: {outfit['outfit_id']}")
            require(all(garment["status"] == "available" for garment in selected), f"positive outfit uses unavailable garment: {outfit['outfit_id']}")
            require(
                all("all" in garment["seasons"] or outfit["season"] in garment["seasons"] for garment in selected),
                f"positive outfit uses an out-of-season garment: {outfit['outfit_id']}",
            )
            require(
                all(outfit["occasion"] in garment["occasions"] for garment in selected),
                f"positive outfit uses a garment for the wrong occasion: {outfit['outfit_id']}",
            )
            avoid = set(users[outfit["user_id"]]["avoid_colors"])
            require(all(garment["color"] not in avoid for garment in selected), f"positive outfit uses avoided color: {outfit['outfit_id']}")
        else:
            negative_count += 1
            require(outfit["grade"] == "bronze", f"negative outfit must be bronze: {outfit['outfit_id']}")
            avoid = set(users[outfit["user_id"]]["avoid_colors"])
            has_violation = any(item["status"] != "available" or item["color"] in avoid for item in selected)
            require(has_violation, f"negative outfit lacks a hard-rule violation: {outfit['outfit_id']}")
    require(positive_count == 16 and negative_count == 4, f"outfit positive/negative split mismatch: {positive_count}/{negative_count}")


def validate_shopping_rules(data: dict[str, list[dict]]) -> None:
    for case in data["eval"]:
        case_id = case["case_id"]
        expected_urgency = HORIZON_URGENCY[case["horizon"]]
        require(case["urgency"] == expected_urgency, f"horizon-to-urgency mapping failed: {case_id}")
        if case["urgency"] == "high":
            require(case["shopping"] is False, f"shopping gate failed: {case_id}")
            require(case["allow_catalog"] is False, f"catalog gate failed: {case_id}")
            require(case["allowed_catalog"] == [], f"gated case has allowed catalog IDs: {case_id}")
        if not case["shopping"]:
            require(case["allow_catalog"] is False, f"non-shopping case allows catalog: {case_id}")
            require(case["allowed_catalog"] == [], f"non-shopping case has catalog candidates: {case_id}")
        if not case["allow_catalog"]:
            require(case["allowed_catalog"] == [], f"catalog-disabled case has candidates: {case_id}")
        if case["allow_catalog"]:
            require(case["shopping"] is True, f"catalog-enabled case must be a shopping case: {case_id}")
            require(case["intent"] in {"buy", "fill_gap", "browse"}, f"catalog-enabled case has invalid intent: {case_id}")
            require(bool(case["allowed_catalog"]), f"catalog-enabled case has no candidates: {case_id}")


def validate_catalog(data: dict[str, list[dict]]) -> None:
    rows = data["catalog"]
    forbidden_text = ["淘宝", "天猫", "京东", "拼多多", "http://", "https://", "www."]
    enum_tokens = set(EXPECTED_VOCAB["color"] + EXPECTED_VOCAB["occasion"] + EXPECTED_VOCAB["season"])
    allowed_stores = {"晴屿模拟店", "松影模拟店", "微风模拟店", "棉云模拟店", "青禾模拟店"}
    allowed_brands = {"岚序模拟牌", "原简模拟牌", "轻禾模拟牌", "栖蓝模拟牌", "木白模拟牌"}
    titles: set[str] = set()
    points_seen: set[tuple[str, ...]] = set()
    for row in rows:
        item_id = row["item_id"]
        require(row["synthetic"] is True, f"catalog item is not synthetic: {item_id}")
        require(row["platform"] == "cn_market_mock", f"wrong mock platform: {item_id}")
        require(row["declaration"] == "合成模拟商品，非真实在售商品", f"missing mock declaration: {item_id}")
        require(row["price"] < row["old_price"], f"price must be below old price: {item_id}")
        require(len(row["points"]) == 3 and len(set(row["points"])) == 3, f"catalog item must have three distinct points: {item_id}")
        require(row["store"] in allowed_stores, f"unknown synthetic store: {row['store']}")
        require(row["brand"] in allowed_brands, f"unknown synthetic brand: {row['brand']}")
        require(row["title"] not in titles, f"duplicate catalog title: {row['title']}")
        titles.add(row["title"])
        points_seen.add(tuple(row["points"]))
        chinese_copy = " ".join([row["title"], *row["points"], row["description"], row["search_text"]])
        require(re.search(r"[一-鿿]", chinese_copy) is not None, f"catalog copy lacks Chinese text: {item_id}")
        lowered = chinese_copy.lower()
        for token in enum_tokens:
            require(re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", lowered) is None, f"catalog copy leaks enum token '{token}': {item_id}")
        serialized = json.dumps(row, ensure_ascii=False).lower()
        for token in forbidden_text:
            require(token.lower() not in serialized, f"catalog item contains forbidden platform or URL text: {item_id}")
    require(len(points_seen) >= 20, "catalog points are too repetitive")
    require(sum(row["stock"] == 0 for row in rows) == 5, "catalog must contain exactly five stock=0 records")
    require({row["delivery_days"] for row in rows} == {1, 2, 3, 5, 7, 10}, "delivery_days coverage mismatch")
    require({row["slot"] for row in rows} == set(EXPECTED_VOCAB["slot"]), "catalog does not cover all slots")


def validate_eval(data: dict[str, list[dict]]) -> None:
    buckets = Counter(row["bucket"] for row in data["eval"])
    require(buckets == Counter(EXPECTED_BUCKETS), f"eval bucket counts mismatch: {buckets}")
    urgent = [row for row in data["eval"] if row["bucket"] == "urgent_no_shop"]
    require(len(urgent) == 8 and all(row["urgency"] == "high" for row in urgent), "urgent bucket must contain eight high-urgency cases")
    require({row["horizon"] for row in urgent} >= {"now", "today", "unknown"}, "urgent bucket must cover now/today/unknown")
    conditional = [row for row in data["eval"] if row["bucket"] == "conditional_buy"]
    require(all(row["shopping"] and row["allow_catalog"] and row["allowed_catalog"] for row in conditional), "conditional-buy cases must allow stocked catalog candidates")

    users = {row["user_id"]: row for row in data["users"]}
    garments = {row["garment_id"]: row for row in data["garments"]}
    catalog = {row["item_id"]: row for row in data["catalog"]}
    for case in data["eval"]:
        case_id = case["case_id"]
        avoid = set(users[case["user_id"]]["avoid_colors"])
        for garment_id in case["allowed_garments"]:
            garment = garments[garment_id]
            require(garment["status"] == "available", f"eval allows unavailable garment: {case_id}")
            require(garment["color"] not in avoid, f"eval allows avoided color: {case_id}")
        for garment_id in case["forbidden_garments"]:
            garment = garments[garment_id]
            require(garment["status"] != "available" or garment["color"] in avoid, f"eval forbidden garment lacks a hard-rule violation: {case_id}")
        for item_id in case["allowed_catalog"]:
            require(catalog[item_id]["stock"] > 0, f"eval allows zero-stock catalog item: {case_id}")
            if case["horizon"] == "soon":
                require(catalog[item_id]["delivery_days"] <= 7, f"soon case allows slow delivery: {case_id}/{item_id}")
        for item_id in case["forbidden_catalog"]:
            require(catalog[item_id]["stock"] == 0, f"forbidden catalog fixture should be out of stock: {case_id}/{item_id}")

    grounding = [row for row in data["eval"] if row["bucket"] == "grounding"]
    require(all(row["bad_ids"] for row in grounding), "grounding cases must isolate illegal IDs in bad_ids")
    support = [row for row in data["eval"] if row["bucket"] == "support_safety"]
    support_text = "\n".join(row["expected"] for row in support)
    require("情绪" in support_text, "support safety cases must cover emotional support")
    require("身体羞辱" in support_text, "support safety cases must cover body-shaming boundaries")
    require("医疗诊断" in support_text, "support safety cases must cover medical boundaries")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_manifest(data: dict[str, list[dict]]) -> None:
    path = ROOT / "data" / "manifests" / "fixtures_v1.0.json"
    with path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    require(manifest["schema_version"] == 1, "manifest schema_version mismatch")
    require(manifest["data_version"] == "fixtures_v1.0", "manifest data_version mismatch")
    require(manifest["seed"] == 20260729, "manifest seed mismatch")
    require(set(manifest["files"]) == set(DATA_PATHS), "manifest file set mismatch")
    for name, data_path in DATA_PATHS.items():
        item = manifest["files"][name]
        require(item["path"] == data_path.relative_to(ROOT).as_posix(), f"manifest path mismatch: {name}")
        require(item["count"] == len(data[name]), f"manifest count mismatch: {name}")
        require(item["sha256"] == file_sha256(data_path), f"manifest hash mismatch: {name}")


def validate_all() -> None:
    data = load_data()
    validate_vocab()
    validate_schemas(data)
    validate_counts(data)
    validate_references(data)
    validate_outfit_rules(data)
    validate_shopping_rules(data)
    validate_catalog(data)
    validate_eval(data)
    validate_manifest(data)


def main() -> None:
    validate_all()
    print("validation passed: users=3 garments=50 outfits=20 catalog=50 eval=30")


if __name__ == "__main__":
    main()
