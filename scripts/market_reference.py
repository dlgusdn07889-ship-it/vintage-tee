from __future__ import annotations

import math
import re
from statistics import median
from typing import Any

import requests

SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
MARKETPLACE_ID = "EBAY_US"
US_ZIP_CODE = "97250"
MAX_REFERENCE_RESULTS = 100

GENERIC_QUERY_WORDS = {
    "vintage", "shirt", "shirts", "tee", "tees", "tshirt", "tshirts",
    "t-shirt", "t-shirts", "band", "tour", "concert", "single", "stitch",
    "old", "graphic", "mens", "men", "women", "size", "large", "xl", "xxl",
    "90s", "1990s", "official", "original", "black", "usa", "made", "the",
    "and", "for", "with", "from",
}

REFERENCE_EXCLUDE_KEYWORDS = [
    "reprint", "re-print", "reproduction", "replica", "fake", "modern",
    "vintage style", "vintage inspired", "retro style", "print on demand",
    "made to order", "custom print", "custom made", "unofficial", "gildan",
    "comfort colors", "bella canvas", "bella+canvas", "next level", "tagless",
    "no tag", "printed tag", "tear away", "s-5xl", "s to 5xl", "choose size",
    "select size", "multiple sizes", "hoodie", "sweatshirt", "poster", "patch",
    "sticker", "kids", "youth", "toddler", "infant",
]

MODERN_YEAR_RE = re.compile(r"\b(?:200\d|201\d|202\d)\b|\b(?:00s|2000s|y2k)\b", re.I)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _parse_amount(data: Any) -> float | None:
    if not isinstance(data, dict):
        return None
    raw = data.get("value")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _shipping(item: dict[str, Any]) -> float | None:
    options = item.get("shippingOptions", [])
    if not isinstance(options, list):
        return None
    for option in options:
        value = _parse_amount(option.get("shippingCost"))
        if value is not None:
            return value
    return None


def _total(item: dict[str, Any]) -> float | None:
    price = _parse_amount(item.get("price"))
    ship = _shipping(item)
    if price is None or price <= 0 or ship is None:
        return None
    return price + ship


def _tokens(query: str) -> list[str]:
    return list(dict.fromkeys(
        token for token in _normalize(query).split()
        if len(token) >= 3 and token not in GENERIC_QUERY_WORDS
    ))


def _comparable(item: dict[str, Any], query_tokens: list[str]) -> bool:
    title = _normalize(str(item.get("title", "")))
    if not title:
        return False
    if any(keyword in title for keyword in REFERENCE_EXCLUDE_KEYWORDS):
        return False
    if MODERN_YEAR_RE.search(title):
        return False

    if query_tokens:
        hits = sum(1 for token in query_tokens if token in title)
        required = 1 if len(query_tokens) == 1 else 2
        if hits < required:
            return False
    return True


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return sorted_values[low]
    fraction = position - low
    return sorted_values[low] * (1 - fraction) + sorted_values[high] * fraction


def get_active_price_reference(
    *,
    token: str,
    query: str,
    maximum_results: int = MAX_REFERENCE_RESULTS,
) -> dict[str, Any] | None:
    """
    활성 즉시구매 매물의 보수적 가격 참고값.
    sold price가 아니므로 실제 체결가로 해석하면 안 된다.
    """
    response = requests.get(
        SEARCH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID,
            "X-EBAY-C-ENDUSERCTX": (
                f"contextualLocation=country%3DUS%2Czip%3D{US_ZIP_CODE}"
            ),
        },
        params={
            "q": query,
            "limit": min(maximum_results, MAX_REFERENCE_RESULTS),
            "filter": "buyingOptions:{FIXED_PRICE},price:[40..1500],priceCurrency:USD",
        },
        timeout=30,
    )
    response.raise_for_status()

    query_tokens = _tokens(query)
    totals: list[float] = []
    for item in response.json().get("itemSummaries", []):
        if not _comparable(item, query_tokens):
            continue
        total = _total(item)
        if total is not None:
            totals.append(total)

    if len(totals) < 3:
        return None

    totals.sort()

    if len(totals) >= 10:
        trim = max(1, int(len(totals) * 0.10))
        trimmed = totals[trim:-trim]
    else:
        trimmed = totals

    if len(trimmed) < 3:
        return None

    med = float(median(trimmed))
    p25 = _percentile(trimmed, 0.25)
    p40 = _percentile(trimmed, 0.40)
    p75 = _percentile(trimmed, 0.75)

    if med <= 0 or p40 <= 0:
        return None

    return {
        "median": round(med, 2),
        "conservative": round(p40, 2),
        "p25": round(p25, 2),
        "p75": round(p75, 2),
        "minimum": round(min(trimmed), 2),
        "maximum": round(max(trimmed), 2),
        "sample_count": len(trimmed),
        "query": query,
    }
