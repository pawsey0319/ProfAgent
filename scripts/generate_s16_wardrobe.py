from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path


SEED = 20260729
DATA_VERSION = "fixtures_s16_womenswear_v1"
SOURCE_ID = "fixtures_s16"
ROOT = Path(__file__).resolve().parents[1]
EXTENSION_PATH = ROOT / "data" / "fixtures" / "garments_s16_womenswear.jsonl"
SCHEMA_PATH = ROOT / "data" / "schemas" / "garment_s16.schema.json"
MANIFEST_PATH = ROOT / "data" / "manifests" / "fixtures_s16_womenswear_v1.json"

BASE_HASHES = {
    "data/fixtures/users.jsonl": "a47a1225037834c85b890fa06011e43ba69d485c9fcf419935fd027d5eaff466",
    "data/fixtures/garments.jsonl": "b9f6d85ace1dc31079b304d342c37a29ba9f6362d63ca3cd548e62f7226be001",
    "data/fixtures/outfits.jsonl": "cf2a439194d4bf209a3fa2289903271c95cb36424dcdb5b772d22e37550045a2",
    "data/fixtures/catalog.jsonl": "c56e47f700b38be3053aeb476d116c3ecfcac0c0784e6c2a6c92a3eae0ce33af",
    "data/eval/eval.jsonl": "a534e04346a60b837528248e38891d9e17a1024895f36aea9433e209409bff66",
}
OWNER_SLOT_ADDITIONS = {
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
EXPECTED_ADDED_OWNER_COUNTS = {"u01": 44, "u02": 12, "u03": 14}
EXPECTED_ADDED_SLOT_COUNTS = {
    "top": 14,
    "bottom": 15,
    "dress": 11,
    "outer": 11,
    "shoes": 10,
    "bag": 4,
    "accessory": 5,
}
EXPECTED_FINAL_OWNER_COUNTS = {"u01": 72, "u02": 24, "u03": 24}
EXPECTED_FINAL_SLOT_COUNTS = {
    "top": 24,
    "bottom": 24,
    "dress": 16,
    "outer": 18,
    "shoes": 18,
    "bag": 10,
    "accessory": 10,
}

NAMES = {
    "top": (
        "真丝飘带衬衫",
        "细针织方领上衣",
        "垂感通勤衬衣",
        "柔棉修身打底衫",
        "泡泡袖约会上衣",
        "简约无袖针织背心",
        "商务翻领衬衫",
        "轻盈亚麻衬衣",
        "运动拉链短上衣",
        "复古圆领开衫",
        "宴会光泽上衣",
        "旅行防晒衬衫",
        "法式领口针织衫",
        "利落半高领上衣",
    ),
    "bottom": (
        "高腰直筒西装裤",
        "垂感阔腿通勤裤",
        "中长百褶半身裙",
        "简洁铅笔半身裙",
        "复古牛仔直筒裤",
        "柔软针织半身裙",
        "轻量旅行束脚裤",
        "运动弹力长裤",
        "高腰伞摆半身裙",
        "九分烟管西装裤",
        "亚麻宽腿长裤",
        "宴会缎面半身裙",
        "工装直筒长裤",
        "通勤开衩半身裙",
        "日常锥形休闲裤",
    ),
    "dress": (
        "收腰通勤连衣裙",
        "简洁衬衫连衣裙",
        "法式约会茶歇裙",
        "商务直筒连衣裙",
        "旅行亚麻连衣裙",
        "柔软针织连衣裙",
        "宴会缎面长裙",
        "复古方领连衣裙",
        "运动翻领连衣裙",
        "日常伞摆连衣裙",
        "会议西装连衣裙",
    ),
    "outer": (
        "双排扣通勤西装",
        "轻量防风旅行外套",
        "短款软呢夹克",
        "经典收腰风衣",
        "羊毛廓形大衣",
        "运动连帽外套",
        "宴会短款披肩",
        "亚麻单排扣西装",
        "复古牛仔夹克",
        "简约针织长开衫",
        "正式会议西装外套",
    ),
    "shoes": (
        "低跟通勤乐福鞋",
        "尖头中跟单鞋",
        "舒适芭蕾平底鞋",
        "轻量旅行运动鞋",
        "宴会细带凉鞋",
        "复古玛丽珍鞋",
        "简洁短靴",
        "运动缓震跑鞋",
        "方头低跟穆勒鞋",
        "商务皮质德比鞋",
    ),
    "bag": (
        "结构感通勤托特包",
        "轻便旅行斜挎包",
        "宴会链条手拿包",
        "日常软皮肩背包",
    ),
    "accessory": (
        "真丝几何方巾",
        "简约金属耳饰",
        "复古细腰带",
        "运动遮阳帽",
        "宴会珍珠项链",
    ),
}

COLORS = (
    "navy",
    "white",
    "beige",
    "black",
    "gray",
    "blue",
    "brown",
    "khaki",
    "green",
    "red",
    "pink",
    "purple",
    "orange",
    "yellow",
)
COLOR_LABELS = {
    "navy": "藏青色",
    "white": "白色",
    "beige": "米色",
    "black": "黑色",
    "gray": "灰色",
    "blue": "蓝色",
    "brown": "棕色",
    "khaki": "卡其色",
    "green": "绿色",
    "red": "红色",
    "pink": "粉色",
    "purple": "紫色",
    "orange": "橙色",
    "yellow": "黄色",
    "multi": "多色",
}
PROFILE_CYCLE = (
    (("commute", "meeting"), ("business", "smart"), 3),
    (("interview", "meeting"), ("formal", "classic"), 4),
    (("date", "daily"), ("soft", "classic"), 2),
    (("daily", "travel"), ("simple", "street"), 1),
    (("sports", "daily"), ("sporty", "simple"), 0),
    (("party", "date"), ("formal", "vintage"), 4),
    (("travel", "outdoor"), ("sporty", "street"), 1),
)
U01_LIFESTYLE_PROFILES = (
    (("date", "party"), ("vintage", "street"), 0),
    (("travel", "outdoor"), ("street", "vintage"), 0),
    (("party", "date"), ("street", "vintage"), 0),
    (("outdoor", "travel"), ("vintage", "street"), 0),
)
OWNER_AVOID_COLOR = {"u01": "purple", "u02": "yellow", "u03": "orange"}
U01_SLOT_COLORS = {
    "top": "navy",
    "bottom": "beige",
    "dress": "red",
    "outer": "gray",
    "shoes": "black",
    "bag": "brown",
    "accessory": "green",
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
SEASON_CYCLE = (
    ("all",),
    ("spring", "autumn"),
    ("summer",),
    ("autumn", "winter"),
    ("spring", "summer"),
    ("winter",),
)
SEASON_LABELS = {
    "all": "四季",
    "spring": "春季",
    "summer": "夏季",
    "autumn": "秋季",
    "winter": "冬季",
}
MATERIALS_BY_SLOT = {
    "top": ("cotton", "knit", "linen", "synthetic"),
    "bottom": ("cotton", "wool", "denim", "linen", "synthetic"),
    "dress": ("cotton", "knit", "linen", "synthetic"),
    "outer": ("wool", "cotton", "denim", "knit", "synthetic"),
    "shoes": ("leather", "synthetic", "cotton"),
    "bag": ("leather", "synthetic", "cotton"),
    "accessory": ("synthetic", "leather", "cotton", "knit"),
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
FITS = ("regular", "straight", "loose", "slim")
FIT_LABELS = {
    "regular": "常规版型",
    "straight": "直筒版型",
    "loose": "宽松版型",
    "slim": "修身版型",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "garment_s16.schema.json",
        "title": "S16 Womenswear Garment Extension",
        "type": "object",
        "additionalProperties": False,
        "required": [
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
        ],
        "properties": {
            "schema_version": {"const": 1},
            "data_version": {"const": DATA_VERSION},
            "source_id": {"const": SOURCE_ID},
            "synthetic": {"const": True},
            "garment_id": {
                "type": "string",
                "pattern": r"^g(?:0(?:5[1-9]|[6-9][0-9])|1(?:0[0-9]|1[0-9]|20))$",
            },
            "user_id": {"enum": ["u01", "u02", "u03"]},
            "name": {"type": "string", "minLength": 1, "maxLength": 30},
            "slot": {
                "enum": [
                    "outer",
                    "top",
                    "bottom",
                    "dress",
                    "shoes",
                    "bag",
                    "accessory",
                ]
            },
            "color": {
                "enum": [
                    "black",
                    "white",
                    "gray",
                    "navy",
                    "beige",
                    "brown",
                    "khaki",
                    "red",
                    "pink",
                    "orange",
                    "yellow",
                    "green",
                    "blue",
                    "purple",
                    "multi",
                ]
            },
            "seasons": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {
                    "enum": ["spring", "summer", "autumn", "winter", "all"]
                },
            },
            "styles": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {
                    "enum": [
                        "simple",
                        "classic",
                        "smart",
                        "formal",
                        "street",
                        "sporty",
                        "soft",
                        "vintage",
                        "business",
                        "campus",
                    ]
                },
            },
            "occasions": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {
                    "enum": [
                        "daily",
                        "commute",
                        "interview",
                        "meeting",
                        "date",
                        "party",
                        "travel",
                        "outdoor",
                        "home",
                        "sports",
                    ]
                },
            },
            "status": {
                "enum": ["available", "laundry", "reserved", "unavailable"]
            },
            "formal": {"type": "integer", "minimum": 0, "maximum": 4},
            "warmth": {"type": "integer", "minimum": 1, "maximum": 5},
            "material": {
                "enum": [
                    "cotton",
                    "knit",
                    "denim",
                    "wool",
                    "linen",
                    "leather",
                    "synthetic",
                ]
            },
            "fit": {"enum": ["slim", "regular", "loose", "straight"]},
            "search_text": {"type": "string", "minLength": 2, "maxLength": 100},
            "audience": {
                "enum": ["womenswear", "unisex_womenswear_compatible"]
            },
        },
    }


