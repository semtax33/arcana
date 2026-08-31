from __future__ import annotations

import sys
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from engine.extractors._internal import sec_filings
from engine.workflows._internal import download_workflow


class FakeAttachments:
    def __init__(self, attachments):
        self._attachments = attachments
        self.primary_html_document = attachments[0]

    def __iter__(self):
        return iter(self._attachments)


class SecFilingsDownloadTest(unittest.TestCase):
    @staticmethod
    def _attachment(document_type, document, content, *, sequence="1", description=""):
        return SimpleNamespace(
            document_type=document_type,
            document=document,
            content=content,
            sequence_number=sequence,
            description=description,
            url=f"https://www.sec.gov/Archives/{document}",
            is_html=lambda: document.lower().endswith((".htm", ".html")),
        )

    @staticmethod
    def _filing(form, filing_date, accession, attachments):
        return SimpleNamespace(
            form=form,
            filing_date=filing_date,
            accession_no=accession,
            period_of_report="2025-06-30",
            attachments=FakeAttachments(attachments),
        )

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

    def test_download_us_filing_htmls_preserves_primary_and_routes_ir_exhibits(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ticker_map = root / "sec_company_tickers.csv"
            output_dir = root / "fillings"
            pd.DataFrame([{"cik": "320193", "ticker": "AAPL", "title": "Apple Inc."}]).to_csv(
                ticker_map,
                index=False,
            )
            tenk_html = b"<!doctype html><html><body>10-K raw</body></html>"
            tenq_html = b"<html><body>10-Q raw</body></html>"
            eightk_html = b"<html><body>8-K raw</body></html>"
            filings = [
                self._filing(
                    "10-K",
                    "2025-10-31",
                    "0000320193-25-000001",
                    [self._attachment("10-K", "aapl-2025.htm", tenk_html)],
                ),
                self._filing(
                    "10-Q",
                    "2025-07-31",
                    "0000320193-25-000002",
                    [self._attachment("10-Q", "aapl-2025q3.htm", tenq_html)],
                ),
                self._filing(
                    "8-K",
                    "2025-08-01",
                    "0000320193-25-000003",
                    [
                        self._attachment("8-K", "aapl-8k.htm", eightk_html),
                        self._attachment("EX-99.1", "earnings.htm", b"<html>earnings</html>", sequence="2"),
                        self._attachment("EX-99.2", "slides.html", b"<html>slides</html>", sequence="3"),
                        self._attachment("EX-99.01", "release.htm", b"<html>release</html>", sequence="4"),
                        self._attachment("EX-99.3", "appendix.pdf", b"%PDF", sequence="5"),
                        self._attachment("EX-101.INS", "instance.xml", b"<xml/>", sequence="6"),
                    ],
                ),
            ]

            summary = sec_filings.download_us_filing_htmls(
                symbols=["AAPL"],
                start_date="2025-01-01",
                end_date="2025-12-31",
                output_dir=output_dir,
                ticker_map_path=ticker_map,
                filings_provider=lambda *_: filings,
                sleep_seconds=0,
            )

            self.assertEqual(summary.primary_html_written, 3)
            self.assertEqual(summary.ir_html_written, 3)
            self.assertEqual(summary.non_html_documents_skipped, 1)
            tenk_path = next((output_dir / "10-K" / "AAPL").glob("*.htm"))
            self.assertEqual(tenk_path.read_bytes(), tenk_html)
            ir_paths = sorted(
                path
                for path in (output_dir / "ir" / "AAPL").iterdir()
                if path.suffix.lower() in {".htm", ".html"}
            )
            self.assertEqual(len(ir_paths), 3)
            metadata = json.loads(
                Path(str(ir_paths[0]) + ".metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["provider"], "edgartools")
            self.assertEqual(metadata["category"], "ir")
            self.assertRegex(metadata["document_type"], r"^EX-99\.")
            self.assertTrue((output_dir / "latest_run.json").exists())

            resumed = sec_filings.download_us_filing_htmls(
                symbols=["AAPL"],
                start_date="2025-01-01",
                end_date="2025-12-31",
                output_dir=output_dir,
                ticker_map_path=ticker_map,
                filings_provider=lambda *_: (_ for _ in ()).throw(AssertionError("provider should not run")),
                sleep_seconds=0,
            )
            self.assertEqual(resumed.symbols_resumed, 1)

    def test_sec_ir_exhibit_matching_accepts_99x_only(self):
        self.assertTrue(sec_filings.is_sec_ir_exhibit(SimpleNamespace(document_type="EX-99")))
        self.assertTrue(sec_filings.is_sec_ir_exhibit(SimpleNamespace(document_type="ex-99.01")))
        self.assertTrue(sec_filings.is_sec_ir_exhibit(SimpleNamespace(document_type="EX-99.9")))
        self.assertFalse(sec_filings.is_sec_ir_exhibit(SimpleNamespace(document_type="EX-101.INS")))
        self.assertFalse(sec_filings.is_sec_ir_exhibit(SimpleNamespace(document_type="EX-9.9")))

    def test_safe_path_part_prefixes_windows_reserved_names(self):
        self.assertEqual(sec_filings._safe_path_part("CON"), "_CON")
        self.assertEqual(sec_filings._safe_path_part("con.htm"), "_con.htm")
        self.assertEqual(sec_filings._safe_path_part("COM1"), "_COM1")
        self.assertEqual(sec_filings._safe_path_part("COMPANY"), "COMPANY")

    def test_edgartools_data_directory_is_forced_to_configured_data_lake_path(self):
        with TemporaryDirectory() as tmpdir:
            configured = Path(tmpdir) / "data-lake" / "cache" / "edgar"
            with patch.dict(
                os.environ,
                {"EDGAR_LOCAL_DATA_DIR": str(Path.home() / ".edgar")},
                clear=False,
            ):
                resolved = sec_filings._configure_edgartools_data_directory(configured)

                self.assertEqual(resolved, configured.resolve())
                self.assertEqual(os.environ["EDGAR_LOCAL_DATA_DIR"], str(configured.resolve()))
                self.assertTrue(configured.is_dir())
                self.assertTrue(
                    (configured / "_tcache" / ".locale_fix_457_applied").is_file()
                )
                self.assertTrue(
                    (configured / "_tcache" / ".empty_response_fix_672_applied").is_file()
                )

    def test_ir_only_mode_queries_8k_and_does_not_write_primary_html(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ticker_map = root / "sec_company_tickers.csv"
            output_dir = root / "fillings"
            pd.DataFrame([{"cik": "320193", "ticker": "AAPL", "title": "Apple Inc."}]).to_csv(
                ticker_map,
                index=False,
            )
            filings = [
                self._filing(
                    "8-K",
                    "2025-08-01",
                    "0000320193-25-000003",
                    [
                        self._attachment("8-K", "aapl-8k.htm", b"<html>8-K</html>"),
                        self._attachment("EX-99.1", "earnings.htm", b"<html>earnings</html>"),
                    ],
                )
            ]
            provider_calls = []

            def provider(company, forms, start_date, end_date):
                provider_calls.append((company["ticker"], forms, start_date, end_date))
                return filings

            summary = sec_filings.download_us_filing_htmls(
                symbols=["AAPL"],
                start_date="2016-01-01",
                end_date="2026-08-30",
                forms=["10-K", "10-Q", "8-K"],
                ir_only=True,
                workers=2,
                output_dir=output_dir,
                ticker_map_path=ticker_map,
                filings_provider=provider,
                sleep_seconds=0,
            )

            self.assertEqual(provider_calls, [("AAPL", ["8-K"], "2016-01-01", "2026-08-30")])
            self.assertEqual(summary.primary_html_written, 0)
            self.assertEqual(summary.ir_html_written, 1)
            self.assertTrue(summary.ir_only)
            self.assertEqual(summary.workers, 2)
            self.assertFalse((output_dir / "8-K").exists())
            self.assertEqual(len(list((output_dir / "ir" / "AAPL").glob("*.htm"))), 1)

    def test_download_workflow_routes_sec_filing_html_options(self):
        summary = SimpleNamespace(to_dict=lambda: {"primary_html_written": 1})
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
                    "--start-date",
                    "2025-01-01",
                    "--end-date",
                    "2025-12-31",
                    "--sec-forms",
                    "10-K,8-K",
                    "--limit",
                    "2",
                    "sec-filings",
                ],
            ),
            patch.object(download_workflow, "download_us_filing_htmls", return_value=summary) as download,
        ):
            download_workflow.main()

        download.assert_called_once_with(
            symbols=["AAPL", "MSFT"],
            start_date="20250101",
            end_date="20251231",
            forms=["10-K", "8-K"],
            offset=0,
            limit=2,
            force=False,
            resume=True,
            ir_only=False,
            workers=1,
            sleep_seconds=0.1,
            retries=3,
            retry_backoff_seconds=30.0,
        )


if __name__ == "__main__":
    unittest.main()
