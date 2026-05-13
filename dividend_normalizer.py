import json
import math
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
base_dir = PROJECT_ROOT / "data-lake" / "silver" / "dart" / "normalized"
dividend_base_dir = PROJECT_ROOT / "data-lake" / "bronze" / "dart" / "dividend"
price_file_path = PROJECT_ROOT / "data-lake" / "silver" / "krx" / "price" / "normalized_price.csv"
krx_price_file_path = PROJECT_ROOT / "data-lake" / "silver" / "krx" / "price" / "normalized_price.csv"


def normalize_stock_code(stock_code):
    return str(stock_code).strip().zfill(6)


def calculate_net_income(stock_code, year, month):
    import pandas as pd

    stock_code = normalize_stock_code(stock_code)
    file_name = f"normalized_{stock_code}_{year}.{month:02d}.csv"
    file_path = base_dir / file_name

    if not file_path.exists():
        print(f"[SKIP] 파일 없음: {file_path}")
        return None
    
    statement_df = pd.read_csv(file_path)
    net_income_matched = statement_df.loc[statement_df["canonical_account_id"] == "NET_INCOME", "normalized_amount"]
    if not net_income_matched.empty:
        net_income = net_income_matched.iloc[0]
    else:
        net_income = None
    
    return net_income


def normalize_dividend_amount(amount):
    if amount is None:
        return 0

    amount_text = str(amount).strip()
    if not amount_text or amount_text == "-":
        return 0

    normalized = re.sub(r"[^0-9]", "", amount_text)
    if not normalized:
        return 0

    return int(normalized)


def normalize_numeric_amount(amount):
    if amount is None:
        return None

    if isinstance(amount, float) and math.isnan(amount):
        return None

    amount_text = str(amount).strip()
    if not amount_text or amount_text == "-":
        return None

    normalized = re.sub(r"[^0-9.-]", "", amount_text)
    if not normalized or normalized in {"-", ".", "-."}:
        return None

    return int(float(normalized))


def calculate_total_dividend_amount(stock_code, year):
    stock_code = normalize_stock_code(stock_code)
    stock_dividend_dir = dividend_base_dir / stock_code

    if not stock_dividend_dir.exists():
        print(f"[SKIP] 파일 없음: {stock_dividend_dir}")
        return None

    total_dividend_amount = 0
    target_year = str(year)

    for file_path in stock_dividend_dir.glob("finance_statement_dividend_*.json"):
        with file_path.open("r", encoding="utf-8") as f:
            dividend_data = json.load(f)

        dividend_base_date = str(dividend_data.get("배당기준일", ""))
        if not dividend_base_date.startswith(target_year):
            continue

        total_dividend_amount += normalize_dividend_amount(
            dividend_data.get("배당금총액")
        )

    return total_dividend_amount


def calculate_payout_ratio(stock_code, year):
    total_dividend_amount = calculate_total_dividend_amount(stock_code, year)
    net_income = normalize_numeric_amount(calculate_net_income(stock_code, year, 12))

    if total_dividend_amount is None or net_income is None:
        return None

    if net_income == 0:
        return None

    return total_dividend_amount / net_income


def get_dividend_per_share_records(stock_code, year, share_type="보통주식"):
    stock_code = normalize_stock_code(stock_code)
    stock_dividend_dir = dividend_base_dir / stock_code

    if not stock_dividend_dir.exists():
        print(f"[SKIP] 파일 없음: {stock_dividend_dir}")
        return []

    records = []
    target_year = str(year)

    for file_path in stock_dividend_dir.glob("finance_statement_dividend_*.json"):
        with file_path.open("r", encoding="utf-8") as f:
            dividend_data = json.load(f)

        dividend_base_date = str(dividend_data.get("배당기준일", ""))
        if not dividend_base_date.startswith(target_year):
            continue

        dividend_per_share_data = dividend_data.get("1주당배당금", {})
        if isinstance(dividend_per_share_data, dict):
            dividend_per_share = dividend_per_share_data.get(share_type)
        else:
            dividend_per_share = dividend_per_share_data

        records.append(
            {
                "stock_code": stock_code,
                "dividend_type": dividend_data.get("배당구분"),
                "dividend_per_share": normalize_dividend_amount(dividend_per_share),
                "dividend_base_date": dividend_base_date,
                "dividend_payment_date": dividend_data.get("배당지급일"),
                "dividend_disclosure_date": dividend_data.get("배당공시일"),
                "source_file": file_path.name,
            }
        )

    return records


