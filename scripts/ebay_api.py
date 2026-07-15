import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from exchange import usd_to_krw
from telegram_alert import send_telegram_message


CLIENT_ID = os.environ["EBAY_CLIENT_ID"]
CLIENT_SECRET = os.environ["EBAY_CLIENT_SECRET"]

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item"

MAX_TOTAL_USD = 500
MAX_HOURS_LEFT = 24

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config" / "search_terms.json"


def load_search_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def create_all_queries(config: dict) -> list[str]:
    subjects = config.get("subjects", [])
    templates = config.get("query_templates", [])
    broad_queries = config.get("broad_queries", [])

    queries = []

    for subject in subjects:
        for template in templates:
            queries.append(template.format(subject=subject))

    queries.extend(broad_queries)

    # 같은 검색어가 중복으로 들어갔을 경우 제거
    return list(dict.fromkeys(queries))


def select_query_batch(
    queries: list[str],
    queries_per_run: int,
) -> list[str]:
    if not queries:
        return []

    # 실행 시각에 따라 검색 구간을 자동으로 변경
    current_hour_number = int(
        datetime.now(timezone.utc).timestamp() // 3600
    )

    start_index = (
        current_hour_number * queries_per_run
    ) % len(queries)

    selected = []

    for offset in range(queries_per_run):
        index = (start_index + offset) % len(queries)
        selected.append(queries[index])

    return selected


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


def get_item_price(item: dict) -> tuple[float, str]:
    price_data = (
        item.get("currentBidPrice")
        or item.get("price")
        or {}
    )

    try:
        price = float(price_data.get("value", 0))
    except (TypeError, ValueError):
        price = 0.0

    currency = price_data.get("currency", "USD")

    return price, currency


def get_item_details(token: str, item_id: str) -> dict:
    response = requests.get(
        f"{ITEM_URL}/{item_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
        timeout=30,
    )

    response.raise_for_status()
    return response.json()


def get_item_price(item: dict) -> tuple[float | None, str]:
    price_data = (
        item.get("currentBidPrice")
        or item.get("minimumPriceToBid")
        or item.get("price")
        or {}
    )

    try:
        value = price_data.get("value")

        if value is None:
            return None, "USD"

        return float(value), price_data.get("currency", "USD")

    except (TypeError, ValueError):
        return None, "USD"


def get_shipping_cost(item: dict) -> float:
    shipping_options = item.get("shippingOptions", [])

    if not shipping_options:
        return 0.0

    shipping_cost = shipping_options[0].get("shippingCost", {})

    try:
        return float(shipping_cost.get("value", 0))
    except (TypeError, ValueError):
        return 0.0


def get_hours_left(end_date: str | None) -> float | None:
    if not end_date:
        return None

    end_time = datetime.fromisoformat(
        end_date.replace("Z", "+00:00")
    )
    now = datetime.now(timezone.utc)

    return (end_time - now).total_seconds() / 3600


def format_time_left(hours_left: float | None) -> str:
    if hours_left is None:
        return "종료시간 정보 없음"

    if hours_left <= 0:
        return "종료됨"

    hours = int(hours_left)
    minutes = int((hours_left - hours) * 60)

    return f"{hours}시간 {minutes}분"


def has_allowed_size(title: str) -> bool:
    normalized = title.upper()

    size_patterns = [
        r"\bL\b",
        r"\bLARGE\b",
        r"\bXL\b",
        r"\bX-LARGE\b",
        r"\bEXTRA LARGE\b",
        r"\bXXL\b",
        r"\b2XL\b",
        r"\b2X\b",
    ]

    return any(
        re.search(pattern, normalized)
        for pattern in size_patterns
    )


def has_excluded_keyword(
    title: str,
    exclude_keywords: list[str],
) -> bool:
    lower_title = title.lower()

    return any(
        keyword.lower() in lower_title
        for keyword in exclude_keywords
    )


def is_qualified_item(
    item: dict,
    exclude_keywords: list[str],
) -> tuple[bool, str]:
    title = item.get("title", "")

    if has_excluded_keyword(title, exclude_keywords):
        return False, "제외 키워드 포함"

    if not has_allowed_size(title):
        return False, "L~XXL 사이즈 확인 불가"

    price, _ = get_item_price(item)

    if price <= 0:
        return False, "현재 입찰가 정보 없음"

    shipping = get_shipping_cost(item)
    total = price + shipping

    if total > MAX_TOTAL_USD:
        return False, "상품가+미국 배송비 $500 초과"

    hours_left = get_hours_left(item.get("itemEndDate"))

    if hours_left is None:
        return False, "종료시간 정보 없음"

    if hours_left <= 0:
        return False, "이미 종료됨"

    if hours_left > MAX_HOURS_LEFT:
        return False, "종료까지 24시간 이상"

    return True, "통과"


def search_one_query(
    token: str,
    query: str,
    limit: int = 200,
) -> list[dict]:
    response = requests.get(
        SEARCH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
        params={
            "q": query,
            "limit": limit,
            "filter": (
                "buyingOptions:{AUCTION},"
                "price:[..500],"
                "priceCurrency:USD"
            ),
            "sort": "endingSoonest",
        },
        timeout=30,
    )

    response.raise_for_status()

    return response.json().get("itemSummaries", [])


