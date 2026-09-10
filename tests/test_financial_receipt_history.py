"""Historical factors must retain original filings and later amendments."""
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from engine.transformers.factors import (
    read_annual_financials, read_ttm_financials, read_quarterly_financials,
    create_stock_factor_dataframe, add_rim_historical_roe_fallback,
)


def write_history(root, records):
    folder = root / "history/035480"
    folder.mkdir(parents=True)
    receipts = []
    for record in records:
        year, received, sales = record[:3]
        receipt = received.replace("-", "") + "000001"
        path = folder / f"{receipt}.csv"
        facts = [
            {"canonical_account_id": "REVENUE", "statement_type": "IS", "original_account_name": "매출액", "normalized_amount": sales},
            {"canonical_account_id": "TOTAL_ASSETS", "statement_type": "BS", "original_account_name": "자산총계", "normalized_amount": 1000},
            {"canonical_account_id": "TOTAL_EQUITY", "statement_type": "BS", "original_account_name": "자본총계", "normalized_amount": 600},
        ]
        if len(record) > 3:
            facts.append({"canonical_account_id": "EAOP", "statement_type": "BS",
                          "original_account_name": "지배기업소유주지분", "normalized_amount": 600})
            facts.append({"canonical_account_id": "NET_INCOME_PARENT", "statement_type": "IS",
                          "original_account_name": "지배기업소유주순이익", "normalized_amount": record[3]})
        pd.DataFrame(facts).to_csv(path, index=False)
        receipts.append({"rcept_no": receipt, "fiscal_year": year, "fiscal_month": 12,
                         "period_end_date": f"{year}-12-31", "report_date": received,
                         "financial_basis": "annual", "normalized_path": path.name,
                         "normalized_sha256": sha256(path.read_bytes()).hexdigest()})
    (folder / "manifest.json").write_text(json.dumps({"schema_version": 1, "market": "kr", "symbol": "035480", "receipts": receipts}), encoding="utf-8")


def test_annual_factor_inputs_keep_original_and_amended_publication_events(tmp_path):
    write_history(tmp_path, [(2021, "2022-03-30", 100), (2021, "2022-08-15", 80)])
    result = read_annual_financials("035480", financial_dir=tmp_path, market="kr",
                                    use_edgartools=False, require_report_metadata=True)
    assert len(result) == 2
    assert result.report_date.tolist() == [pd.Timestamp("2022-03-30"), pd.Timestamp("2022-08-15")]
    assert result.sale.tolist() == [100, 80]


def test_missing_fiscal_year_is_not_treated_as_one_year_growth(tmp_path):
    write_history(tmp_path, [(2020, "2021-03-30", 100), (2022, "2023-03-30", 150)])
    result = read_annual_financials("035480", financial_dir=tmp_path, market="kr",
                                    use_edgartools=False, require_report_metadata=True)
    assert pd.isna(result.iloc[-1].sales_growth_1y)


@pytest.mark.parametrize("field,before,after", [
    ("financial_scope", "OFS", "CFS"),
    ("accounting_regime", "K_GAAP", "K_IFRS"),
])
def test_growth_does_not_compare_different_reported_scopes_or_accounting_regimes(tmp_path, field, before, after):
    write_history(tmp_path, [(2021, "2022-03-30", 100), (2022, "2023-03-30", 150)])
    path = tmp_path / "history/035480/manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["receipts"][0][field] = before
    manifest["receipts"][1][field] = after
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = read_annual_financials("035480", financial_dir=tmp_path, market="kr",
                                    use_edgartools=False, require_report_metadata=True)
    assert result.sale.tolist() == [100, 150]
    assert pd.isna(result.iloc[-1].sales_growth_1y)


def test_older_year_amendment_updates_growth_after_publication_without_rewinding_latest_year(tmp_path):
    write_history(tmp_path, [(2021, "2022-03-30", 100), (2022, "2023-03-30", 150),
                             (2021, "2023-10-10", 80)])
    prices = pd.DataFrame({"security_id": ["SEC_KR_035480"] * 3,
                           "trade_date": pd.to_datetime(["2023-04-01", "2023-10-10", "2023-10-11"]),
                           "close": [100] * 3, "volume": [1000] * 3, "currency": ["KRW"] * 3})
    with (
        patch("engine.transformers.factors.read_stock_prices", return_value=prices),
        patch("engine.transformers.factors.read_stock_shares", return_value=pd.DataFrame()),
    ):
        result = create_stock_factor_dataframe(
            "035480", market="kr", financial_basis="annual", financial_dir=tmp_path,
            use_edgartools=False, require_report_metadata=True, financial_availability_delay_days=1,
            requested_factor_ids={"pvgo_gap_pct"},
        )
    assert result.sale.tolist() == [150, 150, 150]
    assert result.financial_period.eq(pd.Timestamp("2022-12-31")).all()
    assert result.sales_growth_1y.tolist() == [50, 50, 87.5]
    assert result.report_date.iloc[-1] == pd.Timestamp("2023-10-10")


def test_historical_roe_uses_the_vintage_known_at_each_publication(tmp_path):
    records = [(year, f"{year+1}-03-30", 200, 60) for year in range(2019, 2023)]
    records.append((2020, "2023-10-10", 200, 120))
    write_history(tmp_path, records)
    financials = read_annual_financials("035480", financial_dir=tmp_path, market="kr",
                                       use_edgartools=False, require_report_metadata=True)
    daily = pd.DataFrame({"trade_date": pd.to_datetime(["2023-04-01", "2023-10-11"])})
    result = add_rim_historical_roe_fallback(daily, financials)
    # The public financial-factor contract reports ROE in percent.
    assert result.historical_roe_3y_avg.tolist() == pytest.approx([10, 40/3])


def test_ttm_recalculates_after_an_older_quarter_amendment(tmp_path):
    records = [(2021, "2021-05-15", 10), (2021, "2021-08-15", 30),
               (2021, "2021-11-15", 60), (2021, "2022-03-30", 100),
               (2022, "2022-05-15", 25), (2021, "2022-08-16", 5)]
    write_history(tmp_path, records)
    manifest_path = tmp_path / "history/035480/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for record, month in zip(manifest["receipts"], [3, 6, 9, 12, 3, 3]):
        record["fiscal_month"] = month
        record["financial_basis"] = "annual" if month == 12 else "quarterly"
        record["period_end_date"] = str((pd.Timestamp(year=record["fiscal_year"], month=month, day=1)
                                         + pd.offsets.MonthEnd()).date())
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = read_ttm_financials("035480", financial_dir=tmp_path, market="kr",
                                use_edgartools=False, require_report_metadata=True)
    assert len(result) == 6
    assert result.sale.iloc[:3].isna().all()
    assert result.sale.iloc[3:].tolist() == [100, 115, 120]
    assert result.financial_period.iloc[-1] == pd.Timestamp("2022-03-31")
    quarterly = read_quarterly_financials("035480", financial_dir=tmp_path, market="kr",
                                         use_edgartools=False, require_report_metadata=True)
    assert quarterly.sale.tolist() == [10, 20, 30, 40, 25, 25]
