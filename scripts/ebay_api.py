import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from exchange import usd_to_krw
from telegram_alert import send_telegram_message


# =========================================================
# 기본 경로 및 eBay 설정
# =========================================================

CLIENT_ID = os.environ["EBAY_CLIENT_ID"]
CLIENT_SECRET = os.environ["EBAY_CLIENT_SECRET"]

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item"

MARKETPLACE_ID = "EBAY_US"

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"

PRIORITY_KEYWORDS_PATH = CONFIG_DIR / "priority_keywords.json"
SEARCH_PATTERNS_PATH = CONFIG_DIR / "search_patterns.json"
EXCLUDED_KEYWORDS_PATH = CONFIG_DIR / "excluded_keywords.json"
TAG_BRANDS_PATH = CONFIG_DIR / "tag_brands.json"

SEEN_ITEMS_PATH = DATA_DIR / "seen_items.json"

DEFAULT_FORWARDING_FEE_USD = 10.0
DEFAULT_MAX_TOTAL_USD = 500.0
DEFAULT_AUCTION_MAX_HOURS = 24.0
DEFAULT_FIXED_PRICE_NEW_MINUTES = 20
DEFAULT_MAX_ALERTS = 5

# 한 번 실행할 때 선택하는 검색어 구성
TOP_PRIORITY_QUERY_COUNT = 9
NORMAL_QUERY_COUNT = 1
EXPANDED_QUERY_COUNT = 0
BROAD_QUERY_COUNT = 0

SEARCH_LIMIT_PER_QUERY = 50


# =========================================================
# JSON 파일 읽기
# =========================================================

def load_json(
    path: Path,
    default: Any,
) -> Any:
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def load_all_configs() -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    priority_config = load_json(
        PRIORITY_KEYWORDS_PATH,
        {},
    )

    pattern_config = load_json(
        SEARCH_PATTERNS_PATH,
        {},
    )

    excluded_config = load_json(
        EXCLUDED_KEYWORDS_PATH,
        {},
    )

    tag_config = load_json(
        TAG_BRANDS_PATH,
        {},
    )

    return (
        priority_config,
        pattern_config,
        excluded_config,
        tag_config,
    )


# =========================================================
# 이미 알림을 보낸 상품 저장
# =========================================================

def load_seen_item_ids() -> set[str]:
    data = load_json(
        SEEN_ITEMS_PATH,
        {"item_ids": []},
    )

    return set(data.get("item_ids", []))


def save_seen_item_ids(item_ids: set[str]) -> None:
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # 파일이 너무 커지지 않게 최근 일부만 보관
    trimmed_ids = list(item_ids)[-5000:]

    with SEEN_ITEMS_PATH.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            {"item_ids": trimmed_ids},
            file,
            ensure_ascii=False,
            indent=2,
        )


# =========================================================
# 검색어 생성
# =========================================================

def pick_rotating_items(
    items: list[str],
    count: int,
    slot_number: int,
) -> list[str]:
    if not items or count <= 0:
        return []

    count = min(count, len(items))
    start_index = (slot_number * count) % len(items)

    selected = []

    for offset in range(count):
        index = (start_index + offset) % len(items)
        selected.append(items[index])

    return selected


def combine_normal_subjects(
    priority_config: dict[str, Any],
) -> list[str]:
    normal_keys = [
        "normal_movies",
        "normal_anime",
        "normal_harley",
        "normal_brands",
    ]

    subjects: list[str] = []

    for key in normal_keys:
        subjects.extend(
            priority_config.get(key, [])
        )

    return list(dict.fromkeys(subjects))


