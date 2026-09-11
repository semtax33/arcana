"""Stage retained four-issuer SEC versions and verify an original ALXN balance."""
from hashlib import sha256
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.transformers.sec_filings import normalize_us_sec_filings
from engine.transformers.factors import create_stock_factor_dataframe


def pins(paths):
    return {str(path): sha256(path.read_bytes()).hexdigest() for path in paths}


def main():
    output = DATA_LAKE.silver("survivorship", "financial_research", "us_sec_accession_refresh_20260911", "actual_normalization_v3")
    if (output / "summary.json").exists():
        raise ValueError("This verification is closed; use a new output scope for new code")
    output.mkdir(parents=True, exist_ok=True)
    symbols = ["ALXN", "ATVI", "CELG", "TWTR"]
    sources = [DATA_LAKE.bronze("sec", "companyfacts", f"CIK{cik}.json")
               for cik in ["0000899866", "0000718877", "0000816284", "0001418091"]]
    for form in ["10-K", "10-Q"]:
        sources += [path for path in DATA_LAKE.bronze("sec", "fillings", form, "CELG").rglob("*") if path.is_file()]
    production = [DATA_LAKE.silver("sec", "normalized", f"us_normalized_{symbol}.csv") for symbol in symbols]
    production += [DATA_LAKE.silver("sec", "us_report_metadata.csv"),
                   DATA_LAKE.silver("corporate_actions", "prices", "us", "us_ALXN.parquet")]
    before = pins(sources + production)
    (output / "inputs_before.json").write_text(json.dumps(before, indent=2), "utf-8")
    normalize_us_sec_filings(symbols=symbols, start_year=1999, end_year=2026,
        output_dir=output / "normalized", report_metadata_path=output / "metadata.csv",
        use_notes=False, use_edgartools=False, workers=1, save_debug=True)
    records = {}
    for symbol in symbols:
        manifest_path = output / "normalized" / "accessions" / symbol / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        path = manifest_path.parent / manifest["normalized_path"]
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        records[symbol] = dict(rows=len(frame), accessions=frame.accn.nunique(),
            periods=len(frame[["fiscal_year", "fiscal_month"]].drop_duplicates()),
            first_publication=frame.filed.min(), output_pins=pins([manifest_path, path]))
        direct = frame[frame.source.str.startswith("companyfacts_")]
        assert not direct.source_path.eq("").any()
        for source_path, group in direct.groupby("source_path"):
            assert group.source_sha256.eq(sha256(Path(source_path).read_bytes()).hexdigest()).all()
        old = pd.read_csv(DATA_LAKE.silver("sec", "normalized", f"us_normalized_{symbol}.csv"), dtype=str).fillna("")
        new = pd.read_csv(output / "normalized" / f"us_normalized_{symbol}.csv", dtype=str).fillna("")
        sort = ["fiscal_year", "fiscal_month", "canonical_account_id"]
        removals = {("2017", "12", "INTEREST_EXPENSE"), ("2017", "12", "OTHER_NON_OPERATING_INCOME"),
                    ("2018", "12", "LEASE_LIABILITY"), ("2019", "3", "CONTRACT_ASSETS")} if symbol == "CELG" else set()
        removed = old.loc[old[sort].apply(tuple, axis=1).isin(removals)]
        if removals:
            assert set(removed[sort].apply(tuple, axis=1)) == removals
            assert not new[sort].apply(tuple, axis=1).isin(removals).any()
            removed.to_parquet(output / "CELG_other_date_rows_excluded_from_current_period.parquet", index=False)
        old = old.loc[~old[sort].apply(tuple, axis=1).isin(removals)]
        pd.testing.assert_frame_equal(old.sort_values(sort).reset_index(drop=True), new.sort_values(sort).reset_index(drop=True))
        records[symbol]["other_latest_period_values_unchanged"] = True
        records[symbol]["wrong_period_rows_removed"] = len(removed)
    # The 2015 original 10-K index and balance sheet independently establish
    # February 8 publication and (3,281,250,000 + 175,000,000) / 8,258,616,000.
    daily = create_stock_factor_dataframe("ALXN", market="us", financial_basis="annual",
        start_date="2016-02-05", end_date="2016-02-12", financial_dir=output / "normalized",
        report_metadata_path=output / "metadata.csv", use_edgartools=False,
        require_report_metadata=True, financial_availability_delay_days=1,
        requested_factor_ids=["debt_to_equity"], wacc_online_backfill=False)
    selected = daily[[column for column in ["trade_date", "debt_to_equity", "financial_period", "report_date"] if column in daily]]
    selected.to_parquet(output / "ALXN_2015_original_public_daily.parquet", index=False)
    expected = 3456250000 / 8258616000
    after_publication = selected[pd.to_datetime(selected.trade_date) >= "2016-02-09"]
    assert len(after_publication) == 4
    assert (after_publication.debt_to_equity - expected).abs().lt(1e-12).all()
    publication_day = selected[pd.to_datetime(selected.trade_date) == "2016-02-08"]
    assert len(publication_day) == 1 and (publication_day.debt_to_equity - expected).abs().gt(1e-12).all()
    after = pins(sources + production)
    assert before == after
    result = dict(status="accessions_and_original_daily_balance_verified_staging_only", records=records,
        input_pins=before, inputs_unchanged=True, source_count=len(sources),
        ALXN_2015_expected_debt_to_equity=expected, original_publication="2016-02-08",
        usable_from="2016-02-09", public_daily_output=pins([output / "ALXN_2015_original_public_daily.parquet"]),
        production_changed=False, native_published=False, snapshots_published=False,
        coverage_complete=False, runner=pins([Path(__file__).resolve()]))
    (output / "summary.json").write_text(json.dumps(result, indent=2), "utf-8")
    print(json.dumps({"status": result["status"], "rows": sum(item["rows"] for item in records.values()),
        "retained_inputs_unchanged": len(before), "ALXN_2015_expected": expected}), flush=True)


if __name__ == "__main__":
    main()
