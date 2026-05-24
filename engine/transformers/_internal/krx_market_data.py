from datetime import datetime
from glob import glob
from pathlib import Path

import pandas as pd

from engine.core.paths import DATA_LAKE, PROJECT_ROOT, market_csv_name

ENGINE_DIR = Path(__file__).resolve().parent


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


def normalize_price(path: str):
    files = _dedupe_market_symbol_files(_glob_files(path))

    if not files:
        raise FileNotFoundError("CSV 파일을 찾지 못했습니다.")

    df = pd.concat(
        [
            pd.read_csv(file).assign(stock_code=_stock_code_from_path(file))
            for file in files
        ],
        ignore_index=True
    )
    
    df["security_id"] = df["stock_code"].apply(lambda stock_code: f"SEC_KR_{str(stock_code).strip().zfill(6)}")
    df["trade_date"] = df["날짜"].apply(lambda date: datetime.strptime(date, "%Y-%m-%d"))
    df["open"] = df["시가"].apply(lambda price: price)
    df["high"] = df["고가"].apply(lambda price: price)
    df["low"] = df["저가"].apply(lambda price: price)
    df["close"] = df["종가"].apply(lambda price: price)
    df["volume"] = df["거래량"].apply(lambda volume: volume)
    df["adj_close"] = df["종가"].apply(lambda price: price)
    df["currency"] = df["stock_code"].apply(lambda _: "KRW")

    df = df.drop(columns=["날짜", "시가", "고가", "저가", "종가", "거래량", "stock_code", "등락률"])

    _write_csv(df, DATA_LAKE.silver("krx", "price", market_csv_name("normalized_price")))

    return df

def normalize_shares(path: str):
    files = _dedupe_market_symbol_files(_glob_files(path))

    if not files:
        raise FileNotFoundError("CSV 파일을 찾지 못했습니다.")

    df = pd.concat(
        [
            pd.read_csv(file).assign(stock_code=_stock_code_from_path(file))
            for file in files
        ],
        ignore_index=True
    )
    
    df["security_id"] = df["stock_code"].apply(lambda stock_code: f"SEC_KR_{str(stock_code).strip().zfill(6)}")
    df["trade_date"] = df["날짜"].apply(lambda date: datetime.strptime(date, "%Y-%m-%d"))
    df["shares"] = df["상장주식수"].apply(lambda shares: shares)
    df["market_cap"] = df["시가총액"].apply(lambda market_cap: market_cap)

    df = df.drop(columns=["날짜", "상장주식수", "시가총액", "거래량", "거래대금", "상장주식수", "stock_code"])

    _write_csv(df, DATA_LAKE.silver("krx", "shares", market_csv_name("normalized_shares")))

    return df
