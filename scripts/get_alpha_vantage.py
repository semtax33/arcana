from __future__ import annotations

import re
import time
import os
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from engine.extractors.consensus import download_us_consensus

NASDAQ_LISTED_URL = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
)

OTHER_LISTED_URL = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
)


def create_http_session() -> requests.Session:
    """
    재시도와 User-Agent가 설정된 HTTP 세션을 만든다.
    """
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=4,
        pool_maxsize=4,
    )

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; Arcana-Symbol-Collector/1.0; "
                "+https://example.com/contact)"
            ),
            "Accept": "text/plain,*/*",
        }
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


def download_symbol_file(
    session: requests.Session,
    url: str,
) -> tuple[pd.DataFrame, str | None]:
    """
    Nasdaq Trader의 pipe-delimited 파일을 다운로드한다.

    반환값:
        dataframe, file_creation_time_line
    """
    response = session.get(url, timeout=(10, 60))
    response.raise_for_status()

    response.encoding = response.apparent_encoding or "utf-8"
    text = response.text

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    if not lines:
        raise ValueError(f"빈 파일을 받았습니다: {url}")

    creation_time_line = next(
        (
            line
            for line in reversed(lines)
            if line.startswith("File Creation Time")
        ),
        None,
    )

    # 마지막의 File Creation Time 행은 데이터 행이 아니므로 제거한다.
    data_lines = [
        line
        for line in lines
        if not line.startswith("File Creation Time")
    ]

    dataframe = pd.read_csv(
        StringIO("\n".join(data_lines)),
        sep="|",
        dtype=str,
        keep_default_na=False,
    )

    dataframe.columns = [
        column.strip()
        for column in dataframe.columns
    ]

    return dataframe, creation_time_line


def get_column(
    dataframe: pd.DataFrame,
    column_name: str,
    default: str = "",
) -> pd.Series:
    """
    파일 버전별 컬럼 차이를 안전하게 처리한다.
    """
    if column_name in dataframe.columns:
        return dataframe[column_name].astype(str).str.strip()

    return pd.Series(
        default,
        index=dataframe.index,
        dtype="string",
    )


def standardize_nasdaq_listed(
    dataframe: pd.DataFrame,
    creation_time: str | None,
) -> pd.DataFrame:
    result = pd.DataFrame(index=dataframe.index)

    result["symbol"] = get_column(dataframe, "Symbol")
    result["security_name"] = get_column(dataframe, "Security Name")

    result["exchange"] = "NASDAQ"
    result["exchange_code"] = "Q"

    result["market_category"] = get_column(
        dataframe,
        "Market Category",
    )

    result["financial_status"] = get_column(
        dataframe,
        "Financial Status",
    )

    result["is_etf"] = (
        get_column(dataframe, "ETF")
        .str.upper()
        .eq("Y")
    )

    result["is_test_issue"] = (
        get_column(dataframe, "Test Issue")
        .str.upper()
        .eq("Y")
    )

    # 파일 버전에 따라 Round Lot Size 또는 Round Lot으로 존재할 수 있다.
    round_lot = get_column(dataframe, "Round Lot Size")

    if round_lot.eq("").all():
        round_lot = get_column(dataframe, "Round Lot")

    result["round_lot_size"] = pd.to_numeric(
        round_lot,
        errors="coerce",
    ).astype("Int64")

    result["cqs_symbol"] = ""
    result["nasdaq_symbol"] = result["symbol"]
    result["source"] = "nasdaqlisted.txt"
    result["source_creation_time"] = creation_time or ""

    return result


def standardize_other_listed(
    dataframe: pd.DataFrame,
    creation_time: str | None,
) -> pd.DataFrame:
    exchange_names = {
        "A": "NYSE American",
        "N": "NYSE",
        "P": "NYSE Arca",
        "Z": "Cboe BZX",
        "V": "IEX",
    }

    exchange_code = get_column(dataframe, "Exchange")

    result = pd.DataFrame(index=dataframe.index)

    result["symbol"] = get_column(dataframe, "ACT Symbol")
    result["security_name"] = get_column(dataframe, "Security Name")

    result["exchange_code"] = exchange_code
    result["exchange"] = exchange_code.map(exchange_names).fillna(
        "OTHER"
    )

    result["market_category"] = ""
    result["financial_status"] = ""

    result["is_etf"] = (
        get_column(dataframe, "ETF")
        .str.upper()
        .eq("Y")
    )

    result["is_test_issue"] = (
        get_column(dataframe, "Test Issue")
        .str.upper()
        .eq("Y")
    )

    result["round_lot_size"] = pd.to_numeric(
        get_column(dataframe, "Round Lot Size"),
        errors="coerce",
    ).astype("Int64")

    result["cqs_symbol"] = get_column(
        dataframe,
        "CQS Symbol",
    )

    result["nasdaq_symbol"] = get_column(
        dataframe,
        "NASDAQ Symbol",
    )

    result["source"] = "otherlisted.txt"
    result["source_creation_time"] = creation_time or ""

    return result


