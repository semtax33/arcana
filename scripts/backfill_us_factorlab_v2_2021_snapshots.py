from __future__ import annotations

"""Backfill historical quarterly point-in-time inputs for the US FactorLab V2.

This maintenance job reuses the locally prepared historical US price, share,
dividend, and normalized SEC inputs.  It recomputes the factors referenced by
the stored V2 graph and copies only non-null calculated values into the
point-in-time snapshot table.

Run from the project root with::

    python -m scripts.backfill_us_factorlab_v2_2021_snapshots compute --profile 2016-2020 --workers 16
    python -m scripts.backfill_us_factorlab_v2_2021_snapshots snapshots --profile 2016-2020
    python -m scripts.backfill_us_factorlab_v2_2021_snapshots verify --profile 2016-2020
"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
from pathlib import Path
import time
import warnings

import pandas as pd

from api.service.factor_lab_service import FactorLabService
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
SHARD_COUNT = 8
COMPUTE_TARGET_DATES = ("2021-03-31", "2021-06-30", "2021-09-30")
SNAPSHOT_DATES = (*COMPUTE_TARGET_DATES, "2021-12-31")
HISTORICAL_2016_2020_DATES = (
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
)
DATE_PROFILES = {
    "2021": {
        "compute_dates": COMPUTE_TARGET_DATES,
        "snapshot_dates": SNAPSHOT_DATES,
    },
    "2016-2020": {
        "compute_dates": HISTORICAL_2016_2020_DATES,
        "snapshot_dates": HISTORICAL_2016_2020_DATES,
    },
}
UNAVAILABLE_CONSENSUS_FACTOR_IDS = {
    "us_eps_dispersion_pct",
    "us_eps_revision_30d_pct",
    "us_eps_revision_acceleration_30d_pct",
    "us_eps_revision_breadth_30d_pct",
    "us_eps_surprise_pct",
}
MODEL_NAME = (
    "Ungdroo_US_Minervini_Zweig_Innovation_QualityValue_Quarterly_"
    "Robust_v2_20260825_FactorLab"
)


def _client():
    return get_clickhouse_client()


def required_factor_ids() -> list[str]:
    """Read the authoritative input set from the stored strategy graph."""

    graph = FactorLabService().get_experiment_by_name(MODEL_NAME).graph
    payload = graph.model_dump(mode="json")
    pairs = {
        (
            str(node["config"]["factor_id"]),
            str(node["config"].get("financial_basis", "annual")),
        )
        for node in payload["nodes"]
        if node["type"] == "factor_input"
    }
    non_annual = sorted(pair for pair in pairs if pair[1] != "annual")
    if non_annual:
        raise RuntimeError(f"unexpected non-annual V2 factor inputs: {non_annual}")
    return sorted(factor_id for factor_id, _ in pairs)


def _validate_inputs() -> None:
    missing = [
        path
        for path in [FINANCIAL_DIR, REPORT_PATH]
        if not path.exists()
    ]
    for shard_index in range(SHARD_COUNT):
        for stem in ("price", "shares", "dividend"):
            path = INPUT_DIR / f"{stem}-{shard_index:02d}.csv"
            if not path.exists():
                missing.append(path)
    if missing:
        raise RuntimeError(f"historical backfill inputs are missing: {missing}")


def _compute_partition(
    shard_index: int,
    part_index: int,
    part_count: int,
    factor_ids: tuple[str, ...],
    target_dates: tuple[str, ...],
) -> dict[str, int | float]:
    warnings.filterwarnings("ignore")
    price_path = INPUT_DIR / f"price-{shard_index:02d}.csv"
    cache = FactorMarketDataCache(
        market="us",
        price_path=price_path,
        shares_path=INPUT_DIR / f"shares-{shard_index:02d}.csv",
        dividend_path=INPUT_DIR / f"dividend-{shard_index:02d}.csv",
        start_date=target_dates[0],
        end_date=target_dates[-1],
    )
    price_frame, _ = cache._groups("price")
    targets = {pd.Timestamp(value) for value in target_dates}
    stock_codes = sorted(
        price_frame.loc[price_frame["trade_date"].isin(targets), "security_id"]
        .dropna()
        .astype(str)
        .str.removeprefix("SEC_US_")
        .unique()
    )[part_index::part_count]

    client = _client()
    inserted = 0
    buffer: list[pd.DataFrame] = []
    started = time.monotonic()
    try:
        for index, stock_code in enumerate(stock_codes, start=1):
            wide = create_stock_factor_dataframe(
                stock_code,
                financial_basis="annual",
                start_date=target_dates[0],
                end_date=target_dates[-1],
                market="us",
                market_data_cache=cache,
                financial_dir=FINANCIAL_DIR,
                report_metadata_path=REPORT_PATH,
                use_edgartools=False,
            )
            if not wide.empty:
                target = wide.loc[wide["trade_date"].isin(targets)].copy()
                if not target.empty:
                    long = prepare_daily_factor_rows(
                        target,
                        financial_basis="annual",
                        factor_ids=list(factor_ids),
                        sort_rows=False,
                    )
                    if not long.empty:
                        buffer.append(long)

            if len(buffer) >= 50 or index == len(stock_codes):
                if buffer:
                    batch = pd.concat(buffer, ignore_index=True)
                    client.insert_df(
                        "fact_daily_factors",
                        batch,
                        column_names=list(batch.columns),
                    )
                    inserted += len(batch)
                    buffer = []
            if index == 1 or index % 100 == 0 or index == len(stock_codes):
                print(
                    f"shard={shard_index}, part={part_index}/{part_count}, "
                    f"stocks={index}/{len(stock_codes)}, rows={inserted:,}, "
                    f"elapsed={(time.monotonic() - started) / 60:.1f}m",
                    flush=True,
                )
    finally:
        client.close()

    return {
        "shard": shard_index,
        "part": part_index,
        "stocks": len(stock_codes),
        "rows": inserted,
        "minutes": round((time.monotonic() - started) / 60, 2),
    }


def compute(
    *,
    workers: int,
    parts_per_shard: int,
    target_dates: tuple[str, ...] = COMPUTE_TARGET_DATES,
) -> list[dict[str, int | float]]:
    _validate_inputs()
    factor_ids = tuple(required_factor_ids())
    tasks = [
        (shard_index, part_index, parts_per_shard, factor_ids, target_dates)
        for shard_index in range(SHARD_COUNT)
        for part_index in range(parts_per_shard)
    ]
    results = []
    with ProcessPoolExecutor(max_workers=min(workers, len(tasks))) as executor:
        futures = {executor.submit(_compute_partition, *task): task for task in tasks}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    return sorted(results, key=lambda item: (item["shard"], item["part"]))


def load_snapshots(
    *,
    snapshot_dates: tuple[str, ...] = SNAPSHOT_DATES,
    source_start_day: str = COMPUTE_TARGET_DATES[0],
) -> None:
    factor_ids = required_factor_ids()
    client = _client()
    try:
        for snapshot_day in snapshot_dates:
            client.command(
                """
