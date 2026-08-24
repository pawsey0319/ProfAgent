from __future__ import annotations

from typing import Any


# Server-owned, closed product truth for the S16 womenswear extension.  These
# values are the only product nouns sent to CPA image or Vision providers.
CATALOG_PRODUCT_TYPE_BY_GARMENT_ID: dict[str, str] = {
    "g051": "tie-neck blouse", "g052": "square-neck knit top",
    "g053": "draped button-up shirt", "g054": "fitted base-layer top",
    "g055": "puff-sleeve blouse", "g056": "sleeveless knit vest",
    "g057": "collared dress shirt", "g058": "linen shirt",
    "g059": "tailored straight-leg trousers", "g060": "wide-leg trousers",
    "g061": "pleated midi skirt", "g062": "pencil skirt",
    "g063": "straight-leg jeans", "g064": "knit midi skirt",
    "g065": "jogger trousers", "g066": "athletic trousers",
    "g067": "a-line midi skirt", "g068": "waist-fitted work dress",
    "g069": "shirt dress", "g070": "tea dress", "g071": "sheath work dress",
    "g072": "linen travel dress", "g073": "knit dress",
    "g074": "satin evening gown", "g075": "double-breasted blazer",
    "g076": "windbreaker jacket", "g077": "cropped boucle jacket",
    "g078": "trench coat", "g079": "oversized wool overcoat",
    "g080": "hooded sports jacket", "g081": "evening shawl",
    "g082": "low-heel loafers", "g083": "pointed-toe pumps",
    "g084": "ballet flats", "g085": "travel sneakers",
    "g086": "strappy sandals", "g087": "mary jane shoes",
    "g088": "ankle boots", "g089": "structured tote bag",
    "g090": "travel crossbody bag", "g091": "chain clutch bag",
    "g092": "square neck scarf", "g093": "resin earrings",
    "g094": "slim leather belt", "g095": "zip-up crop top",
    "g096": "crew-neck cardigan", "g097": "satin evening blouse",
    "g098": "cigarette trousers", "g099": "linen wide-leg trousers",
    "g100": "satin midi skirt", "g101": "square-neck dress",
    "g102": "polo sports dress", "g103": "linen blazer",
    "g104": "denim jacket", "g105": "running shoes",
    "g106": "low-heel mule shoes", "g107": "sun-protection shirt",
    "g108": "knit sweater", "g109": "mock-neck knit top",
    "g110": "cargo trousers", "g111": "slit midi skirt",
    "g112": "tapered casual trousers", "g113": "a-line day dress",
    "g114": "tailored blazer dress", "g115": "long knit cardigan",
    "g116": "formal blazer", "g117": "leather derby shoes",
    "g118": "soft leather shoulder bag", "g119": "sun hat",
    "g120": "faux pearl necklace",
}

CATALOG_PRODUCT_TYPES = frozenset(CATALOG_PRODUCT_TYPE_BY_GARMENT_ID.values())


def validate_catalog_product_type(value: Any) -> str:
    if not isinstance(value, str) or value not in CATALOG_PRODUCT_TYPES:
        raise ValueError("invalid_catalog_product_type")
    return value


def product_type_for_garment(garment_id: Any) -> str:
    if not isinstance(garment_id, str):
        raise ValueError("invalid_catalog_product_type")
    try:
        return CATALOG_PRODUCT_TYPE_BY_GARMENT_ID[garment_id]
    except KeyError:
        raise ValueError("invalid_catalog_product_type") from None


__all__ = [
    "CATALOG_PRODUCT_TYPE_BY_GARMENT_ID",
    "CATALOG_PRODUCT_TYPES",
    "product_type_for_garment",
    "validate_catalog_product_type",
]