def classify_probable_individual_stock(
    dataframe: pd.DataFrame,
) -> pd.Series:
    """
    Nasdaq Symbol Directory에는 완전한 security type 필드가 없으므로
    ETF 플래그와 Security Name을 이용해 개별주식을 추정한다.

    남기는 대상:
    - Common Stock
    - Ordinary Shares
    - ADR / ADS
    - REIT
    - MLP Common Units
    - SPAC Common Stock
    - Class A / Class B 주식

    제외 대상:
    - ETF / ETN
    - 우선주
    - 워런트
    - Rights
    - SPAC Unit
    - 채권 / Note / Debenture
    - 일반 펀드 / Closed-end Fund
    """
    name = (
        dataframe["security_name"]
        .fillna("")
        .astype(str)
        .str.upper()
    )

    symbol = (
        dataframe["symbol"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    excluded_name_pattern = re.compile(
        r"\b(?:"
        r"WARRANTS?|"
        r"RIGHTS?|"
        r"PREFERRED|PREFERENCE|PREF\.?|"
        r"DEBENTURES?|"
        r"BONDS?|"
        r"NOTES?|"
        r"ETNS?|"
        r"EXCHANGE[- ]TRADED|"
        r"CLOSED[- ]END FUND|"
        r"MUTUAL FUND|"
        r"INCOME FUND|"
        r"MONEY MARKET|"
        r"SUBSCRIPTION RIGHTS?|"
        r"CONTINGENT VALUE RIGHTS?|"
        r"CVRS?"
        r")\b",
        flags=re.IGNORECASE,
    )

    excluded_by_name = name.str.contains(
        excluded_name_pattern,
        regex=True,
        na=False,
    )

    # SPAC Unit 등은 제외하되 MLP의 Common Units는 유지한다.
    contains_unit = name.str.contains(
        r"\bUNITS?\b",
        regex=True,
        na=False,
    )

    contains_common_unit = name.str.contains(
        r"\bCOMMON UNITS?\b",
        regex=True,
        na=False,
    )

    excluded_non_common_unit = (
        contains_unit
        & ~contains_common_unit
    )

    valid_symbol = (
        symbol.ne("")
        & ~symbol.str.contains(
            r"[\s/]",
            regex=True,
            na=False,
        )
    )

    return (
        valid_symbol
        & ~dataframe["is_etf"].fillna(False)
        & ~dataframe["is_test_issue"].fillna(False)
        & ~excluded_by_name
        & ~excluded_non_common_unit
    )


def to_yahoo_finance_symbol(symbol: str) -> str:
    """
    Yahoo Finance에서는 BRK.B 대신 BRK-B와 같은 형식을 사용한다.

    주의:
    데이터 공급자마다 티커 표기 규칙이 다르므로 원본 symbol은
    반드시 별도로 보존하는 것이 좋다.
    """
    return symbol.replace(".", "-")


def get_all_us_tickers() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    반환값:
        all_securities:
            ETF, 우선주, 워런트 등을 포함한 미국 거래소 상장 증권 전체

        individual_stocks:
            일반 개별주식으로 추정되는 종목
    """
    session = create_http_session()

    nasdaq_raw, nasdaq_creation_time = download_symbol_file(
        session,
        NASDAQ_LISTED_URL,
    )

    # 서버 부하를 피하기 위한 짧은 간격
    time.sleep(0.3)

    other_raw, other_creation_time = download_symbol_file(
        session,
        OTHER_LISTED_URL,
    )

    nasdaq = standardize_nasdaq_listed(
        nasdaq_raw,
        nasdaq_creation_time,
    )

    other = standardize_other_listed(
        other_raw,
        other_creation_time,
    )

    all_securities = pd.concat(
        [nasdaq, other],
        ignore_index=True,
    )

    all_securities["symbol"] = (
        all_securities["symbol"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    all_securities["security_name"] = (
        all_securities["security_name"]
        .astype(str)
        .str.strip()
    )

    # 같은 심볼이 중복될 경우 원본 행을 하나만 유지한다.
    all_securities = (
        all_securities
        .drop_duplicates(
            subset=["symbol", "exchange"],
            keep="last",
        )
        .sort_values(
            ["exchange", "symbol"],
            kind="stable",
        )
        .reset_index(drop=True)
    )

    all_securities["is_probable_individual_stock"] = (
        classify_probable_individual_stock(all_securities)
    )

    # 데이터 공급자별 심볼 표기를 별도 컬럼으로 저장
    all_securities["yahoo_symbol"] = (
        all_securities["symbol"]
        .map(to_yahoo_finance_symbol)
    )

    individual_stocks = (
        all_securities.loc[
            all_securities["is_probable_individual_stock"]
        ]
        .copy()
        .reset_index(drop=True)
    )

    return all_securities, individual_stocks


def main() -> None:
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ALPHA_VANTAGE_API_KEY environment variable is required")
    output_directory = Path("./output")
    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    all_securities, individual_stocks = get_all_us_tickers()

    all_securities_path = (
        output_directory
        / "us_exchange_listed_securities.csv"
    )

    individual_stocks_path = (
        output_directory
        / "us_individual_stocks.csv"
    )

    all_securities.to_csv(
        all_securities_path,
        index=False,
        encoding="utf-8-sig",
    )

    individual_stocks.to_csv(
        individual_stocks_path,
        index=False,
        encoding="utf-8-sig",
    )

    print(f"전체 상장 증권: {len(all_securities):,}개")
    print(f"추정 개별주식: {len(individual_stocks):,}개")
    print()

    print("거래소별 개별주식 수")
    print(
        individual_stocks["exchange"]
        .value_counts()
        .sort_index()
        .to_string()
    )

    print()
    print(f"전체 파일: {all_securities_path.resolve()}")
    print(f"주식 파일: {individual_stocks_path.resolve()}")

    print()
    print("샘플:")
    print(
        individual_stocks[
            [
                "symbol",
                "security_name",
                "exchange",
                "yahoo_symbol",
            ]
        ]
        .head(20)
        .to_string(index=False)
    )

    _ = api_key  # The collector reads the same environment variable; never pass or log the value.
    counts = download_us_consensus(
        symbols=individual_stocks["yahoo_symbol"].to_list(),
        sources=["alpha-vantage"],
        alpha_max_calls_per_minute=75,
    )
    print(
        "Alpha Vantage consensus complete: "
        f"written={counts['written']:,}, skipped={counts['skipped']:,}, failed={counts['failed']:,}"
    )


if __name__ == "__main__":
    main()
