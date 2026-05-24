from pathlib import Path
import unittest

import pandas as pd

from engine.core.identifiers import issuer_id_of, security_id_of
from engine.markets.kr import KR_MARKET_CONFIG
from engine.style_score_pipeline import calculate_factor_scores, calculate_style_scores


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class EngineLayeredRefactorTest(unittest.TestCase):
    def test_kr_market_config_preserves_existing_identifiers(self):
        self.assertEqual(KR_MARKET_CONFIG.normalize_symbol("5930"), "005930")
        self.assertEqual(security_id_of("5930", KR_MARKET_CONFIG), "SEC_KR_005930")
        self.assertEqual(issuer_id_of("005935", KR_MARKET_CONFIG), "ISSUER_ID_005930")
        self.assertEqual(KR_MARKET_CONFIG.currency, "KRW")

    def test_style_score_preserves_long_stock_code(self):
        factor_scores = pd.DataFrame(
            [
                {
                    "trade_date": "2026-05-24",
                    "security_id": "SEC_US_AAPL",
                    "issuer_id": "ISSUER_US_AAPL",
                    "stock_code": "NASDAQ-AAPL-LONG",
                    "country": "US",
                    "market_mic": "XNAS",
                    "company_name": "Apple",
                    "industry_schema": "GICS",
                    "industry_code": "IG",
                    "industry_name": "Technology",
                    "factor_id": "epr",
                    "style_group": "VALUE",
                    "factor_direction": 1,
                    "raw_factor_value": 1.0,
                    "winsorized_value": 1.0,
                    "percentile_score": 80.0,
                    "robust_z_score": 0.0,
                    "n_peers": 10,
                    "score_method": "INDUSTRY_PERCENTILE",
                    "fallback_level": "ALL_NON_FINANCIAL",
                    "is_valid": True,
                    "invalid_reason": "",
                    "is_winsorized": False,
                    "is_missing": False,
                    "score_confidence": 1.0,
                    "source_trade_date": "2026-05-24",
                }
            ]
        )

        result = calculate_style_scores(factor_scores, trade_date="2026-05-24")

        self.assertEqual(result.loc[0, "stock_code"], "NASDAQ-AAPL-LONG")
        self.assertEqual(result.loc[0, "country"], "US")
        self.assertEqual(result.loc[0, "market_mic"], "XNAS")

    def test_factor_score_preserves_long_stock_code(self):
        universe = pd.DataFrame(
            [
                {
                    "security_id": f"SEC_US_{index}",
                    "issuer_id": f"ISSUER_US_{index}",
                    "stock_code": f"LONG-SYMBOL-{index}",
                    "country": "US",
                    "market_mic": "XNAS",
                    "company_name": f"Company {index}",
                    "industry_schema": "GICS",
                    "industry_group_code": "IG",
                    "industry_group_name": "Industry",
                    "sector_code": "SEC",
                    "is_financial": False,
                }
                for index in range(5)
            ]
        )
        factors = pd.DataFrame(
            {
                "security_id": universe["security_id"],
                "trade_date": ["2026-05-24"] * len(universe),
                "factor_id": ["epr"] * len(universe),
                "factor_value": list(range(1, len(universe) + 1)),
            }
        )

        result = calculate_factor_scores(universe, factors, trade_date="2026-05-24")

        self.assertEqual(result.loc[0, "stock_code"], "LONG-SYMBOL-0")
        self.assertEqual(result.loc[0, "country"], "US")
        self.assertEqual(result.loc[0, "market_mic"], "XNAS")

    def test_market_extension_migration_is_additive_and_uses_stock_code(self):
        sql = (
            PROJECT_ROOT
            / "data-lake"
            / "meta"
            / "sql"
            / "20260524_add_market_extension_columns.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("ADD COLUMN IF NOT EXISTS country", sql)
        self.assertNotIn("listing_symbol", sql)
        self.assertNotIn("MODIFY COLUMN stock_code", sql)

    def test_transformers_do_not_own_external_io(self):
        transformer_root = PROJECT_ROOT / "engine" / "transformers"
        text = "\n".join(path.read_text(encoding="utf-8") for path in transformer_root.glob("*.py"))

        self.assertNotIn("requests", text)
        self.assertNotIn("pykrx", text)
        self.assertNotIn("clickhouse_connect", text)
        self.assertNotIn("insert_df", text)

    def test_loaders_do_not_call_market_providers(self):
        loader_root = PROJECT_ROOT / "engine" / "loaders"
        text = "\n".join(path.read_text(encoding="utf-8") for path in loader_root.glob("*.py"))

        self.assertNotIn("requests", text)
        self.assertNotIn("pykrx", text)

    def test_engine_uses_core_clickhouse_config_boundary(self):
        engine_root = PROJECT_ROOT / "engine"
        text = "\n".join(path.read_text(encoding="utf-8") for path in engine_root.rglob("*.py"))

        self.assertNotIn("api.config.clickhouse", text)


if __name__ == "__main__":
    unittest.main()
