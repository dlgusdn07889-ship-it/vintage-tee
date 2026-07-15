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
from market_reference import (
    calculate_listing_discount,
    get_active_price_reference,
)


# =========================================================
# eBay 및 프로젝트 설정
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

ARTIST_DATABASE_PATH = CONFIG_DIR / "artist_database.json"
SEARCH_PATTERNS_PATH = CONFIG_DIR / "search_patterns.json"
EXCLUDED_KEYWORDS_PATH = CONFIG_DIR / "excluded_keywords.json"
TAG_BRANDS_PATH = CONFIG_DIR / "tag_brands.json"
SEEN_ITEMS_PATH = DATA_DIR / "seen_items.json"

# 현재는 부담 없는 매입가를 우선
MAX_ITEM_AND_US_SHIPPING_USD = 250.0

FORWARDING_FEE_USD = 10.0
AUCTION_MAX_HOURS_LEFT = 24.0
FIXED_PRICE_MAX_AGE_MINUTES = 20.0

MAX_ALERTS_PER_RUN = 5
SEARCH_LIMIT_PER_QUERY = 50

# 실행당 검색 비율
TIER_1_ARTISTS_PER_RUN = 6
TIER_2_ARTISTS_PER_RUN = 3
TIER_3_ARTISTS_PER_RUN = 1

# 한 아티스트가 알림을 독점하지 못하게 제한
MAX_ALERTS_PER_ARTIST = 1


MIN_ASKING_DISCOUNT_PERCENT = 30.0


# =========================================================
# JSON 불러오기
# =========================================================

def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        print(f"설정 파일 없음: {path}")
        return default

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def load_configs() -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    artist_database = load_json(
        ARTIST_DATABASE_PATH,
        {
            "tier_1": [],
            "tier_2": [],
            "tier_3": [],
        },
    )

    search_patterns = load_json(
        SEARCH_PATTERNS_PATH,
        {
            "top_priority_patterns": [
                "{subject} shirt",
                "{subject} tee",
            ],
            "normal_patterns": [
                "{subject} shirt",
                "{subject} tee",
            ],
        },
    )

    excluded_keywords = load_json(
        EXCLUDED_KEYWORDS_PATH,
        {
            "hard_exclude": [],
            "soft_warning": [],
        },
    )

    tag_brands = load_json(
        TAG_BRANDS_PATH,
        {
            "tag_scores": {},
            "tag_aliases": {},
        },
    )

    return (
        artist_database,
        search_patterns,
        excluded_keywords,
        tag_brands,
    )


# =========================================================
# 중복 알림 기록
# =========================================================

def load_seen_item_ids() -> set[str]:
    data = load_json(
        SEEN_ITEMS_PATH,
        {"item_ids": []},
    )

    return set(data.get("item_ids", []))


def save_seen_item_ids(item_ids: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 지나치게 커지지 않도록 최대 5,000개 보관
    trimmed_ids = sorted(item_ids)[-5000:]

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
# 검색 아티스트 및 검색어 선택
# =========================================================

def pick_rotating_items(
    items: list[str],
    count: int,
    slot_number: int,
    offset_multiplier: int,
) -> list[str]:
    if not items or count <= 0:
        return []

    count = min(count, len(items))

    start_index = (
        slot_number * count * offset_multiplier
    ) % len(items)

    selected: list[str] = []

    for offset in range(count):
        index = (start_index + offset) % len(items)
        selected.append(items[index])

    return selected


def select_artists(
    artist_database: dict[str, Any],
) -> list[tuple[str, str]]:
    # 10분마다 다음 검색 조합으로 이동
    slot_number = int(
        datetime.now(timezone.utc).timestamp() // 600
    )

    tier_1 = artist_database.get("tier_1", [])
    tier_2 = artist_database.get("tier_2", [])
    tier_3 = artist_database.get("tier_3", [])

    selected: list[tuple[str, str]] = []

    for artist in pick_rotating_items(
        tier_1,
        TIER_1_ARTISTS_PER_RUN,
        slot_number,
        1,
    ):
        selected.append((artist, "tier_1"))

    for artist in pick_rotating_items(
        tier_2,
        TIER_2_ARTISTS_PER_RUN,
        slot_number,
        3,
    ):
        selected.append((artist, "tier_2"))

    for artist in pick_rotating_items(
        tier_3,
        TIER_3_ARTISTS_PER_RUN,
        slot_number,
        7,
    ):
        selected.append((artist, "tier_3"))

    random_generator = random.Random(slot_number)
    random_generator.shuffle(selected)

    return selected


def build_queries(
    selected_artists: list[tuple[str, str]],
    search_patterns: dict[str, Any],
) -> list[dict[str, str]]:
    top_patterns = search_patterns.get(
        "top_priority_patterns",
        ["{subject} shirt"],
    )

    normal_patterns = search_patterns.get(
        "normal_patterns",
        ["{subject} shirt"],
    )

    slot_number = int(
        datetime.now(timezone.utc).timestamp() // 600
    )

    queries: list[dict[str, str]] = []

    for index, (artist, tier) in enumerate(selected_artists):
        patterns = (
            top_patterns
            if tier == "tier_1"
            else normal_patterns
        )

        pattern_index = (
            slot_number + index
        ) % len(patterns)

        query = patterns[pattern_index].format(
            subject=artist
        )

        queries.append({
            "artist": artist,
            "tier": tier,
            "query": query,
        })

    return queries


# =========================================================
# eBay 인증 및 요청
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
            "grant_type": "client_credentials",
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


def ebay_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID,
        "X-EBAY-C-ENDUSERCTX": (
            "contextualLocation=country%3DUS%2Czip%3D97250"
        ),
    }


