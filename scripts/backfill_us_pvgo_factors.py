from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

from engine.core.clickhouse import get_clickhouse_client
from engine.loaders._internal.clickhouse_factors import insert_factor_catalog
from engine.loaders.factor_snapshots import insert_factor_snapshots


START_DATE = "2016-01-04"
END_DATE = "2026-08-27"
STAGE_TABLE = "arcana_tmp_us_pvgo_period_v1"
PVGO_FACTOR_IDS = [
    "normalized_operating_margin_5y",
    "normalized_nopat_5y",
    "normalized_earnings_5y",
    "normalized_nopat_growth_3y_pct",
    "incremental_investment_rate_pct",
    "roiic_pct",
    "roiic_wacc_spread",
    "pvgo_pct",
    "pvgo_ev_pct",
    "pvgo_expectation_factor",
    "normalized_pvgo_pct",
    "equity_pvgo_pct",
    "justified_pvgo_pct",
    "pvgo_gap_pct",
    "pvgo_compression_pct",
    "pvgo_change_1y_pctp",
]

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_date(value: str) -> str:
    if not _DATE_PATTERN.fullmatch(value):
        raise ValueError(f"invalid ISO date: {value}")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _basis_parameters(financial_basis: str) -> tuple[int, int, int, int]:
    if financial_basis == "annual":
        return 5, 3, 1, 3
    if financial_basis == "ttm":
        return 20, 12, 4, 12
    raise ValueError("financial_basis must be annual or ttm")


def _execute(client, query: str) -> None:
    client.command(
        query,
        settings={"max_threads": 4, "max_partitions_per_insert_block": 256},
    )


def _year_chunks(start_date: str, end_date: str) -> list[tuple[str, str]]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    chunks = []
    current = start
    while current <= end:
        chunk_end = min(end, date(current.year, 12, 31))
        chunks.append((current.isoformat(), chunk_end.isoformat()))
        current = chunk_end + timedelta(days=1)
    return chunks


def _stage_ddl() -> str:
    return f"""
CREATE TABLE {STAGE_TABLE}
(
    financial_basis LowCardinality(String),
    security_id String,
    financial_period Date,
    effective_date Date,
    fiscal_year Nullable(UInt16),
    currency LowCardinality(String),
    sale Nullable(Float64),
    nopat Nullable(Float64),
    normalized_operating_margin_5y Nullable(Float64),
    normalized_nopat_5y Nullable(Float64),
    normalized_earnings_5y Nullable(Float64),
    normalized_nopat_growth_3y_pct Nullable(Float64),
    incremental_investment_rate_pct Nullable(Float64),
    roiic_pct Nullable(Float64)
)
ENGINE = MergeTree
ORDER BY (financial_basis, security_id, financial_period)
""".strip()


