from datetime import datetime
from glob import glob
from pathlib import Path

import pandas as pd


def normalize_price(path: str):
    files = glob(path)

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

    df.to_csv('./data-lake/silver/krx/price/normalized_price.csv')

    return df

def normalize_shares(path: str):
    files = glob(path)

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

    df.to_csv('./data-lake/silver/krx/shares/normalized_shares.csv')

    return df