def search_one_query(
    token: str,
    query: str,
    buying_option: str,
) -> list[dict[str, Any]]:
    sort_value = (
        "endingSoonest"
        if buying_option == "AUCTION"
        else "newlyListed"
    )

    response = requests.get(
        SEARCH_URL,
        headers=ebay_headers(token),
        params={
            "q": query,
            "limit": SEARCH_LIMIT_PER_QUERY,
            "filter": (
                f"buyingOptions:{{{buying_option}}},"
                f"price:[..{MAX_ITEM_AND_US_SHIPPING_USD}],"
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
    encoded_item_id = quote(item_id, safe="")

    response = requests.get(
        f"{ITEM_URL}/{encoded_item_id}",
        headers=ebay_headers(token),
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


# =========================================================
# 가격 및 배송비
# =========================================================

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
        return float(raw_value), currency
    except (TypeError, ValueError):
        return None, currency


def get_shipping_cost(
    item: dict[str, Any],
) -> float | None:
    shipping_options = item.get(
        "shippingOptions",
        [],
    )

    if not isinstance(shipping_options, list):
        return None

    for option in shipping_options:
        shipping_cost, _ = parse_amount(
            option.get("shippingCost")
        )

        if shipping_cost is not None:
            return shipping_cost

    return None


def resolve_shipping_cost(
    token: str,
    item: dict[str, Any],
    detailed_item: dict[str, Any] | None = None,
) -> tuple[float | None, dict[str, Any]]:
    """미국 배대지 ZIP 기준 배송비를 최대한 안전하게 확인한다."""
    shipping = get_shipping_cost(item)

    if shipping is not None:
        return shipping, detailed_item or item

    if detailed_item is not None:
        shipping = get_shipping_cost(detailed_item)
        if shipping is not None:
            return shipping, detailed_item

    item_id = item.get("itemId")
    if not item_id:
        return None, detailed_item or item

    try:
        fetched_details = get_item_details(token, item_id)
    except requests.RequestException as error:
        print(
            "배송비 상세조회 실패:",
            item.get("title", "제목 없음"),
        )
        print(error)
        return None, detailed_item or item

    return get_shipping_cost(fetched_details), fetched_details


def get_fixed_price(
    item: dict[str, Any],
) -> tuple[float | None, str]:
    return parse_amount(item.get("price"))


def get_auction_price(
    item: dict[str, Any],
) -> tuple[float | None, str, str]:
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

        label = (
            "다음 최소 입찰가"
            if bid_count > 0
            else "시작가"
        )

        return minimum_bid, currency, label

    price, currency = parse_amount(item.get("price"))

    if price is not None and price > 0:
        return price, currency, "시작가"

    return None, "USD", "가격 확인 필요"


def resolve_auction_price(
    token: str,
    item: dict[str, Any],
) -> tuple[float | None, str, str, dict[str, Any]]:
    price, currency, label = get_auction_price(item)

    if price is not None:
        return price, currency, label, item

    item_id = item.get("itemId")

    if not item_id:
        return None, "USD", "가격 확인 필요", item

    try:
        detailed_item = get_item_details(
            token,
            item_id,
        )
    except requests.RequestException as error:
        print(
            "상품 상세조회 실패:",
            item.get("title", "제목 없음"),
        )
        print(error)

        return None, "USD", "가격 확인 필요", item

    price, currency, label = get_auction_price(
        detailed_item
    )

    return price, currency, label, detailed_item


# =========================================================
# 시간 처리
# =========================================================

def parse_ebay_datetime(
    value: str | None,
) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError:
        return None


def get_hours_left(
    end_date: str | None,
) -> float | None:
    end_time = parse_ebay_datetime(end_date)

    if end_time is None:
        return None

    return (
        end_time - datetime.now(timezone.utc)
    ).total_seconds() / 3600


def get_listing_age_minutes(
    item: dict[str, Any],
) -> float | None:
    date_value = (
        item.get("itemCreationDate")
        or item.get("itemOriginDate")
    )

    creation_time = parse_ebay_datetime(date_value)

    if creation_time is None:
        return None

    return (
        datetime.now(timezone.utc) - creation_time
    ).total_seconds() / 60


def format_time_left(hours_left: float | None) -> str:
    if hours_left is None:
        return "종료시간 확인 필요"

    if hours_left <= 0:
        return "종료됨"

    total_minutes = int(hours_left * 60)
    hours, minutes = divmod(total_minutes, 60)

    return f"{hours}시간 {minutes}분"


def format_listing_age(
    age_minutes: float | None,
) -> str:
    if age_minutes is None:
        return "등록시간 확인 필요"

    if age_minutes < 1:
        return "방금 전"

    if age_minutes < 60:
        return f"{int(age_minutes)}분 전"

    return f"{int(age_minutes // 60)}시간 전"


# =========================================================
# 제외 키워드 및 위험 키워드
# =========================================================

def find_matching_keyword(
    text: str,
    keywords: list[str],
) -> str | None:
    lower_text = text.lower()

    for keyword in keywords:
        if keyword.lower() in lower_text:
            return keyword

    return None


def find_warning_keywords(
    text: str,
    keywords: list[str],
) -> list[str]:
    lower_text = text.lower()

    return [
        keyword
        for keyword in keywords
        if keyword.lower() in lower_text
    ]


def has_explicit_small_size(title: str) -> bool:
    normalized = title.lower()

    patterns = [
        r"\bsize\s*xs\b",
        r"\bextra[\s-]?small\b",
        r"\bsize\s*small\b",
        r"\bsize\s*s\b",
        r"\bmens?\s+small\b",
        r"\bwomens?\s+small\b",
        r"\bladies\s+small\b",
        r"\bgirls?\b",
    ]

    return any(
        re.search(pattern, normalized)
        for pattern in patterns
    )


# =========================================================
# 태그 감지 — 현재는 내부 참고용
# =========================================================

def detect_tag(
    title: str,
    tag_config: dict[str, Any],
) -> tuple[str | None, int]:
    lower_title = title.lower()

    scores = tag_config.get("tag_scores", {})
    aliases = tag_config.get("tag_aliases", {})

    for alias, canonical_name in aliases.items():
        if alias.lower() in lower_title:
            return (
                canonical_name,
                int(scores.get(canonical_name, 0)),
            )

    ordered_tags = sorted(
        scores.items(),
        key=lambda entry: len(entry[0]),
        reverse=True,
    )

    for tag_name, score in ordered_tags:
        if tag_name.lower() in lower_title:
            return tag_name, int(score)

    return None, 0



# =========================================================
# 검색 정확도 및 내부 우선순위
# =========================================================

ARTIST_ALIASES: dict[str, list[str]] = {
    "ACDC": ["AC/DC", "AC DC"],
    "Guns N Roses": ["Guns N' Roses", "Guns N Roses", "GNR"],
    "Mötley Crüe": ["Motley Crue", "Mötley Crüe"],
    "Motorhead": ["Motörhead", "Motorhead"],
    "Notorious BIG": ["The Notorious B.I.G.", "Notorious BIG", "Biggie Smalls"],
    "NWA": ["N.W.A.", "NWA"],
    "Run DMC": ["Run-D.M.C.", "Run DMC"],
    "Wu-Tang Clan": ["Wu-Tang Clan", "Wu Tang Clan", "Wu-Tang"],
    "Tupac": ["Tupac", "2Pac", "Makaveli"],
    "Red Hot Chili Peppers": ["Red Hot Chili Peppers", "RHCP"],
    "Rage Against the Machine": ["Rage Against the Machine", "RATM"],
    "Nine Inch Nails": ["Nine Inch Nails", "NIN"],
    "Stone Temple Pilots": ["Stone Temple Pilots", "STP"],
    "Alice in Chains": ["Alice in Chains", "AIC"],
}


def normalize_search_text(value: str) -> str:
    normalized = value.lower().replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def title_matches_artist(title: str, artist: str) -> bool:
    normalized_title = normalize_search_text(title)
    aliases = ARTIST_ALIASES.get(artist, [artist])

    for alias in aliases:
        normalized_alias = normalize_search_text(alias)
        if normalized_alias and normalized_alias in normalized_title:
            return True

    return False


def extract_listing_year(title: str) -> int | None:
    matches = re.findall(r"\b(?:19\d{2}|20\d{2})\b", title)
    if not matches:
        return None

    years = [int(value) for value in matches]
    plausible = [year for year in years if 1970 <= year <= 2030]
    return min(plausible) if plausible else None


def calculate_quality_score(item: dict[str, Any]) -> int:
    title = item.get("title", "").lower()
    tier = item.get("_tier", "tier_3")
    score = {"tier_1": 30, "tier_2": 20, "tier_3": 10}.get(tier, 0)

    year = extract_listing_year(title)
    if year is not None:
        if 1980 <= year <= 1999:
            score += 20
        elif 2000 <= year <= 2005:
            score += 5
        elif year >= 2006:
            score -= 15

    score += min(int(item.get("_tag_score", 0)) // 2, 20)

    if "vintage" in title or "old" in title:
        score += 8
    if "single stitch" in title or "single-stitch" in title:
        score += 10
    if "made in usa" in title or "made in u.s.a" in title:
        score += 5
    if "tour" in title or "concert" in title:
        score += 5

    warnings = item.get("_warnings", [])
    score -= min(len(warnings) * 8, 24)

    total_before_forwarding = float(item.get("_price", 0)) + float(
        item.get("_shipping", 0)
    )
    if total_before_forwarding <= 100:
        score += 10
    elif total_before_forwarding <= 175:
        score += 5

    if item.get("_listing_type") == "FIXED_PRICE":
        age = item.get("_age_minutes")
        if age is not None and age <= 10:
            score += 5
    else:
        hours = item.get("_hours_left")
        if hours is not None and hours <= 6:
            score += 5

    return max(score, 0)

# =========================================================
# 상품 평가
# =========================================================

def evaluate_auction(
    token: str,
    item: dict[str, Any],
    artist: str,
    tier: str,
    hard_excludes: list[str],
    warning_keywords: list[str],
    tag_config: dict[str, Any],
) -> tuple[bool, str, dict[str, Any]]:
    title = item.get("title", "")

    if not title_matches_artist(title, artist):
        return False, "검색 아티스트와 제목 불일치", item

    excluded = find_matching_keyword(
        title,
        hard_excludes,
    )

    if excluded:
        return False, f"제외 키워드: {excluded}", item

    if has_explicit_small_size(title):
        return False, "작은 사이즈 명확", item

    hours_left = get_hours_left(
        item.get("itemEndDate")
    )

    if hours_left is None:
        return False, "종료시간 확인 불가", item

    if hours_left <= 0:
        return False, "종료됨", item

    if hours_left > AUCTION_MAX_HOURS_LEFT:
        return False, "종료까지 24시간 초과", item

    (
        price,
        currency,
        price_label,
        detailed_item,
    ) = resolve_auction_price(token, item)

    if price is None or price <= 0:
        return False, "현재 가격 확인 불가", item

    shipping, detailed_item = resolve_shipping_cost(
        token,
        item,
        detailed_item,
    )

    if shipping is None:
        return False, "미국 배송비 확인 불가", item

    if price + shipping > MAX_ITEM_AND_US_SHIPPING_USD:
        return False, "예산 $250 초과", item

    tag_name, tag_score = detect_tag(
        title,
        tag_config,
    )

    processed = dict(item)

    processed.update({
        "_artist": artist,
        "_tier": tier,
        "_listing_type": "AUCTION",
        "_price": price,
        "_currency": currency,
        "_price_label": price_label,
        "_shipping": shipping,
        "_hours_left": hours_left,
        "_age_minutes": None,
        "_warnings": find_warning_keywords(
            title,
            warning_keywords,
        ),
        "_tag_name": tag_name,
        "_tag_score": tag_score,
    })
    processed["_quality_score"] = calculate_quality_score(processed)

    return True, "통과", processed


def evaluate_fixed_price(
    token: str,
    item: dict[str, Any],
    artist: str,
    tier: str,
    hard_excludes: list[str],
    warning_keywords: list[str],
    tag_config: dict[str, Any],
) -> tuple[bool, str, dict[str, Any]]:
    title = item.get("title", "")

    if not title_matches_artist(title, artist):
        return False, "검색 아티스트와 제목 불일치", item

    excluded = find_matching_keyword(
        title,
        hard_excludes,
    )

    if excluded:
        return False, f"제외 키워드: {excluded}", item

    if has_explicit_small_size(title):
        return False, "작은 사이즈 명확", item

    price, currency = get_fixed_price(item)

    if price is None or price <= 0:
        return False, "즉시구매가 확인 불가", item

    shipping, detailed_item = resolve_shipping_cost(
        token,
        item,
    )

    if shipping is None:
        return False, "미국 배송비 확인 불가", item

    if price + shipping > MAX_ITEM_AND_US_SHIPPING_USD:
        return False, "예산 $250 초과", item

    age_minutes = get_listing_age_minutes(item)

    if (
        age_minutes is not None
        and age_minutes > FIXED_PRICE_MAX_AGE_MINUTES
    ):
        return False, "최근 등록 상품 아님", item

    tag_name, tag_score = detect_tag(
        title,
        tag_config,
    )

    buying_options = item.get("buyingOptions", [])

    processed = dict(item)

    processed.update({
        "_artist": artist,
        "_tier": tier,
        "_listing_type": "FIXED_PRICE",
        "_price": price,
        "_currency": currency,
        "_price_label": "즉시구매가",
        "_shipping": shipping,
        "_hours_left": None,
        "_age_minutes": age_minutes,
        "_warnings": find_warning_keywords(
            title,
            warning_keywords,
        ),
        "_tag_name": tag_name,
        "_tag_score": tag_score,
        "_best_offer": (
            "BEST_OFFER" in buying_options
        ),
    })
    processed["_quality_score"] = calculate_quality_score(processed)

    return True, "통과", processed


# =========================================================
# 모든 검색 실행
# =========================================================

def search_all(
    token: str,
    query_entries: list[dict[str, str]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    auction_items: dict[str, dict[str, Any]] = {}
    fixed_items: dict[str, dict[str, Any]] = {}

    for index, entry in enumerate(
        query_entries,
        start=1,
    ):
        artist = entry["artist"]
        tier = entry["tier"]
        query = entry["query"]

        print(
            f"[{index}/{len(query_entries)}] "
            f"{tier} / {artist} / {query}"
        )

        try:
            results = search_one_query(
                token,
                query,
                "AUCTION",
            )

            for item in results:
                item_id = item.get("itemId")

                if item_id:
                    item["_search_artist"] = artist
                    item["_search_tier"] = tier
                    auction_items[item_id] = item

        except requests.RequestException as error:
            print(f"경매 검색 실패: {query}")
            print(error)

        try:
            results = search_one_query(
                token,
                query,
                "FIXED_PRICE",
            )

            for item in results:
                item_id = item.get("itemId")

                if item_id:
                    item["_search_artist"] = artist
                    item["_search_tier"] = tier
                    fixed_items[item_id] = item

        except requests.RequestException as error:
            print(f"즉시구매 검색 실패: {query}")
            print(error)

    return (
        list(auction_items.values()),
        list(fixed_items.values()),
    )


# =========================================================
# 알림 정렬 및 분산
# =========================================================

def tier_weight(tier: str) -> int:
    weights = {
        "tier_1": 3,
        "tier_2": 2,
        "tier_3": 1,
    }

    return weights.get(tier, 0)


def prepare_alert_items(
    fixed_items: list[dict[str, Any]],
    auction_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    fixed_items.sort(
        key=lambda item: (
            -int(item.get("_quality_score", 0)),
            -tier_weight(item.get("_tier", "")),
            item.get("_age_minutes")
            if item.get("_age_minutes") is not None
            else float("inf"),
        )
    )

    auction_items.sort(
        key=lambda item: (
            -int(item.get("_quality_score", 0)),
            -tier_weight(item.get("_tier", "")),
            item.get("_hours_left")
            if item.get("_hours_left") is not None
            else float("inf"),
        )
    )

    combined = fixed_items + auction_items

    selected: list[dict[str, Any]] = []
    artist_counts: dict[str, int] = {}

    for item in combined:
        artist = item.get("_artist", "알 수 없음")

        current_count = artist_counts.get(artist, 0)

        if current_count >= MAX_ALERTS_PER_ARTIST:
            continue

        selected.append(item)
        artist_counts[artist] = current_count + 1

        if len(selected) >= MAX_ALERTS_PER_RUN:
            break

    return selected




# =========================================================
# 현재 eBay 호가 참고값
# =========================================================

def add_market_references(
    token: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """알림 후보에 한해서 현재 즉시구매 호가 중앙값을 붙인다."""
    reference_cache: dict[str, dict[str, Any] | None] = {}

    for item in items:
        artist = str(item.get("_artist", "")).strip()

        if not artist:
            item["_market_reference"] = None
            continue

        query = f"{artist} vintage shirt"

        if query not in reference_cache:
            try:
                reference_cache[query] = get_active_price_reference(
                    token=token,
                    query=query,
                    maximum_results=50,
                )
            except requests.RequestException as error:
                print(f"호가 참고값 조회 실패: {query}")
                print(error)
                reference_cache[query] = None

        reference = reference_cache[query]
        item["_market_reference"] = reference

        if reference is None:
            item["_asking_discount_percent"] = None
            continue

        total_cost = (
            float(item.get("_price", 0))
            + float(item.get("_shipping", 0))
            + FORWARDING_FEE_USD
        )

        item["_asking_discount_percent"] = calculate_listing_discount(
            total_cost=total_cost,
            reference_median=float(reference["median"]),
        )

    return items


def filter_undervalued_items(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """현재 호가 중앙값보다 최소 기준 이상 저렴한 매물만 남긴다."""
    filtered: list[dict[str, Any]] = []

    for item in items:
        discount = item.get("_asking_discount_percent")

        if discount is None:
            print(
                "호가 비교 제외 / 참고값 없음 / "
                f"{item.get('title', '제목 없음')}"
            )
            continue

        if float(discount) < MIN_ASKING_DISCOUNT_PERCENT:
            print(
                "호가 비교 탈락 / "
                f"{float(discount):.1f}% / "
                f"{item.get('title', '제목 없음')}"
            )
            continue

        filtered.append(item)

    return filtered


# =========================================================
# 텔레그램 메시지
# =========================================================

def build_message(
    item: dict[str, Any],
    exchange_rate: float,
) -> str:
    title = item.get("title", "제목 없음")
    listing_type = item["_listing_type"]

    price = float(item["_price"])
    shipping = float(item["_shipping"])

    total_usd = (
        price
        + shipping
        + FORWARDING_FEE_USD
    )

    price_krw = round(price * exchange_rate)
    shipping_krw = round(shipping * exchange_rate)
    total_krw = round(total_usd * exchange_rate)

    ebay_url = item.get(
        "itemWebUrl",
        "링크 없음",
    )

    if listing_type == "AUCTION":
        type_line = "🔨 경매"
        time_line = (
            "⏰ 남은 시간: "
            f"{format_time_left(item.get('_hours_left'))}"
        )
    else:
        type_line = "⚡ 즉시구매"

        if item.get("_best_offer"):
            type_line += " · 가격 제안 가능"

        time_line = (
            "🕒 등록 시간: "
            f"{format_listing_age(item.get('_age_minutes'))}"
        )

    warnings = item.get("_warnings", [])

    warning_line = ""

    if warnings:
        warning_line = (
            "\n⚠️ 주의: "
            + ", ".join(warnings)
        )

    reference = item.get("_market_reference")
    discount_percent = item.get("_asking_discount_percent")
    reference_lines = ""

    if reference is not None:
        median_usd = float(reference["median"])
        median_krw = round(median_usd * exchange_rate)
        sample_count = int(reference["sample_count"])

        reference_lines = (
            f"\n📊 현재 호가 중앙값: ${median_usd:.2f} "
            f"/ 약 {median_krw:,}원 ({sample_count}건)"
        )

        if discount_percent is not None:
            reference_lines += (
                f"\n💸 현재 호가 대비: {discount_percent:.1f}% 저렴"
                if discount_percent >= 0
                else f"\n💸 현재 호가 대비: {abs(discount_percent):.1f}% 비쌈"
            )

    return f"""
🎯 빈티지 레이더

{type_line}
👕 {title}

💰 {item["_price_label"]}: ${price:.2f} / 약 {price_krw:,}원
🚚 미국 배송비: ${shipping:.2f} / 약 {shipping_krw:,}원
💵 총 예상금액: ${total_usd:.2f} / 약 {total_krw:,}원
📦 배대지 $10 포함
{time_line}{reference_lines}{warning_line}

🔗 eBay 바로가기
{ebay_url}
""".strip()


def send_alerts(
    items: list[dict[str, Any]],
    seen_item_ids: set[str],
) -> set[str]:
    new_items = [
        item
        for item in items
        if item.get("itemId") not in seen_item_ids
    ]

    if not new_items:
        send_telegram_message(
            "🎯 빈티지 레이더\n\n"
            "이번 검색에서는 새로운 조건 충족 상품 없음"
        )
        return seen_item_ids

    exchange_result = usd_to_krw(1)
    exchange_rate = float(exchange_result["rate"])

    for item in new_items:
        item_id = item.get("itemId")

        if not item_id:
            continue

        send_telegram_message(
            build_message(
                item,
                exchange_rate,
            )
        )

        seen_item_ids.add(item_id)

    return seen_item_ids


# =========================================================
# 메인
# =========================================================

def main() -> None:
    (
        artist_database,
        search_patterns,
        excluded_keywords,
        tag_config,
    ) = load_configs()

    selected_artists = select_artists(
        artist_database
    )

    query_entries = build_queries(
        selected_artists,
        search_patterns,
    )

    hard_excludes = excluded_keywords.get(
        "hard_exclude",
        [],
    )

    warning_keywords = excluded_keywords.get(
        "soft_warning",
        [],
    )

    print("이번 실행 검색 대상")
    print("=" * 70)

    for entry in query_entries:
        print(
            f"- {entry['tier']} / "
            f"{entry['artist']} / "
            f"{entry['query']}"
        )

    print("=" * 70)

    token = get_access_token()

    auctions, fixed_items = search_all(
        token,
        query_entries,
    )

    qualified_auctions: list[dict[str, Any]] = []
    qualified_fixed: list[dict[str, Any]] = []

    for item in auctions:
        artist = item.get(
            "_search_artist",
            "알 수 없음",
        )

        tier = item.get(
            "_search_tier",
            "tier_3",
        )

        passed, reason, processed = evaluate_auction(
            token,
            item,
            artist,
            tier,
            hard_excludes,
            warning_keywords,
            tag_config,
        )

        if passed:
            qualified_auctions.append(processed)
        else:
            print(
                f"경매 탈락 / {artist} / "
                f"{reason} / "
                f"{item.get('title', '제목 없음')}"
            )

    for item in fixed_items:
        artist = item.get(
            "_search_artist",
            "알 수 없음",
        )

        tier = item.get(
            "_search_tier",
            "tier_3",
        )

        passed, reason, processed = evaluate_fixed_price(
            token,
            item,
            artist,
            tier,
            hard_excludes,
            warning_keywords,
            tag_config,
        )

        if passed:
            qualified_fixed.append(processed)
        else:
            print(
                f"즉시구매 탈락 / {artist} / "
                f"{reason} / "
                f"{item.get('title', '제목 없음')}"
            )

    alert_items = prepare_alert_items(
        qualified_fixed,
        qualified_auctions,
    )

    alert_items = add_market_references(
        token,
        alert_items,
    )

    alert_items = filter_undervalued_items(
        alert_items,
    )

    print("=" * 70)
    print(f"검색된 경매: {len(auctions)}개")
    print(f"검색된 즉시구매: {len(fixed_items)}개")
    print(
        f"통과한 경매: "
        f"{len(qualified_auctions)}개"
    )
    print(
        f"통과한 즉시구매: "
        f"{len(qualified_fixed)}개"
    )
    print(f"알림 후보: {len(alert_items)}개")
    print("=" * 70)

    seen_item_ids = load_seen_item_ids()

    updated_ids = send_alerts(
        alert_items,
        seen_item_ids,
    )

    save_seen_item_ids(updated_ids)


if __name__ == "__main__":
    main()