def _period_stage_query(
    *,
    financial_basis: str,
    start_date: str,
    end_date: str,
) -> str:
    window, min_periods, annual_lag, growth_lag = _basis_parameters(financial_basis)
    preceding = window - 1
    input_ids = "'sale','nopat','ni_parent','opm','rect','invt','ap','ppent'"
    return f"""
INSERT INTO {STAGE_TABLE}
WITH
period_rows AS
(
    SELECT
        financial_basis,
        security_id,
        assumeNotNull(financial_period) AS financial_period,
        min(trade_date) AS effective_date,
        argMin(fiscal_year, tuple(trade_date, updated_at)) AS fiscal_year,
        argMin(currency, tuple(trade_date, updated_at)) AS currency,
        argMinIf(factor_value, tuple(trade_date, updated_at), factor_id = 'sale' AND isFinite(factor_value)) AS sale,
        argMinIf(factor_value, tuple(trade_date, updated_at), factor_id = 'nopat' AND isFinite(factor_value)) AS nopat,
        argMinIf(factor_value, tuple(trade_date, updated_at), factor_id = 'ni_parent' AND isFinite(factor_value)) AS ni_parent,
        argMinIf(factor_value, tuple(trade_date, updated_at), factor_id = 'opm' AND isFinite(factor_value)) AS opm,
        argMinIf(factor_value, tuple(trade_date, updated_at), factor_id = 'rect' AND isFinite(factor_value)) AS rect,
        argMinIf(factor_value, tuple(trade_date, updated_at), factor_id = 'invt' AND isFinite(factor_value)) AS invt,
        argMinIf(factor_value, tuple(trade_date, updated_at), factor_id = 'ap' AND isFinite(factor_value)) AS ap,
        argMinIf(factor_value, tuple(trade_date, updated_at), factor_id = 'ppent' AND isFinite(factor_value)) AS ppent
    FROM fact_daily_factors
    PREWHERE trade_date >= toDate('{start_date}')
        AND trade_date <= toDate('{end_date}')
        AND financial_basis = '{financial_basis}'
        AND factor_id IN ({input_ids})
        AND startsWith(security_id, 'SEC_US_')
    WHERE financial_period IS NOT NULL
    GROUP BY financial_basis, security_id, financial_period
),
rolling_rows AS
(
    SELECT
        *,
        coalesce(rect, 0) + coalesce(invt, 0) - coalesce(ap, 0) + coalesce(ppent, 0) AS invested_capital,
        avg(opm) OVER rolling_window AS normalized_opm,
        count(opm) OVER rolling_window AS normalized_opm_count,
        avg(if(sale > 0, nopat / sale, NULL)) OVER rolling_window AS normalized_nopat_margin,
        count(if(sale > 0, nopat / sale, NULL)) OVER rolling_window AS normalized_nopat_count,
        avg(ni_parent) OVER rolling_window AS normalized_earnings,
        count(ni_parent) OVER rolling_window AS normalized_earnings_count
    FROM period_rows
    WINDOW rolling_window AS
    (
        PARTITION BY financial_basis, security_id
        ORDER BY financial_period
        ROWS BETWEEN {preceding} PRECEDING AND CURRENT ROW
    )
),
normalized_rows AS
(
    SELECT
        *,
        if(normalized_opm_count >= {min_periods}, normalized_opm, NULL) AS normalized_operating_margin_5y,
        if(
            normalized_nopat_count >= {min_periods} AND sale > 0,
            sale * normalized_nopat_margin,
            NULL
        ) AS normalized_nopat_5y,
        if(normalized_earnings_count >= {min_periods}, normalized_earnings, NULL) AS normalized_earnings_5y
    FROM rolling_rows
),
lagged_rows AS
(
    SELECT
        *,
        lagInFrame(sale, {annual_lag}) OVER history_window AS prior_sale,
        lagInFrame(nopat, {annual_lag}) OVER history_window AS prior_nopat,
        lagInFrame(invested_capital, {annual_lag}) OVER history_window AS prior_invested_capital,
        lagInFrame(normalized_nopat_5y, {growth_lag}) OVER history_window AS prior_normalized_nopat
    FROM normalized_rows
    WINDOW history_window AS
    (
        PARTITION BY financial_basis, security_id
        ORDER BY financial_period
        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
    )
),
derived_rows AS
(
    SELECT
        *,
        invested_capital - prior_invested_capital AS incremental_capital,
        sale - prior_sale AS incremental_sales
    FROM lagged_rows
)
SELECT
    financial_basis,
    security_id,
    financial_period,
    effective_date,
    fiscal_year,
    currency,
    sale,
    nopat,
    normalized_operating_margin_5y,
    normalized_nopat_5y,
    normalized_earnings_5y,
    if(
        normalized_nopat_5y > 0 AND prior_normalized_nopat > 0,
        (pow(normalized_nopat_5y / prior_normalized_nopat, 1 / 3.0) - 1) * 100,
        NULL
    ) AS normalized_nopat_growth_3y_pct,
    if(
        incremental_sales > 0
            AND abs(prior_invested_capital) > 0
            AND abs(incremental_capital) >= abs(prior_invested_capital) * 0.01,
        incremental_capital / incremental_sales * 100,
        NULL
    ) AS incremental_investment_rate_pct,
    if(
        abs(prior_invested_capital) > 0
            AND abs(incremental_capital) >= abs(prior_invested_capital) * 0.01,
        (nopat - prior_nopat) / incremental_capital * 100,
        NULL
    ) AS roiic_pct
FROM derived_rows
""".strip()


