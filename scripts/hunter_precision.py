"""Conservative, metadata-only vintage and comparable checks.

This module NEVER establishes physical authenticity or sold prices.
"""
from __future__ import annotations

import math
import re
import unicodedata
from statistics import median
from typing import Any

WORD_RE = re.compile(r"[a-z0-9]+")
YEAR_RE = re.compile(r"\b(?:19\d\d|20\d\d)\b")
MODERN_ERA_RE = re.compile(r"\b(?:200\d|201\d|202\d|00s|2000s|y2k)\b", re.I)
OLD_ERA_RE = re.compile(r"\b(?:198\d|199\d|80s|90s|1980s|1990s)\b", re.I)
SIZE_CHOICE_RE = re.compile(
    r"\b(?:choose|select)\s+(?:your\s+)?size\b|\b(?:all|multiple)\s+sizes\b|"
    r"\bsizes?\s+(?:available|options?)\b|\b(?:xs|s)\s*(?:-|–|to|/)\s*(?:xl|2xl|3xl|4xl|5xl|6xl)\b|"
    r"\bs\s*[/,]\s*m\s*[/,]\s*l\b",
    re.I,
)
NOISE = {
    "vintage", "original", "official", "rare", "90s", "80s", "1990s", "1980s", "tee",
    "tshirt", "shirt", "shirts", "t", "men", "mens", "women", "womens", "unisex", "black",
    "white", "red", "blue", "green", "large", "medium", "small", "xl", "xxl", "xxxl",
    "single", "stitch", "stitching", "made", "usa", "cotton", "tag", "tagged", "giant",
    "brockum", "winterland", "anvil", "hanes", "fruit", "loom", "tultex", "screen",
    "stars", "best", "concert", "tour", "band", "album", "graphic", "print", "printed",
    "rock", "metal", "grunge", "punk", "90", "the", "and", "for", "with", "from", "vtg",
    "size", "true", "old", "licensed", "copyright", "excellent", "condition", "of", "in", "on",
    "era", "alt", "music", "logo", "print", "tagged", "vintage", "1995", "1994",
}


def norm(text: Any) -> str:
    return " ".join(WORD_RE.findall("".join(c for c in unicodedata.normalize("NFKD", str(text).lower()) if not unicodedata.combining(c))))


def phrase_in(text: str, phrase: str) -> bool:
    value = norm(phrase)
    if not value:
        return False
    return f" {value} " in f" {norm(text)} "


def find_phrase(text: str, phrases: list[str]) -> str | None:
    return next((p for p in phrases if phrase_in(text, p)), None)


def item_text(item: dict[str, Any]) -> str:
    parts = [str(item.get(k, "")) for k in (
        "title", "shortDescription", "conditionDescription", "brand"
    )]
    for aspect in item.get("localizedAspects", []) or []:
        if isinstance(aspect, dict):
            parts.append(str(aspect.get("value", "")))
    return " ".join(parts)


def is_variation(item: dict[str, Any]) -> bool:
    if item.get("itemGroupHref") or "VARIATION" in str(item.get("itemGroupType", "")).upper():
        return True
    for aspect in item.get("localizedAspects", []) or []:
        if not isinstance(aspect, dict):
            continue
        if str(aspect.get("name", "")).lower() in {"size", "shirt size"}:
            value = str(aspect.get("value", ""))
            if SIZE_CHOICE_RE.search(value):
                return True
            sizes = re.findall(r"\b(?:xs|s|m|l|xl|2xl|3xl|4xl|5xl)\b", value.lower())
            if len(set(sizes)) >= 3:
                return True
    return bool(SIZE_CHOICE_RE.search(item_text(item)))


def reject_listing(
    item: dict[str, Any],
    hard_excludes: list[str],
    modern_bodies: list[str],
    *,
    detail: bool = False,
) -> str | None:
    """Reject only demonstrated red flags; absent tag photos => unverified, not fake."""
    title = str(item.get("title", ""))
    text = item_text(item) if detail else title
    hit = find_phrase(text, hard_excludes)
    if hit:
        return f"제외 문구: {hit}"
    body = find_phrase(text, modern_bodies)
    if body:
        return f"현행 바디: {body}"
    if MODERN_ERA_RE.search(title):
        return "2000년 이후 상품 표기"
    if is_variation(item):
        return "사이즈/색상 선택형"
    if detail:
        condition = str(item.get("condition", "")).upper()
        if condition.startswith("NEW") and not re.search(r"\b(?:deadstock|nos|new old stock)\b", text, re.I):
            return "신품(데드스톡 근거 없음)"
        if re.search(r"\b(?:printed|manufactured|produced|reprinted|reissued)\s+(?:in\s+)?20\d\d\b", text, re.I):
            return "현행 제작 연도 명시"
    return None


