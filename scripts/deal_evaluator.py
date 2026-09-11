from __future__ import annotations

from typing import Any


def calculate_discount(total_purchase_cost: float, reference_value: float) -> float:
    if total_purchase_cost <= 0 or reference_value <= 0:
        return 0.0
    return round(((reference_value - total_purchase_cost) / reference_value) * 100, 1)


def calculate_spread(reference_value: float, total_purchase_cost: float) -> float:
    """보수 시세와 총 매입원가의 단순 차이. 실제 순이익이 아니다."""
    return round(reference_value - total_purchase_cost, 2)


def classify_deal(
    *,
    total_purchase_cost: float,
    reference_median: float,
    conservative_reference: float,
    sample_count: int,
    authenticity_score: int,
    tier: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """고가·인기 빈티지의 저평가 매물을 우선하는 판정 엔진."""
    minimum_median = float(settings.get("minimum_reference_median_usd", 180))
    minimum_samples = int(settings.get("minimum_reference_samples", 3))

    if sample_count < minimum_samples:
        return {
            "should_alert": False,
            "rating": "",
            "label": "비교 표본 부족",
            "discount_percent": 0.0,
            "spread_usd": 0.0,
            "confidence": "LOW",
        }

    if reference_median < minimum_median:
        return {
            "should_alert": False,
            "rating": "",
            "label": "시장가치 기준 미달",
            "discount_percent": 0.0,
            "spread_usd": 0.0,
            "confidence": "LOW",
        }

    discount = calculate_discount(total_purchase_cost, conservative_reference)
    spread = calculate_spread(conservative_reference, total_purchase_cost)

    confidence = "HIGH" if sample_count >= 8 else "MEDIUM" if sample_count >= 5 else "LOW"
    extra_discount = 0.0
    required_low_sample_spread = 0.0
    if sample_count < 5:
        extra_discount = float(settings.get("low_sample_extra_discount_percent", 10))
        required_low_sample_spread = float(settings.get("low_sample_min_spread_usd", 140))

    grail_ok = (
        reference_median >= float(settings.get("grail_reference_median_usd", 300))
        and discount >= float(settings.get("grail_min_discount_percent", 30)) + extra_discount
        and spread >= max(
            float(settings.get("grail_min_spread_usd", 120)),
            required_low_sample_spread,
        )
    )

    strong_ok = (
        reference_median >= float(settings.get("strong_reference_median_usd", 240))
        and discount >= float(settings.get("strong_min_discount_percent", 40)) + extra_discount
        and spread >= max(
            float(settings.get("strong_min_spread_usd", 100)),
            required_low_sample_spread,
        )
    )

    hidden_ok = (
        reference_median >= float(settings.get("hidden_reference_median_usd", 180))
        and discount >= float(settings.get("hidden_min_discount_percent", 50)) + extra_discount
        and spread >= max(
            float(settings.get("hidden_min_spread_usd", 90)),
            required_low_sample_spread,
        )
    )

    if grail_ok:
        label = "GRAIL DEAL"
        rating = "★★★★★"
    elif strong_ok:
        label = "STRONG BUY"
        rating = "★★★★☆"
    elif hidden_ok:
        label = "HIDDEN GEM"
        rating = "★★★★☆"
    else:
        return {
            "should_alert": False,
            "rating": "",
            "label": "가치 대비 메리트 부족",
            "discount_percent": discount,
            "spread_usd": spread,
            "confidence": confidence,
        }

    if authenticity_score < 50:
        return {
            "should_alert": False,
            "rating": "",
            "label": "빈티지 근거 부족",
            "discount_percent": discount,
            "spread_usd": spread,
            "confidence": confidence,
        }

    tier_bonus = {"tier_1": 20, "tier_2": 10, "tier_3": 0}.get(tier, 0)
    hunter_score = round(
        min(reference_median, 800) / 8
        + min(discount, 70) * 1.5
        + min(spread, 350) / 3
        + authenticity_score / 3
        + tier_bonus,
        1,
    )

    return {
        "should_alert": True,
        "rating": rating,
        "label": label,
        "discount_percent": discount,
        "spread_usd": spread,
        "confidence": confidence,
        "hunter_score": hunter_score,
    }