def _build_rows() -> list[dict[str, object]]:
    name_offsets = {slot: 0 for slot in NAMES}
    rows: list[dict[str, object]] = []
    for user_id, allocation in OWNER_SLOT_ADDITIONS.items():
        for slot, count in allocation.items():
            for _ in range(count):
                index = len(rows)
                name_index = name_offsets[slot]
                name = NAMES[slot][name_index]
                name_offsets[slot] += 1
                color_index = (index + name_index) % len(COLORS)
                color = (
                    U01_SLOT_COLORS[slot]
                    if user_id == "u01"
                    else COLORS[color_index]
                )
                if color == OWNER_AVOID_COLOR[user_id]:
                    color = COLORS[(color_index + 1) % len(COLORS)]
                seasons = SEASON_CYCLE[(index + name_index) % len(SEASON_CYCLE)]
                if user_id == "u01":
                    occasions, styles, formal = U01_LIFESTYLE_PROFILES[
                        index % len(U01_LIFESTYLE_PROFILES)
                    ]
                else:
                    occasions, styles, formal = PROFILE_CYCLE[
                        index % len(PROFILE_CYCLE)
                    ]
                if slot == "shoes" and user_id == "u01":
                    occasions, styles, formal = (
                        ("date", "party"),
                        ("classic", "formal"),
                        2,
                    )
                materials = MATERIALS_BY_SLOT[slot]
                material = materials[(index + name_index) % len(materials)]
                fit = FITS[(index + name_index) % len(FITS)]
                warmth = 5 if seasons == ("winter",) else 4 if "winter" in seasons else 2
                audience = (
                    "unisex_womenswear_compatible"
                    if slot in {"top", "outer", "shoes", "bag", "accessory"}
                    and index % 4 == 0
                    else "womenswear"
                )
                season_text = "、".join(SEASON_LABELS[item] for item in seasons)
                occasion_text = "、".join(
                    OCCASION_LABELS[item] for item in occasions
                )
                shoe_usage_text = (
                    "，兼顾通勤久走、长时间站立与舒适需求"
                    if slot == "shoes" and user_id == "u01"
                    else ""
                )
                rows.append(
                    {
                        "schema_version": 1,
                        "data_version": DATA_VERSION,
                        "source_id": SOURCE_ID,
                        "synthetic": True,
                        "garment_id": f"g{index + 51:03d}",
                        "user_id": user_id,
                        "name": name,
                        "slot": slot,
                        "color": color,
                        "seasons": list(seasons),
                        "styles": list(styles),
                        "occasions": list(occasions),
                        "status": "available",
                        "formal": formal,
                        "warmth": warmth,
                        "material": material,
                        "fit": fit,
                        "search_text": (
                            f"{name}，{COLOR_LABELS[color]}，{season_text}，"
                            f"{MATERIAL_LABELS[material]}，{FIT_LABELS[fit]}，"
                            f"适合{occasion_text}{shoe_usage_text}"
                        ),
                        "audience": audience,
                    }
                )
    return rows


