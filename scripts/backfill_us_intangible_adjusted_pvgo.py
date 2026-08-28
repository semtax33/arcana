from __future__ import annotations

"""Backfill U.S. intangible-adjusted PVGO factors on FactorLab signal dates.

Financial statement factors are calculated locally from point-in-time SEC
files. Daily prices are retained inside each calculation window so the
252-trading-day compression measure remains genuine, but only the quarterly
signal dates (plus 2016-01-04 and 2026-08-27) are persisted. The same rows are
then copied to the point-in-time snapshot table with source_trade_date equal
to trade_date.
"""

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import date, timedelta
import json
import os
import time
from pathlib import Path
import warnings

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.loaders._internal.clickhouse_factors import insert_factor_catalog
from engine.transformers.factors import read_ttm_financials
from scripts.backfill_us_pvgo_factors import _execute, _validate_date, _year_chunks


START_DATE = "2016-01-04"
END_DATE = "2026-08-27"
FINANCIAL_STAGE = "arcana_tmp_us_intangible_financial_v1"
VALUATION_STAGE = "arcana_tmp_us_intangible_valuation_v1"
STATE_DIR = Path(__file__).resolve().parents[1] / ".codex-tmp" / "us-intangible-pvgo"

TARGET_DATES = (
    "2016-01-04",
    "2016-03-31",
    "2016-06-30",
    "2016-09-30",
    "2016-12-30",
    "2017-03-31",
    "2017-06-30",
    "2017-09-29",
    "2017-12-29",
    "2018-03-29",
    "2018-06-29",
    "2018-09-28",
    "2018-12-31",
    "2019-03-29",
    "2019-06-28",
    "2019-09-30",
    "2019-12-31",
    "2020-03-31",
    "2020-06-30",
    "2020-09-30",
    "2020-12-31",
    "2021-03-31",
    "2021-06-30",
    "2021-09-30",
    "2021-12-31",
    "2022-03-31",
    "2022-06-30",
    "2022-09-30",
    "2022-12-30",
    "2023-03-31",
    "2023-06-30",
    "2023-09-29",
    "2023-12-29",
    "2024-03-28",
    "2024-06-28",
    "2024-09-30",
    "2024-12-31",
    "2025-03-31",
    "2025-06-30",
    "2025-09-30",
    "2025-12-31",
    "2026-03-31",
    "2026-06-30",
    "2026-08-27",
)

FINANCIAL_FACTOR_IDS = (
    "knowledge_capital",
    "organization_capital",
    "intangible_capital",
    "intangible_investment",
    "intangible_amortization",
    "net_intangible_investment",
    "intangible_adjusted_net_income",
    "normalized_intangible_adjusted_earnings_5y",
    "intangible_adjusted_eps",
    "normalized_intangible_adjusted_eps",
    "intangible_adjusted_equity",
    "avg_intangible_adjusted_equity",
    "intangible_adjusted_roe_pct",
    "iroe",
)
DAILY_FACTOR_IDS = (
    "intangible_adjusted_roe_spread_pct",
    "intangible_adjusted_pvgo_pct",
    "normalized_intangible_adjusted_pvgo_pct",
    "intangible_adjusted_pvgo_gap_pct",
    "intangible_adjusted_pvgo_compression_pct",
    "intangible_adjusted_pvgo_change_1y_pctp",
)
FACTOR_IDS = FINANCIAL_FACTOR_IDS + DAILY_FACTOR_IDS

FINANCIAL_COLUMNS = (
    "security_id",
    "effective_date",
    "financial_period",
    "fiscal_year",
    "currency",
) + FINANCIAL_FACTOR_IDS


def _financial_stage_ddl() -> str:
    value_columns = ",\n    ".join(
        f"{column} Nullable(Float64)" for column in FINANCIAL_FACTOR_IDS
    )
    return f"""
CREATE TABLE {FINANCIAL_STAGE}
(
    security_id String,
    effective_date Date,
    financial_period Date,
    fiscal_year Nullable(UInt16),
    currency LowCardinality(String),
    {value_columns}
)
ENGINE = MergeTree
ORDER BY (security_id, effective_date, financial_period)
""".strip()


