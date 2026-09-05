from __future__ import annotations

from typing import Any

import pandas as pd


FINANCIAL_AVAILABILITY_COLUMNS = [
    "security_id",
    "fiscal_year",
    "financial_period",
    "trade_date",
    "rcept_no",
    "pit_safe",
]


def build_kr_financial_availability_dataframe(
    metadata: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Build strict annual-fact availability dates from DART filing metadata.

    Normalized statement snapshots represent the latest locally ingested
    version.  If several filings exist for one company-year, using the latest
    filing date prevents a later correction from being exposed at the original
    filing date.  Rows without a valid public date abstain instead of falling
    back to period end.
    """

    if metadata is None or metadata.empty:
        empty = pd.DataFrame(columns=FINANCIAL_AVAILABILITY_COLUMNS)
        return empty, {
            "input_count": 0,
            "eligible_count": 0,
            "output_count": 0,
            "preperiod_rejected_count": 0,
            "missing_date_rejected_count": 0,
            "nonstatement_rejected_count": 0,
            "nonannual_rejected_count": 0,
        }

    rows = metadata.copy()
    input_count = len(rows)
    def column(name: str, default: Any = "") -> pd.Series:
        if name in rows.columns:
            return rows[name]
        return pd.Series(default, index=rows.index)

    rows["stock_code"] = column("stock_code").astype(str).str.strip().str.zfill(6)
    if "security_id" not in rows.columns:
        rows["security_id"] = "SEC_KR_" + rows["stock_code"]
    rows["security_id"] = rows["security_id"].fillna("").astype(str).str.strip()
    missing_security = rows["security_id"].eq("")
    rows.loc[missing_security, "security_id"] = "SEC_KR_" + rows.loc[
        missing_security, "stock_code"
    ]
    rows["fiscal_year"] = pd.to_numeric(column("fiscal_year"), errors="coerce")
    rows["fiscal_month"] = pd.to_numeric(column("fiscal_month"), errors="coerce")
    rows["financial_period"] = pd.to_datetime(
        column("period_end_date", pd.NaT), errors="coerce"
    )
    rows["trade_date"] = pd.to_datetime(column("report_date", pd.NaT), errors="coerce")
    rows["rcept_no"] = column("rcept_no").fillna("").astype(str).str.strip()

    source_type = column("source_type").fillna("").astype(str)
    statement_mask = source_type.eq("statement")
    annual_mask = rows["fiscal_month"].eq(12)
    date_mask = rows["financial_period"].notna() & rows["trade_date"].notna()
    preperiod_mask = date_mask & rows["trade_date"].lt(rows["financial_period"])
    eligible = statement_mask & annual_mask & date_mask & ~preperiod_mask
    selected = rows.loc[eligible].copy()
    selected["_rcept_no_numeric"] = pd.to_numeric(
        selected["rcept_no"], errors="coerce"
    )
    selected = (
        selected.sort_values(
            ["security_id", "fiscal_year", "trade_date", "_rcept_no_numeric"],
            kind="stable",
            na_position="first",
        )
        .drop_duplicates(["security_id", "fiscal_year"], keep="last")
        .reset_index(drop=True)
    )
    selected["fiscal_year"] = selected["fiscal_year"].astype(int)
    selected["financial_period"] = selected["financial_period"].dt.strftime("%Y-%m-%d")
    selected["trade_date"] = selected["trade_date"].dt.strftime("%Y-%m-%d")
    selected["pit_safe"] = True
    result = selected[FINANCIAL_AVAILABILITY_COLUMNS]
    audit = {
        "input_count": int(input_count),
        "eligible_count": int(eligible.sum()),
        "output_count": int(len(result)),
        "preperiod_rejected_count": int((statement_mask & annual_mask & preperiod_mask).sum()),
        "missing_date_rejected_count": int((statement_mask & annual_mask & ~date_mask).sum()),
        "nonstatement_rejected_count": int((~statement_mask).sum()),
        "nonannual_rejected_count": int((statement_mask & ~annual_mask).sum()),
        "superseded_filing_count": int(eligible.sum() - len(result)),
        "missing_report_date_fallback_count": 0,
    }
    return result, audit
