import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from deal_evaluator import classify_deal
from hunter_runner import cooldown_passed, scout_eligible, scout_message


TAGS = {"strong_tags": {"Brockum": 40}}


def candidate(**overrides):
    item = {
        "itemId": "v1|123456789012|0",
        "itemWebUrl": "https://www.ebay.com/itm/123456789012",
        "title": "Vintage 1994 Slayer Divine Intervention Tour Brockum Single Stitch XL",
        "brand": "Brockum",
        "_tier": "tier_1",
        "_price": 190.0,
        "_shipping": 13.0,
        "_total_cost": 213.0,
        "_auth_score": 110,
        "_auth_reasons": ["Tag Brockum", "1994", "Single Stitch"],
        "_subject": "Divine Intervention",
        "_pre_score": 170,
        "_listing_type": "FIXED_PRICE",
    }
    item.update(overrides)
    return item


class ScarceScoutTests(unittest.TestCase):
    def test_narrow_rare_lead_can_be_sent_without_made_up_comps(self):
        passed, tag = scout_eligible(candidate(), TAGS)
        self.assertTrue(passed)
        self.assertEqual(tag, "Brockum")
        message = scout_message(candidate(), tag)
        self.assertIn("시세 미확인", message)
        self.assertIn("할인율·차익을 계산하지 않았습니다", message)
        self.assertIn("https://www.ebay.com/itm/123456789012", message)

    def test_modern_damage_and_cheap_shirts_do_not_get_scout_alert(self):
        self.assertFalse(scout_eligible(candidate(title="Vintage 2023 Slayer Divine Intervention Brockum"), TAGS)[0])
        self.assertFalse(scout_eligible(candidate(title=candidate()["title"] + " THRASHED"), TAGS)[0])
        self.assertFalse(scout_eligible(candidate(_price=25, _total_cost=38), TAGS)[0])
        self.assertFalse(scout_eligible(candidate(_auth_score=45), TAGS)[0])
        self.assertFalse(scout_eligible(candidate(_subject=""), TAGS)[0])

    def test_scout_alert_cooldown_is_six_hours(self):
        now = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
        self.assertTrue(cooldown_passed({}, now))
        self.assertFalse(cooldown_passed({"last_scout_alert_at": (now-timedelta(hours=1)).isoformat()}, now))
        self.assertTrue(cooldown_passed({"last_scout_alert_at": (now-timedelta(hours=6)).isoformat()}, now))

    def test_two_comps_need_larger_discount_and_spread(self):
        cfg = dict(
            minimum_reference_median_usd=300, minimum_reference_samples=2,
            grail_reference_median_usd=400, grail_min_discount_percent=35,
            grail_min_spread_usd=150,
            strong_reference_median_usd=300, strong_min_discount_percent=40,
            strong_min_spread_usd=125,
            hidden_reference_median_usd=300, hidden_min_discount_percent=50,
            hidden_min_spread_usd=150,
            low_sample_extra_discount_percent=10, low_sample_min_spread_usd=180,
        )
        kwargs = dict(reference_median=500, conservative_reference=440, sample_count=2,
                      authenticity_score=100, tier="tier_1", settings=cfg)
        self.assertFalse(classify_deal(total_purchase_cost=300, **kwargs)["should_alert"])
        result = classify_deal(total_purchase_cost=220, **kwargs)
        self.assertTrue(result["should_alert"])
        self.assertEqual(result["confidence"], "LOW")


if __name__ == "__main__":
    unittest.main()