def _valuation_stage_ddl() -> str:
    return f"""
CREATE TABLE {VALUATION_STAGE}
(
    security_id String,
    effective_date Date,
    cost_of_equity Nullable(Float64),
    justified_pvgo_pct Nullable(Float64)
)
ENGINE = MergeTree
ORDER BY (security_id, effective_date)
""".strip()


def _valuation_stage_query(start_date: str, end_date: str) -> str:
    return f"""
INSERT INTO {VALUATION_STAGE}
SELECT
    security_id,
    trade_date AS effective_date,
    argMaxIf(
        factor_value,
        updated_at,
        factor_id = 'cost_of_equity' AND isFinite(factor_value)
    ) / 100 AS cost_of_equity,
    if(
        countIf(factor_id = 'justified_pvgo_pct' AND isFinite(factor_value)) > 0,
        argMaxIf(
            factor_value,
            updated_at,
            factor_id = 'justified_pvgo_pct' AND isFinite(factor_value)
        ),
        NULL
    ) AS justified_pvgo_pct
FROM fact_daily_factors
PREWHERE trade_date >= toDate('{start_date}')
    AND trade_date <= toDate('{end_date}')
    AND financial_basis = 'ttm'
    AND factor_id IN ('cost_of_equity', 'justified_pvgo_pct')
    AND startsWith(security_id, 'SEC_US_')
GROUP BY security_id, trade_date
HAVING cost_of_equity > 0 AND cost_of_equity < 1
""".strip()


def _symbols(client, start_date: str, end_date: str, stock_codes: str | None) -> list[str]:
    if stock_codes:
        candidates = {
            value.strip().upper()
            for value in stock_codes.split(",")
            if value.strip()
        }
    else:
        rows = client.query(
            f"""
SELECT DISTINCT substring(security_id, 8)
FROM price_daily
WHERE startsWith(security_id, 'SEC_US_')
    AND trade_date >= toDate('{start_date}')
    AND trade_date <= toDate('{end_date}')
ORDER BY substring(security_id, 8)
""".strip()
        ).result_rows
        candidates = {str(row[0]) for row in rows}
    financial_dir = DATA_LAKE.silver("sec", "normalized")
    return sorted(
        symbol
        for symbol in candidates
        if (financial_dir / f"us_normalized_{symbol}.csv").exists()
    )


