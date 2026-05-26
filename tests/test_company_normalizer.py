from pathlib import Path
import unittest

import pandas as pd

from engine.transformers import securities
from engine.transformers.securities import attach_gics_sector, load_gics_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CompanyNormalizerGicsTest(unittest.TestCase):
    def test_default_gics_config_contains_latest_industry_groups(self):
        config = load_gics_config(
            PROJECT_ROOT / "data-lake" / "meta" / "rules" / "gics_rules.yaml"
        )

        self.assertEqual(len(config["sectors"]), 11)
        self.assertEqual(len(config["industry_groups"]), 25)
        self.assertEqual(config["industry_groups"]["4530"], "Semiconductors & Semiconductor Equipment")

    def test_market_gics_configs_are_split_for_kr_and_us(self):
        kr_path = securities.gics_rules_path_for_market("kr")
        us_path = securities.gics_rules_path_for_market("us")

        self.assertEqual(kr_path.name, "gics_rules_kr.yaml")
        self.assertEqual(us_path.name, "gics_rules_us.yaml")
        self.assertEqual(
            load_gics_config(kr_path)["industry_groups"]["4530"],
            "Semiconductors & Semiconductor Equipment",
        )
        self.assertIn("AAPL", load_gics_config(us_path)["manual_overrides"])

    def test_attach_gics_sector_adds_sector_and_industry_group(self):
        mapped = attach_gics_sector(
            pd.DataFrame(
                [
                    {
                        "종목코드": "005930",
                        "회사명": "Example Semi",
                        "업종": "반도체 제조업",
                        "주요제품": "Semiconductor memory",
                        "지역": "서울",
                    }
                ]
            ),
            _minimal_config(),
        )

        self.assertEqual(mapped.loc[0, "gics_sector_code"], "45")
        self.assertEqual(mapped.loc[0, "gics_sector_name"], "Information Technology")
        self.assertEqual(mapped.loc[0, "gics_industry_group_code"], "4530")
        self.assertEqual(
            mapped.loc[0, "gics_industry_group_name"],
            "Semiconductors & Semiconductor Equipment",
        )
        self.assertGreater(mapped.loc[0, "gics_industry_group_confidence"], 0)

    def test_manual_override_can_set_industry_group_and_parent_sector(self):
        config = _minimal_config()
        config["manual_overrides"] = {
            "123456": {
                "industry_group_code": "3520",
                "reason": "known biotech issuer",
            }
        }

        mapped = attach_gics_sector(
            pd.DataFrame(
                [
                    {
                        "종목코드": "123456",
                        "회사명": "Manual Bio",
                        "업종": "기타",
                        "주요제품": "기타",
                    }
                ]
            ),
            config,
        )

        self.assertEqual(mapped.loc[0, "gics_sector_code"], "35")
        self.assertEqual(mapped.loc[0, "gics_industry_group_code"], "3520")
        self.assertEqual(mapped.loc[0, "gics_industry_group_confidence"], 1.0)

    def test_unmatched_row_remains_unmapped(self):
        mapped = attach_gics_sector(
            pd.DataFrame(
                [
                    {
                        "종목코드": "999999",
                        "회사명": "No Match",
                        "업종": "unknown",
                        "주요제품": "unknown",
                    }
                ]
            ),
            _minimal_config(),
        )

        self.assertEqual(mapped.loc[0, "gics_sector_code"], "UNMAPPED")
        self.assertEqual(mapped.loc[0, "gics_industry_group_code"], "UNMAPPED")
        self.assertEqual(mapped.loc[0, "gics_industry_group_confidence"], 0.0)

    def test_us_normalized_issuers_apply_gics_mapping(self):
        ticker_map = pd.DataFrame(
            [
                {"cik": "320193", "ticker": "AAPL", "title": "Apple Inc."},
                {"cik": "19617", "ticker": "JPM", "title": "JPMorgan Chase & Co."},
                {"cik": "34088", "ticker": "XOM", "title": "Exxon Mobil Corporation"},
                {"cik": "999999", "ticker": "DNA", "title": "Example Therapeutics Inc."},
            ]
        )

        with unittest.mock.patch.object(securities, "_get_us_ticker_map", return_value=ticker_map):
            result = securities.get_normalized_sector_and_issuer(market="us")

        by_issuer = result.set_index("issuer_id")
        self.assertEqual(by_issuer.loc["ISSUER_US_AAPL", "sector_code"], "45")
        self.assertEqual(by_issuer.loc["ISSUER_US_AAPL", "industry_group_code"], "4520")
        self.assertEqual(by_issuer.loc["ISSUER_US_JPM", "industry_group_code"], "4010")
        self.assertEqual(by_issuer.loc["ISSUER_US_XOM", "industry_group_code"], "1010")
        self.assertEqual(by_issuer.loc["ISSUER_US_DNA", "industry_group_code"], "3520")


def _minimal_config():
    return {
        "sectors": {
            "35": "Health Care",
            "45": "Information Technology",
        },
        "industry_groups": {
            "3520": "Pharmaceuticals, Biotechnology & Life Sciences",
            "4530": "Semiconductors & Semiconductor Equipment",
        },
        "weights": {
            "industry": 3.0,
            "product": 1.5,
            "confidence_denominator": 6.0,
        },
        "industry_group_weights": {
            "industry": 3.0,
            "product": 1.5,
            "any_text": 1.0,
            "confidence_denominator": 6.0,
        },
        "sector_priority": ["45", "35"],
        "industry_group_priority": ["4530", "3520"],
        "sector_rules": {
            "35": {"any_text_patterns": ["Biotech|바이오"]},
            "45": {"any_text_patterns": ["Semiconductor|반도체"]},
        },
        "industry_group_rules": {
            "3520": {
                "sector_code": "35",
                "any_text_patterns": ["Biotech|바이오"],
            },
            "4530": {
                "sector_code": "45",
                "any_text_patterns": ["Semiconductor|반도체"],
            },
        },
    }


if __name__ == "__main__":
    unittest.main()