def build_selected_queries(
    priority_config: dict[str, Any],
    pattern_config: dict[str, Any],
) -> list[str]:
    top_subjects = priority_config.get(
        "top_priority_bands",
        [],
    )

    normal_subjects = combine_normal_subjects(
        priority_config
    )

    expanded_subjects = priority_config.get(
        "expanded_keywords",
        [],
    )

    top_patterns = pattern_config.get(
        "top_priority_patterns",
        ["{subject} shirt"],
    )

    normal_patterns = pattern_config.get(
        "normal_patterns",
        ["{subject} shirt"],
    )

    broad_queries = pattern_config.get(
        "broad_queries",
        [],
    )

    # 10분마다 다른 검색어 조합을 선택
    slot_number = int(
        datetime.now(timezone.utc).timestamp()
        // 600
    )

    selected_top = pick_rotating_items(
        top_subjects,
        TOP_PRIORITY_QUERY_COUNT,
        slot_number,
    )

    selected_normal = pick_rotating_items(
        normal_subjects,
        NORMAL_QUERY_COUNT,
        slot_number,
    )

    selected_expanded = pick_rotating_items(
        expanded_subjects,
        EXPANDED_QUERY_COUNT,
        slot_number,
    )

    selected_broad = pick_rotating_items(
        broad_queries,
        BROAD_QUERY_COUNT,
        slot_number,
    )

    queries: list[str] = []

    for index, subject in enumerate(selected_top):
        pattern_index = (
            slot_number + index
        ) % len(top_patterns)

        queries.append(
            top_patterns[pattern_index].format(
                subject=subject
            )
        )

    for index, subject in enumerate(
        selected_normal
    ):
        pattern_index = (
            slot_number + index
        ) % len(normal_patterns)

        queries.append(
            normal_patterns[pattern_index].format(
                subject=subject
            )
        )

    for index, subject in enumerate(
        selected_expanded
    ):
        pattern_index = (
            slot_number + index
        ) % len(normal_patterns)

        queries.append(
            normal_patterns[pattern_index].format(
                subject=subject
            )
        )

    queries.extend(selected_broad)

    # 분야별로 섞어서 한 분야만 몰리지 않게 함
    random_generator = random.Random(slot_number)
    random_generator.shuffle(queries)

    return list(dict.fromkeys(queries))


# =========================================================
# eBay 인증과 요청
# =========================================================

def get_access_token() -> str:
    response = requests.post(
        TOKEN_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),
        headers={
            "Content-Type":
                "application/x-www-form-urlencoded",
        },
        data={
            "grant_type":
                "client_credentials",
            "scope":
                "https://api.ebay.com/oauth/api_scope",
        },
        timeout=30,
    )

    response.raise_for_status()

    token = response.json().get("access_token")

    if not token:
        raise RuntimeError(
            "eBay 액세스 토큰을 받지 못했습니다."
        )

    return token


def get_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID":
            MARKETPLACE_ID,
    }


def search_one_query(
    token: str,
    query: str,
    buying_option: str,
) -> list[dict[str, Any]]:
    if buying_option == "AUCTION":
        sort_value = "endingSoonest"
    else:
        sort_value = "newlyListed"

    response = requests.get(
        SEARCH_URL,
        headers=get_headers(token),
        params={
            "q": query,
            "limit": SEARCH_LIMIT_PER_QUERY,
            "filter": (
                f"buyingOptions:{{{buying_option}}},"
                "price:[..500],"
                "priceCurrency:USD"
            ),
            "sort": sort_value,
        },
        timeout=30,
    )

    response.raise_for_status()

    return response.json().get(
        "itemSummaries",
        [],
    )


