import zipfile
from io import BytesIO

import pandas as pd
import requests

from pykrx import stock

DART_API_KEY = "93bf0b5b166e7f5d12b626724d4983526aa71249"


def fetch_corp_list():
    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    params = {
        "crtfc_key": DART_API_KEY
    }

    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()

    zip_data = BytesIO(r.content)

    with zipfile.ZipFile(zip_data) as zf:
        xml_file_name = zf.namelist()[0]

        with zf.open(xml_file_name) as xml_file:
            df = pd.read_xml(
                xml_file,
                xpath=".//list",
                dtype={
                    "corp_code": "string",
                    "corp_name": "string",
                    "corp_eng_name": "string",
                    "stock_code": "string",
                    "modify_date": "string",
                },
            )

    # stock_code가 비어있는 비상장 회사 제거
    df = df[
        df["stock_code"].notna()
        & df["stock_code"].str.strip().ne("")
    ].copy()

    # 혹시 stock_code가 6자리 미만으로 들어온 경우 보정
    df["stock_code"] = df["stock_code"].str.strip().str.zfill(6)

    # 컬럼 순서 정리
    df = df[
        [
            "corp_code",
            "corp_name",
            "corp_eng_name",
            "stock_code",
            "modify_date",
        ]
    ].reset_index(drop=True)

    return df

def kospi_kosdaq_corp_list(date=None):
    """
    DART corp_list() 결과에서 KOSPI, KOSDAQ 상장사만 필터링한다.

    date:
        기준일자. 예: "20260430"
        None이면 오늘 날짜 기준.
    """

    if date is None:
        date = pd.Timestamp.today().strftime("%Y%m%d")

    dart_df = fetch_corp_list().copy()

    # DART stock_code 정리
    dart_df["stock_code"] = (
        dart_df["stock_code"]
        .astype(str)
        .str.strip()
        .str.zfill(6)
    )

    market_rows = []

    for market in ["KOSPI", "KOSDAQ"]:
        tickers = stock.get_market_ticker_list(date, market=market)

        for ticker in tickers:
            market_rows.append({
                "stock_code": ticker,
                "market": market,
            })

    market_df = pd.DataFrame(market_rows)

    # stock_code 기준으로 DART 기업목록과 KRX 상장목록 조인
    result = dart_df.merge(
        market_df,
        on="stock_code",
        how="inner"
    )

    return result.reset_index(drop=True)