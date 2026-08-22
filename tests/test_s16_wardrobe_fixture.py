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
ADDED_OWNER_SLOT_COUNTS = {
    "u01": {
        "top": 8,
        "bottom": 9,
        "dress": 7,
        "outer": 7,
        "shoes": 7,
        "bag": 3,
        "accessory": 3,
    },
    "u02": {
        "top": 3,
        "bottom": 3,
        "dress": 2,
        "outer": 2,
        "shoes": 2,
        "bag": 0,
        "accessory": 0,
    },
    "u03": {
        "top": 3,
        "bottom": 3,
        "dress": 2,
        "outer": 2,
        "shoes": 1,
        "bag": 1,
        "accessory": 2,
    },
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
SEASON_LABELS = {
    "all": "四季",
    "spring": "春季",
    "summer": "夏季",
    "autumn": "秋季",
    "winter": "冬季",
}
STYLE_LABELS = {
    "simple": "简约",
    "classic": "经典",
    "smart": "利落",
    "formal": "正式",
    "street": "街头",
    "sporty": "运动",
    "soft": "柔和",
    "vintage": "复古",
    "business": "商务",
    "campus": "学院",
}
OCCASION_LABELS = {
    "daily": "日常",
    "commute": "通勤",
    "interview": "面试",
    "meeting": "会议",
    "date": "约会",
    "party": "聚会",
    "travel": "旅行",
    "outdoor": "户外",
    "home": "居家",
    "sports": "运动",
}
MATERIAL_LABELS = {
    "cotton": "棉质",
    "knit": "针织",
    "denim": "牛仔",
    "wool": "羊毛",
    "linen": "亚麻",
    "leather": "皮质",
    "synthetic": "合成材质",
}
FIT_LABELS = {
    "regular": "常规版型",
    "straight": "直筒版型",
    "loose": "宽松版型",
    "slim": "修身版型",
}
SHOE_LONG_WALK_CLAIM = "缓震平底结构，适合久走与长时间站立"
SHOE_SHORT_ACTIVITY_CLAIM = "鞋跟或细带结构适合短时活动，长时间活动前需另行确认"
SHOE_UNVERIFIED_SUPPORT_CLAIM = (
    "鞋底与支撑信息需实物确认，久走或长时间站立前需确认舒适度"
)
BAG_CARRY_CLAIM = "容量、重量与携带舒适度需按实际装载确认"
BAG_HAND_CARRY_CLAIM = "手拿方式与容量适合短时场景，长时间携带前需确认"
ACCESSORY_WEAR_CLAIM = "佩戴尺寸与接触舒适度需按实际情况确认"
ALLOWED_COMFORT_CLAIMS = {
    "宽松版型便于活动，仍需按场景确认舒适度",
    "修身版型活动前需确认贴合与舒适度",
    "常规活动前需确认穿着舒适度",
    SHOE_LONG_WALK_CLAIM,
    SHOE_SHORT_ACTIVITY_CLAIM,
    SHOE_UNVERIFIED_SUPPORT_CLAIM,
    BAG_CARRY_CLAIM,
    BAG_HAND_CARRY_CLAIM,
    ACCESSORY_WEAR_CLAIM,
}

SLOT_NAME_MARKERS = {
    "top": ("衬衫", "上衣", "衬衣", "打底衫", "背心", "开衫", "针织衫"),
    "bottom": ("裤", "半身裙"),
    "dress": ("连衣裙", "长裙", "茶歇裙"),
    "outer": ("西装", "外套", "夹克", "风衣", "大衣", "披肩", "长开衫"),
    "shoes": ("鞋", "靴"),
    "bag": ("包",),
    "accessory": ("方巾", "耳饰", "腰带", "帽", "项链"),
}

# Independent test oracle: deliberately duplicated here rather than imported
# from the generator so any profile drift must be reviewed ID by ID.
EXPECTED_PROFILE_ORACLE = {
    "g051": ("飘带衬衫", "top", "synthetic", ("spring", "summer"), ("soft", "smart"), ("date", "meeting"), 2, 1, "regular", "apparel_regular"),
    "g052": ("细针织方领上衣", "top", "knit", ("autumn", "winter"), ("soft", "classic"), ("date", "daily"), 2, 3, "slim", "apparel_slim"),
    "g053": ("垂感通勤衬衣", "top", "cotton", ("all",), ("smart", "business"), ("commute", "meeting"), 3, 2, "regular", "apparel_regular"),
    "g054": ("柔棉修身打底衫", "top", "cotton", ("autumn", "winter"), ("simple", "soft"), ("daily", "home"), 1, 3, "slim", "apparel_slim"),
    "g055": ("泡泡袖约会上衣", "top", "cotton", ("spring", "summer"), ("soft", "vintage"), ("date", "party"), 2, 1, "regular", "apparel_regular"),
    "g056": ("简约无袖针织背心", "top", "knit", ("spring", "summer"), ("simple", "soft"), ("daily", "travel"), 1, 1, "regular", "apparel_regular"),
    "g057": ("商务翻领衬衫", "top", "cotton", ("all",), ("business", "smart"), ("commute", "meeting", "interview"), 4, 2, "regular", "apparel_regular"),
    "g058": ("轻盈亚麻衬衣", "top", "linen", ("spring", "summer"), ("simple", "smart"), ("travel", "daily"), 1, 1, "loose", "apparel_loose"),
    "g059": ("高腰直筒西装裤", "bottom", "wool", ("all",), ("business", "formal"), ("commute", "meeting", "interview"), 4, 2, "straight", "apparel_regular"),
    "g060": ("垂感阔腿通勤裤", "bottom", "synthetic", ("all",), ("smart", "business"), ("commute", "meeting"), 3, 2, "loose", "apparel_loose"),
    "g061": ("中长百褶半身裙", "bottom", "synthetic", ("spring", "autumn"), ("classic", "soft"), ("commute", "date"), 2, 2, "regular", "apparel_regular"),
    "g062": ("简洁铅笔半身裙", "bottom", "wool", ("autumn", "winter"), ("business", "formal"), ("meeting", "interview"), 4, 4, "slim", "apparel_slim"),
    "g063": ("复古牛仔直筒裤", "bottom", "denim", ("all",), ("vintage", "street"), ("daily", "travel"), 1, 2, "straight", "apparel_regular"),
    "g064": ("柔软针织半身裙", "bottom", "knit", ("autumn", "winter"), ("soft", "simple"), ("daily", "date"), 1, 3, "regular", "apparel_regular"),
    "g065": ("轻量旅行束脚裤", "bottom", "synthetic", ("spring", "summer", "autumn"), ("sporty", "simple"), ("travel", "outdoor", "sports"), 0, 1, "regular", "apparel_regular"),
    "g066": ("运动弹力长裤", "bottom", "synthetic", ("all",), ("sporty", "simple"), ("sports", "daily"), 0, 1, "slim", "apparel_slim"),
    "g067": ("高腰伞摆半身裙", "bottom", "cotton", ("spring", "summer"), ("soft", "vintage"), ("date", "daily"), 2, 1, "regular", "apparel_regular"),
    "g068": ("收腰通勤连衣裙", "dress", "wool", ("all",), ("business", "smart"), ("commute", "meeting"), 3, 2, "slim", "apparel_slim"),
    "g069": ("简洁衬衫连衣裙", "dress", "cotton", ("spring", "summer"), ("smart", "simple"), ("commute", "daily"), 2, 1, "straight", "apparel_regular"),
    "g070": ("法式约会茶歇裙", "dress", "synthetic", ("spring", "summer"), ("soft", "vintage"), ("date", "party"), 2, 1, "slim", "apparel_slim"),
    "g071": ("商务直筒连衣裙", "dress", "wool", ("autumn", "winter"), ("business", "formal"), ("meeting", "interview", "commute"), 4, 4, "straight", "apparel_regular"),
    "g072": ("旅行亚麻连衣裙", "dress", "linen", ("spring", "summer"), ("simple", "soft"), ("travel", "daily"), 1, 1, "loose", "apparel_loose"),
    "g073": ("柔软针织连衣裙", "dress", "knit", ("autumn", "winter"), ("soft", "simple"), ("daily", "date"), 1, 3, "regular", "apparel_regular"),
    "g074": ("宴会缎面长裙", "dress", "synthetic", ("spring", "autumn"), ("formal", "soft"), ("party", "date"), 4, 2, "slim", "apparel_slim"),
    "g075": ("双排扣通勤西装", "outer", "wool", ("autumn", "winter"), ("business", "formal"), ("commute", "meeting", "interview"), 4, 4, "regular", "apparel_regular"),
    "g076": ("轻量防风旅行外套", "outer", "synthetic", ("spring", "summer", "autumn"), ("sporty", "simple"), ("travel", "outdoor", "sports"), 0, 1, "loose", "apparel_loose"),
    "g077": ("短款软呢夹克", "outer", "wool", ("autumn", "winter"), ("classic", "smart"), ("commute", "daily"), 2, 4, "regular", "apparel_regular"),
    "g078": ("经典收腰风衣", "outer", "cotton", ("spring", "autumn"), ("classic", "smart"), ("commute", "travel"), 3, 2, "slim", "apparel_slim"),
    "g079": ("羊毛廓形大衣", "outer", "wool", ("winter",), ("classic", "formal"), ("commute", "meeting"), 4, 5, "loose", "apparel_loose"),
    "g080": ("运动连帽外套", "outer", "synthetic", ("all",), ("sporty", "simple"), ("sports", "daily", "travel"), 0, 2, "loose", "apparel_loose"),
    "g081": ("宴会保暖短款披肩", "outer", "synthetic", ("autumn", "winter"), ("formal", "soft"), ("party", "date"), 4, 4, "regular", "apparel_regular"),
    "g082": ("低跟通勤乐福鞋", "shoes", "leather", ("all",), ("business", "classic"), ("commute", "meeting"), 3, 1, "regular", "shoe_short"),
    "g083": ("尖头中跟单鞋", "shoes", "leather", ("spring", "autumn"), ("formal", "classic"), ("meeting", "date", "party"), 4, 1, "slim", "shoe_short"),
    "g084": ("经典芭蕾平底鞋", "shoes", "leather", ("spring", "summer", "autumn"), ("soft", "classic"), ("daily", "date", "travel"), 1, 1, "regular", "shoe_unverified"),
    "g085": ("轻量缓震旅行运动鞋", "shoes", "synthetic", ("all",), ("sporty", "simple"), ("travel", "sports", "outdoor"), 0, 1, "regular", "shoe_evidenced"),
    "g086": ("宴会细带凉鞋", "shoes", "synthetic", ("spring", "summer"), ("formal", "soft"), ("party", "date"), 4, 1, "slim", "shoe_short"),
    "g087": ("复古玛丽珍鞋", "shoes", "leather", ("spring", "autumn"), ("vintage", "classic"), ("date", "daily"), 2, 1, "regular", "shoe_unverified"),
    "g088": ("简洁短靴", "shoes", "leather", ("autumn", "winter"), ("classic", "smart"), ("commute", "daily"), 3, 4, "regular", "shoe_unverified"),
    "g089": ("结构感通勤托特包", "bag", "leather", ("all",), ("business", "smart"), ("commute", "meeting"), 3, 2, "regular", "bag_carry"),
    "g090": ("轻便旅行斜挎包", "bag", "synthetic", ("all",), ("simple", "sporty"), ("travel", "daily", "sports"), 0, 1, "regular", "bag_carry"),
    "g091": ("宴会链条手拿包", "bag", "synthetic", ("all",), ("formal", "vintage"), ("party", "date"), 4, 1, "regular", "bag_hand"),
    "g092": ("几何方巾", "accessory", "synthetic", ("spring", "autumn"), ("smart", "vintage"), ("commute", "date"), 2, 1, "regular", "accessory_wear"),
    "g093": ("简约树脂耳饰", "accessory", "synthetic", ("all",), ("simple", "smart"), ("daily", "meeting"), 2, 1, "regular", "accessory_wear"),
    "g094": ("复古细腰带", "accessory", "leather", ("all",), ("vintage", "classic"), ("daily", "date"), 2, 1, "regular", "accessory_wear"),
    "g095": ("运动拉链短上衣", "top", "synthetic", ("all",), ("sporty", "simple"), ("sports", "daily", "travel"), 0, 1, "regular", "apparel_regular"),
    "g096": ("复古圆领开衫", "top", "knit", ("autumn", "winter"), ("vintage", "soft"), ("date", "daily"), 2, 3, "regular", "apparel_regular"),
    "g097": ("宴会光泽上衣", "top", "synthetic", ("all",), ("formal", "soft"), ("party", "date"), 4, 2, "slim", "apparel_slim"),
    "g098": ("九分烟管西装裤", "bottom", "wool", ("all",), ("business", "smart"), ("commute", "meeting", "interview"), 4, 2, "straight", "apparel_regular"),
    "g099": ("亚麻宽腿长裤", "bottom", "linen", ("spring", "summer"), ("simple", "soft"), ("travel", "daily"), 1, 1, "loose", "apparel_loose"),
    "g100": ("宴会缎面半身裙", "bottom", "synthetic", ("spring", "summer", "autumn"), ("formal", "soft"), ("party", "date"), 4, 1, "regular", "apparel_regular"),
    "g101": ("复古方领连衣裙", "dress", "cotton", ("spring", "summer"), ("vintage", "soft"), ("date", "party"), 2, 1, "regular", "apparel_regular"),
    "g102": ("运动翻领连衣裙", "dress", "synthetic", ("spring", "summer"), ("sporty", "simple"), ("sports", "daily"), 0, 1, "regular", "apparel_regular"),
    "g103": ("亚麻单排扣西装", "outer", "linen", ("spring", "summer"), ("business", "smart"), ("commute", "meeting"), 3, 1, "regular", "apparel_regular"),
    "g104": ("复古牛仔夹克", "outer", "denim", ("all",), ("vintage", "street"), ("daily", "travel"), 1, 2, "loose", "apparel_loose"),
    "g105": ("运动缓震跑鞋", "shoes", "synthetic", ("all",), ("sporty", "simple"), ("sports", "travel", "outdoor"), 0, 1, "regular", "shoe_evidenced"),
    "g106": ("方头低跟穆勒鞋", "shoes", "leather", ("spring", "summer"), ("classic", "smart"), ("commute", "daily"), 2, 1, "regular", "shoe_short"),
    "g107": ("旅行防晒衬衫", "top", "synthetic", ("spring", "summer"), ("sporty", "simple"), ("travel", "outdoor", "sports"), 0, 1, "loose", "apparel_loose"),
    "g108": ("法式领口针织衫", "top", "knit", ("spring", "autumn"), ("soft", "classic"), ("date", "daily"), 2, 2, "regular", "apparel_regular"),
    "g109": ("利落半高领上衣", "top", "knit", ("autumn", "winter"), ("smart", "business"), ("commute", "meeting"), 3, 3, "slim", "apparel_slim"),
    "g110": ("工装直筒长裤", "bottom", "cotton", ("all",), ("street", "simple"), ("daily", "outdoor", "travel"), 1, 2, "straight", "apparel_regular"),
    "g111": ("通勤开衩半身裙", "bottom", "wool", ("autumn", "winter"), ("business", "smart"), ("commute", "meeting"), 3, 4, "slim", "apparel_slim"),
    "g112": ("日常锥形休闲裤", "bottom", "cotton", ("all",), ("simple", "smart"), ("daily", "commute"), 1, 2, "regular", "apparel_regular"),
    "g113": ("日常伞摆连衣裙", "dress", "cotton", ("spring", "summer"), ("soft", "simple"), ("daily", "date"), 1, 1, "regular", "apparel_regular"),
    "g114": ("会议西装连衣裙", "dress", "wool", ("autumn", "winter"), ("business", "formal"), ("meeting", "interview"), 4, 4, "straight", "apparel_regular"),
    "g115": ("简约针织长开衫", "outer", "knit", ("autumn", "winter"), ("simple", "soft"), ("daily", "home"), 1, 3, "loose", "apparel_loose"),
    "g116": ("正式会议西装外套", "outer", "wool", ("all",), ("business", "formal"), ("meeting", "interview", "commute"), 4, 3, "regular", "apparel_regular"),
    "g117": ("商务皮质德比鞋", "shoes", "leather", ("all",), ("business", "formal"), ("commute", "meeting", "interview"), 4, 2, "regular", "shoe_unverified"),
    "g118": ("日常软皮肩背包", "bag", "leather", ("all",), ("simple", "soft"), ("daily", "commute"), 1, 1, "regular", "bag_carry"),
    "g119": ("运动遮阳帽", "accessory", "synthetic", ("spring", "summer"), ("sporty", "simple"), ("sports", "outdoor", "travel"), 0, 1, "regular", "accessory_wear"),
    "g120": ("宴会仿珍珠项链", "accessory", "synthetic", ("all",), ("formal", "classic"), ("party", "date"), 4, 1, "regular", "accessory_wear"),
}

COMFORT_BY_CATEGORY = {
    "apparel_regular": "常规活动前需确认穿着舒适度",
    "apparel_slim": "修身版型活动前需确认贴合与舒适度",
    "apparel_loose": "宽松版型便于活动，仍需按场景确认舒适度",
    "shoe_evidenced": SHOE_LONG_WALK_CLAIM,
    "shoe_short": SHOE_SHORT_ACTIVITY_CLAIM,
    "shoe_unverified": SHOE_UNVERIFIED_SUPPORT_CLAIM,
    "bag_carry": BAG_CARRY_CLAIM,
    "bag_hand": BAG_HAND_CARRY_CLAIM,
    "accessory_wear": ACCESSORY_WEAR_CLAIM,
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


def _controlled_copy_claim(row: dict[str, object]) -> str:
    search_text = str(row["search_text"])
    marker = {
        "bag": "携带说明：",
        "accessory": "佩戴说明：",
    }.get(str(row["slot"]), "舒适说明：")
    assert search_text.count(marker) == 1
    return search_text.split(marker, 1)[1]


def _owner_slot_counts(rows: list[dict[str, object]]) -> dict[str, dict[str, int]]:
    return {
        user_id: {
            slot: sum(
                row.get("user_id") == user_id and row.get("slot") == slot
                for row in rows
            )
            for slot in ADDED_SLOT_COUNTS
        }
        for user_id in ADDED_OWNER_COUNTS
    }


def _refresh_extension_manifest_receipts(
    repository_root: Path,
    rows: list[dict[str, object]],
    *,
    recompute_counts: bool = True,
) -> None:
    """Rebind a deliberately mutated fixture/schema without hiding count drift."""

    fixture_path = repository_root / EXTENSION
    schema_path = repository_root / EXTENSION_SCHEMA
    manifest_path = repository_root / EXTENSION_MANIFEST
    base_rows = _read_jsonl(repository_root / "data/fixtures/garments.jsonl")
    combined = [*base_rows, *rows]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["garments_extension"] = {
        "path": EXTENSION.as_posix(),
        "count": len(rows),
        "sha256": _sha256(fixture_path),
    }
    manifest["files"]["schema"] = {
        "path": EXTENSION_SCHEMA.as_posix(),
        "sha256": _sha256(schema_path),
    }
    if recompute_counts:
        manifest["counts"] = {
            "added_owners": dict(Counter(row.get("user_id") for row in rows)),
            "added_slots": dict(Counter(row.get("slot") for row in rows)),
            "added_owner_slots": _owner_slot_counts(rows),
            "final_owners": dict(Counter(row.get("user_id") for row in combined)),
            "final_slots": dict(Counter(row.get("slot") for row in combined)),
            "final_owner_slots": _owner_slot_counts(combined),
            "total_added": len(rows),
            "total_garments": len(combined),
        }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


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
    for directory in (
        "data/fixtures",
        "data/schemas",
        "data/manifests",
        "data/eval",
    ):
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


def test_every_extension_row_has_coherent_declarative_semantics(
    project_root: Path,
) -> None:
    rows = _extension_rows(project_root)
    by_id = {str(row["garment_id"]): row for row in rows}
    for row in rows:
        name = str(row["name"])
        search_text = str(row["search_text"])
        assert f"材质：{MATERIAL_LABELS[str(row['material'])]}" in search_text
        if row["slot"] in {"top", "bottom", "dress", "outer"}:
            assert f"服装版型：{FIT_LABELS[str(row['fit'])]}" in search_text
        elif row["slot"] == "shoes":
            shoe_fit = "贴合鞋型" if row["fit"] == "slim" else "常规鞋型"
            assert f"鞋型：{shoe_fit}" in search_text
        elif row["slot"] == "bag":
            assert "携带规格：标准" in search_text
        else:
            assert "佩戴规格：标准" in search_text
        if row["slot"] in {"bag", "accessory"}:
            assert f"用途场景：{'、'.join(OCCASION_LABELS[item] for item in row['occasions'])}" in search_text
        else:
            assert f"季节：{'、'.join(SEASON_LABELS[item] for item in row['seasons'])}" in search_text
            assert f"风格：{'、'.join(STYLE_LABELS[item] for item in row['styles'])}" in search_text
            assert f"场景：{'、'.join(OCCASION_LABELS[item] for item in row['occasions'])}" in search_text
            assert f"正式度：{row['formal']}/4" in search_text
            assert f"保暖度：{row['warmth']}/5" in search_text
        comfort_claim = _controlled_copy_claim(row)
        assert comfort_claim in ALLOWED_COMFORT_CLAIMS

        token_contracts = {
            "针织": row["material"] == "knit",
            "牛仔": row["material"] == "denim",
            "羊毛": row["material"] == "wool",
            "亚麻": row["material"] == "linen",
            "皮质": row["material"] == "leather",
            "软皮": row["material"] == "leather",
            "通勤": "commute" in row["occasions"],
            "商务": "business" in row["styles"] and int(row["formal"]) >= 3,
            "会议": "meeting" in row["occasions"] and int(row["formal"]) >= 3,
            "宴会": "party" in row["occasions"] and int(row["formal"]) >= 3,
            "旅行": "travel" in row["occasions"],
            "约会": "date" in row["occasions"],
            "运动": "sporty" in row["styles"] and "sports" in row["occasions"],
        }
        for token, coherent in token_contracts.items():
            if token in name:
                assert coherent, f"{row['garment_id']} {token} metadata drift"
        if "凉鞋" in name:
            assert "winter" not in row["seasons"] and int(row["warmth"]) <= 2
        if "大衣" in name:
            assert "winter" in row["seasons"] and int(row["warmth"]) >= 4
        if "防晒" in name:
            assert "summer" in row["seasons"] and int(row["warmth"]) <= 2
        if "修身" in name:
            assert row["fit"] == "slim"
        if "直筒" in name:
            assert row["fit"] == "straight"
        if "宽腿" in name or "廓形" in name:
            assert row["fit"] == "loose"
        if row["styles"] and "sporty" in row["styles"]:
            assert int(row["formal"]) <= 1
        if "formal" in row["styles"] or "business" in row["styles"]:
            assert int(row["formal"]) >= 3
        if "winter" in row["seasons"] and len(row["seasons"]) == 1:
            assert int(row["warmth"]) >= 4
        if row["seasons"] == ["summer"]:
            assert int(row["warmth"]) <= 2

        if row["slot"] == "shoes" and any(
            token in name for token in ("尖头", "中跟", "细带", "凉鞋")
        ):
            assert comfort_claim == SHOE_SHORT_ACTIVITY_CLAIM
        if comfort_claim == SHOE_LONG_WALK_CLAIM:
            assert row["slot"] == "shoes" and "缓震" in name

    assert {
        key: by_id["g051"][key]
        for key in (
            "name",
            "slot",
            "material",
            "seasons",
            "styles",
            "occasions",
            "formal",
            "warmth",
            "fit",
        )
    } == {
        "name": "飘带衬衫",
        "slot": "top",
        "material": "synthetic",
        "seasons": ["spring", "summer"],
        "styles": ["soft", "smart"],
        "occasions": ["date", "meeting"],
        "formal": 2,
        "warmth": 1,
        "fit": "regular",
    }
    assert by_id["g085"]["search_text"].endswith(
        f"舒适说明：{SHOE_LONG_WALK_CLAIM}"
    )
    assert by_id["g086"]["search_text"].endswith(
        f"舒适说明：{SHOE_SHORT_ACTIVITY_CLAIM}"
    )


def test_all_extension_names_and_copy_are_slot_appropriate_and_evidence_honest(
    project_root: Path,
) -> None:
    rows = _extension_rows(project_root)
    by_id = {str(row["garment_id"]): row for row in rows}
    for row in rows:
        slot = str(row["slot"])
        name = str(row["name"])
        search_text = str(row["search_text"])
        claim = _controlled_copy_claim(row)
        assert any(marker in name for marker in SLOT_NAME_MARKERS[slot]), (
            f"{row['garment_id']} name does not identify its declared slot"
        )

        if slot in {"top", "bottom", "dress", "outer"}:
            assert "服装版型：" in search_text
            assert "鞋型：" not in search_text
        elif slot == "shoes":
            assert "鞋型：" in search_text
            assert "服装版型：" not in search_text
            if "缓震" in name:
                assert claim == SHOE_LONG_WALK_CLAIM
            else:
                assert claim != SHOE_LONG_WALK_CLAIM
                assert "适合久走" not in claim
            if "平底" in name and "缓震" not in name:
                assert claim == SHOE_UNVERIFIED_SUPPORT_CLAIM
        elif slot == "bag":
            assert row["fit"] == "regular"
            assert "携带规格：标准" in search_text
            assert claim in {BAG_CARRY_CLAIM, BAG_HAND_CARRY_CLAIM}
            assert not any(token in search_text for token in ("版型", "保暖度", "贴合身体", "修身", "宽松"))
        else:
            assert row["fit"] == "regular"
            assert "佩戴规格：标准" in search_text
            assert claim == ACCESSORY_WEAR_CLAIM
            assert not any(token in search_text for token in ("版型", "保暖度", "贴合身体", "修身", "宽松"))

    assert by_id["g084"]["name"] == "经典芭蕾平底鞋"
    assert by_id["g084"]["search_text"].endswith(
        f"舒适说明：{SHOE_UNVERIFIED_SUPPORT_CLAIM}"
    )
    assert by_id["g091"]["name"] == "宴会链条手拿包"
    assert by_id["g091"]["fit"] == "regular"
    assert by_id["g091"]["search_text"].endswith(
        f"携带说明：{BAG_HAND_CARRY_CLAIM}"
    )
    for garment_id in ("g083", "g086"):
        assert by_id[garment_id]["search_text"].endswith(
            f"舒适说明：{SHOE_SHORT_ACTIVITY_CLAIM}"
        )
    assert "缓震" in str(by_id["g085"]["name"])
    assert by_id["g085"]["search_text"].endswith(
        f"舒适说明：{SHOE_LONG_WALK_CLAIM}"
    )
    assert "服装版型：常规版型" in str(by_id["g051"]["search_text"])


def test_complete_independent_profile_oracle_and_name_token_rules(
    project_root: Path,
) -> None:
    rows = _extension_rows(project_root)
    assert set(EXPECTED_PROFILE_ORACLE) == {
        f"g{index:03d}" for index in range(51, 121)
    }
    assert len(EXPECTED_PROFILE_ORACLE) == 70
    category_by_claim = {value: key for key, value in COMFORT_BY_CATEGORY.items()}
    assert len(category_by_claim) == len(COMFORT_BY_CATEGORY)

    slot_token_rules = {
        "衬衫": {"top", "dress"},
        "衬衣": {"top"},
        "上衣": {"top"},
        "打底衫": {"top"},
        "背心": {"top"},
        "半身裙": {"bottom"},
        "裤": {"bottom"},
        "连衣裙": {"dress"},
        "茶歇裙": {"dress"},
        "外套": {"outer"},
        "夹克": {"outer"},
        "风衣": {"outer"},
        "大衣": {"outer"},
        "披肩": {"outer"},
        "鞋": {"shoes"},
        "靴": {"shoes"},
        "包": {"bag"},
        "方巾": {"accessory"},
        "耳饰": {"accessory"},
        "腰带": {"accessory"},
        "帽": {"accessory", "outer"},  # 连帽外套 is an outerwear feature.
        "项链": {"accessory"},
    }
    material_token_rules = {
        "针织": "knit",
        "牛仔": "denim",
        "羊毛": "wool",
        "软呢": "wool",
        "亚麻": "linen",
        "柔棉": "cotton",
        "皮质": "leather",
        "软皮": "leather",
        "树脂": "synthetic",
        "仿珍珠": "synthetic",
        "缎面": "synthetic",
    }

    for row in rows:
        garment_id = str(row["garment_id"])
        claim = _controlled_copy_claim(row)
        assert claim in category_by_claim, f"{garment_id} has uncontrolled copy"
        observed = (
            row["name"],
            row["slot"],
            row["material"],
            tuple(row["seasons"]),
            tuple(row["styles"]),
            tuple(row["occasions"]),
            row["formal"],
            row["warmth"],
            row["fit"],
            category_by_claim[claim],
        )
        assert observed == EXPECTED_PROFILE_ORACLE[garment_id]

        name = str(row["name"])
        for token, allowed_slots in slot_token_rules.items():
            if token in name:
                assert row["slot"] in allowed_slots, f"{garment_id} {token} slot drift"
        for token, expected_material in material_token_rules.items():
            if token in name:
                assert row["material"] == expected_material, (
                    f"{garment_id} {token} material drift"
                )


def test_accessory_names_do_not_claim_unsupported_materials(project_root: Path) -> None:
    by_id = {
        str(row["garment_id"]): row for row in _extension_rows(project_root)
    }
    assert by_id["g093"]["name"] == "简约树脂耳饰"
    assert by_id["g093"]["material"] == "synthetic"
    assert by_id["g120"]["name"] == "宴会仿珍珠项链"
    assert by_id["g120"]["material"] == "synthetic"


def test_every_bag_and_accessory_uses_only_purpose_relevant_copy(
    project_root: Path,
) -> None:
    rows = [
        row
        for row in _extension_rows(project_root)
        if row["slot"] in {"bag", "accessory"}
    ]
    assert len(rows) == 9
    forbidden = (
        "版型",
        "保暖度",
        "贴合身体",
        "修身",
        "宽松",
        "缓震",
        "久走",
        "长时间站立",
    )
    for row in rows:
        search_text = str(row["search_text"])
        assert not any(token in search_text for token in forbidden)
        assert "用途场景：" in search_text
        expected_marker = "携带说明：" if row["slot"] == "bag" else "佩戴说明："
        assert search_text.count(expected_marker) == 1


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
    assert set(manifest) == {
        "schema_version",
        "data_version",
        "source_id",
        "seed",
        "base",
        "files",
        "counts",
        "audiences",
    }
    assert manifest.get("schema_version") == 1
    assert manifest.get("data_version") == DATA_VERSION
    assert manifest.get("source_id") == SOURCE_ID
    assert manifest.get("seed") == 20260729
    assert manifest["audiences"] == [
        "womenswear",
        "unisex_womenswear_compatible",
    ]
    assert manifest["counts"] == {
        "added_owners": ADDED_OWNER_COUNTS,
        "added_slots": ADDED_SLOT_COUNTS,
        "added_owner_slots": ADDED_OWNER_SLOT_COUNTS,
        "final_owners": EXPECTED_OWNER_COUNTS,
        "final_slots": EXPECTED_SLOT_COUNTS,
        "final_owner_slots": EXPECTED_OWNER_SLOT_COUNTS,
        "total_added": 70,
        "total_garments": 120,
    }
    assert set(manifest["base"]) == {"data_version", "files"}
    assert manifest["base"]["data_version"] == "fixtures_v1.0"
    assert set(manifest["base"]["files"]) == set(BASE_FILE_SHA256)
    for relative, expected_hash in BASE_FILE_SHA256.items():
        receipt = manifest["base"]["files"][relative]
        assert receipt == {"path": relative, "sha256": expected_hash}
        assert _sha256(project_root / relative) == expected_hash
    assert set(manifest["files"]) == {"garments_extension", "schema"}
    assert manifest["files"]["garments_extension"] == {
        "path": EXTENSION.as_posix(),
        "count": 70,
        "sha256": _sha256(project_root / EXTENSION),
    }
    assert manifest["files"]["schema"] == {
        "path": EXTENSION_SCHEMA.as_posix(),
        "sha256": _sha256(project_root / EXTENSION_SCHEMA),
    }


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


def test_repository_keeps_base_50_compatible_when_overlay_is_absent(
    project_root: Path, tmp_path: Path
) -> None:
    repository_root = _copy_repository_root(project_root, tmp_path / "base-only")
    (repository_root / EXTENSION).unlink()
    repository = FixtureRepository(repository_root)
    assert repository.counts["garments"] == 50
    assert len(_repository_rows(repository)) == 50


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "truncated",
        "extra",
        "internal_duplicate",
        "manifest_missing",
        "manifest_hash",
        "manifest_path",
        "manifest_count",
        "manifest_version",
        "manifest_extra_field",
    ],
)
def test_repository_requires_complete_manifest_bound_overlay_without_partial_state(
    project_root: Path, tmp_path: Path, case: str
) -> None:
    repository_root = _copy_repository_root(project_root, tmp_path / case)
    fixture_path = repository_root / EXTENSION
    manifest_path = repository_root / EXTENSION_MANIFEST
    rows = _read_jsonl(fixture_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if case == "empty":
        _write_jsonl(fixture_path, [])
    elif case == "truncated":
        _write_jsonl(fixture_path, rows[:-1])
    elif case == "extra":
        extra = dict(rows[-1], garment_id="g121", name="越界额外单品")
        _write_jsonl(fixture_path, [*rows, extra])
    elif case == "internal_duplicate":
        _write_jsonl(fixture_path, [*rows[:-1], dict(rows[-2])])
    elif case == "manifest_missing":
        manifest_path.unlink()
    elif case == "manifest_hash":
        manifest["files"]["garments_extension"]["sha256"] = "0" * 64
    elif case == "manifest_path":
        manifest["files"]["garments_extension"]["path"] = "data/fixtures/other.jsonl"
    elif case == "manifest_count":
        manifest["files"]["garments_extension"]["count"] = 69
    elif case == "manifest_version":
        manifest["data_version"] = "fixtures_s16_drift"
    else:
        manifest["unexpected"] = True
    if case in {
        "empty",
        "truncated",
        "extra",
        "internal_duplicate",
    }:
        manifest["files"]["garments_extension"]["sha256"] = _sha256(fixture_path)
    if manifest_path.exists():
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    repository = FixtureRepository.__new__(FixtureRepository)
    with pytest.raises((RuntimeError, ValueError)):
        repository.__init__(repository_root)
    assert not hasattr(repository, "_garments")


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
    _refresh_extension_manifest_receipts(
        repository_root,
        invalid_rows,
        recompute_counts=False,
    )
    with pytest.raises((RuntimeError, ValueError)):
        FixtureRepository(repository_root)


@pytest.mark.parametrize(
    "case",
    [
        "data_version_const",
        "source_id_const",
        "synthetic_const",
        "audience_enum",
        "additional_property",
        "required_and_properties",
        "garment_id_pattern",
    ],
)
def test_repository_rejects_coordinated_schema_row_and_receipt_drift(
    project_root: Path,
    tmp_path: Path,
    case: str,
) -> None:
    repository_root = _copy_repository_root(project_root, tmp_path / case)
    fixture_path = repository_root / EXTENSION
    schema_path = repository_root / EXTENSION_SCHEMA
    rows = _read_jsonl(fixture_path)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    properties = schema["properties"]

    if case == "data_version_const":
        for row in rows:
            row["data_version"] = "fixtures_s16_coordinated_drift"
        properties["data_version"]["const"] = "fixtures_s16_coordinated_drift"
    elif case == "source_id_const":
        for row in rows:
            row["source_id"] = "coordinated_source"
        properties["source_id"]["const"] = "coordinated_source"
    elif case == "synthetic_const":
        for row in rows:
            row["synthetic"] = False
        properties["synthetic"]["const"] = False
    elif case == "audience_enum":
        rows[0]["audience"] = "menswear"
        properties["audience"]["enum"].append("menswear")
    elif case == "additional_property":
        rows[0]["coordinated_extra"] = "schema-and-row-agree"
        schema["additionalProperties"] = True
    elif case == "required_and_properties":
        for row in rows:
            row.pop("name")
        schema["required"].remove("name")
        properties.pop("name")
    else:
        rows[0]["garment_id"] = "g999"
        properties["garment_id"]["pattern"] = r"^g[0-9]{3}$"

    _write_jsonl(fixture_path, rows)
    schema_path.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _refresh_extension_manifest_receipts(repository_root, rows)

    # Prove this is coordinated drift rather than a stale-receipt/schema failure.
    rebound_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(rebound_schema)
    assert all(not list(validator.iter_errors(row)) for row in rows)
    rebound_manifest = json.loads(
        (repository_root / EXTENSION_MANIFEST).read_text(encoding="utf-8")
    )
    assert rebound_manifest["files"]["garments_extension"]["sha256"] == _sha256(
        fixture_path
    )
    assert rebound_manifest["files"]["schema"]["sha256"] == _sha256(schema_path)
    assert rebound_manifest["counts"] == {
        "added_owners": ADDED_OWNER_COUNTS,
        "added_slots": ADDED_SLOT_COUNTS,
        "added_owner_slots": ADDED_OWNER_SLOT_COUNTS,
        "final_owners": EXPECTED_OWNER_COUNTS,
        "final_slots": EXPECTED_SLOT_COUNTS,
        "final_owner_slots": EXPECTED_OWNER_SLOT_COUNTS,
        "total_added": 70,
        "total_garments": 120,
    }

    repository = FixtureRepository.__new__(FixtureRepository)
    with pytest.raises(
        RuntimeError,
        match=(
            "S16 garment overlay (?:schema contract|row identity) drifted"
        ),
    ):
        repository.__init__(repository_root)
    assert not hasattr(repository, "_garments")