def search_multiple_queries(
    token: str,
    queries: list[str],
) -> list[dict]:
    unique_items = {}

    for index, query in enumerate(queries, start=1):
        print(f"[{index}/{len(queries)}] 검색 중: {query}")

        try:
            items = search_one_query(
                token=token,
                query=query,
            )
        except requests.RequestException as error:
            print(f"검색 실패: {query}")
            print(error)
            continue

        for item in items:
            item_id = item.get("itemId")

            if item_id:
                unique_items[item_id] = item

    return list(unique_items.values())


if __name__ == "__main__":
    config = load_search_config()

    all_queries = create_all_queries(config)

    queries_per_run = int(
        config.get("queries_per_run", 20)
    )

    selected_queries = select_query_batch(
        queries=all_queries,
        queries_per_run=queries_per_run,
    )

    exclude_keywords = config.get(
        "exclude_keywords",
        [],
    )

    print(f"전체 저장된 검색어: {len(all_queries)}개")
    print(f"이번 실행 검색어: {len(selected_queries)}개")
    print("=" * 70)

    for query in selected_queries:
        print(f"- {query}")

    print("=" * 70)

    access_token = get_access_token()

    items = search_multiple_queries(
        token=access_token,
        queries=selected_queries,
    )

    qualified_items = []
    rejected_items = []

    for item in items:
        passed, reason = is_qualified_item(
            item=item,
            exclude_keywords=exclude_keywords,
        )

        if passed:
            qualified_items.append(item)
        else:
            rejected_items.append((item, reason))

    qualified_items.sort(
        key=lambda item: (
            get_hours_left(item.get("itemEndDate"))
            if get_hours_left(item.get("itemEndDate"))
            is not None
            else float("inf")
        )
    )

    print("=" * 70)
    print(f"중복 제거 후 매물: {len(items)}개")
    print(f"조건 통과: {len(qualified_items)}개")
    print(f"조건 탈락: {len(rejected_items)}개")
    print("=" * 70)

    for index, item in enumerate(
        qualified_items,
        start=1,
    ):
        title = item.get("title", "제목 없음")
        item_id = item.get("itemId", "")
detailed_item = item

if item_id:
    try:
        detailed_item = get_item_details(
            access_token,
            item_id,
        )
    except requests.RequestException as error:
        print(f"상품 상세 가격 조회 실패: {title}")
        print(error)

price, currency = get_item_price(detailed_item)

if price is None:
    price_text = "가격 확인 필요"
    price_krw_text = ""
    price_for_total = 0.0
else:
    price_krw = round(price * exchange_rate)
    price_text = f"${price:.2f}"
    price_krw_text = f" / 약 {price_krw:,}원"
    price_for_total = price
        shipping = get_shipping_cost(item)
        total = price + shipping

        hours_left = get_hours_left(
            item.get("itemEndDate")
        )

        print(f"[통과 {index}] {title}")
        print(f"현재가: {price:.2f} {currency}")
        print(
            f"미국 배송비: "
            f"{shipping:.2f} {currency}"
        )
        print(
            f"현재 총비용: "
            f"{total:.2f} {currency}"
        )
        print(
            f"남은 시간: "
            f"{format_time_left(hours_left)}"
        )
        print(
            f"링크: "
            f"{item.get('itemWebUrl', '링크 없음')}"
        )
        print("-" * 70)

    if not qualified_items:
        print("이번 검색에서는 기준을 충족한 상품 없음")

        print("\n아깝게 탈락한 후보:")

        for item, reason in rejected_items[:3]:
            print(
                f"- {item.get('title', '제목 없음')}"
            )
            print(f"  탈락 사유: {reason}")

    # 조건 통과 매물을 텔레그램으로 전송
    MAX_ALERTS_PER_RUN = 5
    FORWARDING_FEE_USD = 10.0

    if qualified_items:
        alert_items = qualified_items[:MAX_ALERTS_PER_RUN]

        # 환율 API는 실행당 한 번만 호출
        exchange_rate = usd_to_krw(1)["rate"]

        for item in alert_items:
            title = item.get("title", "제목 없음")
            price_data = item.get("price", {})

            price = float(price_data.get("value", 0))
            shipping = get_shipping_cost(item)

            # 총 예상금액에는 배대지 고정비 $10 포함
            total_usd = price_for_total + shipping + FORWARDING_FEE_USD

            price_krw = round(price * exchange_rate)
            shipping_krw = round(shipping * exchange_rate)
            total_krw = round(total_usd * exchange_rate)

            hours_left = get_hours_left(
                item.get("itemEndDate")
            )
            time_left = format_time_left(hours_left)

            ebay_url = item.get(
                "itemWebUrl",
                "링크 없음",
            )

            message = f"""
🎯 빈티지 레이더

👕 {title}

💰 상품가
{price_text}{price_krw_text}

🚚 미국 배송비
${shipping:.2f} / 약 {shipping_krw:,}원

💵 총 예상금액
${total_usd:.2f} / 약 {total_krw:,}원
(배대지 $10 포함)

⏰ 남은 시간
{time_left}

🔗 eBay 바로가기
{ebay_url}
""".strip()

            send_telegram_message(message)

    else:
        message = (
            "🎯 빈티지 레이더\n\n"
            "이번 검색에서는 기준을 충족한 상품 없음"
        )

        send_telegram_message(message)
