from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from engine.extractors._internal import sec_filings
from engine.workflows._internal import download_workflow


class SecFilingsDownloadTest(unittest.TestCase):
    def test_download_us_companyfacts_writes_cik_json_for_symbols(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ticker_map = root / "sec_company_tickers.csv"
            output_dir = root / "companyfacts"
            pd.DataFrame([{"cik": "320193", "ticker": "AAPL", "title": "Apple Inc."}]).to_csv(
                ticker_map,
                index=False,
            )

            with (
                patch.object(sec_filings, "_download_sec_companyfacts", return_value=b'{"cik":320193,"facts":{}}') as download,
                patch.object(sec_filings, "sleep") as sleep_mock,
            ):
                written = sec_filings.download_us_companyfacts(
                    symbols=["AAPL"],
                    output_dir=output_dir,
                    ticker_map_path=ticker_map,
                    sleep_seconds=0.25,
                )

            out_path = output_dir / "CIK0000320193.json"
            self.assertEqual(written, [out_path])
            self.assertEqual(out_path.read_text(encoding="utf-8"), '{"cik":320193,"facts":{}}')
            download.assert_called_once_with("320193", user_agent=sec_filings.DEFAULT_SEC_USER_AGENT)
            sleep_mock.assert_called_once_with(0.25)

    def test_download_us_companyfacts_accepts_cik_without_ticker_map(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_dir = root / "companyfacts"
            output_dir.mkdir()
            out_path = output_dir / "CIK0000320193.json"
            out_path.write_text("{}", encoding="utf-8")

            with (
                patch.object(sec_filings, "download_sec_company_tickers") as download_tickers,
                patch.object(sec_filings, "_download_sec_companyfacts") as download_companyfacts,
            ):
                written = sec_filings.download_us_companyfacts(
                    symbols=["CIK0000320193"],
                    output_dir=output_dir,
                    ticker_map_path=root / "missing.csv",
                )

            self.assertEqual(written, [])
            download_tickers.assert_not_called()
            download_companyfacts.assert_not_called()

    def test_download_workflow_routes_us_statements_to_sec_companyfacts(self):
        with (
            patch.object(
                sys,
                "argv",
                [
                    "prog",
                    "--market",
                    "us",
                    "--symbols",
                    "AAPL,MSFT",
                    "--offset",
                    "5",
                    "--limit",
                    "2",
                    "--force",
                    "statements",
                ],
            ),
            patch.object(download_workflow, "download_us_companyfacts") as download,
        ):
            download_workflow.main()

        download.assert_called_once_with(
            symbols=["AAPL", "MSFT"],
            offset=5,
            limit=2,
            force=True,
            sleep_seconds=0.1,
        )


if __name__ == "__main__":
    unittest.main()
