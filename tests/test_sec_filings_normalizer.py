from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

from engine.core.paths import parse_statement_snapshot_filename, statement_snapshot_name
from engine.transformers.sec_filings import normalize_us_sec_filings
from engine.transformers._internal.sec_filings import US_MAPPING_RULE_PATH
from engine.workflows._internal.normalize_workflow import MAPPING_RULE_PATH


CANONICAL_ROWS = [
    {
        "canonical_id": "REVENUE",
        "canonical_nm": "Revenue",
        "fs_type": "IS",
        "is_derived": "FALSE",
        "formula": "",
        "description": "",
        "비고": "",
        "鍮꾧퀬": "",
    },
    {
        "canonical_id": "RND",
        "canonical_nm": "R&D",
        "fs_type": "IS",
        "is_derived": "FALSE",
        "formula": "",
        "description": "",
        "비고": "",
        "鍮꾧퀬": "",
    },
]


def write_canonical(path: Path) -> None:
    pd.DataFrame(CANONICAL_ROWS).to_csv(path, index=False, encoding="utf-8")


def write_ticker_map(path: Path, cik: str = "320193", ticker: str = "AAPL") -> None:
    pd.DataFrame([{"cik": cik, "ticker": ticker, "title": "Apple Inc."}]).to_csv(
        path,
        index=False,
        encoding="utf-8",
    )


def write_companyfacts(path: Path, facts: dict) -> None:
    path.write_text(
        json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {"us-gaap": facts},
            }
        ),
        encoding="utf-8",
    )


def write_companyfacts_namespaces(path: Path, facts_by_namespace: dict) -> None:
    path.write_text(
        json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": facts_by_namespace,
            }
        ),
        encoding="utf-8",
    )


def fact(label: str, value: float, tag: str) -> dict:
    return {
        "label": label,
        "description": label,
        "units": {
            "USD": [
                {
                    "start": "2025-01-01",
                    "end": "2025-12-31",
                    "val": value,
                    "accn": f"0000320193-26-{tag[-3:]}",
                    "fy": 2025,
                    "fp": "FY",
                    "form": "10-K",
                    "filed": "2026-01-30",
                    "frame": "CY2025",
                }
            ]
        },
    }