def _assert_contract(rows: list[dict[str, object]]) -> None:
    if [row["garment_id"] for row in rows] != [
        f"g{index:03d}" for index in range(51, 121)
    ]:
        raise RuntimeError("S16 garment IDs are not the frozen g051-g120 sequence")
    if Counter(row["user_id"] for row in rows) != Counter(
        EXPECTED_ADDED_OWNER_COUNTS
    ):
        raise RuntimeError("S16 owner allocation drifted")
    if Counter(row["slot"] for row in rows) != Counter(EXPECTED_ADDED_SLOT_COUNTS):
        raise RuntimeError("S16 slot allocation drifted")
    if len({row["name"] for row in rows}) != len(rows):
        raise RuntimeError("S16 garment names must be unique")


def main() -> None:
    for relative, expected in BASE_HASHES.items():
        path = ROOT / relative
        if not path.is_file() or _sha256(path) != expected:
            raise RuntimeError(f"immutable baseline mismatch: {relative}")

    rows = _build_rows()
    _assert_contract(rows)
    schema = _schema()
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_json(SCHEMA_PATH, schema)
    EXTENSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXTENSION_PATH.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )
    manifest = {
        "schema_version": 1,
        "data_version": DATA_VERSION,
        "source_id": SOURCE_ID,
        "seed": SEED,
        "base": {
            "data_version": "fixtures_v1.0",
            "files": {
                relative: {"path": relative, "sha256": digest}
                for relative, digest in BASE_HASHES.items()
            },
        },
        "files": {
            "garments_extension": {
                "path": EXTENSION_PATH.relative_to(ROOT).as_posix(),
                "count": len(rows),
                "sha256": _sha256(EXTENSION_PATH),
            },
            "schema": {
                "path": SCHEMA_PATH.relative_to(ROOT).as_posix(),
                "sha256": _sha256(SCHEMA_PATH),
            },
        },
        "counts": {
            "added_owners": EXPECTED_ADDED_OWNER_COUNTS,
            "added_slots": EXPECTED_ADDED_SLOT_COUNTS,
            "final_owners": EXPECTED_FINAL_OWNER_COUNTS,
            "final_slots": EXPECTED_FINAL_SLOT_COUNTS,
            "total_added": len(rows),
            "total_garments": 120,
        },
        "audiences": ["womenswear", "unisex_womenswear_compatible"],
    }
    _write_json(MANIFEST_PATH, manifest)
    print(
        "generated S16 womenswear extension: "
        f"garments={len(rows)} sha256={_sha256(EXTENSION_PATH)}"
    )


if __name__ == "__main__":
    main()