def _daily_factor_query(
    *,
    financial_basis: str,
    calculation_start_date: str,
    output_start_date: str,
    output_end_date: str,
) -> str:
    factor_ids_sql = ", ".join(f"'{factor_id}'" for factor_id in PVGO_FACTOR_IDS)
    return f"""
INSERT INTO fact_daily_factors
(
    security_id,
    trade_date,
    factor_id,
    financial_basis,
    factor_value,
    fiscal_year,
    financial_period,
    currency,
    updated_at
)
WITH
daily_market AS
(
    SELECT
        security_id,
        trade_date,
        argMaxIf(financial_period, updated_at, factor_id = 'enterprise_value') AS financial_period,
        argMaxIf(fiscal_year, updated_at, factor_id = 'enterprise_value') AS fiscal_year,
        argMaxIf(currency, updated_at, factor_id = 'enterprise_value') AS currency,
        argMaxIf(factor_value, updated_at, factor_id = 'mcap_mil' AND isFinite(factor_value)) * 1000000 AS market_cap,
        argMaxIf(factor_value, updated_at, factor_id = 'enterprise_value' AND isFinite(factor_value)) AS enterprise_value,
        argMaxIf(factor_value, updated_at, factor_id = 'wacc' AND isFinite(factor_value)) / 100 AS wacc,
        argMaxIf(factor_value, updated_at, factor_id = 'cost_of_equity' AND isFinite(factor_value)) / 100 AS cost_of_equity
    FROM fact_daily_factors
    PREWHERE trade_date >= toDate('{calculation_start_date}')
        AND trade_date <= toDate('{output_end_date}')
        AND financial_basis = '{financial_basis}'
        AND factor_id IN ('mcap_mil', 'enterprise_value', 'wacc', 'cost_of_equity')
        AND startsWith(security_id, 'SEC_US_')
    GROUP BY security_id, trade_date
),
base_rows AS
(
    SELECT
        d.security_id,
        d.trade_date,
        d.financial_period,
        coalesce(d.fiscal_year, p.fiscal_year) AS fiscal_year,
        if(empty(d.currency), p.currency, d.currency) AS currency,
        d.market_cap,
        d.enterprise_value,
        d.wacc,
        d.cost_of_equity,
        p.sale,
        p.nopat,
        p.normalized_operating_margin_5y,
        p.normalized_nopat_5y,
        p.normalized_earnings_5y,
        p.normalized_nopat_growth_3y_pct,
        p.incremental_investment_rate_pct,
        p.roiic_pct,
        d.enterprise_value - d.market_cap AS net_debt,
        p.nopat / d.wacc AS steady_state_ev,
        p.normalized_nopat_5y / d.wacc AS normalized_steady_state_ev,
        greatest(0.0, least(0.25, p.normalized_nopat_growth_3y_pct / 100)) AS growth_rate,
        greatest(0.0, least(5.0, p.incremental_investment_rate_pct / 100)) AS investment_rate
    FROM daily_market AS d
    ANY LEFT JOIN {STAGE_TABLE} AS p
        ON p.financial_basis = '{financial_basis}'
        AND p.security_id = d.security_id
        AND p.financial_period = d.financial_period
    WHERE d.market_cap > 0
        AND d.enterprise_value > 0
        AND d.wacc > 0
        AND d.wacc < 1
),
pvgo_rows AS
(
    SELECT
        *,
        (market_cap - (steady_state_ev - net_debt)) / market_cap * 100 AS pvgo_pct,
        (enterprise_value - steady_state_ev) / enterprise_value * 100 AS pvgo_ev_pct,
        (market_cap - (normalized_steady_state_ev - net_debt)) / market_cap * 100 AS normalized_pvgo_pct,
        if(
            cost_of_equity > 0 AND cost_of_equity < 0.50,
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
        currency,
        [
            {factor_ids_sql}
        ] AS factor_ids,
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
    currency,
    now64(3, 'Asia/Seoul') AS updated_at
FROM factor_rows
ARRAY JOIN arrayZip(factor_ids, factor_values) AS factor_tuple
WHERE factor_tuple.2 IS NOT NULL
    AND isFinite(factor_tuple.2)
    AND trade_date >= toDate('{output_start_date}')
    AND trade_date <= toDate('{output_end_date}')
""".strip()


def _delete_existing_query(
    *,
    financial_basis: str,
    start_date: str,
    end_date: str,
    table: str,
) -> str:
    ids = ", ".join(f"'{factor_id}'" for factor_id in PVGO_FACTOR_IDS)
    return f"""
ALTER TABLE {table} DELETE WHERE
    startsWith(security_id, 'SEC_US_')
    AND trade_date >= toDate('{start_date}')
    AND trade_date <= toDate('{end_date}')
    AND financial_basis = '{financial_basis}'
    AND factor_id IN ({ids})
SETTINGS mutations_sync = 2
""".strip()


def _coverage(client, financial_basis: str, start_date: str, end_date: str):
    ids = ", ".join(f"'{factor_id}'" for factor_id in PVGO_FACTOR_IDS)
    return client.query(
        f"""
SELECT
    factor_id,
    min(trade_date) AS min_date,
    max(trade_date) AS max_date,
    uniqExact(security_id) AS securities,
    count() AS rows
FROM fact_daily_factors
WHERE startsWith(security_id, 'SEC_US_')
    AND trade_date >= toDate('{start_date}')
    AND trade_date <= toDate('{end_date}')
    AND financial_basis = '{financial_basis}'
    AND factor_id IN ({ids})
GROUP BY factor_id
ORDER BY factor_id
SETTINGS max_threads = 4
"""
    ).result_rows


