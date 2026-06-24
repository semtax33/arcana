from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from engine.transformers.erp import (
    COUNTRY_ERP_COLUMNS,
    normalize_country_erp,
    normalize_damodaran_country_erp_frame,
    normalize_fred_risk_free_rate_frame,
)
from engine.workflows._internal import download_workflow


class ErpInputsTest(unittest.TestCase):
    def test_normalize_damodaran_country_erp_frame_extracts_country_erp(self):
        raw = pd.DataFrame(
            [
                ["Last updated: January 5, 2026", None, None, None, None, None],
                [None, None, None, None, None, None],
                [
                    "Country",
                    "Moody's rating",
                    "Adj. Default Spread",
                    "Country Risk Premium",
                    "Equity Risk Premium",
                    "Corporate Tax Rate",
                ],
                ["Korea", "Aa2", "0.42%", "0.64%", "4.87%", "26.40%"],
                ["United States", "Aa1", 0.0023, 0.0036, 0.0459, 0.21],
            ]
        )

        result = normalize_damodaran_country_erp_frame(raw)

        self.assertEqual(result.columns.tolist(), COUNTRY_ERP_COLUMNS)
        by_country = result.set_index("country")
        self.assertEqual(by_country.loc["Korea", "country_code"], "KR")
        self.assertAlmostEqual(by_country.loc["Korea", "equity_risk_premium"], 4.87)
        self.assertEqual(by_country.loc["United States", "country_code"], "US")
        self.assertAlmostEqual(by_country.loc["United States", "equity_risk_premium"], 4.59)
        self.assertEqual(by_country.loc["Korea", "source"], "damodaran_nyu")

    def test_normalize_fred_risk_free_rate_frame_maps_known_series(self):
        raw = pd.DataFrame(
            {
                "observation_date": ["2026-01-01", "2026-01-02", "2026-01-03"],
                "DGS10": [".", "4.10", "4.12"],
            }
        )

        result = normalize_fred_risk_free_rate_frame(raw, series_id="DGS10")

        self.assertEqual(result["market"].tolist(), ["us", "us"])
        self.assertEqual(result["country_code"].tolist(), ["US", "US"])
        self.assertEqual(result["risk_free_rate"].tolist(), [4.10, 4.12])

    def test_normalize_country_erp_falls_back_to_kr_benchmark_minus_risk_free(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing_damodaran = root / "missing.xlsx"
            output = root / "country_erp.csv"
            benchmark = root / "benchmark.csv"
            rates = root / "rates.csv"
            benchmark.write_text(
                "\n".join(
                    [
                        "benchmark_id,trade_date,close",
                        "KOSPI200,2024-01-03,100",
                        "KOSPI200,2026-01-02,121",
                    ]
                ),
                encoding="utf-8",
            )
            rates.write_text(
                "\n".join(
                    [
                        "market,country_code,date,risk_free_rate,source,series_id,updated_at",
                        "kr,KR,2026-01-01,3.0,fred,IRLTLT01KRM156N,2026-01-02",
                    ]
                ),
                encoding="utf-8",
            )

            result = normalize_country_erp(
                missing_damodaran,
                output_path=output,
                risk_free_path=rates,
                benchmark_path=benchmark,
            )
            self.assertTrue(output.exists())

        self.assertEqual(result["country_code"].tolist(), ["KR"])
        self.assertEqual(result["source"].iat[0], "kr_benchmark_minus_government_bond")
        self.assertGreater(result["equity_risk_premium"].iat[0], 6.0)

    def test_download_workflow_routes_erp_inputs_for_both_markets(self):
        with (
            patch.object(sys, "argv", ["prog", "--market", "kr", "erp"]),
            patch.object(download_workflow, "download_default_erp_inputs", return_value=[Path("ctryprem.xlsx")]) as download,
        ):
            download_workflow.main()

        download.assert_called_once()

        with (
            patch.object(sys, "argv", ["prog", "--market", "us", "wacc-inputs"]),
            patch.object(download_workflow, "download_default_erp_inputs", return_value=[Path("ctryprem.xlsx")]) as download,
        ):
            download_workflow.main()

        download.assert_called_once()


if __name__ == "__main__":
    unittest.main()


