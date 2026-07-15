import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from exchange import usd_to_krw
from telegram_alert import send_telegram_message


# ─────────────────────────────────────────────
# 기본 설정
# ─────────────────────────────────────────────

CLIENT_ID = os.environ["EBAY_CLIENT_ID"]
CLIENT_SECRET = os.environ["EBAY_CLIENT_SECRET"]

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item"

MARKETPLACE_ID = "EBAY_US"

MAX_TOTAL_USD = 500.0
MAX_HOURS_LEFT = 24.0
FORWARDING_FEE_USD = 10.0
MAX_ALERTS_PER_RUN = 5

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config" / "search_terms.json"

SIZE_PATTERNS = [
    r"\bL\b",
    r"\bLARGE\b",
    r"\bXL\b",
    r"\bX[\s-]?LARGE\b",
    r"\bEXTRA[\s-]?LARGE\b",
    r"\bXXL\b",
    r"\b2XL\b",
    r"\b2X\b",
]


# ─────────────────────────────────────────────
# 설정 파일 및 검색어
# ─────────────────────────────────────────────

def load_search_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def create_all_queries(config: dict[str, Any]) -> list[str]:
    subjects = config.get("subjects", [])
    templates = config.get("query_templates", [])
    broad_queries = config.get("broad_queries", [])

    queries: list[str] = []

    for subject in subjects:
        for template in templates:
            queries.append(template.format(subject=subject))

    queries.extend(broad_queries)

    # 순서를 유지하면서 중복 제거
    return list(dict.fromkeys(queries))


def select_query_batch(
    queries: list[str],
    queries_per_run: int,
) -> list[str]:
    if not queries:
        return []

    if queries_per_run <= 0:
        return []

    # 매시간 다른 구간의 검색어를 선택
    hour_number = int(
        datetime.now(timezone.utc).timestamp() // 3600
    )

    start_index = (
        hour_number * queries_per_run
    ) % len(queries)

    selected: list[str] = []

    for offset in range(min(queries_per_run, len(queries))):
        index = (start_index + offset) % len(queries)
        selected.append(queries[index])

    return selected


# ─────────────────────────────────────────────
# eBay 인증 및 API 호출
# ─────────────────────────────────────────────

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

    access_token = response.json().get("access_token")

    if not access_token:
        raise RuntimeError("eBay 액세스 토큰이 반환되지 않았습니다.")

    return access_token


def ebay_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID,
    }


def search_one_query(
    token: str,
    query: str,
    limit: int = 200,
) -> list[dict[str, Any]]:
    response = requests.get(
        SEARCH_URL,
        headers=ebay_headers(token),
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
) -> list[dict[str, Any]]:
    unique_items: dict[str, dict[str, Any]] = {}

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