def backfill_basis(
    client,
    *,
    financial_basis: str,
    start_date: str,
    end_date: str,
    replace_existing: bool,
) -> None:
    print(f"[PVGO] staging statement inputs basis={financial_basis}", flush=True)
    _execute(client, f"DROP TABLE IF EXISTS {STAGE_TABLE}")
    _execute(client, _stage_ddl())
    _execute(
        client,
        _period_stage_query(
            financial_basis=financial_basis,
            start_date=start_date,
            end_date=end_date,
        ),
    )
    stage_count = client.query(
        f"SELECT count() FROM {STAGE_TABLE} WHERE financial_basis = '{financial_basis}'"
    ).first_row[0]
    print(f"[PVGO] staged rows={stage_count:,} basis={financial_basis}", flush=True)

    if replace_existing:
        print(f"[PVGO] deleting prior PVGO rows basis={financial_basis}", flush=True)
        _execute(
            client,
            _delete_existing_query(
                financial_basis=financial_basis,
                start_date=start_date,
                end_date=end_date,
                table="fact_daily_factors",
            ),
        )
    chunks = _year_chunks(start_date, end_date)
    for chunk_index, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        calculation_start = max(
            date.fromisoformat(start_date),
            date.fromisoformat(chunk_start) - timedelta(days=400),
        ).isoformat()
        print(
            f"[PVGO] inserting daily factors basis={financial_basis} "
            f"chunk={chunk_index}/{len(chunks)} dates={chunk_start}..{chunk_end}",
            flush=True,
        )
        _execute(
            client,
            _daily_factor_query(
                financial_basis=financial_basis,
                calculation_start_date=calculation_start,
                output_start_date=chunk_start,
                output_end_date=chunk_end,
            ),
        )
    _execute(client, f"DROP TABLE IF EXISTS {STAGE_TABLE}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill Arcana PVGO factors for the U.S. universe."
    )
    parser.add_argument("--start-date", default=START_DATE)
    parser.add_argument("--end-date", default=END_DATE)
    parser.add_argument(
        "--financial-basis",
        choices=["annual", "ttm", "both"],
        default="both",
    )
    parser.add_argument("--replace-existing", action="store_true")
    parser.add_argument("--with-snapshots", action="store_true")
    parser.add_argument(
        "--snapshots-only",
        action="store_true",
        help="Skip factor calculation and copy the requested PVGO rows to snapshots.",
    )
    args = parser.parse_args()

    start_date = _validate_date(args.start_date)
    end_date = _validate_date(args.end_date)
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    bases = ["annual", "ttm"] if args.financial_basis == "both" else [args.financial_basis]

    client = get_clickhouse_client()
    try:
        insert_factor_catalog(client, factor_ids=PVGO_FACTOR_IDS)
        for basis in bases:
            if not args.snapshots_only:
                backfill_basis(
                    client,
                    financial_basis=basis,
                    start_date=start_date,
                    end_date=end_date,
                    replace_existing=args.replace_existing,
                )
                print(f"[PVGO] coverage basis={basis}", flush=True)
                for row in _coverage(client, basis, start_date, end_date):
                    print(row, flush=True)
            if args.with_snapshots or args.snapshots_only:
                if args.replace_existing:
                    _execute(
                        client,
                        _delete_existing_query(
                            financial_basis=basis,
                            start_date=start_date,
                            end_date=end_date,
                            table="fact_daily_factor_snapshot",
                        ),
                    )
                snapshot_rows = 0
                for chunk_index, (chunk_start, chunk_end) in enumerate(
                    _year_chunks(start_date, end_date),
                    start=1,
                ):
                    print(
                        f"[PVGO] snapshot copy basis={basis} "
                        f"chunk={chunk_index}/{len(_year_chunks(start_date, end_date))} "
                        f"dates={chunk_start}..{chunk_end}",
                        flush=True,
                    )
                    snapshot_rows += insert_factor_snapshots(
                        market="us",
                        start_date=chunk_start,
                        end_date=chunk_end,
                        financial_basis=basis,
                        factor_ids=PVGO_FACTOR_IDS,
                        carry_forward=False,
                        client=client,
                    )
                print(
                    f"[PVGO] snapshot rows={snapshot_rows:,} basis={basis}",
                    flush=True,
                )
    finally:
        _execute(client, f"DROP TABLE IF EXISTS {STAGE_TABLE}")
        client.close()

    completed_at = datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
    print(f"[PVGO] complete at {completed_at}", flush=True)


if __name__ == "__main__":
    main()