def _chunks(values: list[str], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _financial_rows_for_batch(symbols: list[str], end_date: str) -> pd.DataFrame:
    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
    metadata_path = DATA_LAKE.silver("sec", "us_report_metadata.csv")
    frames = []
    for symbol in symbols:
        try:
            frame = read_ttm_financials(
                symbol,
                market="us",
                report_metadata_path=metadata_path,
                use_edgartools=False,
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if frame.empty:
            continue
        frame = frame.copy()
        frame["financial_period"] = pd.to_datetime(
            frame.get("financial_period"), errors="coerce"
        )
        frame["effective_date"] = pd.to_datetime(
            frame.get("report_date"), errors="coerce"
        ).fillna(frame["financial_period"])
        frame = frame.loc[
            frame["effective_date"].notna()
            & frame["financial_period"].notna()
            & (frame["effective_date"] <= pd.Timestamp(end_date))
        ].copy()
        if frame.empty:
            continue
        frame["currency"] = "USD"
        for column in FINANCIAL_COLUMNS:
            if column not in frame.columns:
                frame[column] = pd.NA
        frames.append(frame[list(FINANCIAL_COLUMNS)])
    if not frames:
        return pd.DataFrame(columns=FINANCIAL_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def _insert_financial_stage(
    client,
    symbols: list[str],
    *,
    end_date: str,
    workers: int,
    symbol_batch_size: int,
) -> int:
    batches = list(_chunks(symbols, max(1, symbol_batch_size)))
    workers = max(1, workers)
    pending = set()
    batch_iter = iter(enumerate(batches, start=1))
    processed = 0
    inserted = 0
    started = time.monotonic()

    def submit_next(executor) -> bool:
        try:
            batch_index, batch = next(batch_iter)
        except StopIteration:
            return False
        future = executor.submit(_financial_rows_for_batch, batch, end_date)
        future.batch_index = batch_index
        future.symbol_count = len(batch)
        pending.add(future)
        return True

    with ProcessPoolExecutor(max_workers=workers) as executor:
        for _ in range(min(len(batches), workers * 2)):
            submit_next(executor)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                frame = future.result()
                processed += future.symbol_count
                if not frame.empty:
                    frame["effective_date"] = pd.to_datetime(
                        frame["effective_date"], errors="coerce"
                    ).dt.date
                    frame["financial_period"] = pd.to_datetime(
                        frame["financial_period"], errors="coerce"
                    ).dt.date
                    fiscal_year = pd.to_numeric(
                        frame["fiscal_year"], errors="coerce"
                    ).astype("Int64")
                    frame["fiscal_year"] = fiscal_year.astype("object").where(
                        fiscal_year.notna(), None
                    )
                    client.insert_df(
                        FINANCIAL_STAGE,
                        frame,
                        column_names=list(FINANCIAL_COLUMNS),
                    )
                    inserted += len(frame)
                if processed == len(symbols) or processed % 100 <= future.symbol_count:
                    print(
                        f"[INTANGIBLE-PVGO] financial symbols={processed:,}/{len(symbols):,} "
                        f"rows={inserted:,} elapsed={(time.monotonic() - started) / 60:.1f}m",
                        flush=True,
                    )
                submit_next(executor)
    return inserted


def _target_dates_sql(start_date: str, end_date: str) -> str:
    selected = [
        value for value in TARGET_DATES if start_date <= value <= end_date
    ]
    return ", ".join(f"toDate('{value}')" for value in selected)


def _daily_factor_query(
    *,
    calculation_start_date: str,
    output_start_date: str,
    output_end_date: str,
) -> str:
    factor_ids_sql = ", ".join(f"'{factor_id}'" for factor_id in FACTOR_IDS)
    financial_values_sql = ",\n            ".join(
        f"toNullable({factor_id})" for factor_id in FINANCIAL_FACTOR_IDS
    )
    financial_select_sql = ",\n        ".join(
        f"f.{factor_id} AS {factor_id}" for factor_id in FINANCIAL_FACTOR_IDS
    )
    target_dates_sql = _target_dates_sql(output_start_date, output_end_date)
    return f"""
INSERT INTO fact_daily_factors
(
    security_id, trade_date, factor_id, financial_basis, factor_value,
    fiscal_year, financial_period, currency, updated_at
)
WITH
price_rows AS
(
    SELECT
        security_id,
        trade_date,
        argMax(toFloat64(close), updated_at) AS close_price
    FROM price_daily
    PREWHERE trade_date >= toDate('{calculation_start_date}')
        AND trade_date <= toDate('{output_end_date}')
        AND startsWith(security_id, 'SEC_US_')
    WHERE toFloat64(close) > 0
    GROUP BY security_id, trade_date
    ORDER BY security_id, trade_date
),
share_rows AS
(
    SELECT
        security_id,
        trade_date AS effective_date,
        argMax(toFloat64(shares), updated_at) AS shares_value
    FROM stock_shares
    WHERE startsWith(security_id, 'SEC_US_')
        AND trade_date <= toDate('{output_end_date}')
        AND toFloat64(shares) > 0
    GROUP BY security_id, trade_date
    ORDER BY security_id, effective_date
),
financial_rows AS
(
    SELECT *
    FROM {FINANCIAL_STAGE}
    WHERE effective_date <= toDate('{output_end_date}')
    ORDER BY security_id, effective_date
),
valuation_rows AS
(
    SELECT *
    FROM {VALUATION_STAGE}
    WHERE effective_date <= toDate('{output_end_date}')
    ORDER BY security_id, effective_date
),
daily_inputs AS
(
    SELECT
        p.security_id AS security_id,
        p.trade_date AS trade_date,
        f.financial_period AS financial_period,
        f.fiscal_year AS fiscal_year,
        p.close_price * s.shares_value AS market_cap,
        {financial_select_sql},
        v.cost_of_equity AS cost_of_equity,
        v.justified_pvgo_pct AS justified_pvgo_pct
    FROM price_rows AS p
    ASOF LEFT JOIN share_rows AS s
        ON p.security_id = s.security_id
        AND p.trade_date >= s.effective_date
    ASOF LEFT JOIN financial_rows AS f
        ON p.security_id = f.security_id
        AND p.trade_date >= f.effective_date
    ASOF LEFT JOIN valuation_rows AS v
        ON p.security_id = v.security_id
        AND p.trade_date >= v.effective_date
),
pvgo_rows AS
(
    SELECT
        *,
        if(
            market_cap > 0 AND cost_of_equity > 0 AND cost_of_equity < 1,
            intangible_adjusted_net_income / cost_of_equity,
            NULL
        ) AS adjusted_steady_state_equity,
        if(
            market_cap > 0 AND cost_of_equity > 0 AND cost_of_equity < 1,
            normalized_intangible_adjusted_earnings_5y / cost_of_equity,
            NULL
        ) AS normalized_adjusted_steady_state_equity,
        if(
            market_cap > 0 AND cost_of_equity > 0 AND cost_of_equity < 1,
            (market_cap - intangible_adjusted_net_income / cost_of_equity)
                / market_cap * 100,
            NULL
        ) AS intangible_adjusted_pvgo_pct,
        if(
            market_cap > 0 AND cost_of_equity > 0 AND cost_of_equity < 1,
            (
                market_cap
                - normalized_intangible_adjusted_earnings_5y / cost_of_equity
            ) / market_cap * 100,
            NULL
        ) AS normalized_intangible_adjusted_pvgo_pct,
        if(
            cost_of_equity > 0 AND cost_of_equity < 1,
            intangible_adjusted_roe_pct - cost_of_equity * 100,
            NULL
        ) AS intangible_adjusted_roe_spread_pct
    FROM daily_inputs
),
lagged_rows AS
(
    SELECT
        *,
        lagInFrame(normalized_adjusted_steady_state_equity, 252)
            OVER daily_window AS prior_adjusted_steady_state_equity,
        lagInFrame(market_cap, 252) OVER daily_window AS prior_market_cap,
        lagInFrame(normalized_intangible_adjusted_pvgo_pct, 252)
            OVER daily_window AS prior_adjusted_pvgo_pct
    FROM pvgo_rows
    WINDOW daily_window AS
    (
        PARTITION BY security_id
        ORDER BY trade_date
        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
    )
),
factor_rows AS
(
    SELECT
        security_id,
        trade_date,
        fiscal_year,
        financial_period,
        [{factor_ids_sql}] AS factor_ids,
        [
            {financial_values_sql},
            toNullable(intangible_adjusted_roe_spread_pct),
            toNullable(intangible_adjusted_pvgo_pct),
            toNullable(normalized_intangible_adjusted_pvgo_pct),
            toNullable(justified_pvgo_pct - normalized_intangible_adjusted_pvgo_pct),
            toNullable(
                if(
                    normalized_adjusted_steady_state_equity > 0
                        AND prior_adjusted_steady_state_equity > 0
                        AND prior_market_cap > 0,
                    (
                        normalized_adjusted_steady_state_equity
                        - prior_adjusted_steady_state_equity
                    ) / abs(prior_adjusted_steady_state_equity) * 100
                    - (market_cap - prior_market_cap) / abs(prior_market_cap) * 100,
                    NULL
                )
            ),
            toNullable(
                normalized_intangible_adjusted_pvgo_pct
                - prior_adjusted_pvgo_pct
            )
        ] AS factor_values
    FROM lagged_rows
)
SELECT
    security_id,
    trade_date,
    factor_tuple.1 AS factor_id,
    'ttm' AS financial_basis,
    factor_tuple.2 AS factor_value,
    fiscal_year,
    financial_period,
    'USD' AS currency,
    now64(3, 'Asia/Seoul') AS updated_at
FROM factor_rows
ARRAY JOIN arrayZip(factor_ids, factor_values) AS factor_tuple
WHERE has([{target_dates_sql}], trade_date)
    AND (factor_tuple.2 IS NULL OR isFinite(factor_tuple.2))
""".strip()


def _load_snapshots(client, start_date: str, end_date: str) -> None:
    target_dates = [
        date.fromisoformat(value)
        for value in TARGET_DATES
        if start_date <= value <= end_date
    ]
    client.command(
        """
INSERT INTO fact_daily_factor_snapshot
    (trade_date, security_id, factor_id, financial_basis, factor_value,
     source_trade_date, fiscal_year, financial_period, currency, updated_at)
WITH
source AS
(
    SELECT trade_date, security_id, factor_id, financial_basis, factor_value,
           fiscal_year, financial_period, currency
    FROM fact_daily_factors FINAL
    PREWHERE has({days:Array(Date)}, trade_date)
    WHERE startsWith(security_id, 'SEC_US_')
      AND financial_basis = 'ttm'
      AND has({factor_ids:Array(String)}, factor_id)
),
existing AS
(
    SELECT trade_date, security_id, factor_id, financial_basis, updated_at
    FROM fact_daily_factor_snapshot FINAL
    PREWHERE has({days:Array(Date)}, trade_date)
    WHERE startsWith(security_id, 'SEC_US_')
      AND financial_basis = 'ttm'
      AND has({factor_ids:Array(String)}, factor_id)
)
SELECT s.trade_date, s.security_id, s.factor_id, s.financial_basis, s.factor_value,
       s.trade_date, s.fiscal_year, s.financial_period, s.currency,
       greatest(
           now64(3, 'Asia/Seoul'),
           coalesce(
               e.updated_at + toIntervalMillisecond(1),
               now64(3, 'Asia/Seoul')
           )
       )
FROM source AS s
LEFT JOIN existing AS e
    ON s.trade_date = e.trade_date
    AND s.security_id = e.security_id
    AND s.factor_id = e.factor_id
    AND s.financial_basis = e.financial_basis
SETTINGS max_threads = 8
""".strip(),
        parameters={"days": target_dates, "factor_ids": list(FACTOR_IDS)},
    )


def _replace_legacy_iroe(client, start_date: str, end_date: str) -> None:
    """Make the legacy IROE alias exactly match the corrected adjusted ROE."""

    target_dates = [
        date.fromisoformat(value)
        for value in TARGET_DATES
        if start_date <= value <= end_date
    ]
    client.command(
        """
INSERT INTO fact_daily_factors
    (security_id, trade_date, factor_id, financial_basis, factor_value,
     fiscal_year, financial_period, currency, updated_at)
WITH
price_keys AS
(
    SELECT security_id, trade_date
    FROM price_daily
    PREWHERE has({days:Array(Date)}, trade_date)
    WHERE startsWith(security_id, 'SEC_US_')
    GROUP BY security_id, trade_date
),
adjusted AS
(
    SELECT security_id, trade_date, factor_value, fiscal_year,
           financial_period, currency
    FROM fact_daily_factors FINAL
    PREWHERE has({days:Array(Date)}, trade_date)
    WHERE startsWith(security_id, 'SEC_US_')
      AND financial_basis = 'ttm'
      AND factor_id = 'intangible_adjusted_roe_pct'
),
legacy AS
(
    SELECT security_id, trade_date, updated_at
    FROM fact_daily_factors FINAL
    PREWHERE has({days:Array(Date)}, trade_date)
    WHERE startsWith(security_id, 'SEC_US_')
      AND financial_basis = 'ttm'
      AND factor_id = 'iroe'
)
SELECT
    p.security_id,
    p.trade_date,
    'iroe',
    'ttm',
    a.factor_value,
    a.fiscal_year,
    a.financial_period,
    if(empty(a.currency), 'USD', a.currency),
    greatest(
        now64(3, 'Asia/Seoul'),
        coalesce(
            l.updated_at + toIntervalMillisecond(1),
            now64(3, 'Asia/Seoul')
        )
    )
FROM price_keys AS p
LEFT JOIN adjusted AS a
    ON p.security_id = a.security_id
    AND p.trade_date = a.trade_date
LEFT JOIN legacy AS l
    ON p.security_id = l.security_id
    AND p.trade_date = l.trade_date
SETTINGS max_threads = 8
""".strip(),
        parameters={"days": target_dates},
    )


def _coverage(client, table: str, start_date: str, end_date: str) -> list[dict]:
    target_dates = [
        date.fromisoformat(value)
        for value in TARGET_DATES
        if start_date <= value <= end_date
    ]
    rows = client.query(
        f"""
SELECT trade_date, factor_id, uniqExact(security_id) AS security_count
FROM {table} FINAL
PREWHERE has({{days:Array(Date)}}, trade_date)
WHERE startsWith(security_id, 'SEC_US_')
  AND financial_basis = 'ttm'
  AND has({{factor_ids:Array(String)}}, factor_id)
  AND factor_value IS NOT NULL
  {"AND source_trade_date <= trade_date" if table.endswith("snapshot") else ""}
GROUP BY trade_date, factor_id
ORDER BY trade_date, factor_id
""".strip(),
        parameters={"days": target_dates, "factor_ids": list(FACTOR_IDS)},
    ).result_rows
    counts = {(str(day), str(factor_id)): int(count) for day, factor_id, count in rows}
    result = []
    for day in target_dates:
        values = {factor_id: counts.get((str(day), factor_id), 0) for factor_id in FACTOR_IDS}
        result.append({"trade_date": str(day), "factor_counts": values})
    return result


def _verify(client, start_date: str, end_date: str) -> dict:
    result = {
        "start_date": start_date,
        "end_date": end_date,
        "target_dates": [
            value for value in TARGET_DATES if start_date <= value <= end_date
        ],
        "factor_ids": list(FACTOR_IDS),
        "raw": _coverage(client, "fact_daily_factors", start_date, end_date),
        "snapshot": _coverage(
            client,
            "fact_daily_factor_snapshot",
            start_date,
            end_date,
        ),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "coverage.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("run", "snapshots", "verify"), nargs="?", default="run")
    parser.add_argument("--start-date", default=START_DATE)
    parser.add_argument("--end-date", default=END_DATE)
    parser.add_argument("--stock-codes")
    parser.add_argument("--workers", type=int, default=max(1, min(16, os.cpu_count() or 1)))
    parser.add_argument("--symbol-batch-size", type=int, default=20)
    parser.add_argument("--keep-stage", action="store_true")
    args = parser.parse_args()

    start_date = _validate_date(args.start_date)
    end_date = _validate_date(args.end_date)
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    calculation_floor = START_DATE

    client = get_clickhouse_client()
    try:
        insert_factor_catalog(client, factor_ids=list(FACTOR_IDS))
        if args.stage == "snapshots":
            _load_snapshots(client, start_date, end_date)
            print("[INTANGIBLE-PVGO] snapshots complete", flush=True)
            return
        if args.stage == "verify":
            print(json.dumps(_verify(client, start_date, end_date), ensure_ascii=False, indent=2))
            return

        symbols = _symbols(client, calculation_floor, end_date, args.stock_codes)
        print(f"[INTANGIBLE-PVGO] symbols={len(symbols):,}", flush=True)
        _execute(client, f"DROP TABLE IF EXISTS {FINANCIAL_STAGE}")
        _execute(client, f"DROP TABLE IF EXISTS {VALUATION_STAGE}")
        _execute(client, _financial_stage_ddl())
        _execute(client, _valuation_stage_ddl())
        _insert_financial_stage(
            client,
            symbols,
            end_date=end_date,
            workers=args.workers,
            symbol_batch_size=args.symbol_batch_size,
        )
        print("[INTANGIBLE-PVGO] staging cost of equity and justified PVGO", flush=True)
        _execute(client, _valuation_stage_query(calculation_floor, end_date))

        chunks = _year_chunks(start_date, end_date)
        for chunk_index, (chunk_start, chunk_end) in enumerate(chunks, start=1):
            if not any(chunk_start <= value <= chunk_end for value in TARGET_DATES):
                continue
            calculation_start = max(
                date.fromisoformat(calculation_floor),
                date.fromisoformat(chunk_start) - timedelta(days=400),
            ).isoformat()
            print(
                f"[INTANGIBLE-PVGO] factors chunk={chunk_index}/{len(chunks)} "
                f"dates={chunk_start}..{chunk_end}",
                flush=True,
            )
            _execute(
                client,
                _daily_factor_query(
                    calculation_start_date=calculation_start,
                    output_start_date=chunk_start,
                    output_end_date=chunk_end,
                ),
            )
        _replace_legacy_iroe(client, start_date, end_date)
        _load_snapshots(client, start_date, end_date)
        result = _verify(client, start_date, end_date)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    finally:
        if args.stage == "run" and not args.keep_stage:
            _execute(client, f"DROP TABLE IF EXISTS {FINANCIAL_STAGE}")
            _execute(client, f"DROP TABLE IF EXISTS {VALUATION_STAGE}")
        client.close()


if __name__ == "__main__":
    main()