INSERT INTO fact_daily_factor_snapshot
    (trade_date, security_id, factor_id, financial_basis, factor_value,
     source_trade_date, fiscal_year, financial_period, currency, updated_at)
SELECT
    {snapshot_day:Date},
    security_id,
    factor_id,
    'annual',
    tupleElement(latest, 1),
    tupleElement(latest, 2),
    tupleElement(latest, 3),
    tupleElement(latest, 4),
    tupleElement(latest, 5),
    now64(3)
FROM
(
    SELECT
        security_id,
        factor_id,
        argMax(
            tuple(
                factor_value,
                trade_date,
                fiscal_year,
                financial_period,
                currency
            ),
            trade_date
        ) AS latest
    FROM fact_daily_factors FINAL
    PREWHERE trade_date BETWEEN {source_start_day:Date} AND {snapshot_day:Date}
    WHERE startsWith(security_id, 'SEC_US_')
      AND financial_basis = 'annual'
      AND has({factor_ids:Array(String)}, factor_id)
      AND factor_value IS NOT NULL
    GROUP BY security_id, factor_id
)
SETTINGS max_threads = 12
""".strip(),
                parameters={
                    "snapshot_day": snapshot_day,
                    "source_start_day": source_start_day,
                    "factor_ids": factor_ids,
                },
            )
    finally:
        client.close()
    print(f"V2 PIT snapshot carry-forward complete: {len(snapshot_dates)} dates", flush=True)


def mark_unavailable_consensus(
    *,
    snapshot_dates: tuple[str, ...],
) -> list[tuple[str, str]]:
    """Record genuine historical provider gaps without inventing factor values."""

    factor_ids = required_factor_ids()
    client = _client()
    marked: list[tuple[str, str]] = []
    try:
        present = {
            (str(row[0]), str(row[1]))
            for row in client.query(
                """
SELECT DISTINCT toString(trade_date), factor_id
FROM fact_daily_factor_snapshot FINAL
WHERE startsWith(security_id, 'SEC_US_')
  AND has({days:Array(Date)}, trade_date)
  AND financial_basis = 'annual'
  AND source_trade_date <= trade_date
