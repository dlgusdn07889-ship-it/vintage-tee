"""Run precision hunter, then optionally send a clearly unpriced rare-item lead.

No extra eBay calls. A scout alert is not proof of authenticity or a bargain.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UNACCEPTABLE_DAMAGE = re.compile(
    r"\b(?:thrashed|dry[ -]?rot|dryrotted|heavily damaged|major holes|"
    r"large holes|ripped apart|unwearable|for parts)\b", re.I
)
ERA_TITLE = re.compile(r"\b(?:199\d|90s|1990s)\b", re.I)


def _has_strong_tag(item: dict[str, Any], tag_config: dict[str, Any]) -> str | None:
    text = " ".join(str(item.get(key, "")) for key in ("title", "brand", "shortDescription"))
    aspects = item.get("localizedAspects", [])
    if isinstance(aspects, list):
        text += " " + " ".join(str(a.get("value", "")) for a in aspects if isinstance(a, dict))
    for name in sorted(tag_config.get("strong_tags", {}), key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", text, re.I):
            return name
    return None


def scout_eligible(item: dict[str, Any], tags: dict[str, Any]) -> tuple[bool, str]:
    title = str(item.get("title", ""))
    if item.get("_tier") not in {"tier_1", "tier_2"}:
        return False, "artist-tier"
    if not ERA_TITLE.search(title):
        return False, "no-90s-title"
    if UNACCEPTABLE_DAMAGE.search(title + " " + str(item.get("conditionDescription", ""))):
        return False, "damage"
    try:
        price, cost = float(item["_price"]), float(item["_total_cost"])
        score = int(item["_auth_score"])
    except (TypeError, ValueError, KeyError):
        return False, "missing-price-or-evidence"
    if not 95 <= price <= 325 or cost > 350:
        return False, "price-range"
    if score < 90:
        return False, "weak-metadata"
    strong_tag = _has_strong_tag(item, tags)
    if not strong_tag:
        return False, "no-strong-tag-claim"
    reasons = " ".join(item.get("_auth_reasons", []))
    if not ("Single Stitch" in reasons or "Made in USA" in reasons):
        return False, "no-construction-evidence"
    subject = str(item.get("_subject", "")).strip()
    if len(subject) < 5 or not re.search(re.escape(subject), title, re.I):
        return False, "no-distinct-graphic"
    if not str(item.get("itemWebUrl", "")).startswith("https://www.ebay.com/itm/"):
        return False, "no-verified-url"
    return True, strong_tag


def scout_message(item: dict[str, Any], tag: str) -> str:
    auction = item.get("_listing_type") == "AUCTION"
    time_text = (
        f"종료까지 {float(item['_hours_left']):.1f}시간. 입찰가는 변하며 낙찰가가 아닙니다."
        if auction and item.get("_hours_left") is not None
        else "즉시구매 여부·가격은 구매 직전 재확인하세요."
    )
    reasons = ", ".join(item.get("_auth_reasons", []))
    return (
        "👀 희귀 빈티지 수동 검토 후보 — 시세 미확인\n\n"
        f"👕 {item.get('title', '')}\n"
        f"💰 {'현재 입찰가' if auction else '즉시구매가'} ${float(item['_price']):.2f}"
        f" + 미국 배송 ${float(item['_shipping']):.2f}\n"
        f"📦 배대지 $10 포함 예상 원가 ${float(item['_total_cost']):.2f}"
        " (관부가세·판매 수수료 미포함)\n"
        f"🏷️ 판매자 기재 근거: {tag}; {reasons}\n"
        "📉 비교 가능한 활성 호가가 부족하여 할인율·차익을 계산하지 않았습니다.\n"
        "⚠️ 사진 속 택·프린트·연대·손상과 실거래가는 구매 전 직접 검증해야 합니다.\n"
        f"⏰ {time_text}\n"
        f"🔗 {item['itemWebUrl']}"
    )


def cooldown_passed(data: dict[str, Any], now: datetime, hours: int = 6) -> bool:
    raw = data.get("last_scout_alert_at")
    if not raw:
        return True
    try:
        previous = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if previous.tzinfo is None:
            return False
        return (now - previous).total_seconds() >= hours * 3600
    except ValueError:
        return False


def main() -> None:
    import hunter_v7 as hunter
    import ebay_api as legacy
    from hunter_precision import summarize_comps as strict_summarize

    scarce: list[dict[str, Any]] = []
    original_reference = hunter.fetch_reference

    # Only a named album/graphic plus a distinctive title term can use TWO
    # matching active ASKING prices. Existing low-sample extra discount/spread
    # requirements still apply; no sold-price estimate is manufactured.
    def two_comp_summarize(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if kwargs.get("subject") and kwargs.get("signature"):
            kwargs["minimum"] = 2
        return strict_summarize(*args, **kwargs)

    def tracked_reference(
        token: str, artist: str, subject: str, item: dict[str, Any],
        excludes: dict[str, Any], tags: dict[str, Any],
    ) -> dict[str, Any] | None:
        result = original_reference(token, artist, subject, item, excludes, tags)
        if result is None:
            scarce.append(item)
        return result

    hunter.summarize_comps = two_comp_summarize
    hunter.fetch_reference = tracked_reference
    hunter.main()

    _, _, _, _, tags = legacy.load_configs()
    eligible = []
    for candidate in scarce:
        passed, tag = scout_eligible(candidate, tags)
        if passed:
            eligible.append((candidate, tag))
    eligible.sort(
        key=lambda entry: (
            -int(entry[0]["_auth_score"]),
            -int(entry[0]["_pre_score"]),
            float(entry[0]["_total_cost"]),
        )
    )

    seen_path: Path = legacy.SEEN_ITEMS_PATH
    data = legacy.load_json(seen_path, {"item_ids": []})
    if not isinstance(data, dict):
        data = {"item_ids": []}
    seen = set(data.get("item_ids", []))
    now = datetime.now(timezone.utc)
    if not cooldown_passed(data, now):
        print(f"희귀 후보 {len(eligible)}건 / 6시간 알림 간격 유지")
        return

    for item, tag in eligible:
        item_id = str(item["itemId"])
        if item_id in seen:
            continue
        hunter.send_telegram_message(scout_message(item, tag))
        seen.add(item_id)
        data["item_ids"] = sorted(seen)[-5000:]
        data["last_scout_alert_at"] = now.isoformat()
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"수동 검토 후보 1건 알림: {item_id}")
        return
    print(f"희귀 후보 {len(eligible)}건 / 신규 알림 대상 없음")


if __name__ == "__main__":
    main()
