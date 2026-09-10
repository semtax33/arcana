"""Total equity must include ordinary noncontrolling interests when reported."""
import json
from pathlib import Path

import pandas as pd
import pytest
from types import SimpleNamespace

from engine.transformers.sec_filings import normalize_us_sec_filings
from engine.transformers._internal.sec_filings import _match_filing_fact_rule, load_us_mapping_rules


@pytest.mark.parametrize("case", ["ordinary_nci", "parent_only", "different_accession", "negative_nci", "temporary_equity"])
def test_normalized_total_equity_preserves_consolidated_balance(tmp_path, case):
    source = tmp_path / "companyfacts"
    source.mkdir()
    ticker_map = tmp_path / "tickers.csv"
    pd.DataFrame([{"cik": 899866, "ticker": "ALXN", "title": "Alexion"}]).to_csv(ticker_map, index=False)
    # Same-accession Q1 2021 values; parent equity excludes USD 14.2m of NCI.
    amounts = {"Assets": 18650200000, "Liabilities": 6219200000,
               "StockholdersEquity": 12416800000}
    total_tag = "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
    if case != "parent_only":
        amounts[total_tag] = 12431000000
    if case == "negative_nci":
        amounts["StockholdersEquity"] = 12445200000
    if case == "temporary_equity":
        amounts["Assets"] += 20000000
    facts = {tag: {"label": tag, "units": {"USD": [{
        "end": "2021-03-31", "val": value, "accn": "0000899866-21-000039",
        "fy": 2021, "fp": "Q1", "form": "10-Q", "filed": "2021-04-30",
    }]}} for tag, value in amounts.items()}
    if case == "different_accession":
        facts["Assets"]["units"]["USD"][0]["accn"] = "0000899866-21-000040"
        # A conflicting later filing must not justify selecting a component.
        facts["Assets"]["units"]["USD"][0]["val"] = 18636000000
    (source / "CIK0000899866.json").write_text(json.dumps({
        "cik": 899866, "entityName": "Alexion", "facts": {"us-gaap": facts},
    }), encoding="utf-8")
    output = tmp_path / "normalized"
    normalize_us_sec_filings(
        symbols=["ALXN"], start_year=2021, end_year=2021,
        companyfacts_dir=source, output_dir=output, ticker_map_path=ticker_map,
        report_metadata_path=tmp_path / "metadata.csv",
        mapping_rule_path=Path("data-lake/meta/rules/semantic_us_v3.arcana"),
        use_filings=False, use_notes=False, use_edgartools=False, log_progress=False,
    )
    rows = pd.read_csv(output / "us_normalized_ALXN.debug.csv").set_index("canonical_account_id")
    equity = rows.loc["TOTAL_EQUITY"]
    expected = 12416800000 if case == "parent_only" else 12431000000
    assert equity.normalized_amount == expected
    assert equity.filed == "2021-04-30"
    assert equity.accn == "0000899866-21-000039"
    if case in {"ordinary_nci", "negative_nci"}:
        assert rows.loc["TOTAL_ASSETS", "normalized_amount"] == (
            rows.loc["TOTAL_LIABILITIES", "normalized_amount"] + equity.normalized_amount
        )


@pytest.mark.parametrize("has_balance_sheet_role", [False, True])
def test_total_equity_tag_in_aoci_note_needs_appropriate_presentation_scope(has_balance_sheet_role):
    # Actual CELG FY2016 presentation role for the mistagged USD 419.1m.
    concept = "us-gaap_StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
    tree = SimpleNamespace(all_nodes={concept: SimpleNamespace(parent=None)})
    trees = {"http://www.celgene.com/role/AccumulatedOtherComprehensiveIncomeSummaryOfComponentsDetails": tree}
    if has_balance_sheet_role:
        trees["http://www.celgene.com/role/ConsolidatedBalanceSheets"] = tree
    rules = load_us_mapping_rules(Path("data-lake/meta/rules/semantic_us_v3.arcana"))["companyfacts_rules"]
    rule = next(row for row in rules if row["canonical_id"] == "TOTAL_EQUITY")
    match = _match_filing_fact_rule({"concept": concept}, rule, xbrl=SimpleNamespace(presentation_trees=trees))
    assert (match is not None) == has_balance_sheet_role
