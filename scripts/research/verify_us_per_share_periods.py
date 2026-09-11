"""Verify retained ATVI durations through normalization and public daily factors."""
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import warnings

from bs4 import BeautifulSoup
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.transformers.sec_filings import normalize_us_sec_filings
from engine.transformers.factors import create_stock_factor_dataframe


def pins(paths):
    return {str(path.resolve()): sha256(path.read_bytes()).hexdigest() for path in paths}


def main():
    scope = "us_per_share_periods_20260911"
    output = DATA_LAKE.silver("survivorship", "financial_research", scope, "actual_ATVI")
    if (output / "summary.json").exists():
        raise ValueError("Closed verification; use a new scope for changed code")
    output.mkdir(parents=True, exist_ok=True)
    raw_root = DATA_LAKE.bronze("sec", "financial_history", scope, "ATVI")
    companyfacts = DATA_LAKE.bronze("sec", "companyfacts", "CIK0000718877.json")
    inputs = [companyfacts, *raw_root.glob("*"),
        DATA_LAKE.silver("sec", "normalized", "us_normalized_ATVI.csv"),
        DATA_LAKE.silver("sec", "us_report_metadata.csv"),
        DATA_LAKE.silver("corporate_actions", "prices", "us", "us_ATVI.parquet")]
    before = pins(inputs)
    (output / "inputs_before.json").write_text(json.dumps(before, indent=2), "utf-8")
    for name in ["2010_Q2_index.html", "2010_Q2_primary.htm"]:
        source = raw_root / name
        metadata = json.loads(source.with_suffix(source.suffix + ".metadata.json").read_bytes())
        assert metadata["http_status"] == 200
        assert metadata["source_sha256"] == sha256(source.read_bytes()).hexdigest()
    index = BeautifulSoup((raw_root / "2010_Q2_index.html").read_bytes(), "html.parser")
    index_text = re.sub(r"\s+", " ", index.get_text(" ", strip=True))
    assert "2010-08-06" in index_text and "2010-06-30" in index_text
    soup = BeautifulSoup((raw_root / "2010_Q2_primary.htm").read_bytes(), "html.parser")
    table_texts = [re.sub(r"\s+", " ", table.get_text(" ", strip=True)) for table in soup.find_all("table")]
    table_texts = list(dict.fromkeys(text for text in table_texts
        if "weighted-average" in text.lower() and len(text) < 20000))
    assert table_texts
    combined = "\n\n".join(table_texts)
    for excerpt in ["Basic 1,232 1,289 1,239 1,299", "Diluted 1,248 1,332 1,254 1,345",
                    "Basic $ 0.18 $ 0.15 $ 0.48 $ 0.29", "Diluted $ 0.17 $ 0.15 $ 0.47 $ 0.28"]:
        assert excerpt in combined, excerpt
    (output / "primary_statement_tables.txt").write_text(combined, "utf-8")

    expected = {
        "BASIC_EPS": ("EarningsPerShareBasic", "USD/shares", .18, .48),
        "DILUTED_EPS": ("EarningsPerShareDiluted", "USD/shares", .17, .47),
        "BASIC_SHARES": ("WeightedAverageNumberOfSharesOutstandingBasic", "shares", 1232000000, 1239000000),
        "DILUTED_SHARES": ("WeightedAverageNumberOfDilutedSharesOutstanding", "shares", 1248000000, 1254000000),
    }
    facts = json.loads(companyfacts.read_bytes())["facts"]["us-gaap"]
    observations = []
    accession = "0001104659-10-042753"
    for account, (tag, unit, qtd, ytd) in expected.items():
        rows = [row for row in facts[tag]["units"][unit] if row["accn"] == accession and row["end"] == "2010-06-30"]
        for start, amount in [("2010-04-01", qtd), ("2010-01-01", ytd)]:
            matches = [row for row in rows if row.get("start") == start]
            assert matches and all(row["val"] == amount and row["filed"] == "2010-08-06" for row in matches)
            observations.extend(dict(canonical_account_id=account, tag=tag, unit=unit, **row) for row in matches)
    pd.DataFrame(observations).to_parquet(output / "companyfacts_primary_reconciliation.parquet", index=False)
    normalize_us_sec_filings(symbols=["ATVI"], start_year=2010, end_year=2010,
        output_dir=output / "normalized", report_metadata_path=output / "metadata.csv",
        use_notes=False, use_edgartools=False, workers=1, save_debug=False)
    manifest_path = output / "normalized" / "accessions" / "ATVI" / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    normalized_path = manifest_path.parent / manifest["normalized_path"]
    normalized = pd.read_csv(normalized_path, dtype=str, keep_default_na=False)
    selected = normalized[normalized.accn.eq(accession)]
    for account, (_, _, qtd, ytd) in expected.items():
        record = selected[selected.canonical_account_id.eq(account)].iloc[0]
        assert float(record.normalized_amount) == ytd
        durations = json.loads(record.reported_durations)
        assert any(row["period_start"] == "2010-04-01" and row["normalized_amount"] == qtd for row in durations)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", pd.errors.PerformanceWarning)
        daily = create_stock_factor_dataframe("ATVI", market="us", financial_basis="quarterly",
            start_date="2010-05-07", end_date="2010-08-10", financial_dir=output / "normalized",
            report_metadata_path=output / "metadata.csv", use_edgartools=False,
            require_report_metadata=True, financial_availability_delay_days=1,
            requested_factor_ids=["eps", "intangible_adjusted_eps"], wacc_online_backfill=False)
    daily = daily[["trade_date", "financial_period", "report_date", "eps", "DILUTED_EPS",
                   "BASIC_SHARES", "DILUTED_SHARES", "intangible_adjusted_eps", "intangible_adjusted_net_income"]]
    daily.to_parquet(output / "public_daily.parquet", index=False)
    first = daily[pd.to_datetime(daily.trade_date).eq("2010-05-10")].iloc[0]
    publication = daily[pd.to_datetime(daily.trade_date).eq("2010-08-06")].iloc[0]
    available = daily[pd.to_datetime(daily.trade_date).eq("2010-08-09")].iloc[0]
    assert first.eps == .3 and first.DILUTED_SHARES == 1264000000
    assert publication.eps == .3 and publication.DILUTED_SHARES == 1264000000
    assert available.eps == .18 and available.DILUTED_EPS == .17
    assert available.BASIC_SHARES == 1232000000 and available.DILUTED_SHARES == 1248000000
    assert pd.notna(available.intangible_adjusted_eps)
    assert abs(available.intangible_adjusted_eps - available.intangible_adjusted_net_income / 1248000000) < 1e-12
    assert before == pins(inputs)
    result = dict(status="actual_ATVI_quarter_durations_verified_staging_only", input_pins=before,
        inputs_unchanged=True, primary_publication="2010-08-06", usable_from="2010-08-09",
        Q2_basic_eps=.18, Q2_diluted_eps=.17, Q2_basic_weighted_shares=1232000000,
        Q2_diluted_weighted_shares=1248000000, public_daily_rows=len(daily),
        output_pins=pins([manifest_path, normalized_path, output / "public_daily.parquet",
            output / "primary_statement_tables.txt", output / "companyfacts_primary_reconciliation.parquet"]),
        production_changed=False, native_published=False, snapshots_published=False, coverage_complete=False,
        runner=pins([Path(__file__)]))
    (output / "summary.json").write_text(json.dumps(result, indent=2), "utf-8")
    print(json.dumps({key: result[key] for key in ["status", "public_daily_rows", "inputs_unchanged"]}), flush=True)


if __name__ == "__main__":
    main()
