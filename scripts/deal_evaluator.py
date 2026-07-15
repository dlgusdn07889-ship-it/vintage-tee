from __future__ import annotations

from typing import Any


MIN_DISCOUNT_PERCENT = 30.0


def calculate_expected_profit(
    reference_median: float,
    total_purchase_cost: float,
) -> float:
    """
    현재 호가 중앙값에서 총 예상 구매금액을 뺀 단순 예상 차익.
    판매 수수료와 한국 내 배송비는 아직 반영하지 않는다.
    """
    return round(reference_median - total_purchase_cost, 2)


def classify_deal(
    discount_percent: float,
    expected_profit_usd: float,
    sample_count: int,
) -> dict[str, Any]:
    """
    현재 호가 비교 기반의 임시 판정.
    판매 완료 실거래가가 아니므로 과도한 확신은 금지한다.
    """
    if discount_percent < MIN_DISCOUNT_PERCENT:
        return {
            "should_alert": False,
            "rating": "",
            "label": "기준 미달",
        }

    if sample_count < 8:
        return {
            "should_alert": False,
            "rating": "",
            "label": "비교 표본 부족",
        }

    if discount_percent >= 50 and expected_profit_usd >= 100:
        return {
            "should_alert": True,
            "rating": "★★★★★",
            "label": "강력 검토",
        }

    if discount_percent >= 40 and expected_profit_usd >= 60:
        return {
            "should_alert": True,
            "rating": "★★★★☆",
            "label": "우선 검토",
        }

    return {
        "should_alert": True,
        "rating": "★★★☆☆",
        "label": "검토 후보",
    }
