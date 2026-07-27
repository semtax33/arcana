from __future__ import annotations

"""Normalize provider-specific US dividend bronze snapshots into silver files."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from engine.core.identifiers import security_id_of
from engine.core.paths import DATA_LAKE, market_csv_name
from engine.extractors._internal.us_dividends import (
    BRONZE_US_DIVIDEND_DIR,
    US_DIVIDEND_SOURCE_PRIORITY,
)
from engine.extractors._internal.yfinance_market_prices import (
    FILTERED_UNIVERSE_PATH,
    normalize_yfinance_ticker,
)
from engine.markets.us import US_MARKET_CONFIG


US_DIVIDEND_EVENT_COLUMNS = [
    "ticker",
    "cik",
    "company_name",
    "exchange",
    "dividend_ex_date",
    "dividend_declared_date",
    "dividend_record_date",
    "dividend_payment_date",
    "dividend_amount_per_share",
    "source",
    "source_snapshot_date",
    "sec_filing_date",
    "source_form",
    "annual_dps",
    "annual_eps",
    "payout_ratio_dps_over_eps",
    "payout_ratio_total_dividends_over_net_income",
]
US_DAILY_DIVIDEND_COLUMNS = [
    "security_id",
    "trade_date",
    "dividend",
    "payout_ratio",
    "dividend_percent",
    "currency",
    "updated_at",
]
US_SILVER_DIVIDEND_DIR = DATA_LAKE.silver("us", "dividend")
US_DIVIDEND_EVENTS_PATH = US_SILVER_DIVIDEND_DIR / "us_dividend_events.csv"
US_DIVIDEND_NORMALIZED_PATH = US_SILVER_DIVIDEND_DIR / market_csv_name("dividend_normalized", market="us")
US_SEC_TICKER_MAP_PATH = DATA_LAKE.meta("sec_company_tickers.csv")
US_SEC_FINANCIAL_DIR = DATA_LAKE.silver("sec", "normalized")


def build_us_dividend_events_dataframe(
    *,
    bronze_root: str | Path = BRONZE_US_DIVIDEND_DIR,
    ticker_map_path: str | Path = US_SEC_TICKER_MAP_PATH,
    financial_dir: str | Path = US_SEC_FINANCIAL_DIR,
) -> pd.DataFrame:
    """Build the current provider-prioritized event view from bronze snapshots."""
    root = Path(bronze_root)
    metadata = _ticker_metadata(ticker_map_path)
    latest = {
        provider: _latest_provider_snapshots(root, provider)
        for provider in US_DIVIDEND_SOURCE_PRIORITY
        if provider != "edgartools"
    }
    rows: list[dict[str, object]] = []
    covered: set[str] = set()

    for provider in US_DIVIDEND_SOURCE_PRIORITY:
        if provider == "edgartools":
            # The 999.Ex provider is a collector stub in this release.
            continue
        for ticker, snapshot in sorted(latest.get(provider, {}).items()):
            if ticker in covered:
                continue
            events = _provider_events(
                provider,
                ticker,
                snapshot["payload"],
                snapshot["snapshot_date"],
                metadata.get(ticker, {}),
            )
            if not events:
                continue
            rows.extend(events)
            covered.add(ticker)

    if not rows:
        return pd.DataFrame(columns=US_DIVIDEND_EVENT_COLUMNS)
    events = _dedupe_events(pd.DataFrame(rows))
    return _add_annual_metrics(events, financial_dir=financial_dir)


def write_us_dividend_events_file(
    *,
    output_path: str | Path = US_DIVIDEND_EVENTS_PATH,
    bronze_root: str | Path = BRONZE_US_DIVIDEND_DIR,
    ticker_map_path: str | Path = US_SEC_TICKER_MAP_PATH,
    financial_dir: str | Path = US_SEC_FINANCIAL_DIR,
) -> pd.DataFrame:
    output_path = Path(output_path)
    events = build_us_dividend_events_dataframe(
        bronze_root=bronze_root,
        ticker_map_path=ticker_map_path,
        financial_dir=financial_dir,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    events.to_csv(output_path, index=False, encoding="utf-8-sig")
    return events


def normalize_us_dividends(
    *,
    bronze_root: str | Path = BRONZE_US_DIVIDEND_DIR,
    events_path: str | Path = US_DIVIDEND_EVENTS_PATH,
    daily_path: str | Path = US_DIVIDEND_NORMALIZED_PATH,
    ticker_map_path: str | Path = US_SEC_TICKER_MAP_PATH,
    financial_dir: str | Path = US_SEC_FINANCIAL_DIR,
) -> dict[str, Path | int]:
    """Normalize the latest Alpha/yfinance dividend snapshots into silver CSVs."""
    events_path = Path(events_path)
    daily_path = Path(daily_path)
    events = write_us_dividend_events_file(
        output_path=events_path,
        bronze_root=bronze_root,
        ticker_map_path=ticker_map_path,
        financial_dir=financial_dir,
    )
    daily = create_us_stock_dividend_dataframe(events_path=events_path)
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    daily.to_csv(daily_path, index=False, encoding="utf-8-sig")
    return {
        "events_path": events_path,
        "daily_path": daily_path,
        "events": len(events),
        "daily_rows": len(daily),
    }


def read_us_dividend_events(path: str | Path = US_DIVIDEND_EVENTS_PATH) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=US_DIVIDEND_EVENT_COLUMNS)
    frame = pd.read_csv(path, dtype={"ticker": str, "cik": str})
    frame = frame.drop(
        columns=[column for column in frame.columns if str(column).startswith("Unnamed")],
        errors="ignore",
    )
    for column in US_DIVIDEND_EVENT_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    return frame[US_DIVIDEND_EVENT_COLUMNS]


def create_us_stock_dividend_dataframe(
    stock_code: str | None = None,
    *,
    events_path: str | Path = US_DIVIDEND_EVENTS_PATH,
) -> pd.DataFrame:
    """Create payment-date-only rows for the existing stock_dividend table."""
    events = read_us_dividend_events(events_path)
    if events.empty:
        return pd.DataFrame(columns=US_DAILY_DIVIDEND_COLUMNS)
    frame = events.copy()
    frame["ticker"] = frame["ticker"].map(normalize_yfinance_ticker)
    if stock_code is not None:
        frame = frame.loc[frame["ticker"] == normalize_yfinance_ticker(stock_code)].copy()
    frame["trade_date"] = pd.to_datetime(frame["dividend_payment_date"], errors="coerce")
    frame["dividend"] = pd.to_numeric(frame["dividend_amount_per_share"], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "dividend"])
    frame = frame.loc[frame["dividend"] > 0].copy()
    if frame.empty:
        return pd.DataFrame(columns=US_DAILY_DIVIDEND_COLUMNS)
    frame["security_id"] = frame["ticker"].map(lambda ticker: security_id_of(ticker, US_MARKET_CONFIG))
    frame["payout_ratio"] = pd.to_numeric(frame["payout_ratio_dps_over_eps"], errors="coerce")
    frame["dividend_percent"] = pd.NA
    frame["currency"] = US_MARKET_CONFIG.currency
    frame["updated_at"] = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    return frame[US_DAILY_DIVIDEND_COLUMNS].sort_values(["security_id", "trade_date"]).reset_index(drop=True)


def _latest_provider_snapshots(root: Path, provider: str) -> dict[str, dict[str, object]]:
    provider_root = root / provider
    if not provider_root.exists():
        return {}
    result: dict[str, dict[str, object]] = {}
    for path in sorted(provider_root.glob("snapshot_date=*/ticker=*.json")):
        snapshot_date = path.parent.name.removeprefix("snapshot_date=")
        ticker = normalize_yfinance_ticker(path.stem.removeprefix("ticker="))
        if not ticker:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        current = result.get(ticker)
        if current is None or snapshot_date >= str(current["snapshot_date"]):
            result[ticker] = {"snapshot_date": snapshot_date, "payload": payload}
    return result


def _provider_events(
    provider: str,
    ticker: str,
    payload: object,
    snapshot_date: object,
    metadata: dict[str, str],
) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    values = payload.get("data")
    if not isinstance(values, list):
        return []
    source = "ALPHA_VANTAGE" if provider == "alpha-vantage" else "YFINANCE"
    rows = []
    for item in values:
        if not isinstance(item, dict):
            continue
        amount = pd.to_numeric(item.get("amount"), errors="coerce")
        if pd.isna(amount) or float(amount) <= 0:
            continue
        ex_date = _date_text(item.get("ex_dividend_date"))
        declared_date = _date_text(item.get("declaration_date"))
        record_date = _date_text(item.get("record_date"))
        payment_date = _date_text(item.get("payment_date"))
        if not any((ex_date, declared_date, record_date, payment_date)):
            continue
        rows.append(
            {
                "ticker": ticker,
                "cik": metadata.get("cik", ""),
                "company_name": metadata.get("company_name", ""),
                "exchange": metadata.get("exchange", ""),
                "dividend_ex_date": ex_date,
                "dividend_declared_date": declared_date,
                "dividend_record_date": record_date,
                "dividend_payment_date": payment_date,
                "dividend_amount_per_share": float(amount),
                "source": source,
                "source_snapshot_date": str(snapshot_date),
                "sec_filing_date": "",
                "source_form": "",
            }
        )
    return rows


def _dedupe_events(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame(columns=US_DIVIDEND_EVENT_COLUMNS)
    frame = events.copy()
    frame["ticker"] = frame["ticker"].map(normalize_yfinance_ticker)
    frame["dividend_amount_per_share"] = pd.to_numeric(frame["dividend_amount_per_share"], errors="coerce")
    frame = frame.dropna(subset=["dividend_amount_per_share"])
    frame = frame.loc[(frame["ticker"] != "") & (frame["dividend_amount_per_share"] > 0)].copy()
    for column in [
        "dividend_ex_date",
        "dividend_declared_date",
        "dividend_record_date",
        "dividend_payment_date",
    ]:
        frame[column] = frame[column].map(_date_text)
    frame["_event_date"] = frame[
        ["dividend_ex_date", "dividend_payment_date", "dividend_record_date", "dividend_declared_date"]
    ].replace("", pd.NA).bfill(axis=1).iloc[:, 0].fillna("")
    frame = frame.loc[frame["_event_date"] != ""].copy()
    frame["_amount_key"] = frame["dividend_amount_per_share"].map(lambda value: format(float(value), ".12g"))
    priority = {"ALPHA_VANTAGE": 0, "EDGARTOOLS_999EX": 1, "YFINANCE": 2}
    frame["_source_rank"] = frame["source"].map(priority).fillna(9)
    frame = frame.sort_values(
        ["_source_rank", "source_snapshot_date"],
        ascending=[True, False],
        kind="stable",
    )
    frame = frame.groupby(["ticker", "_event_date", "_amount_key"], as_index=False, sort=False).head(1)
    for column in US_DIVIDEND_EVENT_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    return (
        frame[US_DIVIDEND_EVENT_COLUMNS]
        .sort_values(["ticker", "dividend_ex_date", "dividend_payment_date"], kind="stable")
        .reset_index(drop=True)
    )


def _add_annual_metrics(events: pd.DataFrame, *, financial_dir: str | Path) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame(columns=US_DIVIDEND_EVENT_COLUMNS)
    frame = events.copy()
    payment_year = pd.to_datetime(frame["dividend_payment_date"], errors="coerce").dt.year
    frame["_payment_year"] = payment_year
    dps = (
        frame.dropna(subset=["_payment_year"])
        .groupby(["ticker", "_payment_year"])["dividend_amount_per_share"]
        .sum()
        .to_dict()
    )
    financials = _annual_financial_metrics(frame["ticker"], financial_dir)
    values = []
    for row in frame.to_dict("records"):
        year = row.get("_payment_year")
        year_key = int(year) if not pd.isna(year) else None
        ticker = str(row["ticker"])
        annual_dps = dps.get((ticker, year)) if year_key is not None else None
        metrics = financials.get((ticker, year_key), {}) if year_key is not None else {}
        annual_eps = metrics.get("annual_eps")
        values.append(
            {
                "annual_dps": annual_dps,
                "annual_eps": annual_eps,
                "payout_ratio_dps_over_eps": (
                    annual_dps / annual_eps
                    if annual_dps is not None and annual_eps is not None and annual_eps > 0
                    else None
                ),
                "payout_ratio_total_dividends_over_net_income": metrics.get(
                    "payout_ratio_total_dividends_over_net_income"
                ),
            }
        )
    metrics_frame = pd.DataFrame(values, index=frame.index)
    for column in metrics_frame.columns:
        frame[column] = metrics_frame[column]
    return frame[US_DIVIDEND_EVENT_COLUMNS]


def _annual_financial_metrics(tickers, financial_dir: str | Path) -> dict[tuple[str, int], dict[str, float | None]]:
    root = Path(financial_dir)
    result: dict[tuple[str, int], dict[str, float | None]] = {}
    for ticker in sorted({normalize_yfinance_ticker(value) for value in tickers if str(value).strip()}):
        path = root / f"us_normalized_{ticker}.csv"
        if not path.exists():
            continue
        try:
            frame = pd.read_csv(path)
        except (OSError, ValueError):
            continue
        required = {"canonical_account_id", "normalized_amount", "fiscal_year"}
        if not required.issubset(frame.columns):
            continue
        frame["fiscal_year"] = pd.to_numeric(frame["fiscal_year"], errors="coerce")
        frame["fiscal_month"] = pd.to_numeric(frame.get("fiscal_month"), errors="coerce")
        frame["normalized_amount"] = pd.to_numeric(frame["normalized_amount"], errors="coerce")
        for year, year_frame in frame.dropna(subset=["fiscal_year"]).groupby("fiscal_year"):
            values: dict[str, float] = {}
            for account_id, account_frame in year_frame.groupby("canonical_account_id", sort=False):
                ordered = account_frame.sort_values("fiscal_month", na_position="first")
                amounts = ordered["normalized_amount"].dropna()
                if not amounts.empty:
                    values[str(account_id)] = float(amounts.iloc[-1])
            eps = values.get("DILUTED_EPS", values.get("BASIC_EPS"))
            net_income = values.get("NET_INCOME")
            dividends_paid = values.get("DIV_PAID")
            result[(ticker, int(year))] = {
                "annual_eps": eps,
                "payout_ratio_total_dividends_over_net_income": (
                    abs(dividends_paid) / net_income
                    if dividends_paid is not None and net_income is not None and net_income > 0
                    else None
                ),
            }
    return result


def _ticker_metadata(ticker_map_path: str | Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    path = Path(ticker_map_path)
    if path.exists():
        try:
            frame = pd.read_csv(path, dtype=str).fillna("")
        except (OSError, ValueError):
            frame = pd.DataFrame()
        lower = {str(column).lower(): column for column in frame.columns}
        ticker_column = lower.get("ticker", lower.get("symbol"))
        cik_column = lower.get("cik", lower.get("cik_str"))
        name_column = lower.get("title", lower.get("name", lower.get("company_name")))
        if ticker_column is not None:
            for row in frame.to_dict("records"):
                ticker = normalize_yfinance_ticker(row.get(ticker_column, ""))
                if ticker:
                    result[ticker] = {
                        "cik": _normalize_cik(row.get(cik_column, "")) if cik_column else "",
                        "company_name": _text(row.get(name_column, "")) if name_column else "",
                        "exchange": "",
                    }
    if FILTERED_UNIVERSE_PATH.exists():
        try:
            universe = pd.read_csv(FILTERED_UNIVERSE_PATH, dtype=str).fillna("")
        except (OSError, ValueError):
            universe = pd.DataFrame()
        for row in universe.to_dict("records"):
            ticker = normalize_yfinance_ticker(row.get("ticker", row.get("symbol", "")))
            if not ticker:
                continue
            current = result.setdefault(ticker, {"cik": "", "company_name": "", "exchange": ""})
            current["company_name"] = current["company_name"] or _text(
                row.get("security_name", row.get("company_name", ""))
            )
            current["exchange"] = current["exchange"] or _text(row.get("exchange", ""))
    return result


def _date_text(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    parsed = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(parsed) else parsed.date().isoformat()


def _normalize_cik(value: object) -> str:
    text = "".join(character for character in _text(value) if character.isdigit())
    return str(int(text)) if text else ""


def _text(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()