def calculate_total_dividend_per_share(stock_code, year, share_type="보통주식"):
    records = get_dividend_per_share_records(stock_code, year, share_type)
    return sum(record["dividend_per_share"] for record in records)


def find_latest_dividend_year(stock_code, year, share_type="보통주식", min_year=2015):
    for current_year in range(int(year), int(min_year) - 1, -1):
        records = get_dividend_per_share_records(stock_code, current_year, share_type)
        if records:
            return current_year

    return None


def calculate_total_dividend_per_share_with_fallback(
    stock_code,
    year,
    share_type="보통주식",
    min_year=2015,
):
    dividend_year = find_latest_dividend_year(stock_code, year, share_type, min_year)
    if dividend_year is None:
        return 0

    return calculate_total_dividend_per_share(stock_code, dividend_year, share_type)


def calculate_payout_ratio_with_fallback(
    stock_code,
    year,
    share_type="보통주식",
    min_year=2015,
):
    for current_year in range(int(year), int(min_year) - 1, -1):
        records = get_dividend_per_share_records(stock_code, current_year, share_type)
        if not records:
            continue

        payout_ratio = calculate_payout_ratio(stock_code, current_year)
        if payout_ratio is not None:
            return payout_ratio

    return None


def resolve_price_file_path(path=None):
    if path is not None:
        return Path(path)

    if price_file_path.exists():
        return price_file_path

    return krx_price_file_path


def get_daily_stock_prices(stock_code, path=None):
    import pandas as pd

    stock_code = normalize_stock_code(stock_code)
    security_id = f"SEC_KR_{stock_code}"
    resolved_path = resolve_price_file_path(path)

    if not resolved_path.exists():
        print(f"[SKIP] 파일 없음: {resolved_path}")
        return pd.DataFrame()

    price_df = pd.read_csv(resolved_path)
    price_df = price_df.drop(
        columns=[column for column in price_df.columns if column.startswith("Unnamed")],
        errors="ignore",
    )
    price_df = price_df.loc[price_df["security_id"] == security_id].copy()
    price_df["trade_date"] = pd.to_datetime(price_df["trade_date"])
    price_df["stock_code"] = stock_code

    return price_df.sort_values("trade_date").reset_index(drop=True)


def calculate_daily_dividend_yield(
    stock_code,
    year,
    share_type="보통주식",
    price_column="close",
    path=None,
):
    import pandas as pd

    total_dividend_per_share = calculate_total_dividend_per_share_with_fallback(
        stock_code,
        year,
        share_type,
    )
    price_df = get_daily_stock_prices(stock_code, path)

    if price_df.empty:
        return price_df

    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    daily_dividend_yield_df = price_df.loc[
        (price_df["trade_date"].dt.year == int(year))
        & (price_df["trade_date"].dt.date <= today)
    ].copy()

    if daily_dividend_yield_df.empty:
        return daily_dividend_yield_df

    daily_dividend_yield_df[price_column] = pd.to_numeric(
        daily_dividend_yield_df[price_column],
        errors="coerce",
    )
    daily_dividend_yield_df["annual_dividend_per_share"] = total_dividend_per_share
    daily_dividend_yield_df["dividend_yield"] = (
        daily_dividend_yield_df["annual_dividend_per_share"]
        / daily_dividend_yield_df[price_column]
    )
    daily_dividend_yield_df["dividend_yield_percent"] = (
        daily_dividend_yield_df["dividend_yield"] * 100
    )

    return daily_dividend_yield_df


