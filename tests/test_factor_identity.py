import unittest

from api.service.factor_identity import canonical_factor_id


class FactorIdentityTest(unittest.TestCase):
    def test_canonical_factor_id_accepts_catalog_aliases_and_display_names(self):
        cases = {
            "EV/NOPAT": "ev_to_nopat",
            "EV_TO_NOPAT": "ev_to_nopat",
            "EV TO NOPAT": "ev_to_nopat",
            "WORKING CAPITAL TURNOVER": "working_capital_turnover",
            "FCF TO EV YIELD": "fcf_to_ev_yield",
            "R&D / Market Cap": "rnd_to_market_cap",
            "TOTAL_SCORE": "style_total_score",
            "Style Score": "style_total_score",
        }

        for raw_value, expected in cases.items():
            with self.subTest(raw_value=raw_value):
                self.assertEqual(canonical_factor_id(raw_value), expected)

    def test_canonical_factor_id_rejects_unknown_unsafe_punctuation(self):
        with self.assertRaises(ValueError):
            canonical_factor_id("roe;DROP")


if __name__ == "__main__":
    unittest.main()
