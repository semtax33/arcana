from __future__ import annotations

"""Backfill the historical U.S. TTM factors used by the PVGO/QVIR hybrid.

The ordinary historical backfill only checked whether a factor existed at all
on a date.  That is insufficient for FactorLab because one surviving security
can make a sparse factor look complete.  This script recalculates every
available security on the exact quarterly signal dates needed by a backtest
that starts on 2017-01-03, then copies the point-in-time rows into the snapshot
table with ``source_trade_date == trade_date``.

It deliberately reuses the audited local SEC/price cache produced by
``.codex_backfill_us_signal_factors.py``.  No historical value is fabricated;
rows remain absent when the source statement history cannot support a factor.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
import json
import os
from pathlib import Path
import time
import traceback
import warnings

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.loaders._internal.clickhouse_factors import prepare_daily_factor_rows
from engine.transformers._internal.factor_metrics import (
    FactorMarketDataCache,
    create_stock_factor_dataframe,
)


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".codex-tmp" / "us-signal-factor-backfill"
INPUT_DIR = WORK / "inputs"
FINANCIAL_DIR = WORK / "financial"
REPORT_PATH = WORK / "report.csv"
STATE_DIR = WORK / "hybrid-quarterly-2017"
SHARD_COUNT = 8

# These are the prior trading days for the quarterly execution dates from
# 2017-01-03 through 2022-01-03.  The cache ends on 2021-12-31; later signal
# dates are already covered by the normal production pipeline.
TARGET_DATES = (
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
)

TTM_FACTOR_IDS = (
    "asset_yoy_pct",
    "capex_growth_2y_pct",
    "cash_to_debt",
    "delta_economic_profit",
    "economic_profit_yield",
    "eps_yoy_pct",
    "fcf_interest_coverage",
    "fcf_to_ev_yield",
    "gross_profitability_pct",
    "inventory_growth_1y_pct",
    "iroe",
    "net_external_financing_pct",
    "opm",
    "percent_total_accruals_pct",
    "pvgo_compression_pct",
    "pvgo_gap_pct",
    "pvgo_pct",
    "rdsr_pct",
    "rim_upside_potential",
    "roic_wacc_spread",
    "roic_wacc_spread_growth_1y",
    "roiic_wacc_spread",
    "rpr",
    "sales_growth_1y",
)


def _client():
    return get_clickhouse_client()


def _input_path(dataset: str, shard_index: int) -> Path:
    return INPUT_DIR / f"{dataset}-{shard_index:02d}.csv"


def _validate_inputs() -> None:
    missing = [
        path
        for path in (
            REPORT_PATH,
            *(
                _input_path(dataset, shard)
                for dataset in ("price", "shares", "dividend")
                for shard in range(SHARD_COUNT)
            ),
        )
        if not path.exists()
    ]
    if missing:
        raise FileNotFoundError("required historical cache is missing: " + ", ".join(map(str, missing)))
    if not FINANCIAL_DIR.exists():
        raise FileNotFoundError(f"required historical financial cache is missing: {FINANCIAL_DIR}")


def _stock_codes(cache: FactorMarketDataCache) -> list[str]:
    price_frame, _ = cache._groups("price")
    target_dates = {pd.Timestamp(value) for value in TARGET_DATES}
    codes = (
        price_frame.loc[price_frame["trade_date"].isin(target_dates), "security_id"]
        .dropna()
        .astype(str)
        .str.removeprefix("SEC_US_")
        .unique()
    )
    return sorted(
        code
        for code in codes
        if (FINANCIAL_DIR / f"us_normalized_{code}.csv").exists()
    )


def _insert_buffer(client, frames: list[pd.DataFrame]) -> int:
    if not frames:
        return 0
    rows = pd.concat(frames, ignore_index=True)
    client.insert_df("fact_daily_factors", rows, column_names=list(rows.columns))
    return len(rows)


def compute_shard(shard_index: int, *, force: bool = False) -> dict[str, object]:
    warnings.filterwarnings("ignore")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    marker = STATE_DIR / f"compute-{shard_index:02d}.json"
    if marker.exists() and not force:
        return json.loads(marker.read_text(encoding="utf-8"))

    cache = FactorMarketDataCache(
        market="us",
        price_path=_input_path("price", shard_index),
        shares_path=_input_path("shares", shard_index),
        dividend_path=_input_path("dividend", shard_index),
        start_date=TARGET_DATES[0],
        end_date=TARGET_DATES[-1],
    )
    stock_codes = _stock_codes(cache)
    target_dates = {pd.Timestamp(value) for value in TARGET_DATES}
    client = _client()
    buffer: list[pd.DataFrame] = []
    inserted = 0
    completed = 0
    failures: list[dict[str, str]] = []
    started = time.monotonic()
    try:
        for index, stock_code in enumerate(stock_codes, start=1):
            try:
                wide = create_stock_factor_dataframe(
                    stock_code,
                    financial_basis="ttm",
                    start_date=TARGET_DATES[0],
                    end_date=TARGET_DATES[-1],
                    market="us",
                    market_data_cache=cache,
                    financial_dir=FINANCIAL_DIR,
                    report_metadata_path=REPORT_PATH,
                    use_edgartools=False,
                )
                if not wide.empty:
                    target = wide.loc[wide["trade_date"].isin(target_dates)].copy()
                    rows = prepare_daily_factor_rows(
                        target,
                        financial_basis="ttm",
                        factor_ids=list(TTM_FACTOR_IDS),
                        sort_rows=False,
                    )
                    if not rows.empty:
                        buffer.append(rows)
                completed += 1
            except Exception as exc:  # Keep independent symbols from blocking the shard.
                failures.append(
                    {
                        "stock_code": stock_code,
                        "error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(limit=5),
                    }
                )

            if len(buffer) >= 40 or index == len(stock_codes):
                inserted += _insert_buffer(client, buffer)
                buffer = []
            if index == 1 or index % 50 == 0 or index == len(stock_codes):
                print(
                    f"[HYBRID-BACKFILL] shard={shard_index:02d} "
                    f"stocks={index:,}/{len(stock_codes):,} rows={inserted:,} "
                    f"failures={len(failures):,} elapsed={(time.monotonic()-started)/60:.1f}m",
                    flush=True,
                )
    finally:
        client.close()

    result: dict[str, object] = {
        "shard": shard_index,
        "stocks": len(stock_codes),
        "completed": completed,
        "inserted_rows": inserted,
        "failure_count": len(failures),
        "failures": failures,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    marker.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def run_all(*, workers: int, force: bool) -> list[dict[str, object]]:
    _validate_inputs()
    worker_count = max(1, min(int(workers), SHARD_COUNT))
    results: list[dict[str, object]] = []
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(compute_shard, shard, force=force): shard
            for shard in range(SHARD_COUNT)
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                f"[HYBRID-BACKFILL] finished shard={result['shard']} "
                f"rows={result['inserted_rows']:,} failures={result['failure_count']:,}",
                flush=True,
            )
    return sorted(results, key=lambda item: int(item["shard"]))


def load_snapshots() -> None:
    client = _client()
    try:
        client.command(
            """
