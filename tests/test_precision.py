import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hunter_precision import (
    comparable, find_phrase, graphic_terms, is_variation, norm,
    phrase_in, reject_listing, summarize_comps, usd_total,
)

HARD = ["cd", "kids", "reprint", "printed tag", "no tag", "modern print", "hoodie"]
MODERN = ["gildan", "comfort colors"]


def listing(item_id, title, price, shipping=0, **extra):
    return {
        "itemId": item_id, "title": title,
        "price": {"value": str(price), "currency": "USD"},
        "shippingOptions": [{"shippingCost": {"value": str(shipping), "currency": "USD"}}],
        "itemWebUrl": f"https://www.ebay.com/itm/{item_id}", **extra,
    }


class HunterPrecisionTests(unittest.TestCase):
    def test_short_keyword_is_word_boundary_not_cd_inside_acdc(self):
        self.assertIsNone(find_phrase("ACDC 1994 concert shirt", HARD))
        self.assertEqual(find_phrase("ACDC CD album", HARD), "cd")

    def test_reprint_overrides_vintage_claims(self):
        item = listing("1", "Vintage 1994 Nirvana shirt reprint Giant", 23)
        self.assertIsNotNone(reject_listing(item, HARD, MODERN))

    def test_variations_with_metadata(self):
        item = listing("2", "Nirvana 1994 Vintage Shirt", 25,
                       itemGroupHref="https://example.com/group")
        self.assertTrue(is_variation(item))
        item2 = listing("3", "Vintage Nirvana shirt choose your size", 25)
        self.assertTrue(is_variation(item2))
        item3 = listing("4", "Vintage Nirvana shirt size XL", 99)
        self.assertFalse(is_variation(item3))

    def test_modern_body_and_new_without_deadstock(self):
        item = listing("5", "Nirvana 1994 Giant Tee", 40,
                       localizedAspects=[{"name": "Brand", "value": "Gildan"}])
        self.assertIsNotNone(reject_listing(item, HARD, MODERN, detail=True))
        fresh = listing("6", "Nirvana 1994 Giant Tee", 60, condition="NEW")
        self.assertIn("신품", reject_listing(fresh, HARD, MODERN, detail=True))

    def test_excludes_candidate_and_other_design(self):
        other = listing("2", "Vintage 1995 Nirvana Heart Shaped Box Tee", 500)
        self.assertFalse(comparable(other, artist="Nirvana", subject="In Utero",
                                    signature=["angel"], candidate_id="1",
                                    hard_excludes=HARD, modern_bodies=MODERN))
        same = listing("1", "Vintage 1994 Nirvana In Utero Angel Tee", 500)
        self.assertFalse(comparable(same, artist="Nirvana", subject="In Utero",
                                    signature=["angel"], candidate_id="1",
                                    hard_excludes=HARD, modern_bodies=MODERN))

    def test_free_shipping_is_zero_not_unknown(self):
        self.assertEqual(usd_total(listing("1", "shirt", 200, 0)), 200)
        missing = listing("2", "shirt", 200)
        missing.pop("shippingOptions")
        self.assertIsNone(usd_total(missing))

    def test_conservative_reference_excludes_fake_and_own_listing(self):
        items = [
            listing("self", "Vintage 1994 Nirvana In Utero Angel Tee", 200),
            listing("c1", "Vintage 1994 Nirvana In Utero Angel Tee Giant", 400),
            listing("c2", "Vintage 1993 Nirvana In Utero Angel Tee Giant", 480),
            listing("c3", "Vintage 1995 Nirvana In Utero Angel Tee Giant", 560),
            listing("fake", "Nirvana In Utero Angel reprint tee 1994", 1200),
            listing("wrong", "Vintage 1995 Nirvana Heart Shaped Box Tee", 1200),
        ]
        data = summarize_comps(items, artist="Nirvana", subject="In Utero",
                               signature=["angel"], candidate_id="self",
                               hard_excludes=HARD, modern_bodies=MODERN)
        self.assertEqual(data["sample_count"], 3)
        self.assertEqual(data["median"], 480)
        self.assertEqual(data["conservative"], 440)
        self.assertEqual(len(data["comparison_urls"]), 3)
        self.assertEqual(data["comparison_type"], "active_asking_not_sold")

    def test_no_mixed_price_ranges_or_insufficient_comps(self):
        items = [listing("1", "Vintage 1994 Tool Opiate Skeleton Tee", 80),
                 listing("2", "Vintage 1994 Tool Opiate Skeleton Tee", 450),
                 listing("3", "Vintage 1994 Tool Opiate Skeleton Tee", 1300)]
        kwargs = dict(artist="Tool", subject="Opiate", signature=["skeleton"],
                      candidate_id="x", hard_excludes=HARD, modern_bodies=MODERN)
        self.assertIsNone(summarize_comps(items, **kwargs))
        self.assertIsNone(summarize_comps(items[:2], **kwargs))

    def test_acdc_artist_alias(self):
        item = listing("3", "Vintage 1991 AC/DC Thunderstruck Tee", 400)
        self.assertTrue(comparable(item, artist="ACDC", subject="Thunderstruck",
                                   signature=[], candidate_id="self",
                                   hard_excludes=HARD, modern_bodies=MODERN))

    def test_unicode_normalized_and_generic_signature(self):
        self.assertEqual(norm("Mötley Crüe"), "motley crue")
        self.assertTrue(phrase_in("Mötley Crüe Tour", "Motley Crue"))
        self.assertEqual(graphic_terms("Nirvana Vintage 1994 Giant In Utero Angel Shirt",
                                       "Nirvana", "In Utero"), ["angel"])


if __name__ == "__main__":
    unittest.main()
