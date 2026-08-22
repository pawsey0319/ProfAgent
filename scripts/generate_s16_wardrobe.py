from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
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
EXPECTED_FINAL_OWNER_SLOT_COUNTS = {
    "u01": {"top": 14, "bottom": 14, "dress": 10, "outer": 11, "shoes": 11, "bag": 6, "accessory": 6},
    "u02": {"top": 5, "bottom": 5, "dress": 3, "outer": 4, "shoes": 4, "bag": 2, "accessory": 1},
    "u03": {"top": 5, "bottom": 5, "dress": 3, "outer": 3, "shoes": 3, "bag": 2, "accessory": 3},
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
SEASON_LABELS = {
    "all": "四季",
    "spring": "春季",
    "summer": "夏季",
    "autumn": "秋季",
    "winter": "冬季",
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

COMFORT_LOOSE = "宽松版型便于活动，仍需按场景确认舒适度"
COMFORT_SLIM = "修身版型活动前需确认贴合与舒适度"
COMFORT_REGULAR = "常规活动前需确认穿着舒适度"
COMFORT_WALK = "适合一般步行，长时间活动前需确认舒适度"
COMFORT_LONG_WALK = "缓震平底结构，适合久走与长时间站立"
COMFORT_SHORT_SHOE = "鞋跟或细带结构适合短时活动，长时间活动前需另行确认"


@dataclass(frozen=True)
class GarmentProfile:
    name: str
    material: str
    seasons: tuple[str, ...]
    styles: tuple[str, ...]
    occasions: tuple[str, ...]
    formal: int
    warmth: int
    fit: str
    comfort: str


P = GarmentProfile
GARMENT_PROFILES = {
    "top": (
        P("飘带衬衫", "synthetic", ("spring", "summer"), ("soft", "smart"), ("date", "meeting"), 2, 1, "regular", COMFORT_REGULAR),
        P("细针织方领上衣", "knit", ("autumn", "winter"), ("soft", "classic"), ("date", "daily"), 2, 3, "slim", COMFORT_SLIM),
        P("垂感通勤衬衣", "cotton", ("all",), ("smart", "business"), ("commute", "meeting"), 3, 2, "regular", COMFORT_REGULAR),
        P("柔棉修身打底衫", "cotton", ("autumn", "winter"), ("simple", "soft"), ("daily", "home"), 1, 3, "slim", COMFORT_SLIM),
        P("泡泡袖约会上衣", "cotton", ("spring", "summer"), ("soft", "vintage"), ("date", "party"), 2, 1, "regular", COMFORT_REGULAR),
        P("简约无袖针织背心", "knit", ("spring", "summer"), ("simple", "soft"), ("daily", "travel"), 1, 1, "regular", COMFORT_REGULAR),
        P("商务翻领衬衫", "cotton", ("all",), ("business", "smart"), ("commute", "meeting", "interview"), 4, 2, "regular", COMFORT_REGULAR),
        P("轻盈亚麻衬衣", "linen", ("spring", "summer"), ("simple", "smart"), ("travel", "daily"), 1, 1, "loose", COMFORT_LOOSE),
        P("运动拉链短上衣", "synthetic", ("all",), ("sporty", "simple"), ("sports", "daily", "travel"), 0, 1, "regular", COMFORT_REGULAR),
        P("复古圆领开衫", "knit", ("autumn", "winter"), ("vintage", "soft"), ("date", "daily"), 2, 3, "regular", COMFORT_REGULAR),
        P("宴会光泽上衣", "synthetic", ("all",), ("formal", "soft"), ("party", "date"), 4, 2, "slim", COMFORT_SLIM),
        P("旅行防晒衬衫", "synthetic", ("spring", "summer"), ("sporty", "simple"), ("travel", "outdoor", "sports"), 0, 1, "loose", COMFORT_LOOSE),
        P("法式领口针织衫", "knit", ("spring", "autumn"), ("soft", "classic"), ("date", "daily"), 2, 2, "regular", COMFORT_REGULAR),
        P("利落半高领上衣", "knit", ("autumn", "winter"), ("smart", "business"), ("commute", "meeting"), 3, 3, "slim", COMFORT_SLIM),
    ),
    "bottom": (
        P("高腰直筒西装裤", "wool", ("all",), ("business", "formal"), ("commute", "meeting", "interview"), 4, 2, "straight", COMFORT_REGULAR),
        P("垂感阔腿通勤裤", "synthetic", ("all",), ("smart", "business"), ("commute", "meeting"), 3, 2, "loose", COMFORT_LOOSE),
        P("中长百褶半身裙", "synthetic", ("spring", "autumn"), ("classic", "soft"), ("commute", "date"), 2, 2, "regular", COMFORT_REGULAR),
        P("简洁铅笔半身裙", "wool", ("autumn", "winter"), ("business", "formal"), ("meeting", "interview"), 4, 4, "slim", COMFORT_SLIM),
        P("复古牛仔直筒裤", "denim", ("all",), ("vintage", "street"), ("daily", "travel"), 1, 2, "straight", COMFORT_REGULAR),
        P("柔软针织半身裙", "knit", ("autumn", "winter"), ("soft", "simple"), ("daily", "date"), 1, 3, "regular", COMFORT_REGULAR),
        P("轻量旅行束脚裤", "synthetic", ("spring", "summer", "autumn"), ("sporty", "simple"), ("travel", "outdoor", "sports"), 0, 1, "regular", COMFORT_REGULAR),
        P("运动弹力长裤", "synthetic", ("all",), ("sporty", "simple"), ("sports", "daily"), 0, 1, "slim", COMFORT_SLIM),
        P("高腰伞摆半身裙", "cotton", ("spring", "summer"), ("soft", "vintage"), ("date", "daily"), 2, 1, "regular", COMFORT_REGULAR),
        P("九分烟管西装裤", "wool", ("all",), ("business", "smart"), ("commute", "meeting", "interview"), 4, 2, "straight", COMFORT_REGULAR),
        P("亚麻宽腿长裤", "linen", ("spring", "summer"), ("simple", "soft"), ("travel", "daily"), 1, 1, "loose", COMFORT_LOOSE),
        P("宴会缎面半身裙", "synthetic", ("spring", "summer", "autumn"), ("formal", "soft"), ("party", "date"), 4, 1, "regular", COMFORT_REGULAR),
        P("工装直筒长裤", "cotton", ("all",), ("street", "simple"), ("daily", "outdoor", "travel"), 1, 2, "straight", COMFORT_REGULAR),
        P("通勤开衩半身裙", "wool", ("autumn", "winter"), ("business", "smart"), ("commute", "meeting"), 3, 4, "slim", COMFORT_SLIM),
        P("日常锥形休闲裤", "cotton", ("all",), ("simple", "smart"), ("daily", "commute"), 1, 2, "regular", COMFORT_REGULAR),
    ),
    "dress": (
        P("收腰通勤连衣裙", "wool", ("all",), ("business", "smart"), ("commute", "meeting"), 3, 2, "slim", COMFORT_SLIM),
        P("简洁衬衫连衣裙", "cotton", ("spring", "summer"), ("smart", "simple"), ("commute", "daily"), 2, 1, "straight", COMFORT_REGULAR),
        P("法式约会茶歇裙", "synthetic", ("spring", "summer"), ("soft", "vintage"), ("date", "party"), 2, 1, "slim", COMFORT_SLIM),
        P("商务直筒连衣裙", "wool", ("autumn", "winter"), ("business", "formal"), ("meeting", "interview", "commute"), 4, 4, "straight", COMFORT_REGULAR),
        P("旅行亚麻连衣裙", "linen", ("spring", "summer"), ("simple", "soft"), ("travel", "daily"), 1, 1, "loose", COMFORT_LOOSE),
        P("柔软针织连衣裙", "knit", ("autumn", "winter"), ("soft", "simple"), ("daily", "date"), 1, 3, "regular", COMFORT_REGULAR),
        P("宴会缎面长裙", "synthetic", ("spring", "autumn"), ("formal", "soft"), ("party", "date"), 4, 2, "slim", COMFORT_SLIM),
        P("复古方领连衣裙", "cotton", ("spring", "summer"), ("vintage", "soft"), ("date", "party"), 2, 1, "regular", COMFORT_REGULAR),
        P("运动翻领连衣裙", "synthetic", ("spring", "summer"), ("sporty", "simple"), ("sports", "daily"), 0, 1, "regular", COMFORT_REGULAR),
        P("日常伞摆连衣裙", "cotton", ("spring", "summer"), ("soft", "simple"), ("daily", "date"), 1, 1, "regular", COMFORT_REGULAR),
        P("会议西装连衣裙", "wool", ("autumn", "winter"), ("business", "formal"), ("meeting", "interview"), 4, 4, "straight", COMFORT_REGULAR),
    ),
    "outer": (
        P("双排扣通勤西装", "wool", ("autumn", "winter"), ("business", "formal"), ("commute", "meeting", "interview"), 4, 4, "regular", COMFORT_REGULAR),
        P("轻量防风旅行外套", "synthetic", ("spring", "summer", "autumn"), ("sporty", "simple"), ("travel", "outdoor", "sports"), 0, 1, "loose", COMFORT_LOOSE),
        P("短款软呢夹克", "wool", ("autumn", "winter"), ("classic", "smart"), ("commute", "daily"), 2, 4, "regular", COMFORT_REGULAR),
        P("经典收腰风衣", "cotton", ("spring", "autumn"), ("classic", "smart"), ("commute", "travel"), 3, 2, "slim", COMFORT_SLIM),
        P("羊毛廓形大衣", "wool", ("winter",), ("classic", "formal"), ("commute", "meeting"), 4, 5, "loose", COMFORT_LOOSE),
        P("运动连帽外套", "synthetic", ("all",), ("sporty", "simple"), ("sports", "daily", "travel"), 0, 2, "loose", COMFORT_LOOSE),
        P("宴会短款披肩", "synthetic", ("autumn", "winter"), ("formal", "soft"), ("party", "date"), 4, 4, "regular", COMFORT_REGULAR),
        P("亚麻单排扣西装", "linen", ("spring", "summer"), ("business", "smart"), ("commute", "meeting"), 3, 1, "regular", COMFORT_REGULAR),
        P("复古牛仔夹克", "denim", ("all",), ("vintage", "street"), ("daily", "travel"), 1, 2, "loose", COMFORT_LOOSE),
        P("简约针织长开衫", "knit", ("autumn", "winter"), ("simple", "soft"), ("daily", "home"), 1, 3, "loose", COMFORT_LOOSE),
        P("正式会议西装外套", "wool", ("all",), ("business", "formal"), ("meeting", "interview", "commute"), 4, 3, "regular", COMFORT_REGULAR),
    ),
    "shoes": (
        P("低跟通勤乐福鞋", "leather", ("all",), ("business", "classic"), ("commute", "meeting"), 3, 1, "regular", COMFORT_WALK),
        P("尖头中跟单鞋", "leather", ("spring", "autumn"), ("formal", "classic"), ("meeting", "date", "party"), 4, 1, "slim", COMFORT_SHORT_SHOE),
        P("舒适芭蕾平底鞋", "leather", ("spring", "summer", "autumn"), ("soft", "classic"), ("daily", "date", "travel"), 1, 1, "regular", COMFORT_LONG_WALK),
        P("轻量旅行运动鞋", "synthetic", ("all",), ("sporty", "simple"), ("travel", "sports", "outdoor"), 0, 1, "regular", COMFORT_LONG_WALK),
        P("宴会细带凉鞋", "synthetic", ("spring", "summer"), ("formal", "soft"), ("party", "date"), 4, 1, "slim", COMFORT_SHORT_SHOE),
        P("复古玛丽珍鞋", "leather", ("spring", "autumn"), ("vintage", "classic"), ("date", "daily"), 2, 1, "regular", COMFORT_WALK),
        P("简洁短靴", "leather", ("autumn", "winter"), ("classic", "smart"), ("commute", "daily"), 3, 4, "regular", COMFORT_WALK),
        P("运动缓震跑鞋", "synthetic", ("all",), ("sporty", "simple"), ("sports", "travel", "outdoor"), 0, 1, "regular", COMFORT_LONG_WALK),
        P("方头低跟穆勒鞋", "leather", ("spring", "summer"), ("classic", "smart"), ("commute", "daily"), 2, 1, "regular", COMFORT_WALK),
        P("商务皮质德比鞋", "leather", ("all",), ("business", "formal"), ("commute", "meeting", "interview"), 4, 2, "regular", COMFORT_WALK),
    ),
    "bag": (
        P("结构感通勤托特包", "leather", ("all",), ("business", "smart"), ("commute", "meeting"), 3, 2, "regular", COMFORT_REGULAR),
        P("轻便旅行斜挎包", "synthetic", ("all",), ("simple", "sporty"), ("travel", "daily", "sports"), 0, 1, "regular", COMFORT_REGULAR),
        P("宴会链条手拿包", "synthetic", ("all",), ("formal", "vintage"), ("party", "date"), 4, 1, "slim", COMFORT_SLIM),
        P("日常软皮肩背包", "leather", ("all",), ("simple", "soft"), ("daily", "commute"), 1, 1, "regular", COMFORT_REGULAR),
    ),
    "accessory": (
        P("几何方巾", "synthetic", ("spring", "autumn"), ("smart", "vintage"), ("commute", "date"), 2, 1, "regular", COMFORT_REGULAR),
        P("简约金属耳饰", "synthetic", ("all",), ("simple", "smart"), ("daily", "meeting"), 2, 1, "regular", COMFORT_REGULAR),
        P("复古细腰带", "leather", ("all",), ("vintage", "classic"), ("daily", "date"), 2, 1, "slim", COMFORT_SLIM),
        P("运动遮阳帽", "synthetic", ("spring", "summer"), ("sporty", "simple"), ("sports", "outdoor", "travel"), 0, 1, "regular", COMFORT_REGULAR),
        P("宴会珍珠项链", "synthetic", ("all",), ("formal", "classic"), ("party", "date"), 4, 1, "regular", COMFORT_REGULAR),
    ),
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
            "search_text": {"type": "string", "minLength": 2, "maxLength": 220},
            "audience": {
                "enum": ["womenswear", "unisex_womenswear_compatible"]
            },
        },
    }


def _build_rows() -> list[dict[str, object]]:
    profile_offsets = {slot: 0 for slot in GARMENT_PROFILES}
    rows: list[dict[str, object]] = []
    for user_id, allocation in OWNER_SLOT_ADDITIONS.items():
        for slot, count in allocation.items():
            for _ in range(count):
                index = len(rows)
                profile_index = profile_offsets[slot]
                profile = GARMENT_PROFILES[slot][profile_index]
                profile_offsets[slot] += 1
                color_index = (index + profile_index) % len(COLORS)
                color = (
                    U01_SLOT_COLORS[slot]
                    if user_id == "u01"
                    else COLORS[color_index]
                )
                if color == OWNER_AVOID_COLOR[user_id]:
                    color = COLORS[(color_index + 1) % len(COLORS)]
                audience = (
                    "unisex_womenswear_compatible"
                    if slot in {"top", "outer", "shoes", "bag", "accessory"}
                    and index % 4 == 0
                    else "womenswear"
                )
                season_text = "、".join(
                    SEASON_LABELS[item] for item in profile.seasons
                )
                style_text = "、".join(
                    STYLE_LABELS[item] for item in profile.styles
                )
                occasion_text = "、".join(
                    OCCASION_LABELS[item] for item in profile.occasions
                )
                rows.append(
                    {
                        "schema_version": 1,
                        "data_version": DATA_VERSION,
                        "source_id": SOURCE_ID,
                        "synthetic": True,
                        "garment_id": f"g{index + 51:03d}",
                        "user_id": user_id,
                        "name": profile.name,
                        "slot": slot,
                        "color": color,
                        "seasons": list(profile.seasons),
                        "styles": list(profile.styles),
                        "occasions": list(profile.occasions),
                        "status": "available",
                        "formal": profile.formal,
                        "warmth": profile.warmth,
                        "material": profile.material,
                        "fit": profile.fit,
                        "search_text": (
                            f"{profile.name}；颜色：{COLOR_LABELS[color]}；"
                            f"季节：{season_text}；材质：{MATERIAL_LABELS[profile.material]}；"
                            f"版型：{FIT_LABELS[profile.fit]}；风格：{style_text}；"
                            f"场景：{occasion_text}；正式度：{profile.formal}/4；"
                            f"保暖度：{profile.warmth}/5；舒适说明：{profile.comfort}"
                        ),
                        "audience": audience,
                    }
                )
    return rows


def _assert_contract(rows: list[dict[str, object]]) -> None:
    if {slot: len(profiles) for slot, profiles in GARMENT_PROFILES.items()} != (
        EXPECTED_ADDED_SLOT_COUNTS
    ):
        raise RuntimeError("S16 declarative profile counts drifted")
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
    added_owner_slots = {
        user_id: {
            slot: sum(
                row["user_id"] == user_id and row["slot"] == slot for row in rows
            )
            for slot in EXPECTED_ADDED_SLOT_COUNTS
        }
        for user_id in OWNER_SLOT_ADDITIONS
    }
    if added_owner_slots != OWNER_SLOT_ADDITIONS:
        raise RuntimeError("S16 owner-slot allocation drifted")


def _manifest(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
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
            "added_owner_slots": OWNER_SLOT_ADDITIONS,
            "final_owners": EXPECTED_FINAL_OWNER_COUNTS,
            "final_slots": EXPECTED_FINAL_SLOT_COUNTS,
            "final_owner_slots": EXPECTED_FINAL_OWNER_SLOT_COUNTS,
            "total_added": len(rows),
            "total_garments": 120,
        },
        "audiences": ["womenswear", "unisex_womenswear_compatible"],
    }


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
    manifest = _manifest(rows)
    _write_json(MANIFEST_PATH, manifest)
    print(
        "generated S16 womenswear extension: "
        f"garments={len(rows)} sha256={_sha256(EXTENSION_PATH)}"
    )


if __name__ == "__main__":
    main()
