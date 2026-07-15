from statistics import median
from typing import Any

import requests


SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
MARKETPLACE_ID = "EBAY_US"


def parse_price(item: dict[str, Any]) -> float | None:
    price_data = item.get("price", {})

    try:
        value = float(price_data.get("value", 0))
    except (TypeError, ValueError):
        return None

    return value if value > 0 else None


def get_active_price_reference(
    token: str,
    query: str,
    maximum_results: int = 50,
) -> dict[str, Any] | None:
    response = requests.get(
        SEARCH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID,
            "X-EBAY-C-ENDUSERCTX": (
                "contextualLocation=country%3DUS%2Czip%3D97250"
            ),
        },
        params={
            "q": query,
            "limit": maximum_results,
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

    prices = []

    for item in items:
        price = parse_price(item)

        if price is not None:
            prices.append(price)

    if len(prices) < 5:
        return None

    prices.sort()

    # 지나치게 싸거나 비싼 극단값 제거
    trim_count = max(1, int(len(prices) * 0.1))
    trimmed_prices = prices[trim_count:-trim_count]

    if not trimmed_prices:
        trimmed_prices = prices

    return {
        "median": round(median(trimmed_prices), 2),
        "minimum": round(min(trimmed_prices), 2),
        "maximum": round(max(trimmed_prices), 2),
        "sample_count": len(trimmed_prices),
    }


def calculate_listing_discount(
    total_cost: float,
    reference_median: float,
) -> float:
    if reference_median <= 0:
        return 0.0

    return round(
        ((reference_median - total_cost) / reference_median) * 100,
        1,
    )
