from __future__ import annotations

import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from deal_evaluator import classify_deal
from exchange import usd_to_krw
from market_reference import get_active_price_reference
from telegram_alert import send_telegram_message

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item"
MARKETPLACE_ID = "EBAY_US"
US_ZIP_CODE = "97250"

RADAR_MODE = os.environ.get("RADAR_MODE", "ALL").upper()
if RADAR_MODE not in {"ALL", "AUCTION", "FIXED_PRICE"}:
    raise ValueError("RADAR_MODE must be ALL, AUCTION, or FIXED_PRICE")

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"

PROFILE_PATH = CONFIG_DIR / "hunter_profile.json"
ARTIST_DATABASE_PATH = CONFIG_DIR / "artist_database.json"
SEARCH_PATTERNS_PATH = CONFIG_DIR / "search_patterns.json"
EXCLUDED_KEYWORDS_PATH = CONFIG_DIR / "excluded_keywords.json"
TAG_BRANDS_PATH = CONFIG_DIR / "tag_brands.json"
SEEN_ITEMS_PATH = DATA_DIR / f"seen_items_{RADAR_MODE.lower()}.json"

ARTIST_ALIASES: dict[str, list[str]] = {
    "ACDC": ["AC/DC", "AC DC", "ACDC"],
    "Guns N Roses": ["Guns N' Roses", "Guns N Roses", "GNR"],
    "Mötley Crüe": ["Motley Crue", "Mötley Crüe"],
    "Motorhead": ["Motörhead", "Motorhead"],
    "Notorious BIG": ["The Notorious B.I.G.", "Notorious BIG", "Biggie Smalls", "Biggie"],
    "Run DMC": ["Run-D.M.C.", "Run DMC"],
    "Wu-Tang Clan": ["Wu-Tang Clan", "Wu Tang Clan", "Wu-Tang"],
    "Tupac": ["Tupac", "2Pac", "Makaveli"],
    "Red Hot Chili Peppers": ["Red Hot Chili Peppers", "RHCP"],
    "Rage Against the Machine": ["Rage Against the Machine", "RATM"],
    "Nine Inch Nails": ["Nine Inch Nails", "NIN"],
    "Alice in Chains": ["Alice in Chains", "AIC"],
}

MULTI_SIZE_PATTERNS = [
    r"\b(?:xs|s)\s*[-–]\s*(?:2xl|3xl|4xl|5xl|6xl)\b",
    r"\b(?:xs|s)\s+to\s+(?:2xl|3xl|4xl|5xl|6xl)\b",
    r"\b(?:s|m|l|xl)\s*[,/]\s*(?:m|l|xl|2xl)\b",
    r"\bs\s+m\s+l\s+xl(?:\s+2xl)?(?:\s+3xl)?\b",
    r"\ball sizes\b",
    r"\bchoose (?:your )?size\b",
    r"\bselect (?:your )?size\b",
    r"\bavailable in (?:multiple )?sizes\b",
    r"\bmultiple sizes\b",
    r"\bsize options\b",
]

SMALL_SIZE_PATTERNS = [
    r"\bsize\s*xs\b", r"\bextra[\s-]?small\b", r"\bsize\s*small\b",
    r"\bsize\s*s\b", r"\bmens?\s+small\b", r"\bwomens?\s+small\b",
    r"\bladies\s+small\b",
]

YEAR_RE = re.compile(r"\b(?:19\d{2}|20\d{2})\b")
NINETIES_RE = re.compile(r"\b199\d\b|\b90'?s\b|\b1990s\b", re.I)
MODERN_YEAR_RE = re.compile(r"\b(?:200\d|201\d|202\d)\b|\b(?:00s|2000s|y2k)\b", re.I)

API_CALLS = {"search": 0, "detail": 0, "reference": 0}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        print(f"설정 파일 없음: {path}")
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_configs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    profile = load_json(PROFILE_PATH, {})
    artist_db = load_json(ARTIST_DATABASE_PATH, {"tiers": {}})
    patterns = load_json(SEARCH_PATTERNS_PATH, {})
    excludes = load_json(EXCLUDED_KEYWORDS_PATH, {"hard_exclude": [], "soft_warning": []})
    tags = load_json(TAG_BRANDS_PATH, {"strong_tags": {}, "supporting_tags": {}, "modern_tags": [], "aliases": {}})
    return profile, artist_db, patterns, excludes, tags


