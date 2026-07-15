import os
import re
from datetime import datetime, timezone

import requests


CLIENT_ID = os.environ["EBAY_CLIENT_ID"]
CLIENT_SECRET = os.environ["EBAY_CLIENT_SECRET"]

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

MAX_TOTAL_USD = 500
MAX_HOURS_LEFT = 24

EXCLUDE_KEYWORDS = [
    "reprint",
    "reproduction",
    "modern",
    "dry rot",
    "kids",
    "kid's",
    "youth",
]

SIZE_PATTERNS = [
    r"\bL\b",
    r"\bLARGE\b",
    r"\bXL\b",
    r"\bX-LARGE\b",
    r"\bXXL\b",
    r"\b2XL\b",
    r"\b2X\b",
]


def get_access_token() -> str:
    response = requests.post(
        TOKEN_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=30,
    )

    response.raise_for_status()
    return response.json()["access_token"]


def get_shipping_cost(item: dict) -> float:
    shipping_options = item.get("shippingOptions", [])

    if not shipping_options:
        return 0.0

    shipping_cost = shipping_options[0].get("shippingCost", {})
    return float(shipping_cost.get("value", 0))


def get_hours_left(end_date: str | None) -> float | None:
    if not end_date:
        return None

    end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)

    return (end - now).total_seconds() / 3600


def format_time_left(hours_left: float | None) -> str:
    if hours_left is None:
        return "종료시간 정보 없음"

    if hours_left <= 0:
        return "종료됨"

    hours = int(hours_left)
    minutes = int((hours_left - hours) * 60)

    return f"{hours}시간 {minutes}분"


def has_allowed_size(title: str) -> bool:
    upper_title = title.upper()

    return any(
        re.search(pattern, upper_title)
        for pattern in SIZE_PATTERNS
    )


def has_excluded_keyword(title: str) -> bool:
    lower_title = title.lower()

    return any(
        keyword in lower_title
        for keyword in EXCLUDE_KEYWORDS
    )


def is_qualified_item(item: dict) -> tuple[bool, str]:
    title = item.get("title", "")

    if has_excluded_keyword(title):
        return False, "제외 키워드 포함"

    if not has_allowed_size(title):
        return False, "L~XXL 사이즈 확인 불가"

    price_data = item.get("price", {})
    price = float(price_data.get("value", 0))
    shipping = get_shipping_cost(item)
    total = price + shipping

    if total > MAX_TOTAL_USD:
        return False, "$500 초과"

    hours_left = get_hours_left(item.get("itemEndDate"))

    if hours_left is None:
        return False, "종료시간 없음"

    if hours_left <= 0:
        return False, "이미 종료됨"

    if hours_left > MAX_HOURS_LEFT:
        return False, "24시간 이상 남음"

    return True, "통과"


def search_auctions(
    query: str = "vintage t shirt",
    limit: int = 200,
) -> list[dict]:
    token = get_access_token()

    response = requests.get(
        SEARCH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
        params={
            "q": query,
            "limit": limit,
            "filter": "buyingOptions:{AUCTION},price:[..500],priceCurrency:USD",
            "sort": "endingSoonest",
        },
        timeout=30,
    )

    response.raise_for_status()
    return response.json().get("itemSummaries", [])


if __name__ == "__main__":
    items = search_auctions()
    qualified_items = []
    rejected_items = []

    for item in items:
        passed, reason = is_qualified_item(item)

        if passed:
            qualified_items.append(item)
        else:
            rejected_items.append((item, reason))

    print(f"전체 검색 결과: {len(items)}개")
    print(f"조건 통과: {len(qualified_items)}개")
    print(f"조건 탈락: {len(rejected_items)}개")
    print("=" * 70)

    for index, item in enumerate(qualified_items, start=1):
        title = item.get("title", "제목 없음")
        price_data = item.get("price", {})
        price = float(price_data.get("value", 0))
        currency = price_data.get("currency", "USD")
        shipping = get_shipping_cost(item)
        total = price + shipping
        hours_left = get_hours_left(item.get("itemEndDate"))

        print(f"[통과 {index}] {title}")
        print(f"현재가: {price:.2f} {currency}")
        print(f"미국 배송비: {shipping:.2f} {currency}")
        print(f"현재 총비용: {total:.2f} {currency}")
        print(f"남은 시간: {format_time_left(hours_left)}")
        print(f"링크: {item.get('itemWebUrl', '링크 없음')}")
        print("-" * 70)

    if not qualified_items:
        print("이번 검색에서는 기준을 충족한 상품 없음")

        print("\n아깝게 탈락한 후보:")
        for item, reason in rejected_items[:3]:
            print(f"- {item.get('title', '제목 없음')}")
            print(f"  탈락 사유: {reason}")