def get_item_details(
    token: str,
    item_id: str,
) -> dict[str, Any]:
    encoded_item_id = quote(item_id, safe="")

    response = requests.get(
        f"{ITEM_URL}/{encoded_item_id}",
        headers=ebay_headers(token),
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


# ─────────────────────────────────────────────
# 가격 및 배송비
# ─────────────────────────────────────────────

def parse_amount(
    amount_data: Any,
) -> tuple[float | None, str]:
    if not isinstance(amount_data, dict):
        return None, "USD"

    raw_value = amount_data.get("value")
    currency = amount_data.get("currency", "USD")

    if raw_value is None:
        return None, currency

    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None, currency

    return value, currency


def get_summary_price(
    item: dict[str, Any],
) -> tuple[float | None, str, str]:
    """
    검색 결과에서 가격을 찾는다.

    반환값:
    가격, 통화, 표시명
    """

    current_bid, currency = parse_amount(
        item.get("currentBidPrice")
    )

    if current_bid is not None and current_bid > 0:
        return current_bid, currency, "현재 입찰가"

    regular_price, currency = parse_amount(
        item.get("price")
    )

    if regular_price is not None and regular_price > 0:
        return regular_price, currency, "시작가"

    return None, "USD", "가격"


def get_detailed_price(
    item: dict[str, Any],
) -> tuple[float | None, str, str]:
    """
    상품 상세정보에서 경매 가격을 찾는다.

    우선순위:
    현재 최고 입찰가 → 다음 최소 입찰가/시작가 → 일반 가격
    """

    current_bid, currency = parse_amount(
        item.get("currentBidPrice")
    )

    if current_bid is not None and current_bid > 0:
        return current_bid, currency, "현재 입찰가"

    minimum_bid, currency = parse_amount(
        item.get("minimumPriceToBid")
    )

    if minimum_bid is not None and minimum_bid > 0:
        bid_count = int(item.get("bidCount", 0) or 0)

        if bid_count > 0:
            return minimum_bid, currency, "다음 최소 입찰가"

        return minimum_bid, currency, "시작가"

    regular_price, currency = parse_amount(
        item.get("price")
    )

    if regular_price is not None and regular_price > 0:
        return regular_price, currency, "상품가"

    return None, "USD", "가격 확인 필요"


def resolve_item_price(
    token: str,
    item: dict[str, Any],
) -> tuple[float | None, str, str, dict[str, Any]]:
    """
    검색 결과에 가격이 있으면 사용하고,
    없으면 상품 상세 API를 한 번 호출한다.
    """

    price, currency, label = get_summary_price(item)

    if price is not None:
        return price, currency, label, item

    item_id = item.get("itemId")

    if not item_id:
        return None, "USD", "가격 확인 필요", item

    try:
        detailed_item = get_item_details(
            token=token,
            item_id=item_id,
        )
    except requests.RequestException as error:
        print(
            "상세 가격 조회 실패:",
            item.get("title", "제목 없음"),
        )
        print(error)

        return None, "USD", "가격 확인 필요", item

    price, currency, label = get_detailed_price(
        detailed_item
    )

    return price, currency, label, detailed_item


def get_shipping_cost(item: dict[str, Any]) -> float:
    shipping_options = item.get("shippingOptions", [])

    if not isinstance(shipping_options, list):
        return 0.0

    for option in shipping_options:
        shipping_cost = option.get("shippingCost", {})

        value, _ = parse_amount(shipping_cost)

        if value is not None:
            return value

    return 0.0


# ─────────────────────────────────────────────
# 시간, 사이즈, 제외 키워드
# ─────────────────────────────────────────────

def get_hours_left(
    end_date: str | None,
) -> float | None:
    if not end_date:
        return None

    try:
        end_time = datetime.fromisoformat(
            end_date.replace("Z", "+00:00")
        )
    except ValueError:
        return None

    now = datetime.now(timezone.utc)

    return (end_time - now).total_seconds() / 3600


def format_time_left(
    hours_left: float | None,
) -> str:
    if hours_left is None:
        return "종료시간 정보 없음"

    if hours_left <= 0:
        return "종료됨"

    total_minutes = int(hours_left * 60)
    hours, minutes = divmod(total_minutes, 60)

    return f"{hours}시간 {minutes}분"


def has_allowed_size(title: str) -> bool:
    normalized_title = title.upper()

    return any(
        re.search(pattern, normalized_title)
        for pattern in SIZE_PATTERNS
    )


def find_excluded_keyword(
    title: str,
    exclude_keywords: list[str],
) -> str | None:
    lower_title = title.lower()

    for keyword in exclude_keywords:
        if keyword.lower() in lower_title:
            return keyword

    return None


# ─────────────────────────────────────────────
# 매물 필터링
# ─────────────────────────────────────────────

def evaluate_item(
    token: str,
    item: dict[str, Any],
    exclude_keywords: list[str],
) -> tuple[bool, str, dict[str, Any]]:
    title = item.get("title", "")

    excluded_keyword = find_excluded_keyword(
        title=title,
        exclude_keywords=exclude_keywords,
    )

    if excluded_keyword:
        return (
            False,
            f"제외 키워드 포함: {excluded_keyword}",
            item,
        )

    if not has_allowed_size(title):
        return False, "L~XXL 사이즈 확인 불가", item

    hours_left = get_hours_left(
        item.get("itemEndDate")
    )

    if hours_left is None:
        return False, "종료시간 정보 없음", item

    if hours_left <= 0:
        return False, "이미 종료됨", item

    if hours_left > MAX_HOURS_LEFT:
        return False, "종료까지 24시간 이상", item

    price, currency, price_label, detailed_item = (
        resolve_item_price(
            token=token,
            item=item,
        )
    )

    if price is None or price <= 0:
        return False, "입찰가 또는 시작가 확인 불가", item

    # 검색 결과의 배송비가 더 안정적으로 제공되는 경우가 있어
    # 우선 검색 결과 배송비를 사용하고, 없으면 상세정보를 사용한다.
    shipping = get_shipping_cost(item)

    if shipping == 0:
        shipping = get_shipping_cost(detailed_item)

    total_before_forwarding = price + shipping

    if total_before_forwarding > MAX_TOTAL_USD:
        return (
            False,
            "상품가+미국 배송비 $500 초과",
            item,
        )

    enriched_item = dict(item)

    enriched_item["_resolved_price"] = price
    enriched_item["_resolved_currency"] = currency
    enriched_item["_price_label"] = price_label
    enriched_item["_shipping_cost"] = shipping
    enriched_item["_hours_left"] = hours_left
    enriched_item["_detailed_item"] = detailed_item

    return True, "통과", enriched_item


# ─────────────────────────────────────────────
# 텔레그램
# ─────────────────────────────────────────────

def build_telegram_message(
    item: dict[str, Any],
    exchange_rate: float,
) -> str:
    title = item.get("title", "제목 없음")

    price = float(item["_resolved_price"])
    shipping = float(item["_shipping_cost"])
    price_label = item["_price_label"]

    total_usd = (
        price
        + shipping
        + FORWARDING_FEE_USD
    )

    price_krw = round(price * exchange_rate)
    shipping_krw = round(shipping * exchange_rate)
    total_krw = round(total_usd * exchange_rate)

    time_left = format_time_left(
        item.get("_hours_left")
    )

    ebay_url = item.get(
        "itemWebUrl",
        "링크 없음",
    )

    return f"""
🎯 빈티지 레이더

👕 {title}

💰 {price_label}
${price:.2f} / 약 {price_krw:,}원

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


def send_qualified_items(
    qualified_items: list[dict[str, Any]],
) -> None:
    if not qualified_items:
        send_telegram_message(
            "🎯 빈티지 레이더\n\n"
            "이번 검색에서는 기준을 충족한 상품 없음"
        )
        return

    exchange_result = usd_to_krw(1)
    exchange_rate = float(exchange_result["rate"])

    alert_items = qualified_items[:MAX_ALERTS_PER_RUN]

    for item in alert_items:
        message = build_telegram_message(
            item=item,
            exchange_rate=exchange_rate,
        )

        send_telegram_message(message)


# ─────────────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────────────

def main() -> None:
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

    token = get_access_token()

    items = search_multiple_queries(
        token=token,
        queries=selected_queries,
    )

    qualified_items: list[dict[str, Any]] = []
    rejected_items: list[
        tuple[dict[str, Any], str]
    ] = []

    for index, item in enumerate(items, start=1):
        print(
            f"[{index}/{len(items)}] 필터 확인: "
            f"{item.get('title', '제목 없음')}"
        )

        passed, reason, processed_item = evaluate_item(
            token=token,
            item=item,
            exclude_keywords=exclude_keywords,
        )

        if passed:
            qualified_items.append(processed_item)
        else:
            rejected_items.append((item, reason))

    # 종료가 빠른 매물부터 정렬
    qualified_items.sort(
        key=lambda current_item: float(
            current_item.get(
                "_hours_left",
                float("inf"),
            )
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
        price = float(item["_resolved_price"])
        shipping = float(item["_shipping_cost"])
        price_label = item["_price_label"]

        print(
            f"[통과 {index}] "
            f"{item.get('title', '제목 없음')}"
        )
        print(f"{price_label}: ${price:.2f}")
        print(f"미국 배송비: ${shipping:.2f}")
        print(
            "상품가+미국 배송비: "
            f"${price + shipping:.2f}"
        )
        print(
            "남은 시간: "
            f"{format_time_left(item['_hours_left'])}"
        )
        print(
            "링크: "
            f"{item.get('itemWebUrl', '링크 없음')}"
        )
        print("-" * 70)

    if not qualified_items:
        print("이번 검색에서는 기준을 충족한 상품 없음")

        if rejected_items:
            print("\n아깝게 탈락한 후보:")

            for item, reason in rejected_items[:3]:
                print(
                    f"- {item.get('title', '제목 없음')}"
                )
                print(f"  탈락 사유: {reason}")

    send_qualified_items(qualified_items)


if __name__ == "__main__":
    main()