def normalize(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def parse_amount(data: Any) -> tuple[float | None, str]:
    if not isinstance(data, dict):
        return None, "USD"
    raw = data.get("value")
    if raw is None:
        return None, str(data.get("currency", "USD"))
    try:
        return float(raw), str(data.get("currency", "USD"))
    except (TypeError, ValueError):
        return None, str(data.get("currency", "USD"))


def get_shipping(item: dict[str, Any]) -> float | None:
    options = item.get("shippingOptions", [])
    if not isinstance(options, list):
        return None
    for option in options:
        value, _ = parse_amount(option.get("shippingCost"))
        if value is not None:
            return value
    return None


def get_access_token() -> str:
    client_id = os.environ["EBAY_CLIENT_ID"]
    client_secret = os.environ["EBAY_CLIENT_SECRET"]
    response = requests.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("eBay 액세스 토큰을 받지 못했습니다.")
    return token


def ebay_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID,
        "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country%3DUS%2Czip%3D{US_ZIP_CODE}",
    }


def search_one_query(token: str, query: str, buying_option: str, max_price: float, limit: int) -> list[dict[str, Any]]:
    API_CALLS["search"] += 1
    response = requests.get(
        SEARCH_URL,
        headers=ebay_headers(token),
        params={
            "q": query,
            "limit": limit,
            "filter": f"buyingOptions:{{{buying_option}}},price:[..{max_price}],priceCurrency:USD",
            "sort": "endingSoonest" if buying_option == "AUCTION" else "newlyListed",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("itemSummaries", [])


def get_item_details(token: str, item_id: str) -> dict[str, Any]:
    API_CALLS["detail"] += 1
    response = requests.get(
        f"{ITEM_URL}/{quote(item_id, safe='')}",
        headers=ebay_headers(token),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def normalize_artist_database(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    raw = data.get("tiers", data)
    result = {"tier_1": [], "tier_2": [], "tier_3": []}
    for tier in result:
        for entry in raw.get(tier, []):
            if isinstance(entry, str):
                result[tier].append({"name": entry, "keywords": [entry]})
                continue
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            keywords = [str(v).strip() for v in entry.get("keywords", []) if str(v).strip()]
            if name not in keywords:
                keywords.insert(0, name)
            result[tier].append({"name": name, "keywords": list(dict.fromkeys(keywords))})
    return result


def rotating_slice(items: list[dict[str, Any]], count: int, slot: int, multiplier: int) -> list[dict[str, Any]]:
    if not items or count <= 0:
        return []
    count = min(count, len(items))
    start = (slot * count * multiplier) % len(items)
    return [items[(start + i) % len(items)] for i in range(count)]


def choose_artists(profile: dict[str, Any], artist_db: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    search = profile["search"]
    slot_seconds = 600 if RADAR_MODE == "FIXED_PRICE" else 3600
    slot = int(datetime.now(timezone.utc).timestamp() // slot_seconds)
    db = normalize_artist_database(artist_db)
    selected: list[tuple[dict[str, Any], str]] = []
    specs = [
        ("tier_1", int(search["tier_1_artists_per_run"]), 1),
        ("tier_2", int(search["tier_2_artists_per_run"]), 3),
        ("tier_3", int(search["tier_3_artists_per_run"]), 7),
    ]
    for tier, count, mult in specs:
        for artist in rotating_slice(db[tier], count, slot, mult):
            selected.append((artist, tier))
    random.Random(slot).shuffle(selected)
    return selected


def artist_query_name(artist: str, patterns: dict[str, Any]) -> str:
    ambiguous = {normalize(v) for v in patterns.get("ambiguous_artists", [])}
    if normalize(artist) in ambiguous:
        return f"{artist} {patterns.get('ambiguous_artist_suffix', 'band')}"
    return artist


def build_queries(profile: dict[str, Any], selected: list[tuple[dict[str, Any], str]], patterns: dict[str, Any]) -> list[dict[str, Any]]:
    slot_seconds = 600 if RADAR_MODE == "FIXED_PRICE" else 3600
    slot = int(datetime.now(timezone.utc).timestamp() // slot_seconds)
    broad_patterns = patterns.get("tier_1_broad_patterns", ["{artist} vintage shirt"])
    specific_patterns = patterns.get("specific_patterns", ["{artist} {subject} shirt"])
    queries: list[dict[str, Any]] = []

    for index, (entry, tier) in enumerate(selected):
        artist = entry["name"]
        query_artist = artist_query_name(artist, patterns)
        keywords = entry.get("keywords", [artist])
        specific_keywords = [k for k in keywords if normalize(k) != normalize(artist)] or [artist]
        subject = specific_keywords[(slot + index * 3) % len(specific_keywords)]
        specific = specific_patterns[(slot + index) % len(specific_patterns)].format(artist=query_artist, subject=subject)
        queries.append({
            "artist": artist,
            "tier": tier,
            "subject": subject,
            "query": specific,
            "specific": True,
            "keywords": keywords,
        })
        if tier == "tier_1":
            broad = broad_patterns[(slot + index) % len(broad_patterns)].format(artist=query_artist, subject=artist)
            queries.append({
                "artist": artist,
                "tier": tier,
                "subject": artist,
                "query": broad,
                "specific": False,
                "keywords": keywords,
            })

    dedup: dict[str, dict[str, Any]] = {}
    for entry in queries:
        dedup[normalize(entry["query"])] = entry
    return list(dedup.values())


def title_matches_artist(title: str, artist: str) -> bool:
    normalized_title = normalize(title)
    candidates = ARTIST_ALIASES.get(artist, [artist])
    return any(normalize(candidate) in normalized_title for candidate in candidates if normalize(candidate))


def contains_keyword(text: str, keywords: list[str]) -> str | None:
    low = text.lower()
    for keyword in keywords:
        if keyword.lower() in low:
            return keyword
    return None


def has_multi_size(text: str) -> bool:
    return any(re.search(pattern, text.lower()) for pattern in MULTI_SIZE_PATTERNS)


def explicit_small_size(text: str) -> bool:
    return any(re.search(pattern, text.lower()) for pattern in SMALL_SIZE_PATTERNS)


def is_variation(item: dict[str, Any]) -> bool:
    if item.get("itemGroupHref"):
        return True
    if "VARIATION" in str(item.get("itemGroupType", "")).upper():
        return True
    return False


def build_item_text(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("title", "")), str(item.get("shortDescription", "")),
        str(item.get("condition", "")), str(item.get("conditionDescription", "")),
        str(item.get("brand", "")),
    ]
    aspects = item.get("localizedAspects", [])
    if isinstance(aspects, list):
        for aspect in aspects:
            if isinstance(aspect, dict):
                parts.append(str(aspect.get("name", "")))
                parts.append(str(aspect.get("value", "")))
    return " ".join(parts)


def detect_tag(text: str, tag_config: dict[str, Any]) -> tuple[str | None, int, bool]:
    low = text.lower()
    strong = tag_config.get("strong_tags", {})
    supporting = tag_config.get("supporting_tags", {})
    aliases = tag_config.get("aliases", {})

    for alias, canonical in aliases.items():
        if alias.lower() in low:
            score = int(strong.get(canonical, supporting.get(canonical, 0)))
            return canonical, score, canonical in strong

    all_tags = {**supporting, **strong}
    for name in sorted(all_tags, key=len, reverse=True):
        if name.lower() in low:
            return name, int(all_tags[name]), name in strong
    return None, 0, False


def authenticity_check(item: dict[str, Any], profile: dict[str, Any], excludes: dict[str, Any], tags: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    text = build_item_text(item)
    low = text.lower()
    auth = profile["authenticity"]
    era = profile["era"]

    hard_hit = contains_keyword(text, excludes.get("hard_exclude", []))
    if hard_hit:
        return False, f"제외 키워드: {hard_hit}", {}
    if auth.get("reject_variations", True) and is_variation(item):
        return False, "사이즈/색상 선택형 variation", {}
    if has_multi_size(text):
        return False, "다중 사이즈 주문형", {}
    if auth.get("reject_modern_years", True) and MODERN_YEAR_RE.search(text):
        return False, "2000년대 이후 연대 신호", {}

    modern_tag = contains_keyword(text, tags.get("modern_tags", []))
    if auth.get("reject_modern_bodies", True) and modern_tag:
        return False, f"현대 바디: {modern_tag}", {}

    tag_name, tag_score, strong_tag = detect_tag(text, tags)
    if auth.get("reject_no_tag", True) and not tag_name:
        return False, "확인 가능한 빈티지 택 없음", {}

    years = [int(v) for v in YEAR_RE.findall(text)]
    era_years = [y for y in years if int(era["min_year"]) <= y <= int(era["max_year"])]
    explicit_90s = bool(era_years or NINETIES_RE.search(text))
    single_stitch = "single stitch" in low or "single-stitch" in low
    made_usa = "made in usa" in low or "made in u.s.a" in low or "made in u.s.a." in low

    if era.get("require_era_evidence", True) and not explicit_90s:
        allow_strong = bool(era.get("allow_strong_body_without_explicit_year", True))
        if not (allow_strong and strong_tag and (single_stitch or made_usa)):
            return False, "90년대 근거 부족", {}

    if tag_name and not strong_tag and not (explicit_90s and (single_stitch or made_usa)):
        return False, f"{tag_name} 연대 근거 부족", {}

    score = tag_score
    reasons = [f"Tag {tag_name}"] if tag_name else []
    if explicit_90s:
        score += 35
        reasons.append(str(min(era_years)) if era_years else "90s")
    if single_stitch:
        score += 20
        reasons.append("Single Stitch")
    if made_usa:
        score += 15
        reasons.append("Made in USA")
    if "copyright" in low or "licensed" in low or "licenced" in low:
        score += 8
        reasons.append("Copyright/License")
    if "used" in low or "pre-owned" in low or "preowned" in low:
        score += 5

    if score < int(auth.get("minimum_score", 50)):
        return False, f"빈티지 신뢰도 {score}점", {}

    warnings = [kw for kw in excludes.get("soft_warning", []) if kw.lower() in low]
    return True, "통과", {
        "authenticity_score": score,
        "authenticity_reasons": reasons,
        "warnings": warnings,
        "tag_name": tag_name,
        "era_year": min(era_years) if era_years else None,
    }


def prefilter_summary(item: dict[str, Any], meta: dict[str, Any], excludes: dict[str, Any]) -> tuple[bool, str]:
    title = str(item.get("title", ""))
    if not title_matches_artist(title, meta["artist"]):
        return False, "아티스트 불일치"
    hit = contains_keyword(title, excludes.get("hard_exclude", []))
    if hit:
        return False, f"제외 키워드: {hit}"
    if MODERN_YEAR_RE.search(title):
        return False, "현대 연대"
    if has_multi_size(title):
        return False, "다중 사이즈"
    if explicit_small_size(title):
        return False, "작은 사이즈"
    return True, "통과"


def summary_priority(item: dict[str, Any], meta: dict[str, Any], tags: dict[str, Any]) -> int:
    title = str(item.get("title", ""))
    low = title.lower()
    score = {"tier_1": 45, "tier_2": 28, "tier_3": 15}.get(meta["tier"], 0)
    if meta.get("specific"):
        score += 20
        if normalize(meta.get("subject", "")) in normalize(title):
            score += 15
    if NINETIES_RE.search(title):
        score += 30
    _, tag_score, strong_tag = detect_tag(title, tags)
    score += min(tag_score, 30)
    if strong_tag:
        score += 10
    if "single stitch" in low or "single-stitch" in low:
        score += 20
    if "made in usa" in low or "made in u.s.a" in low:
        score += 15
    if "vintage" in low:
        score += 5
    watch = item.get("watchCount")
    try:
        if watch is not None:
            score += min(int(watch), 40) // 2
    except (TypeError, ValueError):
        pass
    try:
        score += min(int(item.get("bidCount", 0) or 0), 10)
    except (TypeError, ValueError):
        pass
    return score


def price_for_listing(item: dict[str, Any], listing_type: str) -> tuple[float | None, str, str]:
    if listing_type == "FIXED_PRICE":
        price, currency = parse_amount(item.get("price"))
        return price, currency, "즉시구매가"
    current, currency = parse_amount(item.get("currentBidPrice"))
    if current is not None and current > 0:
        return current, currency, "현재 입찰가"
    minimum, currency = parse_amount(item.get("minimumPriceToBid"))
    if minimum is not None and minimum > 0:
        return minimum, currency, "다음 최소 입찰가" if int(item.get("bidCount", 0) or 0) else "시작가"
    price, currency = parse_amount(item.get("price"))
    return price, currency, "시작가"


def parse_ebay_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def hours_left(item: dict[str, Any]) -> float | None:
    end = parse_ebay_time(item.get("itemEndDate"))
    if not end:
        return None
    return (end - datetime.now(timezone.utc)).total_seconds() / 3600


def listing_age_minutes(item: dict[str, Any]) -> float | None:
    created = parse_ebay_time(item.get("itemCreationDate") or item.get("itemOriginDate"))
    if not created:
        return None
    return (datetime.now(timezone.utc) - created).total_seconds() / 60


def balanced_top(candidates: list[dict[str, Any]], limit: int, per_artist: int = 2) -> list[dict[str, Any]]:
    candidates = sorted(candidates, key=lambda x: -int(x.get("_pre_score", 0)))
    selected: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for item in candidates:
        artist = item["_meta"]["artist"]
        if counts.get(artist, 0) >= per_artist:
            continue
        selected.append(item)
        counts[artist] = counts.get(artist, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def reference_query(item: dict[str, Any]) -> str:
    artist = item["_artist"]
    subject = str(item.get("_subject", "")).strip()
    if subject and normalize(subject) != normalize(artist):
        return f"{artist} {subject} vintage shirt"
    return f"{artist} vintage shirt"


def load_seen() -> set[str]:
    data = load_json(SEEN_ITEMS_PATH, {"item_ids": []})
    return set(data.get("item_ids", []))


def save_seen(ids: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with SEEN_ITEMS_PATH.open("w", encoding="utf-8") as fh:
        json.dump({"item_ids": sorted(ids)[-5000:]}, fh, ensure_ascii=False, indent=2)


def format_age(minutes: float | None) -> str:
    if minutes is None:
        return "확인 필요"
    if minutes < 60:
        return f"{max(0, int(minutes))}분 전"
    return f"{int(minutes // 60)}시간 전"


def format_hours(hours: float | None) -> str:
    if hours is None:
        return "확인 필요"
    total = max(0, int(hours * 60))
    return f"{total // 60}시간 {total % 60}분"


def build_message(item: dict[str, Any], exchange_rate: float) -> str:
    reference = item["_reference"]
    total = item["_total_cost"]
    price = item["_price"]
    shipping = item["_shipping"]
    result = item["_deal"]
    listing_type = item["_listing_type"]
    watch = item.get("_watch_count")
    time_line = (
        f"⏰ 남은 시간: {format_hours(item.get('_hours_left'))}"
        if listing_type == "AUCTION"
        else f"🕒 등록: {format_age(item.get('_age_minutes'))}"
    )
    auth_line = ", ".join(item.get("_auth_reasons", [])) or "확인 근거 있음"
    watch_line = f"\n❤️ Watch: {watch}" if watch is not None else ""
    warning_line = ""
    if item.get("_warnings"):
        warning_line = "\n⚠️ " + ", ".join(item["_warnings"])

    return (
        f"🔥 {result['label']} {result['rating']}\n\n"
        f"👕 {item.get('title', '제목 없음')}\n"
        f"🎸 {item['_artist']} · 90s verified\n\n"
        f"💰 {item['_price_label']}: ${price:.2f} / 약 {round(price * exchange_rate):,}원\n"
        f"🚚 미국 배송비: ${shipping:.2f}\n"
        f"📦 총 매입예상: ${total:.2f} / 약 {round(total * exchange_rate):,}원\n\n"
        f"📊 활성 호가 중앙값: ${reference['median']:.2f}\n"
        f"🛡️ 보수 시세(P40): ${reference['conservative']:.2f} · {reference['sample_count']}건\n"
        f"📉 보수 시세 대비: {result['discount_percent']:.1f}% 저렴\n"
        f"💵 시세-매입 스프레드: ${result['spread_usd']:.2f}\n"
        f"🎯 Hunter Score: {result['hunter_score']:.1f} · 신뢰 {result['confidence']}\n"
        f"🏷️ 근거: {auth_line}{watch_line}\n"
        f"{time_line}{warning_line}\n\n"
        f"🔗 {item.get('itemWebUrl', '')}\n\n"
        f"※ 시세는 eBay 활성 호가 기준이며 실거래 완료가가 아닙니다."
    )


def main() -> None:
    profile, artist_db, patterns, excludes, tags = load_configs()
    if not profile:
        raise RuntimeError("config/hunter_profile.json이 필요합니다.")

    search_cfg = profile["search"]
    budget_cfg = profile["budget"]
    selected = choose_artists(profile, artist_db)
    queries = build_queries(profile, selected, patterns)
    token = get_access_token()

    print(f"Hunter V6 / mode={RADAR_MODE} / queries={len(queries)}")
    for q in queries:
        print(f"- {q['tier']} / {q['artist']} / {q['query']}")

    listing_types = [RADAR_MODE] if RADAR_MODE != "ALL" else ["FIXED_PRICE", "AUCTION"]
    summaries: dict[str, dict[str, Any]] = {}

    for listing_type in listing_types:
        for meta in queries:
            try:
                results = search_one_query(
                    token,
                    meta["query"],
                    listing_type,
                    float(budget_cfg["max_item_plus_us_shipping_usd"]),
                    int(search_cfg["limit_per_query"]),
                )
            except requests.RequestException as exc:
                print(f"검색 실패 / {meta['query']} / {exc}")
                continue

            for item in results:
                item_id = item.get("itemId")
                if not item_id:
                    continue
                passed, _ = prefilter_summary(item, meta, excludes)
                if not passed:
                    continue
                score = summary_priority(item, meta, tags)
                existing = summaries.get(item_id)
                if existing is None or score > int(existing.get("_pre_score", 0)):
                    copy = dict(item)
                    copy["_meta"] = meta
                    copy["_listing_type"] = listing_type
                    copy["_pre_score"] = score
                    summaries[item_id] = copy

    detail_budget = int(
        search_cfg["fixed_detail_budget"] if RADAR_MODE == "FIXED_PRICE"
        else search_cfg["auction_detail_budget"]
    )
    detail_targets = balanced_top(list(summaries.values()), detail_budget)
    verified: list[dict[str, Any]] = []

    for summary in detail_targets:
        item_id = summary["itemId"]
        try:
            detail = get_item_details(token, item_id)
        except requests.RequestException as exc:
            print(f"상세조회 실패 / {summary.get('title')} / {exc}")
            continue

        meta = summary["_meta"]
        if not title_matches_artist(str(detail.get("title", summary.get("title", ""))), meta["artist"]):
            continue

        passed, reason, auth_data = authenticity_check(detail, profile, excludes, tags)
        if not passed:
            print(f"빈티지 탈락 / {meta['artist']} / {reason} / {summary.get('title')}")
            continue

        listing_type = summary["_listing_type"]
        price, currency, price_label = price_for_listing(detail, listing_type)
        if price is None or price <= 0:
            price, currency, price_label = price_for_listing(summary, listing_type)
        shipping = get_shipping(detail)
        if shipping is None:
            shipping = get_shipping(summary)
        if price is None or price <= 0 or shipping is None:
            continue

        if price + shipping > float(budget_cfg["max_item_plus_us_shipping_usd"]):
            continue

        h_left = hours_left(detail if detail.get("itemEndDate") else summary)
        if listing_type == "AUCTION":
            if h_left is None or h_left <= 0 or h_left > float(search_cfg["auction_max_hours_left"]):
                continue

        item = dict(detail)
        item.update({
            "_artist": meta["artist"],
            "_tier": meta["tier"],
            "_subject": meta["subject"],
            "_listing_type": listing_type,
            "_price": price,
            "_currency": currency,
            "_price_label": price_label,
            "_shipping": shipping,
            "_total_cost": price + shipping + float(budget_cfg["forwarding_fee_usd"]),
            "_auth_score": int(auth_data["authenticity_score"]),
            "_auth_reasons": auth_data["authenticity_reasons"],
            "_warnings": auth_data["warnings"],
            "_hours_left": h_left,
            "_age_minutes": listing_age_minutes(summary),
            "_watch_count": summary.get("watchCount", detail.get("watchCount")),
            "_pre_score": summary["_pre_score"] + int(auth_data["authenticity_score"]),
        })
        verified.append(item)

    verified.sort(key=lambda x: -int(x.get("_pre_score", 0)))
    ref_budget = int(
        search_cfg["fixed_reference_budget"] if RADAR_MODE == "FIXED_PRICE"
        else search_cfg["auction_reference_budget"]
    )
    reference_targets = verified[:ref_budget]
    reference_cache: dict[str, dict[str, Any] | None] = {}
    deals: list[dict[str, Any]] = []

    for item in reference_targets:
        query = reference_query(item)
        if query not in reference_cache:
            try:
                API_CALLS["reference"] += 1
                reference_cache[query] = get_active_price_reference(token=token, query=query, maximum_results=100)
            except requests.RequestException as exc:
                print(f"시세조회 실패 / {query} / {exc}")
                reference_cache[query] = None
        reference = reference_cache[query]
        if not reference:
            continue

        result = classify_deal(
            total_purchase_cost=float(item["_total_cost"]),
            reference_median=float(reference["median"]),
            conservative_reference=float(reference["conservative"]),
            sample_count=int(reference["sample_count"]),
            authenticity_score=int(item["_auth_score"]),
            tier=item["_tier"],
            settings=profile["deal"],
        )
        if not result["should_alert"]:
            print(
                f"딜 탈락 / {item['_artist']} / {result['label']} / "
                f"median ${reference['median']:.0f} / {result['discount_percent']:.1f}% / {item.get('title')}"
            )
            continue
        item["_reference"] = reference
        item["_deal"] = result
        deals.append(item)

    deals.sort(key=lambda x: (
        -float(x["_deal"].get("hunter_score", 0)),
        -float(x["_reference"]["median"]),
        -float(x["_deal"]["discount_percent"]),
    ))

    max_alerts = int(search_cfg["max_alerts_per_run"])
    max_per_artist = int(search_cfg["max_alerts_per_artist"])
    seen = load_seen()
    counts: dict[str, int] = {}
    final: list[dict[str, Any]] = []
    for item in deals:
        item_id = item.get("itemId")
        if not item_id or item_id in seen:
            continue
        artist = item["_artist"]
        if counts.get(artist, 0) >= max_per_artist:
            continue
        final.append(item)
        counts[artist] = counts.get(artist, 0) + 1
        if len(final) >= max_alerts:
            break

    if final:
        exchange_rate = float(usd_to_krw(1)["rate"])
        for item in final:
            send_telegram_message(build_message(item, exchange_rate))
            seen.add(item["itemId"])
        save_seen(seen)
    else:
        print("알림 대상 없음 — 텔레그램 전송 생략")

    print(
        f"summary={len(summaries)} / detail={len(detail_targets)} / verified={len(verified)} / "
        f"deals={len(deals)} / alerts={len(final)} / calls={API_CALLS}"
    )


if __name__ == "__main__":
    main()
