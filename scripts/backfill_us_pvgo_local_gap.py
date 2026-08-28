from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import date, timedelta
import os
import time

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.transformers.factors import read_annual_financials, read_ttm_financials
from scripts.backfill_us_pvgo_factors import (
    PVGO_FACTOR_IDS,
    _execute,
    _validate_date,
    _year_chunks,
)


FINANCIAL_STAGE = "arcana_tmp_us_pvgo_local_financial_v1"
WACC_STAGE = "arcana_tmp_us_pvgo_wacc_events_v1"
FINANCIAL_COLUMNS = [
    "security_id",
    "effective_date",
    "financial_period",
    "fiscal_year",
    "currency",
    "sale",
    "nopat",
    "net_debt",
    "normalized_operating_margin_5y",
    "normalized_nopat_5y",
    "normalized_earnings_5y",
    "normalized_nopat_growth_3y_pct",
    "incremental_investment_rate_pct",
    "roiic_pct",
]


def _financial_stage_ddl() -> str:
    return f"""
CREATE TABLE {FINANCIAL_STAGE}
(
    security_id String,
    effective_date Date,
    financial_period Date,
    fiscal_year Nullable(UInt16),
    currency LowCardinality(String),
    sale Nullable(Float64),
    nopat Nullable(Float64),
    net_debt Nullable(Float64),
    normalized_operating_margin_5y Nullable(Float64),
    normalized_nopat_5y Nullable(Float64),
    normalized_earnings_5y Nullable(Float64),
    normalized_nopat_growth_3y_pct Nullable(Float64),
    incremental_investment_rate_pct Nullable(Float64),
    roiic_pct Nullable(Float64)
)
ENGINE = MergeTree
ORDER BY (security_id, effective_date, financial_period)
""".strip()


def _wacc_stage_ddl() -> str:
    return f"""
CREATE TABLE {WACC_STAGE}
(
    security_id String,
    effective_date Date,
    wacc Nullable(Float64),
    cost_of_equity Nullable(Float64)
)
ENGINE = MergeTree
ORDER BY (security_id, effective_date)
""".strip()


def _wacc_stage_query(start_date: str, end_date: str, financial_basis: str) -> str:
    return f"""
INSERT INTO {WACC_STAGE}
SELECT
    security_id,
    trade_date AS effective_date,
    argMaxIf(factor_value, updated_at, factor_id = 'wacc' AND isFinite(factor_value)) / 100 AS wacc,
    argMaxIf(factor_value, updated_at, factor_id = 'cost_of_equity' AND isFinite(factor_value)) / 100 AS cost_of_equity
FROM fact_daily_factors
PREWHERE trade_date >= toDate('{start_date}')
    AND trade_date <= toDate('{end_date}')
    AND financial_basis = '{financial_basis}'
    AND factor_id IN ('wacc', 'cost_of_equity')
    AND startsWith(security_id, 'SEC_US_')
GROUP BY security_id, trade_date
HAVING wacc > 0 AND wacc < 1
""".strip()


