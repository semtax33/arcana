from __future__ import annotations

"""Fill the 2012 PVGO-compression warm-up gap without touching other factors."""

import argparse
from pathlib import Path

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from scripts.backfill_kr_historical_factors import (
    BackfillContract,
    _initial_status,
    _validated_status,
    _write_json,
    backup_and_delete_scope,
    backfill_factors,
    build_snapshots,
    verify,
)
from scripts.backfill_kr_pvgo_factors_2012_2016 import (
    FINANCIAL_DIR,
    REPORT_METADATA_PATH,
    TARGET_PATH,
)


START_DATE = "2012-01-01"
END_DATE = "2012-12-31"
FACTOR_IDS = ("pvgo_compression_pct",)
FINANCIAL_BASES = ("ttm",)
START_WARMUP_DAYS = 800
MARCAP_SHARES_PATH = DATA_LAKE.silver(
    "krx", "shares", "kr_pvgo_2011_2016_marcap_shares.csv"
)
STATUS_PATH = DATA_LAKE.meta("kr_pvgo_compression_2012_factor_backfill_status.json")
REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "deliverables"
    / "kr_pvgo_compression_2012_factor_load_report.json"
)


def load_contract(target_path: Path = TARGET_PATH) -> BackfillContract:
    target = pd.read_csv(target_path, dtype={"security_id": str})
    return BackfillContract.create(
        security_ids=target["security_id"].dropna(),
        factor_ids=FACTOR_IDS,
        start_date=START_DATE,
        end_date=END_DATE,
        bases=FINANCIAL_BASES,
    )


def run(
    command: str,
    *,
    batch_size: int = 64,
    parallel_workers: int = 12,
    apply: bool = False,
) -> None:
    client = get_clickhouse_client(send_receive_timeout=3_600)
    try:
        contract = load_contract()
        if command == "prepare":
            _write_json(STATUS_PATH, _initial_status(contract))
            print(f"[PREPARED] targets={len(contract.security_ids):,}", flush=True)
            return
        status = _validated_status(STATUS_PATH, contract)
        if command in {"factors", "all"}:
            if not apply:
                raise RuntimeError("factor backfill requires --apply")
            if not MARCAP_SHARES_PATH.exists():
                raise FileNotFoundError(MARCAP_SHARES_PATH)
            backup_and_delete_scope(client, contract, status, status_path=STATUS_PATH)
            backfill_factors(
                client,
                contract,
                status,
                status_path=STATUS_PATH,
                batch_size=batch_size,
                parallel_workers=parallel_workers,
                shares_path=MARCAP_SHARES_PATH,
                financial_dir=FINANCIAL_DIR,
                report_metadata_path=REPORT_METADATA_PATH,
                # Compression needs 252 trading days and WACC beta needs 104
                # weekly observations. Eight hundred calendar days covers
                # both without processing the generic eleven-year warm-up.
                start_warmup_days=START_WARMUP_DAYS,
            )
        if command in {"snapshots", "all"}:
            if not apply:
                raise RuntimeError("snapshot build requires --apply")
            build_snapshots(client, contract, status, status_path=STATUS_PATH)
        if command in {"verify", "all"}:
            verify(client, contract, status, report_path=REPORT_PATH)
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "factors", "snapshots", "verify", "all"))
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--parallel-workers", type=int, default=12)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    run(
        args.command,
        batch_size=max(1, args.batch_size),
        parallel_workers=max(1, args.parallel_workers),
        apply=args.apply,
    )


if __name__ == "__main__":
    main()
