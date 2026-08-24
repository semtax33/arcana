from __future__ import annotations

import pandas as pd


DATE_COLUMN = "날짜"
OPEN_COLUMN = "시가"
HIGH_COLUMN = "고가"
LOW_COLUMN = "저가"
CLOSE_COLUMN = "종가"
VOLUME_COLUMN = "거래량"
CHANGE_RATE_COLUMN = "등락률"


def fetch_finance_datareader_price(
    stock_code: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    import FinanceDataReader as fdr

    source = fdr.DataReader(str(stock_code).strip(), start_date, end_date)
    return normalize_finance_datareader_price_frame(source)


def finance_datareader_stock_codes() -> list[str]:
    import FinanceDataReader as fdr

    listing = fdr.StockListing("KRX")
    code_column = next(
        (column for column in ("Code", "Symbol", "code", "symbol") if column in listing.columns),
        None,
    )
    if code_column is None:
        raise KeyError("FinanceDataReader KRX listing is missing a stock-code column")
    return sorted({_normalize_stock_code(value) for value in listing[code_column].dropna()})


def normalize_finance_datareader_price_frame(source: pd.DataFrame) -> pd.DataFrame:
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = sorted(required - set(source.columns))
    if missing:
        raise ValueError(f"FinanceDataReader price frame is missing required columns: {missing}")
    if source.empty:
        return pd.DataFrame(
            columns=[
                DATE_COLUMN,
                OPEN_COLUMN,
                HIGH_COLUMN,
                LOW_COLUMN,
                CLOSE_COLUMN,
                VOLUME_COLUMN,
                CHANGE_RATE_COLUMN,
            ]
        )

    dates = pd.to_datetime(source.index, errors="coerce")
    valid = dates.notna()
    filtered = source.loc[valid].copy()
    dates = dates[valid]
    result = pd.DataFrame(index=filtered.index)
    result[DATE_COLUMN] = dates.strftime("%Y-%m-%d")
    result[OPEN_COLUMN] = pd.to_numeric(filtered["Open"], errors="coerce")
    result[HIGH_COLUMN] = pd.to_numeric(filtered["High"], errors="coerce")
    result[LOW_COLUMN] = pd.to_numeric(filtered["Low"], errors="coerce")
    result[CLOSE_COLUMN] = pd.to_numeric(filtered["Close"], errors="coerce")
    result[VOLUME_COLUMN] = pd.to_numeric(filtered["Volume"], errors="coerce")
    if "Change" in filtered.columns:
        result[CHANGE_RATE_COLUMN] = pd.to_numeric(filtered["Change"], errors="coerce") * 100.0
    else:
        result[CHANGE_RATE_COLUMN] = pd.NA
    return result.reset_index(drop=True)


def _normalize_stock_code(value) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else text.upper()
