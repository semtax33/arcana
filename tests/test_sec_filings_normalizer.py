from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd
import yaml

from engine.core.paths import parse_statement_snapshot_filename, statement_snapshot_name
from engine.transformers.sec_filings import normalize_us_sec_filings
from engine.transformers._internal.sec_filings import (
    US_MAPPING_RULE_PATH,
    SecFactCandidate,
    _companyfacts_accession_period_ends,
    _fact_units_for_rule,
    _notes_rule_matches_tag,
    add_formula_derived_candidates,
    dedupe_candidates,
    normalize_sec_date,
)
from engine.workflows._internal.normalize_workflow import MAPPING_RULE_PATH


CANONICAL_ROWS = [
    {
        "canonical_id": "REVENUE",
        "canonical_nm": "Revenue",
        "fs_type": "IS",
        "is_derived": "FALSE",
        "formula": "",
        "description": "",
        "鍮꾧퀬": "",
    },
    {
        "canonical_id": "CAPEX_PPE",
        "canonical_nm": "Capex",
        "fs_type": "CF",
        "is_derived": "FALSE",
        "formula": "",
        "description": "",
        "鍮꾧퀬": "",
    },
    {
        "canonical_id": "RND",
        "canonical_nm": "R&D",
        "fs_type": "IS",
        "is_derived": "FALSE",
        "formula": "",
        "description": "",
        "鍮꾧퀬": "",
    },
    {
        "canonical_id": "BUYBACK",
        "canonical_nm": "Buyback",
        "fs_type": "CF",
        "is_derived": "FALSE",
        "formula": "",
        "description": "",
        "鍮꾧퀬": "",
    },
    {
        "canonical_id": "INTEREST_EXPENSE",
        "canonical_nm": "Interest expense",
        "fs_type": "IS",
        "is_derived": "FALSE",
        "formula": "",
        "description": "",
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
    def test_accession_period_anchor_ignores_short_post_period_event(self):
        common = {
            "accn": "0000000001-24-000001",
            "fy": 2024,
            "fp": "Q3",
            "form": "10-Q",
            "filed": "2024-11-04",
        }
        period_ends = _companyfacts_accession_period_ends(
            [
                (
                    "REVENUE",
                    [
                        {
                            **common,
                            "start": "2024-01-01",
                            "end": "2024-09-30",
                        },
                        {
                            **common,
                            "start": "2023-01-01",
                            "end": "2023-09-30",
                        },
                    ],
                ),
                (
                    "PPE_DISPOSAL_PROCEEDS",
                    [
                        {
                            **common,
                            "start": "2024-10-01",
                            "end": "2024-10-31",
                        }
                    ],
                ),
            ]
        )

        self.assertEqual(next(iter(period_ends.values())), "2024-09-30")

    def test_normalize_sec_date_accepts_companyfacts_and_notes_formats(self):
        self.assertEqual(normalize_sec_date("2018-06-29"), "2018-06-29")
        self.assertEqual(normalize_sec_date("20180629"), "2018-06-29")
        self.assertEqual(normalize_sec_date(""), "")

    def test_companyfacts_uses_current_period_instead_of_later_filed_comparative(self):
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
            accession = "0000320193-26-000001"
            write_companyfacts(
                companyfacts / "CIK0000320193.json",
                {
                    "Revenues": {
                        "label": "Revenue",
                        "description": "Revenue",
                        "units": {
                            "USD": [
                                {
                                    "start": "2023-01-01",
                                    "end": "2023-12-31",
                                    "val": 900,
                                    "accn": accession,
                                    "fy": 2025,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2026-04-13",
                                    "frame": "CY2023",
                                },
                                {
                                    "start": "2024-01-01",
                                    "end": "2024-12-31",
                                    "val": 700,
                                    "accn": accession,
                                    "fy": 2025,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2026-04-13",
                                    "frame": "CY2024",
                                },
                                {
                                    "start": "2025-01-01",
                                    "end": "2025-12-31",
                                    "val": 400,
                                    "accn": accession,
                                    "fy": 2025,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2026-04-13",
                                    "frame": "CY2025",
                                },
                            ]
                        },
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

            df = pd.read_csv(output / "us_normalized_AAPL.csv")
            debug = pd.read_csv(output / "us_normalized_AAPL.debug.csv")
            revenue = df.loc[df["canonical_account_id"].eq("REVENUE")].iloc[0]
            revenue_debug = debug.loc[debug["canonical_account_id"].eq("REVENUE")].iloc[0]

            self.assertEqual(float(revenue["normalized_amount"]), 400)
            self.assertEqual(revenue_debug["period_end"], "2025-12-31")

    def test_companyfacts_accession_period_blocks_stale_primary_tag(self):
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
            accession = "0000320193-26-000001"
            common = {
                "accn": accession,
                "fy": 2025,
                "fp": "FY",
                "form": "10-K",
                "filed": "2026-04-13",
            }
            write_companyfacts(
                companyfacts / "CIK0000320193.json",
                {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "label": "Revenue",
                        "units": {
                            "USD": [
                                {
                                    **common,
                                    "start": "2024-01-01",
                                    "end": "2024-12-31",
                                    "val": 700,
                                    "frame": "CY2024",
                                }
                            ]
                        },
                    },
                    "Revenues": {
                        "label": "Revenue",
                        "units": {
                            "USD": [
                                {
                                    **common,
                                    "start": "2025-01-01",
                                    "end": "2025-12-31",
                                    "val": 400,
                                    "frame": "CY2025",
                                }
                            ]
                        },
                    },
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

            df = pd.read_csv(output / "us_normalized_AAPL.csv")
            debug = pd.read_csv(output / "us_normalized_AAPL.debug.csv")
            revenue = df.loc[df["canonical_account_id"].eq("REVENUE")].iloc[0]
            revenue_debug = debug.loc[debug["canonical_account_id"].eq("REVENUE")].iloc[0]
            self.assertEqual(float(revenue["normalized_amount"]), 400)
            self.assertEqual(revenue_debug["source"], "companyfacts_alternate")
            self.assertEqual(revenue_debug["period_end"], "2025-12-31")

    def test_scoped_rebuild_replaces_only_requested_symbol_years(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            companyfacts = root / "companyfacts"
            output = root / "out"
            companyfacts.mkdir()
            output.mkdir()
            canonical = root / "canonical.csv"
            ticker_map = root / "tickers.csv"
            metadata = root / "metadata.csv"
            write_canonical(canonical)
            write_ticker_map(ticker_map)
            write_companyfacts(
                companyfacts / "CIK0000320193.json",
                {"Revenues": fact("Revenue", 400, "revenue")},
            )
            pd.DataFrame(
                [
                    {
                        "canonical_account_id": "REVENUE",
                        "statement_type": "IS",
                        "normalized_amount": 300,
                        "fiscal_year": 2024,
                        "fiscal_month": 12,
                    },
                    {
                        "canonical_account_id": "RND",
                        "statement_type": "IS",
                        "normalized_amount": 999,
                        "fiscal_year": 2025,
                        "fiscal_month": 12,
                    },
                ]
            ).to_csv(output / "us_normalized_AAPL.csv", index=False)
            pd.DataFrame(
                [
                    {
                        "stock_code": "AAPL",
                        "fiscal_year": 2025,
                        "fiscal_month": 12,
                        "report_date": "2026-01-01",
                        "rcept_no": "stale",
                        "source_type": "statement",
                    },
                    {
                        "stock_code": "MSFT",
                        "fiscal_year": 2025,
                        "fiscal_month": 12,
                        "report_date": "2026-01-02",
                        "rcept_no": "keep",
                        "source_type": "statement",
                    },
                ]
            ).to_csv(metadata, index=False)

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

            result = pd.read_csv(output / "us_normalized_AAPL.csv")
            result_metadata = pd.read_csv(metadata, dtype={"rcept_no": str})
            self.assertEqual(
                float(
                    result.loc[
                        result["canonical_account_id"].eq("REVENUE")
                        & result["fiscal_year"].eq(2024),
                        "normalized_amount",
                    ].iat[0]
                ),
                300,
            )
            self.assertFalse(
                (
                    result["canonical_account_id"].eq("RND")
                    & result["fiscal_year"].eq(2025)
                ).any()
            )
            self.assertEqual(
                float(
                    result.loc[
                        result["canonical_account_id"].eq("REVENUE")
                        & result["fiscal_year"].eq(2025),
                        "normalized_amount",
                    ].iat[0]
                ),
                400,
            )
            self.assertIn("MSFT", set(result_metadata["stock_code"]))
            self.assertNotIn("stale", set(result_metadata["rcept_no"]))

    def test_share_rules_only_accept_share_units(self):
        fact = {
            "units": {
                "USD": [{"val": 1_000}],
                "shares": [{"val": 100}],
            }
        }

        units = _fact_units_for_rule(fact, "COMMON_SHARES_OUTSTANDING")

        self.assertEqual(units, [("shares", [{"val": 100}])])

    def test_formula_derived_candidates_fill_only_accounting_identities(self):
        base = {
            "symbol": "AAPL",
            "cik": "320193",
            "entity_name": "Apple Inc.",
            "statement_type": "IS",
            "fiscal_year": 2025,
            "fiscal_month": 12,
            "period_end": "2025-12-31",
            "filed": "2026-01-30",
            "accn": "0000320193-26-000001",
            "form": "10-K",
            "fp": "FY",
            "source": "companyfacts_primary",
            "amount_policy": "as_reported",
            "cash_direction": "",
        }
        candidates = [
            SecFactCandidate(
                **base,
                canonical_id="REVENUE",
                canonical_name="Revenue",
                value=100,
                raw_value=100,
                rule_id="test:REVENUE",
                reason="test",
                original_account_name="Revenue",
            ),
            SecFactCandidate(
                **base,
                canonical_id="COGS",
                canonical_name="COGS",
                value=40,
                raw_value=40,
                rule_id="test:COGS",
                reason="test",
                original_account_name="COGS",
            ),
            SecFactCandidate(
                **base,
                canonical_id="OPERATING_EXPENSES_TOTAL",
                canonical_name="Operating expenses",
                value=20,
                raw_value=20,
                rule_id="test:OPERATING_EXPENSES_TOTAL",
                reason="test",
                original_account_name="Operating expenses",
            ),
        ]

        result = dedupe_candidates(
            add_formula_derived_candidates(
                candidates,
                canonical_names={
                    "GROSS_PROFIT": "Gross profit",
                    "OPERATING_INCOME": "Operating income",
                },
            )
        )
        by_id = {candidate.canonical_id: candidate for candidate in result}

        self.assertEqual(by_id["GROSS_PROFIT"].value, 60)
        self.assertEqual(by_id["GROSS_PROFIT"].source, "derived_formula")
        self.assertEqual(by_id["OPERATING_INCOME"].value, 40)
        self.assertEqual(by_id["OPERATING_INCOME"].source, "derived_formula")
        self.assertNotIn("RND", by_id)

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

            df = pd.read_csv(output / "us_normalized_AAPL.csv")
            debug = pd.read_csv(output / "us_normalized_AAPL.debug.csv")

            self.assertEqual(df["fiscal_year"].iat[0], 2025)
            self.assertEqual(df["fiscal_month"].iat[0], 12)
            self.assertEqual(df["fiscal_quarter"].iat[0], 4)
            self.assertFalse((output / "us_normalized_AAPL_2025.12.csv").exists())
            self.assertFalse((output / "us_normalized_AAPL_2025.12.debug.csv").exists())
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

            df = pd.read_csv(output / "us_normalized_AAPL.csv")
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

            df = pd.read_csv(output / "us_normalized_AAPL.csv")
            debug = pd.read_csv(output / "us_normalized_AAPL.debug.csv")

            self.assertEqual(float(df.loc[df["canonical_account_id"].eq("RND"), "normalized_amount"].iat[0]), 25)
            self.assertEqual(debug.loc[debug["canonical_account_id"].eq("RND"), "source"].iat[0], "companyfacts_label")

    def test_companyfacts_label_rule_rejects_rnd_policy_extension(self):
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
                        "CustomResearchAndDevelopmentPolicy": fact(
                            "Research and Development Policy",
                            25,
                            "custompolicy",
                        )
                    }
                },
            )

            written = normalize_us_sec_filings(
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

            self.assertEqual(written, [])

    def test_notes_tag_exclude_patterns_override_exact_tags(self):
        rule = {
            "tags": ["DeferredTaxAssetsCapitalizedResearchAndDevelopmentCosts"],
            "tag_patterns": ["(?i)researchanddevelopment"],
            "tag_exclude_patterns": ["(?i)deferredtax", "(?i)capitalized"],
        }

        self.assertFalse(
            _notes_rule_matches_tag(
                rule,
                "DeferredTaxAssetsCapitalizedResearchAndDevelopmentCosts",
            )
        )

    def test_us_notes_rules_reject_noisy_observed_tags(self):
        rules = yaml.safe_load(US_MAPPING_RULE_PATH.read_text(encoding="utf-8"))
        notes_by_id = {rule["id"]: rule for rule in rules["notes_rules"]}

        self.assertFalse(
            _notes_rule_matches_tag(
                notes_by_id["notes_rnd"],
                "DeferredTaxAssetsCapitalizedResearchAndDevelopmentCosts",
            )
        )
        self.assertFalse(
            _notes_rule_matches_tag(
                notes_by_id["notes_rnd"],
                "CapitalizedSoftwareDevelopmentCostsForSoftwareSoldToCustomers",
            )
        )
        self.assertFalse(
            _notes_rule_matches_tag(
                notes_by_id["notes_buyback"],
                "PaymentsForRepurchaseOfCommonStockAndPaymentsRelatedToTaxWithholdingForShareBasedCompensation",
            )
        )
        self.assertFalse(
            _notes_rule_matches_tag(
                notes_by_id["notes_buyback"],
                "PaymentsForRepurchaseOfTreasuryStockRelatedToEquityAwards",
            )
        )
        self.assertFalse(
            _notes_rule_matches_tag(
                notes_by_id["notes_dividends_paid"],
                "PaymentsOfDividendsToNoncontrollingInterests",
            )
        )

    def test_us_notes_rules_accept_safe_observed_tags(self):
        rules = yaml.safe_load(US_MAPPING_RULE_PATH.read_text(encoding="utf-8"))
        notes_by_id = {rule["id"]: rule for rule in rules["notes_rules"]}

        safe_tags = [
            ("notes_capex_ppe", "PaymentsToAcquireOtherPropertyPlantAndEquipment"),
            ("notes_capex_ppe", "PaymentsToAcquirePropertyPlantAndEquipmentAndInternalUseSoftware"),
            ("notes_buyback", "PaymentsForRepurchaseOfTreasuryStock"),
            ("notes_buyback", "PaymentsForRepurchaseOfRedeemableConvertiblePreferredStock"),
            ("notes_dividends_paid", "PaymentsOfOrdinaryDividendsAndSpecialDividends"),
            ("notes_rnd", "ProductDevelopmentExpense"),
            ("notes_rnd", "ResearchAndDevelopmentExpenseNetOfGrantReimbursement"),
        ]
        for rule_id, tag in safe_tags:
            with self.subTest(rule_id=rule_id, tag=tag):
                self.assertTrue(_notes_rule_matches_tag(notes_by_id[rule_id], tag))

    def test_companyfacts_tag_rule_requires_declared_namespace(self):
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
                    "ifrs-full": {
                        "ResearchAndDevelopmentExpense": fact(
                            "Unrelated operating item",
                            25,
                            "ifrsrnd",
                        )
                    }
                },
            )

            written = normalize_us_sec_filings(
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

            self.assertEqual(written, [])

    def test_companyfacts_label_rule_rejects_srt_taxonomy(self):
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
                    "srt": {
                        "FutureNetCashFlowsRelatingToProvedOilAndGasReservesIncomeTaxExpense": fact(
                            "Income Tax Expense",
                            25,
                            "srttax",
                        )
                    }
                },
            )

            written = normalize_us_sec_filings(
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

            self.assertEqual(written, [])

    def test_cash_flow_direction_survives_companyfacts_mapping(self):
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
                {"PaymentsToAcquireProductiveAssets": fact("Capital expenditures", -12, "capex")},
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

            df = pd.read_csv(output / "us_normalized_AAPL.csv")
            debug = pd.read_csv(output / "us_normalized_AAPL.debug.csv")

            self.assertEqual(float(df.loc[df["canonical_account_id"].eq("CAPEX_PPE"), "normalized_amount"].iat[0]), 12)
            self.assertEqual(float(df.loc[df["canonical_account_id"].eq("CAPEX_PPE"), "cash_effect_amount"].iat[0]), -12)
            self.assertEqual(debug.loc[debug["canonical_account_id"].eq("CAPEX_PPE"), "cash_direction"].iat[0], "outflow")

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

            df = pd.read_csv(output / "us_normalized_AAPL.csv")
            debug = pd.read_csv(output / "us_normalized_AAPL.debug.csv")

            self.assertEqual(float(df.loc[df["canonical_account_id"].eq("RND"), "normalized_amount"].iat[0]), 40)
            self.assertEqual(debug.loc[debug["canonical_account_id"].eq("RND"), "source"].iat[0], "notes")

    def test_notes_buyback_keeps_abs_outflow_policy(self):
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
            write_companyfacts(companyfacts / "CIK0000320193.json", {})
            (notes / "sub.tsv").write_text(
                "adsh\tcik\tname\tform\tperiod\tfy\tfp\tfiled\n"
                "0000320193-26-000001\t320193\tApple Inc.\t10-K\t20251231\t2025\tFY\t20260130\n",
                encoding="utf-8",
            )
            (notes / "num.tsv").write_text(
                "adsh\ttag\tversion\tddate\tuom\tdimh\tvalue\n"
                "0000320193-26-000001\tPaymentsForRepurchaseOfPreferredStockAndPreferenceStock\tus-gaap/2025\t20251231\tUSD\t0x00000000\t-70\n",
                encoding="utf-8",
            )
            (notes / "tag.tsv").write_text(
                "tag\tversion\tcustom\tabstract\tdatatype\tiord\tcrdr\ttlabel\tdoc\n"
                "PaymentsForRepurchaseOfPreferredStockAndPreferenceStock\tus-gaap/2025\t0\t0\tmonetaryItemType\tO\tC\tPayments for Repurchase of Preferred Stock\tBuyback\n",
                encoding="utf-8",
            )
            (notes / "pre.tsv").write_text(
                "adsh\treport\tline\tstmt\tinpth\ttag\tversion\tprole\tplabel\tnegating\n"
                "0000320193-26-000001\t2\t1\tCF\t0\tPaymentsForRepurchaseOfPreferredStockAndPreferenceStock\tus-gaap/2025\tterseLabel\tPayments for Repurchase of Preferred Stock\t0\n",
                encoding="utf-8",
            )
            (notes / "ren.tsv").write_text(
                "adsh\treport\trfile\tmenucat\tshortname\tlongname\troleuri\tparentroleuri\tparentreport\tultparentrpt\n"
                "0000320193-26-000001\t2\tH\tS\tStatement of Cash Flows\tStatement of Cash Flows Financing Activities\trole\t\t\t\n",
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
                use_edgartools=False,
            )

            df = pd.read_csv(output / "us_normalized_AAPL.csv")
            debug = pd.read_csv(output / "us_normalized_AAPL.debug.csv")

            self.assertEqual(float(df.loc[df["canonical_account_id"].eq("BUYBACK"), "normalized_amount"].iat[0]), 70)
            self.assertEqual(float(df.loc[df["canonical_account_id"].eq("BUYBACK"), "cash_effect_amount"].iat[0]), -70)
            self.assertEqual(debug.loc[debug["canonical_account_id"].eq("BUYBACK"), "source"].iat[0], "notes")
            self.assertEqual(debug.loc[debug["canonical_account_id"].eq("BUYBACK"), "cash_direction"].iat[0], "outflow")

    def test_notes_tag_pattern_requires_statement_context(self):
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
            write_companyfacts(companyfacts / "CIK0000320193.json", {})
            (notes / "sub.tsv").write_text(
                "adsh\tcik\tname\tform\tperiod\tfy\tfp\tfiled\n"
                "0000320193-26-000001\t320193\tApple Inc.\t10-K\t20251231\t2025\tFY\t20260130\n",
                encoding="utf-8",
            )
            (notes / "num.tsv").write_text(
                "adsh\ttag\tversion\tddate\tuom\tdimh\tvalue\n"
                "0000320193-26-000001\tCustomResearchAndDevelopmentExpense\taapl/2025\t20251231\tUSD\t0x00000000\t40\n",
                encoding="utf-8",
            )
            (notes / "tag.tsv").write_text(
                "tag\tversion\tcustom\tabstract\tdatatype\tiord\tcrdr\ttlabel\tdoc\n"
                "CustomResearchAndDevelopmentExpense\taapl/2025\t1\t0\tmonetaryItemType\tI\tD\tResearch and Development\tR&D\n",
                encoding="utf-8",
            )
            (notes / "pre.tsv").write_text(
                "adsh\treport\tline\tstmt\tinpth\ttag\tversion\tprole\tplabel\tnegating\n"
                "0000320193-26-000001\t2\t1\tBS\t0\tCustomResearchAndDevelopmentExpense\taapl/2025\tterseLabel\tResearch and Development\t0\n",
                encoding="utf-8",
            )
            (notes / "ren.tsv").write_text(
                "adsh\treport\trfile\tmenucat\tshortname\tlongname\troleuri\tparentroleuri\tparentreport\tultparentrpt\n"
                "0000320193-26-000001\t2\tH\tS\tBalance Sheet\tBalance Sheet\trole\t\t\t\n",
                encoding="utf-8",
            )

            written = normalize_us_sec_filings(
                symbols=["AAPL"],
                start_year=2025,
                end_year=2025,
                companyfacts_dir=companyfacts,
                notes_root=root / "notes",
                output_dir=output,
                ticker_map_path=ticker_map,
                canonical_csv_path=canonical,
                report_metadata_path=metadata,
                use_edgartools=False,
            )

            self.assertEqual(written, [])

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

            df = pd.read_csv(output / "us_normalized_AAPL.csv")
            debug = pd.read_csv(output / "us_normalized_AAPL.debug.csv")

            self.assertEqual(float(df.loc[df["canonical_account_id"].eq("RND"), "normalized_amount"].iat[0]), 50)
            self.assertEqual(debug.loc[debug["canonical_account_id"].eq("RND"), "source"].iat[0], "edgartools")

    def test_default_edgartools_skips_empty_local_companyfacts(self):
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

            with patch(
                "engine.transformers._internal.sec_filings.default_edgartools_provider",
                side_effect=AssertionError("default edgartools provider should not be called"),
            ):
                written = normalize_us_sec_filings(
                    symbols=["AAPL"],
                    start_year=2025,
                    end_year=2025,
                    companyfacts_dir=companyfacts,
                    notes_root=root / "missing-notes",
                    output_dir=output,
                    ticker_map_path=ticker_map,
                    canonical_csv_path=canonical,
                    report_metadata_path=metadata,
                )

            self.assertEqual(written, [])


if __name__ == "__main__":
    unittest.main()