""".strip(),
                parameters={"days": list(snapshot_dates)},
            ).result_rows
        }
        unsupported = []
        rows = []
        for day in snapshot_dates:
            for factor_id in factor_ids:
                if (day, factor_id) in present:
                    continue
                if factor_id not in UNAVAILABLE_CONSENSUS_FACTOR_IDS:
                    unsupported.append((day, factor_id))
                    continue
                rows.append(
                    {
                        "trade_date": pd.Timestamp(day).date(),
                        "security_id": "SEC_US_AAPL",
                        "factor_id": factor_id,
                        "financial_basis": "annual",
                        "factor_value": None,
                        "source_trade_date": pd.Timestamp(day).date(),
                        "fiscal_year": None,
                        "financial_period": None,
                        "currency": "USD",
                        "updated_at": pd.Timestamp.now(),
                    }
                )
                marked.append((day, factor_id))
        if unsupported:
            raise RuntimeError(f"non-consensus snapshot gaps remain: {unsupported}")
        if rows:
            frame = pd.DataFrame(rows)
            client.insert_df(
                "fact_daily_factor_snapshot",
                frame,
                column_names=list(frame.columns),
            )
    finally:
        client.close()
    print(f"unavailable consensus markers inserted: {len(marked)}", flush=True)
    return marked


def coverage(*, snapshot_dates: tuple[str, ...] = SNAPSHOT_DATES) -> pd.DataFrame:
    factor_ids = required_factor_ids()
    client = _client()
    try:
        return client.query_df(
            """
SELECT trade_date, factor_id, count() AS security_count
FROM fact_daily_factor_snapshot FINAL
WHERE startsWith(security_id, 'SEC_US_')
  AND has({days:Array(Date)}, trade_date)
  AND financial_basis = 'annual'
  AND has({factor_ids:Array(String)}, factor_id)
  AND source_trade_date <= trade_date
  AND factor_value IS NOT NULL
GROUP BY trade_date, factor_id
ORDER BY trade_date, factor_id
""".strip(),
            parameters={"days": list(snapshot_dates), "factor_ids": factor_ids},
        )
    finally:
        client.close()


def verify(*, snapshot_dates: tuple[str, ...] = SNAPSHOT_DATES) -> dict[str, object]:
    factor_ids = required_factor_ids()
    frame = coverage(snapshot_dates=snapshot_dates)
    non_null_present = {
        (pd.Timestamp(row.trade_date).date().isoformat(), str(row.factor_id))
        for row in frame.itertuples(index=False)
    }
    client = _client()
    try:
        present = {
            (str(row[0]), str(row[1]))
            for row in client.query(
                """
SELECT DISTINCT toString(trade_date), factor_id
FROM fact_daily_factor_snapshot FINAL
WHERE startsWith(security_id, 'SEC_US_')
  AND has({days:Array(Date)}, trade_date)
  AND financial_basis = 'annual'
  AND source_trade_date <= trade_date
""".strip(),
                parameters={"days": list(snapshot_dates)},
            ).result_rows
        }
    finally:
        client.close()
    missing = [
        (day, factor_id)
        for day in snapshot_dates
        for factor_id in factor_ids
        if (day, factor_id) not in present
    ]
    if missing:
        raise RuntimeError(f"V2 snapshot gaps remain: {missing}")
    unavailable = [
        (day, factor_id)
        for day in snapshot_dates
        for factor_id in factor_ids
        if (day, factor_id) not in non_null_present
    ]
    unexpected_unavailable = [
        pair for pair in unavailable if pair[1] not in UNAVAILABLE_CONSENSUS_FACTOR_IDS
    ]
    if unexpected_unavailable:
        raise RuntimeError(
            f"non-consensus V2 factors lack real values: {unexpected_unavailable}"
        )
    summary = {
        day: {
            "factor_count": len(factor_ids),
            "non_null_factor_count": int(
                frame.loc[
                    pd.to_datetime(frame["trade_date"]).eq(pd.Timestamp(day)),
                    "factor_id",
                ].nunique()
            ),
            "minimum_security_count": int(
                frame.loc[
                    pd.to_datetime(frame["trade_date"]).eq(pd.Timestamp(day)),
                    "security_count",
                ].min()
            ),
            "unavailable_consensus": [
                factor_id for pair_day, factor_id in unavailable if pair_day == day
            ],
        }
        for day in snapshot_dates
    }
    print(frame.to_string(index=False), flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    compute_parser = subparsers.add_parser("compute")
    compute_parser.add_argument("--workers", type=int, default=16)
    compute_parser.add_argument("--parts-per-shard", type=int, default=2)
    compute_parser.add_argument("--profile", choices=sorted(DATE_PROFILES), default="2021")
    snapshots_parser = subparsers.add_parser("snapshots")
    snapshots_parser.add_argument("--profile", choices=sorted(DATE_PROFILES), default="2021")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--profile", choices=sorted(DATE_PROFILES), default="2021")
    args = parser.parse_args()
    profile = DATE_PROFILES[args.profile]
    compute_dates = tuple(profile["compute_dates"])
    snapshot_dates = tuple(profile["snapshot_dates"])

    if args.command == "compute":
        print(
            json.dumps(
                compute(
                    workers=max(1, args.workers),
                    parts_per_shard=max(1, args.parts_per_shard),
                    target_dates=compute_dates,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "snapshots":
        load_snapshots(
            snapshot_dates=snapshot_dates,
            source_start_day=compute_dates[0],
        )
        mark_unavailable_consensus(snapshot_dates=snapshot_dates)
    else:
        verify(snapshot_dates=snapshot_dates)


if __name__ == "__main__":
    main()
