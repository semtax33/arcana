"""Receipt-scoped financial inputs evaluated using information then available."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pandas as pd


def read_receipt_financial_history(stock_code, financial_dir, market, *, basis="annual", cumulative_statement_types=None):
    """Return filing events, or None when no receipt history is installed.

    A manifest is the publication boundary. Unlisted staging files are ignored;
    listed files must match their digest and retain their actual receipt date.
    """
    from engine.transformers._internal.factor_metrics import (
        add_annual_financial_factors, aggregate_annual_canonical_values,
        extract_fallback_values, security_id_for_market, _financial_frame_from_periodized,
    )
    from engine.transformers._internal.filing_periods import add_quarter_and_ttm_amounts

    market = str(market or "kr").strip().lower()
    if basis not in {"annual", "quarterly", "ttm"}:
        raise ValueError("Unsupported financial history basis")
    root = Path(financial_dir) / "history" / str(stock_code)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != 1 or manifest.get("symbol") != stock_code
            or manifest.get("market") != market):
        raise ValueError("Financial receipt manifest identity/schema mismatch")
    receipts = []
    identities = set()
    for record in manifest["receipts"]:
        if basis == "annual" and record["financial_basis"] != "annual":
            continue
        receipt = str(record["rcept_no"])
        if receipt in identities:
            raise ValueError("Duplicate financial receipt")
        identities.add(receipt)
        path = (root / record["normalized_path"]).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("Financial receipt path must remain inside its history directory")
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != record["normalized_sha256"]:
            raise ValueError("Financial receipt digest mismatch")
        report_date = pd.Timestamp(record["report_date"])
        period_end = pd.Timestamp(record["period_end_date"])
        if pd.isna(report_date) or pd.isna(period_end) or report_date < period_end:
            raise ValueError("Financial receipt publication precedes its reported period")
        frame = pd.read_csv(path)
        frame["normalized_amount"] = pd.to_numeric(frame["normalized_amount"], errors="coerce")
        frame = frame.loc[frame.canonical_account_id.notna() & frame.canonical_account_id.ne("UNMAPPED")]
        if frame.empty:
            raise ValueError("Financial receipt has no canonical facts")
        values = aggregate_annual_canonical_values(frame)
        values.update(extract_fallback_values(frame))
        values["_fs_type_by_id"] = dict(zip(frame.canonical_account_id, frame.statement_type))
        values.update(stock_code=stock_code, security_id=security_id_for_market(stock_code, market),
                      fiscal_year=int(record["fiscal_year"]), fiscal_month=int(record["fiscal_month"]),
                      financial_period=period_end, report_date=report_date, rcept_no=receipt,
                      source_url=record.get("source_url", ""),
                      financial_scope=str(record.get("financial_scope", "UNKNOWN")).strip().upper(),
                      accounting_regime=str(record.get("accounting_regime", "UNKNOWN")).strip().upper())
        receipts.append(values)
    if not receipts:
        return pd.DataFrame()
    versions = pd.DataFrame(receipts).sort_values(["report_date", "rcept_no"], kind="stable")
    latest_by_period = {}
    events = []
    for published, group in versions.groupby("report_date", sort=True):
        for row in group.to_dict("records"):
            latest_by_period[row["financial_period"]] = row
        known_history = pd.DataFrame(latest_by_period.values()).sort_values("financial_period").reset_index(drop=True)
        source_row = known_history.iloc[-1]
        # A change of consolidation scope or accounting regime cannot become
        # growth, a lagged average or a TTM sum without a reviewed bridge.
        # Re-evaluate the boundary for every publication vintage: a later
        # restatement of old periods can restore comparability prospectively.
        comparable = known_history[["financial_scope", "accounting_regime"]]
        boundaries = comparable.ne(comparable.shift()).any(axis=1)
        last_boundary = boundaries[boundaries].index[-1]
        known_history = known_history.loc[last_boundary:].reset_index(drop=True)
        comparability_start = known_history.iloc[0].financial_period
        if basis == "annual":
            if known_history.fiscal_year.duplicated().any():
                raise ValueError("Multiple annual periods in one fiscal year require explicit transition handling")
            years = range(int(known_history.fiscal_year.min()), int(known_history.fiscal_year.max()) + 1)
            known_history = known_history.set_index("fiscal_year").reindex(years).reset_index()
            known_history["fiscal_month"] = known_history.fiscal_month.fillna(12)
            periods_per_year = 1
        else:
            if not known_history.fiscal_month.isin([3, 6, 9, 12]).all():
                raise ValueError("Quarter history requires an identified fiscal quarter")
            known_history["_quarter_sequence"] = known_history.fiscal_year * 4 + known_history.fiscal_month // 3 - 1
            if known_history._quarter_sequence.duplicated().any():
                raise ValueError("Multiple financial periods occupy one fiscal quarter")
            sequence = range(int(known_history._quarter_sequence.min()), int(known_history._quarter_sequence.max()) + 1)
            known_history = known_history.set_index("_quarter_sequence").reindex(sequence)
            known_history["fiscal_year"] = known_history.index // 4
            known_history["fiscal_month"] = (known_history.index % 4 + 1) * 3
            known_history = known_history.reset_index(drop=True)
            periods_per_year = 4
        # Keep missing periods in lag/rolling windows; they are not zero facts.
        missing_period = pd.to_datetime(dict(year=known_history.fiscal_year.astype(int),
                                            month=known_history.fiscal_month.astype(int), day=1)) + pd.offsets.MonthEnd()
        known_history["financial_period"] = known_history.financial_period.fillna(missing_period)
        if basis == "annual":
            calculated = add_annual_financial_factors(known_history, periods_per_year=1)
        else:
            periodized = add_quarter_and_ttm_amounts(known_history, cumulative_statement_types=cumulative_statement_types)
            amounts = _financial_frame_from_periodized(periodized, suffix="_ttm" if basis == "ttm" else "_quarter")
            calculated = add_annual_financial_factors(amounts, periods_per_year=4, annualized_flows=basis == "ttm")
        event = calculated.iloc[-1].copy()
        event["latest_statement_report_date"] = event["report_date"]
        event["report_date"] = published
        event["rcept_no"] = source_row["rcept_no"]
        event["source_url"] = source_row["source_url"]
        event["trigger_rcept_no"] = group.iloc[-1]["rcept_no"]
        event["financial_history_vintage"] = True
        event["financial_history_periods_per_year"] = periods_per_year
        event["history_comparability_start_date"] = comparability_start
        window = 3 * periods_per_year
        event["historical_roe_3y_avg"] = calculated.roe.rolling(window, min_periods=window).mean().iloc[-1]
        events.append(event)
    return pd.DataFrame(events).reset_index(drop=True)
