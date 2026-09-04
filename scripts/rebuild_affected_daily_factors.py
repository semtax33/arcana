from __future__ import annotations

import argparse
import hashlib
import json
import time
import warnings
from pathlib import Path
from typing import Any

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.loaders._internal.clickhouse_factors import insert_daily_factors
from engine.transformers._internal.factor_metrics import FactorMarketDataCache


DEFAULT_TARGET_PATH = DATA_LAKE.meta("us_companyfacts_period_label_factor_targets.csv")
DEFAULT_STATUS_PATH = DATA_LAKE.meta("us_companyfacts_period_label_factor_rebuild_status.json")
DEFAULT_KR_TARGET_PATH = DATA_LAKE.meta("kr_dart_period_column_factor_targets.csv")
DEFAULT_KR_STATUS_PATH = DATA_LAKE.meta("kr_dart_period_column_factor_rebuild_status.json")
BASES = ("annual", "quarterly", "ttm")
DELETE_TABLES = (
    "fact_daily_factors",
    "fact_daily_factor_snapshot",
    "fact_daily_factor_score",
    "fact_daily_style_score",
)

warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


def _read_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            temp_path.write_text(serialized, encoding="utf-8")
            temp_path.replace(path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(attempt + 1)
    assert last_error is not None
    raise last_error


def _target_symbols(path: Path, *, market: str) -> tuple[list[str], list[str]]:
    frame = pd.read_csv(path, dtype={"symbol": str, "security_id": str})
    market = str(market).strip().lower()
    if market not in {"us", "kr"}:
        raise ValueError("market must be 'us' or 'kr'")
    symbols = sorted(
        {
            (
                str(symbol).strip().upper()
                if market == "us"
                else str(symbol).strip().zfill(6)
            )
            for symbol in frame["symbol"].dropna()
            if str(symbol).strip()
        }
    )
    security_ids = sorted(
        {
            str(security_id).strip()
            for security_id in frame["security_id"].dropna()
            if str(security_id).strip()
        }
    )
    expected_ids = {f"SEC_{market.upper()}_{symbol}" for symbol in symbols}
    if set(security_ids) != expected_ids:
        raise ValueError(
            f"target security_id values do not match the audited {market.upper()} symbols"
        )
    return symbols, security_ids


def delete_affected_rows(
    client: Any,
    *,
    market: str,
    security_ids: list[str],
    completed_tables: set[str] | None = None,
    on_table_complete: Any | None = None,
) -> None:
    prefix = f"SEC_{market.upper()}_"
    if not security_ids or any(not security_id.startswith(prefix) for security_id in security_ids):
        raise ValueError(f"refusing delete: expected a non-empty {prefix} security_id list")
    params = {"security_ids": security_ids}
    completed_tables = completed_tables or set()
    for table in DELETE_TABLES:
        if table in completed_tables:
            print(f"[SKIP] delete table={table} already completed", flush=True)
            continue
        print(
            f"[START] delete table={table}, security_ids={len(security_ids)}",
            flush=True,
        )
        client.command(
            f"ALTER TABLE {table} DELETE WHERE "
            "security_id IN {security_ids:Array(String)} "
            "SETTINGS mutations_sync = 2",
            parameters=params,
        )
        print(f"[DONE] delete table={table}", flush=True)
        if on_table_complete is not None:
            on_table_complete(table)


def rebuild_daily_factors(
    *,
    market: str,
    target_path: str | Path,
    status_path: str | Path,
    delete_first: bool,
    batch_size: int,
    parallel_workers: int,
    insert_batch_size: int,
    insert_max_rows: int,
) -> None:
    target_path = Path(target_path)
    status_path = Path(status_path)
    market = str(market).strip().lower()
    symbols, security_ids = _target_symbols(target_path, market=market)
    target_digest = hashlib.sha256("\n".join(security_ids).encode("utf-8")).hexdigest()
    status = _read_status(status_path)
    status.setdefault("market", market)
    status.setdefault("target_count", len(symbols))
    status.setdefault("target_sha256", target_digest)
    status.setdefault("deleted", False)
    status.setdefault("deleted_tables", [])
    status.setdefault("completed", {basis: [] for basis in BASES})
    if int(status.get("target_count", 0)) != len(symbols):
        raise ValueError("factor rebuild status target_count does not match the audit target")
    if status.get("market") != market or status.get("target_sha256") != target_digest:
        raise ValueError("factor rebuild status belongs to a different market or target set")

    client = get_clickhouse_client(send_receive_timeout=3_600)
    try:
        if delete_first and not bool(status.get("deleted")):
            deleted_tables = {
                str(table) for table in status.get("deleted_tables", [])
            }

            def checkpoint_deleted_table(table: str) -> None:
                deleted_tables.add(table)
                status["deleted_tables"] = sorted(deleted_tables)
                _write_status(status_path, status)

            delete_affected_rows(
                client,
                market=market,
                security_ids=security_ids,
                completed_tables=deleted_tables,
                on_table_complete=checkpoint_deleted_table,
            )
            status["deleted"] = True
            _write_status(status_path, status)
        if not bool(status.get("deleted")):
            raise RuntimeError("affected factor rows must be deleted before loading")

        cache = FactorMarketDataCache(market=market)
        started_at = time.monotonic()
        for basis in BASES:
            completed = {
                str(symbol).strip().upper()
                for symbol in status.get("completed", {}).get(basis, [])
            }
            pending = [symbol for symbol in symbols if symbol not in completed]
            print(
                f"[INFO] factor rebuild basis={basis}, total={len(symbols)}, "
                f"completed={len(completed)}, pending={len(pending)}",
                flush=True,
            )
            for offset in range(0, len(pending), batch_size):
                batch = pending[offset : offset + batch_size]
                result = insert_daily_factors(
                    stock_codes=batch,
                    market=market,
                    financial_basis=basis,
                    client=client,
                    insert_catalog=False,
                    insert_batch_size=insert_batch_size,
                    insert_max_rows=insert_max_rows,
                    progress_interval=max(1, min(len(batch), 5)),
                    reader_mode="cached",
                    parallel_workers=parallel_workers,
                    market_data_cache=cache,
                    use_edgartools=False,
                    split_insert_by_partition=False,
                )
                inserted_rows = int(result.attrs.get("inserted_rows", 0))
                completed.update(batch)
                status["completed"][basis] = sorted(completed)
                _write_status(status_path, status)
                print(
                    f"[DONE] factor batch basis={basis}, symbols={len(batch)}, "
                    f"inserted_rows={inserted_rows:,}, "
                    f"completed={len(completed)}/{len(symbols)}, "
                    f"elapsed={time.monotonic() - started_at:.1f}s",
                    flush=True,
                )
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete and rebuild factors only for audited period-label targets."
    )
    parser.add_argument("--market", choices=("us", "kr"), default="us")
    parser.add_argument("--targets")
    parser.add_argument("--status")
    parser.add_argument("--delete-first", action="store_true")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--parallel-workers", type=int, default=4)
    parser.add_argument("--insert-batch-size", type=int, default=8)
    parser.add_argument("--insert-max-rows", type=int, default=6_000_000)
    args = parser.parse_args()
    target_path = args.targets or str(
        DEFAULT_TARGET_PATH if args.market == "us" else DEFAULT_KR_TARGET_PATH
    )
    status_path = args.status or str(
        DEFAULT_STATUS_PATH if args.market == "us" else DEFAULT_KR_STATUS_PATH
    )
    rebuild_daily_factors(
        market=args.market,
        target_path=target_path,
        status_path=status_path,
        delete_first=args.delete_first,
        batch_size=max(args.batch_size, 1),
        parallel_workers=max(args.parallel_workers, 1),
        insert_batch_size=max(args.insert_batch_size, 1),
        insert_max_rows=max(args.insert_max_rows, 1),
    )


if __name__ == "__main__":
    main()
