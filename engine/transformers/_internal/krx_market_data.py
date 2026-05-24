from datetime import datetime
from glob import glob
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[3]
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


def normalize_price(path: str):
    files = _glob_files(path)

    if not files:
        raise FileNotFoundError("CSV 파일을 찾지 못했습니다.")

    df = pd.concat(
        [
            pd.read_csv(file).assign(stock_code=Path(file).name)
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

    _write_csv(df, PROJECT_ROOT / 'data-lake' / 'silver' / 'krx' / 'price' / 'normalized_price.csv')

    return df

def normalize_shares(path: str):
    files = _glob_files(path)

    if not files:
        raise FileNotFoundError("CSV 파일을 찾지 못했습니다.")

    df = pd.concat(
        [
            pd.read_csv(file).assign(stock_code=Path(file).name)
            for file in files
        ],
        ignore_index=True
    )
    
    df["security_id"] = df["stock_code"].apply(lambda stock_code: f"SEC_KR_{str(stock_code).strip().zfill(6)}")
    df["trade_date"] = df["날짜"].apply(lambda date: datetime.strptime(date, "%Y-%m-%d"))
    df["shares"] = df["상장주식수"].apply(lambda shares: shares)
    df["market_cap"] = df["시가총액"].apply(lambda market_cap: market_cap)

    df = df.drop(columns=["날짜", "상장주식수", "시가총액", "거래량", "거래대금", "상장주식수", "stock_code"])

    _write_csv(df, PROJECT_ROOT / 'data-lake' / 'silver' / 'krx' / 'shares' / 'normalized_shares.csv')

    return df
