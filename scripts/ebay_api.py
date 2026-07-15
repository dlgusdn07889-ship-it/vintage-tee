import os
from datetime import datetime, timezone

import requests


CLIENT_ID = os.environ["EBAY_CLIENT_ID"]
CLIENT_SECRET = os.environ["EBAY_CLIENT_SECRET"]

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"


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


def format_time_left(end_date: str | None) -> str:
    if not end_date:
        return "종료시간 정보 없음"

    end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    seconds = int((end - now).total_seconds())

    if seconds <= 0:
        return "종료됨"

    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60

    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}일 {hours}시간"

    return f"{hours}시간 {minutes}분"


def search_auctions(
    query: str = "Nirvana Giant vintage shirt",
    limit: int = 10,
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
            "filter": "buyingOptions:{AUCTION}",
            "sort": "endingSoonest",
        },
        timeout=30,
    )

    response.raise_for_status()
    return response.json().get("itemSummaries", [])


if __name__ == "__main__":
    items = search_auctions()

    print(f"검색된 경매 수: {len(items)}")
    print("=" * 60)

    for index, item in enumerate(items, start=1):
        price_data = item.get("price", {})
        price = float(price_data.get("value", 0))
        currency = price_data.get("currency", "USD")
        shipping = get_shipping_cost(item)
        total = price + shipping

        print(f"[{index}] {item.get('title', '제목 없음')}")
        print(f"현재가: {price:.2f} {currency}")
        print(f"미국 배송비: {shipping:.2f} {currency}")
        print(f"현재 총비용: {total:.2f} {currency}")
        print(f"남은 시간: {format_time_left(item.get('itemEndDate'))}")
        print(f"링크: {item.get('itemWebUrl', '링크 없음')}")
        print("-" * 60)
