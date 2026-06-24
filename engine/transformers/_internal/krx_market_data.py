from glob import glob
from pathlib import Path

import pandas as pd

from engine.core.paths import DATA_LAKE, PROJECT_ROOT, market_csv_name

ENGINE_DIR = Path(__file__).resolve().parent

DATE_COLUMN = "\ub0a0\uc9dc"
OPEN_COLUMN = "\uc2dc\uac00"
HIGH_COLUMN = "\uace0\uac00"
LOW_COLUMN = "\uc800\uac00"
CLOSE_COLUMN = "\uc885\uac00"
VOLUME_COLUMN = "\uac70\ub798\ub7c9"
CHANGE_RATE_COLUMN = "\ub4f1\ub77d\ub960"
MARKET_CAP_COLUMN = "\uc2dc\uac00\ucd1d\uc561"
TRADING_VALUE_COLUMN = "\uac70\ub798\ub300\uae08"
LISTED_SHARES_COLUMN = "\uc0c1\uc7a5\uc8fc\uc2dd\uc218"


def _glob_files(path: str) -> list[str]:
    files = glob(path)
    if files:
        return files

    path_obj = Path(path)
    if path_obj.is_absolute():
        return files

    for base_dir in (PROJECT_ROOT, ENGINE_DIR):
        files = glob(str(base_dir / path_obj))
        if files:
            return files

    return files


def _write_csv(df: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")


def _stock_code_from_path(path: str | Path) -> str:
    stem = Path(path).stem
    if stem.lower().startswith("kr_"):
        stem = stem[3:]
    return stem


def _dedupe_market_symbol_files(files: list[str]) -> list[str]:
    by_stock_code: dict[str, str] = {}
    for file in files:
        stock_code = _stock_code_from_path(file)
        if stock_code not in by_stock_code or Path(file).name.lower().startswith("kr_"):
            by_stock_code[stock_code] = file
    return list(by_stock_code.values())


def _read_market_symbol_files(path: str) -> pd.DataFrame:
    files = _dedupe_market_symbol_files(_glob_files(path))
    if not files:
        raise FileNotFoundError("CSV file not found")
    return pd.concat(
        [pd.read_csv(file).assign(stock_code=_stock_code_from_path(file)) for file in files],
        ignore_index=True,
    )


def _parse_trade_dates(values):
    dates = pd.to_datetime(values, errors="coerce", format="mixed")
    invalid = dates.isna()
    if invalid.any():
        sample = values.loc[invalid].head(3).astype(str).tolist() if hasattr(values, "loc") else []
        raise ValueError(f"invalid KRX trade_date values: {sample}")
    return dates.dt.normalize()


def _require_columns(df: pd.DataFrame, columns: list[str]) -> None:
    missing = [column for column in columns if column not in df.columns]
    if missing:
        raise KeyError(f"missing KRX columns: {missing}")


def _security_id(stock_code) -> str:
    return f"SEC_KR_{str(stock_code).strip().zfill(6)}"


def normalize_price(path: str):
    df = _read_market_symbol_files(path)
    _require_columns(
        df,
        [DATE_COLUMN, OPEN_COLUMN, HIGH_COLUMN, LOW_COLUMN, CLOSE_COLUMN, VOLUME_COLUMN],
    )

    result = pd.DataFrame()
    result["security_id"] = df["stock_code"].apply(_security_id)
    result["trade_date"] = _parse_trade_dates(df[DATE_COLUMN])
    result["open"] = df[OPEN_COLUMN]
    result["high"] = df[HIGH_COLUMN]
    result["low"] = df[LOW_COLUMN]
    result["close"] = df[CLOSE_COLUMN]
    result["volume"] = df[VOLUME_COLUMN]
    result["adj_close"] = df[CLOSE_COLUMN]
    result["currency"] = "KRW"

    _write_csv(result, DATA_LAKE.silver("krx", "price", market_csv_name("normalized_price")))
    return result


def normalize_shares(path: str):
    df = _read_market_symbol_files(path)
    _require_columns(df, [DATE_COLUMN, LISTED_SHARES_COLUMN, MARKET_CAP_COLUMN])

    result = pd.DataFrame()
    result["security_id"] = df["stock_code"].apply(_security_id)
    result["trade_date"] = _parse_trade_dates(df[DATE_COLUMN])
    result["shares"] = df[LISTED_SHARES_COLUMN]
    result["market_cap"] = df[MARKET_CAP_COLUMN]

    _write_csv(result, DATA_LAKE.silver("krx", "shares", market_csv_name("normalized_shares")))
    return result