def era_evidence(item: dict[str, Any]) -> bool:
    text = item_text(item)
    return bool(OLD_ERA_RE.search(text) or phrase_in(text, "single stitch") or phrase_in(text, "made in usa"))


def graphic_terms(title: str, artist: str, subject: str) -> list[str]:
    base = set(norm(f"{artist} {subject}").split()) | NOISE
    terms = []
    for token in norm(title).split():
        if token in base or token.isdigit() or len(token) < 4 or re.fullmatch(r"(?:19|20)\d\d", token):
            continue
        if token not in terms:
            terms.append(token)
    return terms[:3]


def comparable(item: dict[str, Any], *, artist: str, subject: str, signature: list[str],
               candidate_id: str, hard_excludes: list[str], modern_bodies: list[str]) -> bool:
    title = str(item.get("title", ""))
    if not title or str(item.get("itemId", "")) == candidate_id:
        return False
    artist_aliases = {
        "ACDC": ["ACDC", "AC/DC", "AC DC"],
        "Guns N Roses": ["Guns N Roses", "Guns N’ Roses", "Guns N' Roses", "GNR"],
        "Nine Inch Nails": ["Nine Inch Nails", "NIN"],
        "Red Hot Chili Peppers": ["Red Hot Chili Peppers", "RHCP"],
        "Rage Against the Machine": ["Rage Against the Machine", "RATM"],
        "Alice in Chains": ["Alice in Chains", "AIC"],
        "Mötley Crüe": ["Mötley Crüe", "Motley Crue"],
        "Wu-Tang Clan": ["Wu Tang Clan", "Wu-Tang Clan", "Wu Tang"],
        "Tupac": ["Tupac", "2Pac", "Makaveli"],
    }.get(artist, [artist])
    if not any(phrase_in(title, alias) for alias in artist_aliases):
        return False
    if subject and not phrase_in(title, subject):
        return False
    if reject_listing(item, hard_excludes, modern_bodies):
        return False
    if not era_evidence(item):
        return False
    if signature and not any(phrase_in(title, token) for token in signature):
        return False
    return True


def usd_total(item: dict[str, Any]) -> float | None:
    try:
        price = item["price"]
        if price.get("currency") != "USD":
            return None
        amount = float(price["value"])
        for option in item.get("shippingOptions", []) or []:
            ship = option.get("shippingCost", {})
            if ship.get("currency") == "USD":
                return amount + float(ship["value"])
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    return None


def percentile(sorted_values: list[float], q: float) -> float:
    pos = (len(sorted_values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def summarize_comps(items: list[dict[str, Any]], *, artist: str, subject: str,
                    signature: list[str], candidate_id: str, hard_excludes: list[str],
                    modern_bodies: list[str], minimum: int = 3) -> dict[str, Any] | None:
    unique = {}
    for item in items:
        if not comparable(item, artist=artist, subject=subject, signature=signature,
                          candidate_id=candidate_id, hard_excludes=hard_excludes, modern_bodies=modern_bodies):
            continue
        total = usd_total(item)
        if total is None or total <= 0:
            continue
        unique[str(item["itemId"])] = (total, item.get("itemWebUrl", ""))
    if len(unique) < minimum:
        return None
    pairs = sorted(unique.values(), key=lambda x: x[0])
    values = [v for v, _ in pairs]
    med = median(values)
    low = percentile(values, 0.25)
    if med / max(low, 1) > 2.0 or percentile(values, 0.75) / max(low, 1) > 2.7:
        return None
    return {
        "median": round(med, 2),
        "conservative": round(low, 2),
        "sample_count": len(values),
        "comparison_urls": [url for _, url in pairs[:3] if url],
        "comparison_type": "active_asking_not_sold",
    }
