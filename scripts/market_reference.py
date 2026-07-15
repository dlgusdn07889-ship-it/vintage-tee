from __future__ import annotations

import re
from statistics import median
from typing import Any

import requests


SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
MARKETPLACE_ID = "EBAY_US"
US_ZIP_CODE = "97250"

MIN_REFERENCE_SAMPLES = 8
MAX_REFERENCE_RESULTS = 100
TRIM_RATIO = 0.15

GENERIC_QUERY_WORDS = {
    "vintage",
    "shirt",
    "shirts",
    "tee",
    "tees",
    "tshirt",
    "tshirts",
    "t-shirt",
    "t-shirts",
    "band",
    "tour",
    "concert",
    "single",
    "stitch",
    "old",
    "graphic",
    "mens",
    "men",
    "women",
    "size",
    "large",
    "xl",
    "xxl",
}

REFERENCE_EXCLUDE_KEYWORDS = [
    "reprint",
    "re-print",
    "reproduction",
    "replica",
    "fake",
    "modern",
    "vintage style",
    "vintage inspired",
    "print on demand",
    "made to order",
    "custom print",
    "custom made",
    "unofficial",
    "gildan",
    "comfort colors",
    "bella canvas",
    "bella+canvas",
    "kids",
    "youth",
    "toddler",
    "infant",
    "hoodie",
    "sweatshirt",
    "poster",
    "patch",
    "sticker",
]


def _parse_amount(data: Any) -> float | None:
    if not isinstance(data, dict):
        return None

    raw_value = data.get("value")

    if raw_value is None:
        return None

    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None

    return value if value >= 0 else None


def _get_shipping_cost(item: dict[str, Any]) -> float | None:
    shipping_options = item.get("shippingOptions", [])

    if not isinstance(shipping_options, list):
        return None

    for option in shipping_options:
        value = _parse_amount(option.get("shippingCost"))

        if value is not None:
            return value

    return None


def _get_total_cost(item: dict[str, Any]) -> float | None:
    price = _parse_amount(item.get("price"))

    if price is None or price <= 0:
        return None

    shipping = _get_shipping_cost(item)

    if shipping is None:
        return None

    return price + shipping


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _important_query_tokens(query: str) -> list[str]:
    tokens = [
        token
        for token in _normalize_text(query).split()
        if len(token) >= 3 and token not in GENERIC_QUERY_WORDS
    ]

    return list(dict.fromkeys(tokens))


def _is_comparable_listing(
    item: dict[str, Any],
    query_tokens: list[str],
) -> bool:
    title = str(item.get("title", ""))
    normalized_title = _normalize_text(title)

    if not normalized_title:
        return False

    if any(
        keyword in normalized_title
        for keyword in REFERENCE_EXCLUDE_KEYWORDS
    ):
        return False

    # 검색어에서 뽑은 핵심 단어가 제목에 하나도 없으면 비교군에서 제외
    if query_tokens and not any(
        token in normalized_title
        for token in query_tokens
    ):
        return False

    return True


def get_active_price_reference(
    token: str,
    query: str,
    maximum_results: int = MAX_REFERENCE_RESULTS,
) -> dict[str, Any] | None:
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
            "filter": (
                "buyingOptions:{FIXED_PRICE},"
                "price:[20..1000],"
                "priceCurrency:USD"
            ),
            "sort": "price",
        },
        timeout=30,
    )

    response.raise_for_status()

    items = response.json().get("itemSummaries", [])
    query_tokens = _important_query_tokens(query)

    totals: list[float] = []

    for item in items:
        if not _is_comparable_listing(item, query_tokens):
            continue

        total_cost = _get_total_cost(item)

        if total_cost is not None:
            totals.append(total_cost)

    if len(totals) < MIN_REFERENCE_SAMPLES:
        return None

    totals.sort()

    trim_count = int(len(totals) * TRIM_RATIO)

    if trim_count > 0 and len(totals) - (trim_count * 2) >= MIN_REFERENCE_SAMPLES:
        trimmed_totals = totals[trim_count:-trim_count]
    else:
        trimmed_totals = totals

    if len(trimmed_totals) < MIN_REFERENCE_SAMPLES:
        return None

    reference_median = round(median(trimmed_totals), 2)

    if reference_median <= 0:
        return None

    return {
        "median": reference_median,
        "minimum": round(min(trimmed_totals), 2),
        "maximum": round(max(trimmed_totals), 2),
        "sample_count": len(trimmed_totals),
    }


def calculate_listing_discount(
    total_cost: float,
    reference_median: float,
) -> float:
    if total_cost <= 0 or reference_median <= 0:
        return 0.0

    return round(
        ((reference_median - total_cost) / reference_median) * 100,
        1,
    )
