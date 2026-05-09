from pathlib import Path
import re
from time import sleep

import pandas as pd

from pykrx import stock


def fetch_price(stock_code: str, start_date: str, end_date: str):
    df = stock.get_market_ohlcv_by_date(start_date, end_date, stock_code)
    return df

def fetch_share(stock_code: str, start_date: str, end_date: str):
    df = stock.get_market_cap(start_date, end_date, stock_code)
    return df

def fetch_all_prices(stock_codes: list[str], download_offset: int, start_date: str, end_date: str):
    download_stock_codes = sorted(stock_codes)[download_offset:]

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+download_offset})....")
        ticker = stock_code
        dir = f"./data-lake/bronze/krx/price"

        prices = _with_date_column(fetch_price(ticker, start_date, end_date))
        out_path = Path(dir) / _safe_filename(ticker)
        out_path.parent.mkdir(parents=True, exist_ok=True)  # 폴더 없으면 생성
        prices.to_csv(out_path, index=False, encoding="utf-8-sig")
        sleep(0.1)

def fetch_all_shares(stock_codes: list[str], download_offset: int, start_date: str, end_date: str):
    download_stock_codes = sorted(stock_codes)[download_offset:]

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+download_offset})....")
        ticker = stock_code
        dir = f"./data-lake/bronze/krx/shares"

        shares = _with_date_column(fetch_share(ticker, start_date, end_date))
        out_path = Path(dir) / _safe_filename(ticker)
        out_path.parent.mkdir(parents=True, exist_ok=True)  # 폴더 없으면 생성
        shares.to_csv(out_path, index=False, encoding="utf-8-sig")
        sleep(0.1)


def _with_date_column(df: pd.DataFrame) -> pd.DataFrame:
    if "날짜" in df.columns:
        return df.copy()

    result = df.reset_index()
    return result.rename(columns={result.columns[0]: "날짜"})


def _safe_filename(name: str) -> str:
    # 윈도우/리눅스에서 파일명으로 못 쓰는 문자들 치환
    name = re.sub(r'[\\/*?:"<>|]+', "_", name).strip()
    return name or "output.csv"
