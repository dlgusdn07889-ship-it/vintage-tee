"""Hunter V7: active-listing deal discovery with conservative title-level comps.

No AI API key is required. Results are leads for human authentication, never
certificates of vintage authenticity or evidence of completed sale prices.
"""
from __future__ import annotations

import json
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

import ebay_api as legacy
from deal_evaluator import classify_deal
from exchange import usd_to_krw
from hunter_precision import (
    graphic_terms, item_text, norm, phrase_in, reject_listing,
    summarize_comps,
)
from telegram_alert import send_telegram_message

ROOT = Path(__file__).resolve().parents[1]
MODE = os.environ.get("RADAR_MODE", "ALL").upper()
if MODE not in {"FIXED_PRICE", "AUCTION", "ALL"}:
    raise ValueError("RADAR_MODE must be FIXED_PRICE, AUCTION or ALL")


def rate_limited(error: requests.RequestException) -> bool:
    response = getattr(error, "response", None)
    return response is not None and response.status_code in {403, 429}


def choose_artists(profile: dict[str, Any], database: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """Full-coverage round robin; tier-3 never gets stranded by multiplier/GCD."""
    cfg = profile["search"]
    slot = int(datetime.now(timezone.utc).timestamp() // (600 if MODE == "FIXED_PRICE" else 3600))
    tiers = legacy.normalize_artist_database(database)
    output = []
    for tier in ("tier_1", "tier_2", "tier_3"):
        items = tiers[tier]
        if not items:
            continue
        count = min(len(items), int(cfg[f"{tier}_artists_per_run"]))
        start = slot * count % len(items)
        output.extend((items[(start + i) % len(items)], tier) for i in range(count))
    random.Random(slot).shuffle(output)
    return output


def candidate_queries(selected: list[tuple[dict[str, Any], str]], patterns: dict[str, Any]) -> list[dict[str, Any]]:
    # Keep a broad artist query in every tier; rotate one graphic/album query.
    slot = int(datetime.now(timezone.utc).timestamp() // (600 if MODE == "FIXED_PRICE" else 3600))
    queries = []
    for index, (entry, tier) in enumerate(selected):
        artist = entry["name"]
        safe_name = legacy.artist_query_name(artist, patterns)
        keywords = [k for k in entry.get("keywords", []) if norm(k) != norm(artist)]
        subject = keywords[(slot + index) % len(keywords)] if keywords else ""
        broad = f"{safe_name} vintage shirt"
        queries.append({"artist": artist, "tier": tier, "subject": "", "query": broad,
                        "specific": False, "keywords": entry.get("keywords", [])})
        # One specific query for premium artists only: stay within the 5,000/day API allowance.
        if tier == "tier_1" and subject and len(norm(subject)) >= 5:
            queries.append({"artist": artist, "tier": tier, "subject": subject,
                            "query": f"{safe_name} {subject} shirt", "specific": True,
                            "keywords": entry.get("keywords", [])})
    dedup = {}
    for query in queries:
        dedup[norm(query["query"])] = query
    return list(dedup.values())


def choose_subject(title: str, meta: dict[str, Any]) -> str:
    subject = meta.get("subject", "")
    if subject and phrase_in(title, subject):
        return subject
    keywords = [k for k in meta.get("keywords", []) if len(norm(k)) >= 5]
    matches = [k for k in keywords if norm(k) != norm(meta["artist"]) and phrase_in(title, k)]
    return max(matches, key=len) if matches else ""


def price_candidate(item: dict[str, Any], listing_type: str) -> tuple[float | None, str, str]:
    return legacy.price_for_listing(item, listing_type)


def fetch_reference(token: str, artist: str, subject: str, item: dict[str, Any],
                    excludes: dict[str, Any], tags: dict[str, Any]) -> dict[str, Any] | None:
    signature = graphic_terms(str(item.get("title", "")), artist, subject)
    if not subject and len(signature) < 2:
        print(f"비교 보류 / 그래픽 특정 불가 / {item.get('title', '')}")
        return None
    query = " ".join(part for part in [artist, subject, "shirt"] if part)
    legacy.API_CALLS["reference"] += 1
    response = requests.get(
        legacy.SEARCH_URL,
        headers=legacy.ebay_headers(token),
        params={
            "q": query,
            "limit": 100,
            "filter": "buyingOptions:{FIXED_PRICE},price:[40..2000],priceCurrency:USD",
            "fieldgroups": "EXTENDED",
        },
        timeout=30,
    )
    response.raise_for_status()
    comps = summarize_comps(
        response.json().get("itemSummaries", []),
        artist=artist, subject=subject, signature=signature,
        candidate_id=str(item["itemId"]),
        hard_excludes=excludes.get("hard_exclude", []),
        modern_bodies=tags.get("modern_tags", []),
        minimum=3,
    )
    if comps:
        comps["query"] = query
        comps["signature"] = signature
    return comps


def alert_text(item: dict[str, Any], rate: float) -> str:
    ref = item["_reference"]
    deal = item["_deal"]
    auction = item["_listing_type"] == "AUCTION"
    links = "\n".join(f"  - {link}" for link in ref["comparison_urls"][:3]) or "  - 제공되지 않음"
    timing = (
        f"경매 종료까지 {item['_hours_left']:.1f}시간 · 현재 입찰가는 낙찰가가 아닙니다."
        if auction else "즉시구매 · 가격 및 판매 가능 여부는 구매 직전에 재확인"
    )
    return (
        f"🎯 90s HUNTER / {deal['label']}\n"
        f"👕 {item.get('title', '')}\n"
        f"🎸 {item['_artist']} · 판매자 정보 기반 추정(실물 진품 미확정)\n\n"
        f"💰 {'현재 입찰가' if auction else '즉시구매가'} ${item['_price']:.2f} + 미국배송 ${item['_shipping']:.2f}\n"
        f"📦 배대지 $10 포함 매입추정 ${item['_total_cost']:.2f} (약 {round(item['_total_cost'] * rate):,}원)\n"
        f"📊 유사 제목의 활성 호가 중앙값 ${ref['median']:.2f} / 보수 하단(P25) ${ref['conservative']:.2f}\n"
        f"📉 P25 대비 {deal['discount_percent']:.1f}% 낮음 / 호가 차액 ${deal['spread_usd']:.2f} (순이익 아님)\n"
        f"🔎 유사 호가 {ref['sample_count']}건 / 표본 신뢰 {deal['confidence']}\n"
        f"🏷️ 판매자 기재 근거: {', '.join(item['_auth_reasons'])}\n"
        f"⚠️ 그래픽·태그 사진·연대·드라이로트·한국 관부가세/판매 수수료 별도 검증 필요.\n"
        f"⏰ {timing}\n\n"
        f"🔗 구매 링크: {item.get('itemWebUrl', '')}\n"
        f"📎 참고 호가 링크:\n{links}\n"
        f"※ 같은 그래픽 실물/판매완료가 검증 아님. 호가를 실거래가로 해석하지 마세요."
    )


def main() -> None:
    profile, artists, patterns, excludes, tags = legacy.load_configs()
    budget = profile["budget"]
    search = profile["search"]
    selected = choose_artists(profile, artists)
    queries = candidate_queries(selected, patterns)
    token = legacy.get_access_token()
    seen = legacy.load_seen()
    found: dict[str, dict[str, Any]] = {}
    review: list[dict[str, Any]] = []
    kinds = [MODE] if MODE != "ALL" else ["FIXED_PRICE", "AUCTION"]
    print(f"Hunter V7 / {MODE} / searches={len(queries)} / seen={len(seen)}")

    for kind in kinds:
        for meta in queries:
            try:
                results = legacy.search_one_query(
                    token, meta["query"], kind,
                    float(budget["max_item_plus_us_shipping_usd"]),
                    int(search["limit_per_query"]),
                )
            except requests.RequestException as exc:
                if rate_limited(exc):
                    print("eBay 사용 한도/접근 오류: 검색 중단")
                    raise
                print(f"검색 오류 / {meta['query']} / {exc}")
                continue
            for summary in results:
                ident = str(summary.get("itemId", ""))
                if not ident or ident in seen:
                    continue
                title = str(summary.get("title", ""))
                if not legacy.title_matches_artist(title, meta["artist"]):
                    continue
                if legacy.explicit_small_size(title):
                    continue
                reason = reject_listing(summary, excludes.get("hard_exclude", []),
                                        tags.get("modern_tags", []))
                if reason:
                    continue
                price, currency, _ = price_candidate(summary, kind)
                if price is None or currency != "USD" or price <= 0:
                    continue
                rank = legacy.summary_priority(summary, meta, tags)
                if 80 <= price <= float(budget["max_item_plus_us_shipping_usd"]):
                    rank += 20
                elif price < 40:
                    rank -= 20
                candidate = dict(summary)
                candidate.update({"_meta": meta, "_listing_type": kind, "_pre_score": rank})
                if ident not in found or rank > found[ident]["_pre_score"]:
                    found[ident] = candidate

    detail_budget = int(search.get("fixed_detail_budget", 10) if MODE == "FIXED_PRICE"
                        else search.get("auction_detail_budget", 6))
    shortlist = legacy.balanced_top(list(found.values()), detail_budget, per_artist=3)
    verified = []
    for summary in shortlist:
        try:
            detail = legacy.get_item_details(token, summary["itemId"])
        except requests.RequestException as exc:
            if rate_limited(exc):
                raise
            print(f"상세 오류 / {summary.get('title')} / {exc}")
            continue
        meta = summary["_meta"]
        if not legacy.title_matches_artist(str(detail.get("title", "")), meta["artist"]):
            continue
        reason = reject_listing(detail, excludes.get("hard_exclude", []),
                                tags.get("modern_tags", []), detail=True)
        if reason:
            print(f"상세 제외 / {reason} / {detail.get('title', '')}")
            continue
        # Precision gate above uses word boundaries. The legacy scorer's raw
        # substring test would otherwise discard AC/DC because of "cd".
        clean_excludes = {**excludes, "hard_exclude": []}
        passed, reason, auth = legacy.authenticity_check(detail, profile, clean_excludes, tags)
        if not passed:
            print(f"연대/바디 근거 부족 / {reason} / {detail.get('title', '')}")
            continue
        kind = summary["_listing_type"]
        price, currency, label = price_candidate(detail, kind)
        if price is None or currency != "USD" or price <= 0:
            price, currency, label = price_candidate(summary, kind)
        shipping = legacy.get_shipping(detail)
        if shipping is None:
            shipping = legacy.get_shipping(summary)
        if price is None or currency != "USD" or shipping is None:
            continue
        if price + shipping > float(budget["max_item_plus_us_shipping_usd"]):
            continue
        left = legacy.hours_left(detail if detail.get("itemEndDate") else summary)
        if kind == "AUCTION" and (
            left is None or left <= 0 or left > float(search.get("auction_max_hours_left", 6))
        ):
            continue
        item = dict(detail)
        item.update({
            "itemId": str(summary["itemId"]),
            "itemWebUrl": detail.get("itemWebUrl") or summary.get("itemWebUrl", ""),
            "_artist": meta["artist"], "_tier": meta["tier"],
            "_subject": choose_subject(str(detail.get("title", "")), meta),
            "_listing_type": kind, "_price": price, "_shipping": shipping,
            "_total_cost": price + shipping + float(budget["forwarding_fee_usd"]),
            "_auth_score": int(auth["authenticity_score"]),
            "_auth_reasons": auth["authenticity_reasons"],
            "_hours_left": left,
            "_pre_score": int(summary["_pre_score"]) + int(auth["authenticity_score"]),
        })
        verified.append(item)

    verified.sort(key=lambda x: -x["_pre_score"])
    ref_budget = int(search.get("fixed_reference_budget", 5) if MODE == "FIXED_PRICE"
                     else search.get("auction_reference_budget", 3))
    deals = []
    for item in verified[:ref_budget]:
        try:
            ref = fetch_reference(token, item["_artist"], item["_subject"],
                                  item, excludes, tags)
        except requests.RequestException as exc:
            if rate_limited(exc):
                raise
            print(f"호가 조회 오류 / {item.get('title', '')} / {exc}")
            continue
        if not ref:
            review.append({"title": item.get("title"), "url": item.get("itemWebUrl"),
                           "reason": "동일 디자인으로 확인 가능한 활성 호가 3건 미만 또는 가격 분산 과다"})
            continue
        deal = classify_deal(
            total_purchase_cost=item["_total_cost"],
            reference_median=ref["median"], conservative_reference=ref["conservative"],
            sample_count=ref["sample_count"], authenticity_score=item["_auth_score"],
            tier=item["_tier"], settings=profile["deal"],
        )
        if not deal["should_alert"]:
            continue
        item["_reference"] = ref
        item["_deal"] = deal
        deals.append(item)

    deals.sort(key=lambda x: -float(x["_deal"].get("hunter_score", 0)))
    sent = 0
    per_artist: dict[str, int] = {}
    if deals:
        rate = float(usd_to_krw(1)["rate"])
        for item in deals:
            if item["itemId"] in seen or sent >= int(search["max_alerts_per_run"]):
                continue
            artist = item["_artist"]
            if per_artist.get(artist, 0) >= int(search["max_alerts_per_artist"]):
                continue
            send_telegram_message(alert_text(item, rate))
            seen.add(item["itemId"])
            legacy.save_seen(seen)
            per_artist[artist] = per_artist.get(artist, 0) + 1
            sent += 1
    else:
        print("검증된 새 저평가 후보 없음 — 텔레그램 생략")

    legacy.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (legacy.DATA_DIR / f"hunter_review_{MODE.lower()}.json").write_text(
        json.dumps(review[:30], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"summary={len(found)} detail={len(shortlist)} verified={len(verified)} "
          f"deals={len(deals)} alerts={sent} review={len(review)} calls={legacy.API_CALLS}")


if __name__ == "__main__":
    main()