def create_stock_dividend_dataframe(
    stock_code,
    year=None,
    share_type="보통주식",
    price_column="close",
    path=None,
):
    import pandas as pd

    stock_code = normalize_stock_code(stock_code)
    price_df = get_daily_stock_prices(stock_code, path)

    schema_columns = [
        "security_id",
        "trade_date",
        "dividend",
        "payout_ratio",
        "dividend_percent",
        "currency",
        "updated_at",
    ]

    if price_df.empty:
        return pd.DataFrame(columns=schema_columns)

    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    price_df = price_df.loc[price_df["trade_date"].dt.date <= today].copy()
    price_df[price_column] = pd.to_numeric(price_df[price_column], errors="coerce")
    price_df["year"] = price_df["trade_date"].dt.year

    if year is not None:
        price_df = price_df.loc[price_df["year"] == int(year)].copy()

    if price_df.empty:
        return pd.DataFrame(columns=schema_columns)

    years = sorted(price_df["year"].dropna().astype(int).unique())
    dividend_by_year = {
        current_year: calculate_total_dividend_per_share_with_fallback(
            stock_code,
            current_year,
            share_type,
        )
        for current_year in years
    }
    payout_ratio_by_year = {
        current_year: calculate_payout_ratio_with_fallback(
            stock_code,
            current_year,
            share_type,
        )
        for current_year in years
    }

    updated_at = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    result_df = price_df[["security_id", "trade_date", price_column, "currency", "year"]].copy()
    result_df["dividend"] = result_df["year"].map(dividend_by_year)
    result_df["payout_ratio"] = result_df["year"].map(payout_ratio_by_year)
    result_df["dividend_percent"] = (result_df["dividend"] / result_df[price_column]) * 100
    result_df["currency"] = "KRW"
    result_df["updated_at"] = updated_at

    result_df = result_df[schema_columns]
    return result_df.sort_values(["security_id", "trade_date"]).reset_index(drop=True)


def create_all_stock_dividend_dataframe(
    share_type="보통주식",
    price_column="close",
):
    from company import kospi_kosdaq_corp_list

    schema_columns = [
        "security_id",
        "trade_date",
        "dividend",
        "payout_ratio",
        "dividend_percent",
        "currency",
        "updated_at",
    ]
    corps_list = kospi_kosdaq_corp_list()
    stock_codes = sorted(
        normalize_stock_code(stock_code)
        for stock_code in corps_list["stock_code"].dropna().tolist()
    )
    resolved_path = resolve_price_file_path()

    if not resolved_path.exists():
        print(f"[SKIP] 파일 없음: {resolved_path}")
        return pd.DataFrame(columns=schema_columns)

    price_df = pd.read_csv(resolved_path)
    price_df = price_df.drop(
        columns=[column for column in price_df.columns if column.startswith("Unnamed")],
        errors="ignore",
    )
    price_df["trade_date"] = pd.to_datetime(price_df["trade_date"])
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    price_df = price_df.loc[price_df["trade_date"].dt.date <= today].copy()
    price_df[price_column] = pd.to_numeric(price_df[price_column], errors="coerce")
    price_df["year"] = price_df["trade_date"].dt.year

    updated_at = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    result_dfs = []

    for stock_code in stock_codes:
        security_id = f"SEC_KR_{stock_code}"
        stock_price_df = price_df.loc[price_df["security_id"] == security_id].copy()

        if stock_price_df.empty:
            continue

        for year in range(2015, today.year + 1):
            yearly_price_df = stock_price_df.loc[
                stock_price_df["year"] == year
            ].copy()

            if yearly_price_df.empty:
                continue

            dividend = calculate_total_dividend_per_share_with_fallback(
                stock_code,
                year,
                share_type,
            )
            payout_ratio = calculate_payout_ratio_with_fallback(
                stock_code,
                year,
                share_type,
            )

            yearly_result_df = yearly_price_df[[
                "security_id",
                "trade_date",
                price_column,
            ]].copy()
            yearly_result_df["dividend"] = dividend
            yearly_result_df["payout_ratio"] = payout_ratio
            yearly_result_df["dividend_percent"] = (
                yearly_result_df["dividend"] / yearly_result_df[price_column]
            ) * 100
            yearly_result_df["currency"] = "KRW"
            yearly_result_df["updated_at"] = updated_at
            yearly_result_df = yearly_result_df[schema_columns]
            result_dfs.append(yearly_result_df)

    if not result_dfs:
        return pd.DataFrame(columns=schema_columns)

    return (
        pd.concat(result_dfs, ignore_index=True)
        .sort_values(["security_id", "trade_date"])
        .reset_index(drop=True)
    )