def _financial_rows_for_batch(
    symbols: list[str],
    financial_basis: str,
    end_date: str,
) -> pd.DataFrame:
    metadata_path = DATA_LAKE.silver("sec", "us_report_metadata.csv")
    frames = []
    reader = read_ttm_financials if financial_basis == "ttm" else read_annual_financials
    for symbol in symbols:
        try:
            frame = reader(
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
        frames.append(frame[FINANCIAL_COLUMNS])
    if not frames:
        return pd.DataFrame(columns=FINANCIAL_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def _chunks(values: list[str], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _insert_financial_stage(
    client,
    symbols: list[str],
    *,
    financial_basis: str,
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
        future = executor.submit(
            _financial_rows_for_batch,
            batch,
            financial_basis,
            end_date,
        )
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
                        column_names=FINANCIAL_COLUMNS,
                    )
                    inserted += len(frame)
                print(
                    f"[PVGO-LOCAL] financial stage symbols={processed:,}/{len(symbols):,} "
                    f"rows={inserted:,} elapsed={(time.monotonic() - started) / 60:.1f}m",
                    flush=True,
                )
                submit_next(executor)
    return inserted


def _local_daily_query(
    *,
    financial_basis: str,
    calculation_start_date: str,
    output_start_date: str,
    output_end_date: str,
) -> str:
    ids_sql = ", ".join(f"'{factor_id}'" for factor_id in PVGO_FACTOR_IDS)
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
wacc_rows AS
(
    SELECT *
    FROM {WACC_STAGE}
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
        f.sale AS sale,
        f.nopat AS nopat,
        f.net_debt AS net_debt,
        f.normalized_operating_margin_5y AS normalized_operating_margin_5y,
        f.normalized_nopat_5y AS normalized_nopat_5y,
        f.normalized_earnings_5y AS normalized_earnings_5y,
        f.normalized_nopat_growth_3y_pct AS normalized_nopat_growth_3y_pct,
        f.incremental_investment_rate_pct AS incremental_investment_rate_pct,
        f.roiic_pct AS roiic_pct,
        w.wacc AS wacc,
        w.cost_of_equity AS cost_of_equity
    FROM price_rows AS p
    ASOF LEFT JOIN share_rows AS s
        ON p.security_id = s.security_id
        AND p.trade_date >= s.effective_date
    ASOF LEFT JOIN financial_rows AS f
        ON p.security_id = f.security_id
        AND p.trade_date >= f.effective_date
    ASOF LEFT JOIN wacc_rows AS w
        ON p.security_id = w.security_id
        AND p.trade_date >= w.effective_date
),
base_rows AS
(
    SELECT
        *,
        market_cap + net_debt AS enterprise_value,
        nopat / wacc AS steady_state_ev,
        normalized_nopat_5y / wacc AS normalized_steady_state_ev,
        greatest(0.0, least(0.25, normalized_nopat_growth_3y_pct / 100)) AS growth_rate,
        greatest(0.0, least(5.0, incremental_investment_rate_pct / 100)) AS investment_rate
    FROM daily_inputs
    WHERE market_cap > 0
        AND wacc > 0
        AND wacc < 1
),
pvgo_rows AS
(
    SELECT
        *,
        (market_cap - (steady_state_ev - net_debt)) / market_cap * 100 AS pvgo_pct,
        (enterprise_value - steady_state_ev) / enterprise_value * 100 AS pvgo_ev_pct,
        (market_cap - (normalized_steady_state_ev - net_debt)) / market_cap * 100 AS normalized_pvgo_pct,
        if(
            cost_of_equity > 0 AND cost_of_equity < 1,
            (market_cap - normalized_earnings_5y / cost_of_equity) / market_cap * 100,
            NULL
        ) AS equity_pvgo_pct,
        roiic_pct - wacc * 100 AS roiic_wacc_spread,
        if(
            normalized_nopat_5y > 0
                AND sale > 0
                AND growth_rate > 0
                AND incremental_investment_rate_pct IS NOT NULL
                AND roiic_pct / 100 > wacc,
            arraySum(
                arrayMap(
                    year -> greatest(
                        assumeNotNull(normalized_nopat_5y)
                            * pow(1 + assumeNotNull(growth_rate), year - 1)
                            * assumeNotNull(growth_rate)
                            / assumeNotNull(wacc)
                            - assumeNotNull(sale)
                            * pow(1 + assumeNotNull(growth_rate), year - 1)
                            * assumeNotNull(growth_rate)
                            * assumeNotNull(investment_rate),
                        0.0
                    ) / pow(1 + assumeNotNull(wacc), year),
                    range(1, 11)
                )
            ) / market_cap * 100,
            NULL
        ) AS justified_pvgo_pct
    FROM base_rows
    WHERE enterprise_value > 0
),
lagged_rows AS
(
    SELECT
        *,
        lagInFrame(steady_state_ev, 252) OVER daily_window AS prior_steady_state_ev,
        lagInFrame(market_cap, 252) OVER daily_window AS prior_market_cap,
        lagInFrame(pvgo_pct, 252) OVER daily_window AS prior_pvgo_pct
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
        [{ids_sql}] AS factor_ids,
        [
            toNullable(normalized_operating_margin_5y),
            toNullable(normalized_nopat_5y),
            toNullable(normalized_earnings_5y),
            toNullable(normalized_nopat_growth_3y_pct),
            toNullable(incremental_investment_rate_pct),
            toNullable(roiic_pct),
            toNullable(roiic_wacc_spread),
            toNullable(pvgo_pct),
            toNullable(pvgo_ev_pct),
            toNullable(-pvgo_pct),
            toNullable(normalized_pvgo_pct),
            toNullable(equity_pvgo_pct),
            toNullable(justified_pvgo_pct),
            toNullable(justified_pvgo_pct - pvgo_pct),
            toNullable(
                if(
                    prior_steady_state_ev > 0 AND prior_market_cap > 0,
                    (steady_state_ev - prior_steady_state_ev) / abs(prior_steady_state_ev) * 100
                        - (market_cap - prior_market_cap) / abs(prior_market_cap) * 100,
                    NULL
                )
            ),
            toNullable(pvgo_pct - prior_pvgo_pct)
        ] AS factor_values
    FROM lagged_rows
)
SELECT
    security_id,
    trade_date,
    factor_tuple.1 AS factor_id,
    '{financial_basis}' AS financial_basis,
    factor_tuple.2 AS factor_value,
    fiscal_year,
    financial_period,
    'USD' AS currency,
    now64(3, 'Asia/Seoul') AS updated_at
FROM factor_rows
ARRAY JOIN arrayZip(factor_ids, factor_values) AS factor_tuple
WHERE factor_tuple.2 IS NOT NULL
    AND isFinite(factor_tuple.2)
    AND trade_date >= toDate('{output_start_date}')
    AND trade_date <= toDate('{output_end_date}')
""".strip()


def _symbols(client, start_date: str, end_date: str, stock_codes: str | None) -> list[str]:
    if stock_codes:
        return sorted({value.strip().upper() for value in stock_codes.split(",") if value.strip()})
    rows = client.query(
        f"""
SELECT DISTINCT substring(security_id, 8)
FROM price_daily
WHERE startsWith(security_id, 'SEC_US_')
    AND trade_date >= toDate('{start_date}')
    AND trade_date <= toDate('{end_date}')
ORDER BY substring(security_id, 8)
"""
    ).result_rows
    financial_dir = DATA_LAKE.silver("sec", "normalized")
    return [
        str(row[0])
        for row in rows
        if (financial_dir / f"us_normalized_{row[0]}.csv").exists()
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fill U.S. PVGO gaps from local SEC statements and daily prices."
    )
    parser.add_argument("--start-date", default="2021-01-01")
    parser.add_argument("--end-date", default="2026-08-27")
    parser.add_argument("--financial-basis", choices=["annual", "ttm"], default="ttm")
    parser.add_argument("--stock-codes")
    parser.add_argument("--workers", type=int, default=max(1, min(16, os.cpu_count() or 1)))
    parser.add_argument("--symbol-batch-size", type=int, default=20)
    parser.add_argument("--keep-stage", action="store_true")
    args = parser.parse_args()

    start_date = _validate_date(args.start_date)
    end_date = _validate_date(args.end_date)
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    calculation_floor = max(
        date(2016, 1, 4),
        date.fromisoformat(start_date) - timedelta(days=400),
    ).isoformat()

    client = get_clickhouse_client()
    try:
        symbols = _symbols(client, calculation_floor, end_date, args.stock_codes)
        print(f"[PVGO-LOCAL] symbols={len(symbols):,}", flush=True)
        _execute(client, f"DROP TABLE IF EXISTS {FINANCIAL_STAGE}")
        _execute(client, f"DROP TABLE IF EXISTS {WACC_STAGE}")
        _execute(client, _financial_stage_ddl())
        _execute(client, _wacc_stage_ddl())
        _insert_financial_stage(
            client,
            symbols,
            financial_basis=args.financial_basis,
            end_date=end_date,
            workers=args.workers,
            symbol_batch_size=args.symbol_batch_size,
        )
        print("[PVGO-LOCAL] staging WACC events", flush=True)
        _execute(
            client,
            _wacc_stage_query("2016-01-04", end_date, args.financial_basis),
        )

        chunks = _year_chunks(start_date, end_date)
        for chunk_index, (chunk_start, chunk_end) in enumerate(chunks, start=1):
            calculation_start = max(
                date.fromisoformat(calculation_floor),
                date.fromisoformat(chunk_start) - timedelta(days=400),
            ).isoformat()
            print(
                f"[PVGO-LOCAL] daily factors chunk={chunk_index}/{len(chunks)} "
                f"dates={chunk_start}..{chunk_end}",
                flush=True,
            )
            _execute(
                client,
                _local_daily_query(
                    financial_basis=args.financial_basis,
                    calculation_start_date=calculation_start,
                    output_start_date=chunk_start,
                    output_end_date=chunk_end,
                ),
            )
    finally:
        if not args.keep_stage:
            _execute(client, f"DROP TABLE IF EXISTS {FINANCIAL_STAGE}")
            _execute(client, f"DROP TABLE IF EXISTS {WACC_STAGE}")
        client.close()

    print("[PVGO-LOCAL] complete", flush=True)


if __name__ == "__main__":
    main()
