from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.core.source_storage import write_source_dataframe
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


START_DATE = "2012-01-01"
END_DATE = "2016-12-31"
FINANCIAL_BASES = ("ttm",)
FACTOR_IDS = (
    "pvgo_gap_pct",
    "roiic_wacc_spread",
    "pvgo_compression_pct",
    "pvgo_pct",
)
TARGET_PATH = DATA_LAKE.meta("kr_pvgo_2012_2016_factor_targets.csv")
STATUS_PATH = DATA_LAKE.meta("kr_pvgo_2012_2016_factor_backfill_status.json")
MARCAP_SHARES_PATH = DATA_LAKE.silver(
    "krx", "shares", "kr_pvgo_2012_2016_marcap_shares.csv"
)
FINANCIAL_DIR = DATA_LAKE.silver("dart", "pvgo-2012-2016-normalized")
REPORT_METADATA_PATH = DATA_LAKE.silver(
    "dart", "kr_pvgo_2012_2016_report_metadata.csv"
)
REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "deliverables"
    / "kr_pvgo_2012_2016_factor_load_report.json"
)


def build_target_query() -> str:
    return """
SELECT
    security_id,
    replaceOne(security_id, 'SEC_KR_', '') AS symbol,
    min(trade_date) AS min_price_date,
    max(trade_date) AS max_price_date,
    count() AS price_row_count
FROM price_daily FINAL
WHERE startsWith(security_id, 'SEC_KR_')
  AND trade_date BETWEEN {start_date:Date} AND {end_date:Date}
GROUP BY security_id
ORDER BY security_id
""".strip()


def prepare_target_manifest(client, *, target_path: Path = TARGET_PATH) -> pd.DataFrame:
    result = client.query(
        build_target_query(),
        parameters={"start_date": START_DATE, "end_date": END_DATE},
    )
    frame = pd.DataFrame(result.result_rows, columns=result.column_names)
    if frame.empty:
        raise RuntimeError("no Korean price universe found for 2012-2016")
    if frame["security_id"].duplicated().any():
        raise RuntimeError("target manifest contains duplicate security ids")
    write_source_dataframe(
        target_path,
        frame,
        source="clickhouse-price-daily-final",
        metadata={"start_date": START_DATE, "end_date": END_DATE},
    )
    return frame


def load_contract(target_path: Path = TARGET_PATH) -> BackfillContract:
    target = pd.read_csv(target_path, dtype={"security_id": str, "symbol": str})
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
    target_path: Path = TARGET_PATH,
    status_path: Path = STATUS_PATH,
    report_path: Path = REPORT_PATH,
    marcap_shares_path: Path = MARCAP_SHARES_PATH,
    batch_size: int = 16,
    parallel_workers: int = 12,
    apply: bool = False,
) -> None:
    client = get_clickhouse_client(send_receive_timeout=3_600)
    try:
        if command == "prepare":
            frame = prepare_target_manifest(client, target_path=target_path)
            contract = load_contract(target_path)
            status = _initial_status(contract)
            _write_json(status_path, status)
            print(
                f"[PREPARED] targets={len(frame):,}, contract={contract.factor_sha256}",
                flush=True,
            )
            return

        contract = load_contract(target_path)
        status = _validated_status(status_path, contract)
        if command in {"factors", "all"}:
            if not apply:
                raise RuntimeError("factor backfill requires --apply")
            if not marcap_shares_path.exists():
                raise FileNotFoundError(
                    "marcap shares recovery must complete before factor backfill: "
                    f"{marcap_shares_path}"
                )
            if not FINANCIAL_DIR.exists():
                raise FileNotFoundError(
                    "official DART XBRL financial recovery must complete before "
                    f"factor backfill: {FINANCIAL_DIR}"
                )
            if not REPORT_METADATA_PATH.exists():
                raise FileNotFoundError(
                    "point-in-time report metadata recovery must complete before "
                    f"factor backfill: {REPORT_METADATA_PATH}"
                )
            backup_and_delete_scope(
                client,
                contract,
                status,
                status_path=status_path,
            )
            backfill_factors(
                client,
                contract,
                status,
                status_path=status_path,
                batch_size=max(1, batch_size),
                parallel_workers=max(1, parallel_workers),
                shares_path=marcap_shares_path,
                financial_dir=FINANCIAL_DIR,
                report_metadata_path=REPORT_METADATA_PATH,
            )
        if command in {"snapshots", "all"}:
            if not apply:
                raise RuntimeError("snapshot build requires --apply")
            build_snapshots(
                client,
                contract,
                status,
                status_path=status_path,
            )
        if command in {"verify", "all"}:
            verify(client, contract, status, report_path=report_path)
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill the four TTM PVGO Expectations Alpha inputs and snapshots "
            "for the Korean 2012-2016 bridge period."
        )
    )
    parser.add_argument(
        "command", choices=("prepare", "factors", "snapshots", "verify", "all")
    )
    parser.add_argument("--target-path", type=Path, default=TARGET_PATH)
    parser.add_argument("--status-path", type=Path, default=STATUS_PATH)
    parser.add_argument("--report-path", type=Path, default=REPORT_PATH)
    parser.add_argument("--marcap-shares-path", type=Path, default=MARCAP_SHARES_PATH)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--parallel-workers", type=int, default=12)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    run(
        args.command,
        target_path=args.target_path,
        status_path=args.status_path,
        report_path=args.report_path,
        marcap_shares_path=args.marcap_shares_path,
        batch_size=args.batch_size,
        parallel_workers=args.parallel_workers,
        apply=args.apply,
    )


if __name__ == "__main__":
    main()