INSERT INTO fact_daily_factor_snapshot
    (trade_date, security_id, factor_id, financial_basis, factor_value,
     source_trade_date, fiscal_year, financial_period, currency, updated_at)
SELECT trade_date, security_id, factor_id, financial_basis, factor_value,
       trade_date, fiscal_year, financial_period, currency, now64(3)
FROM fact_daily_factors FINAL
PREWHERE has({days:Array(Date)}, trade_date)
WHERE startsWith(security_id, 'SEC_US_')
  AND financial_basis = 'ttm'
  AND has({factor_ids:Array(String)}, factor_id)
  AND factor_value IS NOT NULL
SETTINGS max_threads = 8
""".strip(),
            parameters={"days": list(TARGET_DATES), "factor_ids": list(TTM_FACTOR_IDS)},
        )
    finally:
        client.close()
    print("[HYBRID-BACKFILL] exact-date snapshot copy complete", flush=True)


def coverage(table: str) -> list[dict[str, object]]:
    if table not in {"fact_daily_factors", "fact_daily_factor_snapshot"}:
        raise ValueError("unsupported table")
    client = _client()
    try:
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
            parameters={"days": list(TARGET_DATES), "factor_ids": list(TTM_FACTOR_IDS)},
        ).result_rows
    finally:
        client.close()
    counts = {(str(day), str(factor_id)): int(count) for day, factor_id, count in rows}
    result = []
    for day in TARGET_DATES:
        values = {factor_id: counts.get((day, factor_id), 0) for factor_id in TTM_FACTOR_IDS}
        ordered = sorted(values.values())
        result.append(
            {
                "trade_date": day,
                "minimum": ordered[0],
                "median": ordered[len(ordered) // 2],
                "pvgo_pct": values["pvgo_pct"],
                "pvgo_gap_pct": values["pvgo_gap_pct"],
                "roiic_wacc_spread": values["roiic_wacc_spread"],
                "rpr": values["rpr"],
                "sales_growth_1y": values["sales_growth_1y"],
                "factor_counts": values,
            }
        )
    return result


def verify() -> dict[str, object]:
    result = {
        "generated_at": pd.Timestamp.now(tz="Asia/Seoul").isoformat(),
        "start_date": date(2017, 1, 3).isoformat(),
        "end_date": date(2026, 8, 27).isoformat(),
        "target_signal_dates": list(TARGET_DATES),
        "factors": list(TTM_FACTOR_IDS),
        "raw": coverage("fact_daily_factors"),
        "snapshot": coverage("fact_daily_factor_snapshot"),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    output = STATE_DIR / "coverage.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("run", "compute-shard", "snapshots", "verify"))
    parser.add_argument("--shard", type=int)
    parser.add_argument("--workers", type=int, default=min(SHARD_COUNT, os.cpu_count() or 1))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.stage == "run":
        results = run_all(workers=args.workers, force=args.force)
        print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
    elif args.stage == "compute-shard":
        if args.shard is None or not 0 <= args.shard < SHARD_COUNT:
            raise ValueError(f"--shard must be between 0 and {SHARD_COUNT - 1}")
        print(json.dumps(compute_shard(args.shard, force=args.force), ensure_ascii=False, indent=2))
    elif args.stage == "snapshots":
        load_snapshots()
    else:
        verify()


if __name__ == "__main__":
    main()