def get_item_details(
    token: str,
    item_id: str,
) -> dict[str, Any]:
    encoded_item_id = quote(
        item_id,
        safe="",
    )

    response = requests.get(
        f"{ITEM_URL}/{encoded_item_id}",
        headers=get_headers(token),
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


# =========================================================
# 가격과 배송비 처리
# =========================================================

def parse_amount(
    amount_data: Any,
) -> tuple[float | None, str]:
    if not isinstance(amount_data, dict):
        return None, "USD"

    raw_value = amount_data.get("value")
    currency = amount_data.get(
        "currency",
        "USD",
    )

    if raw_value is None:
        return None, currency

    try:
        return float(raw_value), currency
    except (TypeError, ValueError):
        return None, currency


def get_shipping_cost(
    item: dict[str, Any],
) -> float:
    shipping_options = item.get(
        "shippingOptions",
        [],
    )

    if not isinstance(shipping_options, list):
        return 0.0

    for option in shipping_options:
        value, _ = parse_amount(
            option.get("shippingCost")
        )

        if value is not None:
            return value

    return 0.0


def get_fixed_price(
    item: dict[str, Any],
) -> tuple[float | None, str]:
    price, currency = parse_amount(
        item.get("price")
    )

    return price, currency


def get_auction_price_from_summary(
    item: dict[str, Any],
) -> tuple[float | None, str, str]:
    current_bid, currency = parse_amount(
        item.get("currentBidPrice")
    )

    if current_bid is not None:
        return (
            current_bid,
            currency,
            "현재 입찰가",
        )

    price, currency = parse_amount(
        item.get("price")
    )

    if price is not None:
        return price, currency, "시작가"

    return None, "USD", "가격 확인 필요"


def get_auction_price_from_details(
    item: dict[str, Any],
) -> tuple[float | None, str, str]:
    current_bid, currency = parse_amount(
        item.get("currentBidPrice")
    )

    if current_bid is not None:
        return (
            current_bid,
            currency,
            "현재 입찰가",
        )

    minimum_bid, currency = parse_amount(
        item.get("minimumPriceToBid")
    )

    if minimum_bid is not None:
        bid_count = int(
            item.get("bidCount", 0) or 0
        )

        if bid_count > 0:
            return (
                minimum_bid,
                currency,
                "다음 최소 입찰가",
            )

        return (
            minimum_bid,
            currency,
            "시작가",
        )

    price, currency = parse_amount(
        item.get("price")
    )

    if price is not None:
        return price, currency, "시작가"

    return None, "USD", "가격 확인 필요"


def resolve_auction_price(
    token: str,
    item: dict[str, Any],
) -> tuple[
    float | None,
    str,
    str,
    dict[str, Any],
]:
    price, currency, label = (
        get_auction_price_from_summary(item)
    )

    if price is not None and price > 0:
        return price, currency, label, item

    item_id = item.get("itemId")

    if not item_id:
        return (
            None,
            "USD",
            "가격 확인 필요",
            item,
        )

    try:
        detailed_item = get_item_details(
            token,
            item_id,
        )
    except requests.RequestException as error:
        print(
            "상세 가격 조회 실패:",
            item.get("title", "제목 없음"),
        )
        print(error)

        return (
            None,
            "USD",
            "가격 확인 필요",
            item,
        )

    price, currency, label = (
        get_auction_price_from_details(
            detailed_item
        )
    )

    return (
        price,
        currency,
        label,
        detailed_item,
    )


# =========================================================
# 날짜와 시간
# =========================================================

def parse_ebay_datetime(
    date_value: str | None,
) -> datetime | None:
    if not date_value:
        return None

    try:
        return datetime.fromisoformat(
            date_value.replace(
                "Z",
                "+00:00",
            )
        )
    except ValueError:
        return None


def get_hours_left(
    end_date: str | None,
) -> float | None:
    end_time = parse_ebay_datetime(end_date)

    if end_time is None:
        return None

    now = datetime.now(timezone.utc)

    return (
        end_time - now
    ).total_seconds() / 3600


def get_listing_age_minutes(
    item: dict[str, Any],
) -> float | None:
    date_value = (
        item.get("itemCreationDate")
        or item.get("itemOriginDate")
    )

    created_time = parse_ebay_datetime(
        date_value
    )

    if created_time is None:
        return None

    now = datetime.now(timezone.utc)

    return (
        now - created_time
    ).total_seconds() / 60


def format_time_left(
    hours_left: float | None,
) -> str:
    if hours_left is None:
        return "종료시간 확인 필요"

    if hours_left <= 0:
        return "종료됨"

    total_minutes = int(hours_left * 60)
    hours, minutes = divmod(
        total_minutes,
        60,
    )

    return f"{hours}시간 {minutes}분"


def format_listing_age(
    age_minutes: float | None,
) -> str:
    if age_minutes is None:
        return "신규 등록"

    if age_minutes < 1:
        return "방금 전"

    if age_minutes < 60:
        return f"{int(age_minutes)}분 전"

    hours = int(age_minutes // 60)
    return f"{hours}시간 전"


# =========================================================
# 제외 키워드와 사이즈
# =========================================================

def find_hard_exclude_keyword(
    title: str,
    hard_excludes: list[str],
) -> str | None:
    lower_title = title.lower()

    for keyword in hard_excludes:
        if keyword.lower() in lower_title:
            return keyword

    return None


def find_soft_warnings(
    title: str,
    warning_keywords: list[str],
) -> list[str]:
    lower_title = title.lower()

    return [
        keyword
        for keyword in warning_keywords
        if keyword.lower() in lower_title
    ]


def has_clearly_small_size(
    title: str,
) -> bool:
    normalized = title.lower()

    small_size_patterns = [
        r"\bsize\s*xs\b",
        r"\bextra small\b",
        r"\bsize\s*small\b",
        r"\bsize\s*s\b",
        r"\bmens?\s+small\b",
        r"\bmen'?s\s+small\b",
        r"\bwomens?\s+small\b",
        r"\bwomen'?s\s+small\b",
        r"\bladies\s+small\b",
    ]

    return any(
        re.search(pattern, normalized)
        for pattern in small_size_patterns
    )


# =========================================================
# 태그 감지 — 내부 계산용
# =========================================================

def detect_tag_brand(
    title: str,
    tag_config: dict[str, Any],
) -> tuple[str | None, int]:
    lower_title = title.lower()

    tag_scores = tag_config.get(
        "tag_scores",
        {},
    )

    aliases = tag_config.get(
        "tag_aliases",
        {},
    )

    for alias, canonical_name in aliases.items():
        if alias.lower() in lower_title:
            score = int(
                tag_scores.get(
                    canonical_name,
                    0,
                )
            )
            return canonical_name, score

    sorted_tags = sorted(
        tag_scores.items(),
        key=lambda entry: len(entry[0]),
        reverse=True,
    )

    for tag_name, score in sorted_tags:
        if tag_name.lower() in lower_title:
            return tag_name, int(score)

    return None, 0


# =========================================================
# 경매·즉시구매 평가
# =========================================================

def evaluate_auction(
    token: str,
    item: dict[str, Any],
    hard_excludes: list[str],
    soft_warnings: list[str],
    tag_config: dict[str, Any],
    auction_max_hours: float,
    maximum_total_usd: float,
) -> tuple[bool, str, dict[str, Any]]:
    title = item.get("title", "")

    excluded_keyword = (
        find_hard_exclude_keyword(
            title,
            hard_excludes,
        )
    )

    if excluded_keyword:
        return (
            False,
            f"제외 키워드: {excluded_keyword}",
            item,
        )

    if has_clearly_small_size(title):
        return False, "작은 사이즈 명확", item

    hours_left = get_hours_left(
        item.get("itemEndDate")
    )

    if hours_left is None:
        return False, "종료시간 없음", item

    if hours_left <= 0:
        return False, "이미 종료됨", item

    if hours_left > auction_max_hours:
        return False, "종료 24시간 초과", item

    (
        price,
        currency,
        price_label,
        detailed_item,
    ) = resolve_auction_price(
        token,
        item,
    )

    if price is None or price <= 0:
        return False, "경매 가격 확인 불가", item

    shipping = get_shipping_cost(item)

    if shipping == 0:
        shipping = get_shipping_cost(
            detailed_item
        )

    if price + shipping > maximum_total_usd:
        return False, "$500 초과", item

    warning_list = find_soft_warnings(
        title,
        soft_warnings,
    )

    tag_name, tag_score = detect_tag_brand(
        title,
        tag_config,
    )

    processed = dict(item)

    processed.update({
        "_listing_type": "AUCTION",
        "_price": price,
        "_currency": currency,
        "_price_label": price_label,
        "_shipping": shipping,
        "_hours_left": hours_left,
        "_age_minutes": None,
        "_warnings": warning_list,
        "_detected_tag": tag_name,
        "_tag_score": tag_score,
    })

    return True, "통과", processed


def evaluate_fixed_price(
    item: dict[str, Any],
    hard_excludes: list[str],
    soft_warnings: list[str],
    tag_config: dict[str, Any],
    new_listing_minutes: float,
    maximum_total_usd: float,
) -> tuple[bool, str, dict[str, Any]]:
    title = item.get("title", "")

    excluded_keyword = (
        find_hard_exclude_keyword(
            title,
            hard_excludes,
        )
    )

    if excluded_keyword:
        return (
            False,
            f"제외 키워드: {excluded_keyword}",
            item,
        )

    if has_clearly_small_size(title):
        return False, "작은 사이즈 명확", item

    price, currency = get_fixed_price(item)

    if price is None or price <= 0:
        return False, "즉시구매가 없음", item

    shipping = get_shipping_cost(item)

    if price + shipping > maximum_total_usd:
        return False, "$500 초과", item

    age_minutes = get_listing_age_minutes(item)

    # 등록 시간이 제공되면 최근 등록 기준을 적용
    if (
        age_minutes is not None
        and age_minutes > new_listing_minutes
    ):
        return False, "최근 등록 상품 아님", item

    warning_list = find_soft_warnings(
        title,
        soft_warnings,
    )

    tag_name, tag_score = detect_tag_brand(
        title,
        tag_config,
    )

    buying_options = item.get(
        "buyingOptions",
        [],
    )

    processed = dict(item)

    processed.update({
        "_listing_type": "FIXED_PRICE",
        "_price": price,
        "_currency": currency,
        "_price_label": "즉시구매가",
        "_shipping": shipping,
        "_hours_left": None,
        "_age_minutes": age_minutes,
        "_warnings": warning_list,
        "_detected_tag": tag_name,
        "_tag_score": tag_score,
        "_best_offer": (
            "BEST_OFFER" in buying_options
        ),
    })

    return True, "통과", processed


# =========================================================
# 통합 검색
# =========================================================

def search_all_queries(
    token: str,
    queries: list[str],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    unique_auctions: dict[
        str,
        dict[str, Any],
    ] = {}

    unique_fixed_items: dict[
        str,
        dict[str, Any],
    ] = {}

    for index, query in enumerate(
        queries,
        start=1,
    ):
        print(
            f"[{index}/{len(queries)}] 검색: "
            f"{query}"
        )

        try:
            auction_items = search_one_query(
                token,
                query,
                "AUCTION",
            )
        except requests.RequestException as error:
            print(f"경매 검색 실패: {query}")
            print(error)
            auction_items = []

        try:
            fixed_items = search_one_query(
                token,
                query,
                "FIXED_PRICE",
            )
        except requests.RequestException as error:
            print(f"즉시구매 검색 실패: {query}")
            print(error)
            fixed_items = []

        for item in auction_items:
            item_id = item.get("itemId")

            if item_id:
                unique_auctions[item_id] = item

        for item in fixed_items:
            item_id = item.get("itemId")

            if item_id:
                unique_fixed_items[item_id] = item

    return (
        list(unique_auctions.values()),
        list(unique_fixed_items.values()),
    )


# =========================================================
# 텔레그램 메시지
# =========================================================

def build_message(
    item: dict[str, Any],
    exchange_rate: float,
    forwarding_fee_usd: float,
) -> str:
    title = item.get("title", "제목 없음")

    listing_type = item["_listing_type"]
    price = float(item["_price"])
    shipping = float(item["_shipping"])

    total_usd = (
        price
        + shipping
        + forwarding_fee_usd
    )

    price_krw = round(price * exchange_rate)
    shipping_krw = round(
        shipping * exchange_rate
    )
    total_krw = round(
        total_usd * exchange_rate
    )

    ebay_url = item.get(
        "itemWebUrl",
        "링크 없음",
    )

    warning_list = item.get(
        "_warnings",
        [],
    )

    warning_text = ""

    if warning_list:
        warning_text = (
            "\n⚠️ 주의 키워드: "
            + ", ".join(warning_list)
        )

    if listing_type == "AUCTION":
        type_text = "🔨 경매"
        time_text = (
            "⏰ 남은 시간: "
            + format_time_left(
                item.get("_hours_left")
            )
        )
    else:
        type_text = "⚡ 즉시구매"
        time_text = (
            "🕒 등록 시간: "
            + format_listing_age(
                item.get("_age_minutes")
            )
        )

        if item.get("_best_offer"):
            type_text += " · 가격 제안 가능"

    return f"""
🎯 빈티지 레이더

{type_text}
👕 {title}

💰 {item["_price_label"]}: ${price:.2f} / 약 {price_krw:,}원
🚚 미국 배송비: ${shipping:.2f} / 약 {shipping_krw:,}원
💵 총 예상금액: ${total_usd:.2f} / 약 {total_krw:,}원
📦 배대지 $10 포함

{time_text}{warning_text}

🔗 eBay 바로가기
{ebay_url}
""".strip()


def send_alerts(
    items: list[dict[str, Any]],
    seen_ids: set[str],
    maximum_alerts: int,
    forwarding_fee_usd: float,
) -> set[str]:
    if not items:
        send_telegram_message(
            "🎯 빈티지 레이더\n\n"
            "이번 검색에서는 기준을 충족한 상품 없음"
        )
        return seen_ids

    exchange_result = usd_to_krw(1)
    exchange_rate = float(
        exchange_result["rate"]
    )

    sent_count = 0

    for item in items:
        if sent_count >= maximum_alerts:
            break

        item_id = item.get("itemId")

        if not item_id:
            continue

        if item_id in seen_ids:
            continue

        message = build_message(
            item,
            exchange_rate,
            forwarding_fee_usd,
        )

        send_telegram_message(message)

        seen_ids.add(item_id)
        sent_count += 1

    if sent_count == 0:
        send_telegram_message(
            "🎯 빈티지 레이더\n\n"
            "이번 검색에서는 새로운 조건 충족 상품 없음"
        )

    return seen_ids


# =========================================================
# 메인
# =========================================================

def main() -> None:
    (
        priority_config,
        pattern_config,
        excluded_config,
        tag_config,
    ) = load_all_configs()

    selected_queries = build_selected_queries(
        priority_config,
        pattern_config,
    )

    hard_excludes = excluded_config.get(
        "hard_exclude",
        [],
    )

    soft_warnings = excluded_config.get(
        "soft_warning",
        [],
    )

    new_listing_minutes = float(
        pattern_config.get(
            "fixed_price_new_listing_minutes",
            DEFAULT_FIXED_PRICE_NEW_MINUTES,
        )
    )

    auction_max_hours = float(
        pattern_config.get(
            "auction_max_hours_left",
            DEFAULT_AUCTION_MAX_HOURS,
        )
    )

    forwarding_fee_usd = float(
        pattern_config.get(
            "forwarding_fee_usd",
            DEFAULT_FORWARDING_FEE_USD,
        )
    )

    maximum_total_usd = float(
        pattern_config.get(
            "max_item_and_us_shipping_usd",
            DEFAULT_MAX_TOTAL_USD,
        )
    )

    maximum_alerts = int(
        pattern_config.get(
            "max_alerts_per_run",
            DEFAULT_MAX_ALERTS,
        )
    )

    print("이번 실행 검색어:")
    print("=" * 60)

    for query in selected_queries:
        print(f"- {query}")

    print("=" * 60)

    token = get_access_token()

    auctions, fixed_items = search_all_queries(
        token,
        selected_queries,
    )

    print(f"검색된 경매: {len(auctions)}개")
    print(
        f"검색된 즉시구매: "
        f"{len(fixed_items)}개"
    )

    qualified_auctions = []
    qualified_fixed_items = []

    rejected_count = 0

    for item in auctions:
        passed, reason, processed = (
            evaluate_auction(
                token,
                item,
                hard_excludes,
                soft_warnings,
                tag_config,
                auction_max_hours,
                maximum_total_usd,
            )
        )

        if passed:
            qualified_auctions.append(
                processed
            )
        else:
            rejected_count += 1
            print(
                "경매 탈락:",
                item.get("title", "제목 없음"),
                "/",
                reason,
            )

    for item in fixed_items:
        passed, reason, processed = (
            evaluate_fixed_price(
                item,
                hard_excludes,
                soft_warnings,
                tag_config,
                new_listing_minutes,
                maximum_total_usd,
            )
        )

        if passed:
            qualified_fixed_items.append(
                processed
            )
        else:
            rejected_count += 1
            print(
                "즉시구매 탈락:",
                item.get("title", "제목 없음"),
                "/",
                reason,
            )

    # 즉시구매는 최신순
    qualified_fixed_items.sort(
        key=lambda item: (
            item.get("_age_minutes")
            if item.get("_age_minutes")
            is not None
            else 0
        )
    )

    # 경매는 종료 임박순
    qualified_auctions.sort(
        key=lambda item: (
            item.get("_hours_left")
            if item.get("_hours_left")
            is not None
            else float("inf")
        )
    )

    # 즉시구매를 먼저 보여주고 경매를 이어서 표시
    combined_items = (
        qualified_fixed_items
        + qualified_auctions
    )

    print("=" * 60)
    print(
        f"통과한 즉시구매: "
        f"{len(qualified_fixed_items)}개"
    )
    print(
        f"통과한 경매: "
        f"{len(qualified_auctions)}개"
    )
    print(f"탈락: {rejected_count}개")
    print("=" * 60)

    seen_ids = load_seen_item_ids()

    updated_seen_ids = send_alerts(
        combined_items,
        seen_ids,
        maximum_alerts,
        forwarding_fee_usd,
    )

    save_seen_item_ids(
        updated_seen_ids
    )


if __name__ == "__main__":
    main()