class SecFilingsNormalizerTest(unittest.TestCase):
    def test_market_mapping_rule_paths_prefer_prefixed_files(self):
        self.assertEqual(MAPPING_RULE_PATH.name, "kr_mapping.yaml")
        self.assertEqual(US_MAPPING_RULE_PATH.name, "us_mapping.yaml")

    def test_statement_snapshot_name_keeps_us_ticker(self):
        name = statement_snapshot_name("AAPL", 2025, 12, market="us")

        self.assertEqual(name, "us_normalized_AAPL_2025.12.csv")
        self.assertEqual(
            parse_statement_snapshot_filename(name),
            {"market": "us", "stock_code": "AAPL", "year": 2025, "month": 12},
        )

    def test_companyfacts_primary_wins_over_alternate_notes_and_edgartools(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            companyfacts = root / "companyfacts"
            output = root / "out"
            companyfacts.mkdir()
            canonical = root / "canonical.csv"
            ticker_map = root / "tickers.csv"
            metadata = root / "metadata.csv"
            write_canonical(canonical)
            write_ticker_map(ticker_map)
            write_companyfacts(
                companyfacts / "CIK0000320193.json",
                {
                    "ResearchAndDevelopmentExpense": fact("R&D primary", 10, "primary"),
                    "InProcessResearchAndDevelopmentExpense": fact("R&D alternate", 20, "alternate"),
                },
            )

            normalize_us_sec_filings(
                symbols=["AAPL"],
                start_year=2025,
                end_year=2025,
                companyfacts_dir=companyfacts,
                notes_root=root / "missing-notes",
                output_dir=output,
                ticker_map_path=ticker_map,
                canonical_csv_path=canonical,
                report_metadata_path=metadata,
                edgartools_provider=lambda *_: [
                    {
                        "symbol": "AAPL",
                        "cik": "320193",
                        "canonical_id": "RND",
                        "statement_type": "IS",
                        "fiscal_year": 2025,
                        "fiscal_month": 12,
                        "value": 30,
                        "tag": "ResearchAndDevelopmentExpense",
                    }
                ],
            )

            df = pd.read_csv(output / "us_normalized_AAPL_2025.12.csv")
            debug = pd.read_csv(output / "us_normalized_AAPL_2025.12.debug.csv")

            self.assertEqual(float(df.loc[df["canonical_account_id"].eq("RND"), "normalized_amount"].iat[0]), 10)
            self.assertEqual(debug.loc[debug["canonical_account_id"].eq("RND"), "source"].iat[0], "companyfacts_primary")
            self.assertEqual(debug.loc[debug["canonical_account_id"].eq("RND"), "cik"].astype(str).iat[0], "320193")

    def test_companyfacts_alternate_used_when_primary_missing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            companyfacts = root / "companyfacts"
            output = root / "out"
            companyfacts.mkdir()
            canonical = root / "canonical.csv"
            ticker_map = root / "tickers.csv"
            metadata = root / "metadata.csv"
            write_canonical(canonical)
            write_ticker_map(ticker_map)
            write_companyfacts(
                companyfacts / "CIK0000320193.json",
                {"InProcessResearchAndDevelopmentExpense": fact("IPR&D", 20, "alternate")},
            )

            normalize_us_sec_filings(
                symbols=["AAPL"],
                start_year=2025,
                end_year=2025,
                companyfacts_dir=companyfacts,
                notes_root=root / "missing-notes",
                output_dir=output,
                ticker_map_path=ticker_map,
                canonical_csv_path=canonical,
                report_metadata_path=metadata,
                use_edgartools=False,
            )

            df = pd.read_csv(output / "us_normalized_AAPL_2025.12.csv")
            self.assertEqual(float(df.loc[df["canonical_account_id"].eq("RND"), "normalized_amount"].iat[0]), 20)

    def test_companyfacts_label_rule_maps_non_us_gaap_custom_fact(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            companyfacts = root / "companyfacts"
            output = root / "out"
            companyfacts.mkdir()
            canonical = root / "canonical.csv"
            ticker_map = root / "tickers.csv"
            metadata = root / "metadata.csv"
            write_canonical(canonical)
            write_ticker_map(ticker_map)
            write_companyfacts_namespaces(
                companyfacts / "CIK0000320193.json",
                {
                    "aapl": {
                        "CustomResearchAndDevelopment": fact(
                            "Research and Development",
                            25,
                            "customrnd",
                        )
                    }
                },
            )

            normalize_us_sec_filings(
                symbols=["AAPL"],
                start_year=2025,
                end_year=2025,
                companyfacts_dir=companyfacts,
                notes_root=root / "missing-notes",
                output_dir=output,
                ticker_map_path=ticker_map,
                canonical_csv_path=canonical,
                report_metadata_path=metadata,
                use_edgartools=False,
            )

            df = pd.read_csv(output / "us_normalized_AAPL_2025.12.csv")
            debug = pd.read_csv(output / "us_normalized_AAPL_2025.12.debug.csv")

            self.assertEqual(float(df.loc[df["canonical_account_id"].eq("RND"), "normalized_amount"].iat[0]), 25)
            self.assertEqual(debug.loc[debug["canonical_account_id"].eq("RND"), "source"].iat[0], "companyfacts_label")

    def test_notes_and_edgartools_fill_missing_values(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            companyfacts = root / "companyfacts"
            notes = root / "notes" / "2025_12_notes"
            output = root / "out"
            companyfacts.mkdir()
            notes.mkdir(parents=True)
            canonical = root / "canonical.csv"
            ticker_map = root / "tickers.csv"
            metadata = root / "metadata.csv"
            write_canonical(canonical)
            write_ticker_map(ticker_map)
            write_companyfacts(
                companyfacts / "CIK0000320193.json",
                {"Revenues": fact("Revenue", 100, "revenue")},
            )
            (notes / "sub.tsv").write_text(
                "adsh\tcik\tname\tform\tperiod\tfy\tfp\tfiled\n"
                "0000320193-26-000001\t320193\tApple Inc.\t10-K\t20251231\t2025\tFY\t20260130\n",
                encoding="utf-8",
            )
            (notes / "num.tsv").write_text(
                "adsh\ttag\tversion\tddate\tuom\tdimh\tvalue\n"
                "0000320193-26-000001\tResearchAndDevelopmentExpense\tus-gaap/2025\t20251231\tUSD\t0x00000000\t40\n",
                encoding="utf-8",
            )
            (notes / "tag.tsv").write_text(
                "tag\tversion\tcustom\tabstract\tdatatype\tiord\tcrdr\ttlabel\tdoc\n"
                "ResearchAndDevelopmentExpense\tus-gaap/2025\t0\t0\tmonetaryItemType\tI\tD\tResearch and Development\tR&D\n",
                encoding="utf-8",
            )
            (notes / "pre.tsv").write_text(
                "adsh\treport\tline\tstmt\tinpth\ttag\tversion\tprole\tplabel\tnegating\n"
                "0000320193-26-000001\t2\t1\tIS\t0\tResearchAndDevelopmentExpense\tus-gaap/2025\tterseLabel\tResearch and Development\t0\n",
                encoding="utf-8",
            )
            (notes / "ren.tsv").write_text(
                "adsh\treport\trfile\tmenucat\tshortname\tlongname\troleuri\tparentroleuri\tparentreport\tultparentrpt\n"
                "0000320193-26-000001\t2\tH\tS\tStatement of Operations\tStatement of Operations\trole\t\t\t\n",
                encoding="utf-8",
            )

            normalize_us_sec_filings(
                symbols=["AAPL"],
                start_year=2025,
                end_year=2025,
                companyfacts_dir=companyfacts,
                notes_root=root / "notes",
                output_dir=output,
                ticker_map_path=ticker_map,
                canonical_csv_path=canonical,
                report_metadata_path=metadata,
                edgartools_provider=lambda *_: [
                    {
                        "symbol": "AAPL",
                        "cik": "320193",
                        "canonical_id": "RND",
                        "statement_type": "IS",
                        "fiscal_year": 2025,
                        "fiscal_month": 12,
                        "value": 50,
                        "tag": "ResearchAndDevelopmentExpense",
                    }
                ],
            )

            df = pd.read_csv(output / "us_normalized_AAPL_2025.12.csv")
            debug = pd.read_csv(output / "us_normalized_AAPL_2025.12.debug.csv")

            self.assertEqual(float(df.loc[df["canonical_account_id"].eq("RND"), "normalized_amount"].iat[0]), 40)
            self.assertEqual(debug.loc[debug["canonical_account_id"].eq("RND"), "source"].iat[0], "notes")

    def test_edgartools_fills_when_sec_sources_missing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            companyfacts = root / "companyfacts"
            output = root / "out"
            companyfacts.mkdir()
            canonical = root / "canonical.csv"
            ticker_map = root / "tickers.csv"
            metadata = root / "metadata.csv"
            write_canonical(canonical)
            write_ticker_map(ticker_map)
            write_companyfacts(companyfacts / "CIK0000320193.json", {})

            normalize_us_sec_filings(
                symbols=["AAPL"],
                start_year=2025,
                end_year=2025,
                companyfacts_dir=companyfacts,
                notes_root=root / "missing-notes",
                output_dir=output,
                ticker_map_path=ticker_map,
                canonical_csv_path=canonical,
                report_metadata_path=metadata,
                edgartools_provider=lambda *_: [
                    {
                        "symbol": "AAPL",
                        "cik": "320193",
                        "canonical_id": "RND",
                        "statement_type": "IS",
                        "fiscal_year": 2025,
                        "fiscal_month": 12,
                        "value": 50,
                        "tag": "ResearchAndDevelopmentExpense",
                    }
                ],
            )

            df = pd.read_csv(output / "us_normalized_AAPL_2025.12.csv")
            debug = pd.read_csv(output / "us_normalized_AAPL_2025.12.debug.csv")

            self.assertEqual(float(df.loc[df["canonical_account_id"].eq("RND"), "normalized_amount"].iat[0]), 50)
            self.assertEqual(debug.loc[debug["canonical_account_id"].eq("RND"), "source"].iat[0], "edgartools")


if __name__ == "__main__":
    unittest.main()
