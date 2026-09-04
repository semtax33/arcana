from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.loaders.factor_snapshots import (
    build_factor_snapshot_insert_query,
    build_incremental_factor_snapshot_insert_query,
)
from engine.transformers._internal.factor_metrics import preferred_factor_columns


BASES = ("annual", "quarterly", "ttm")
DEFAULT_TARGET_PATH = DATA_LAKE.meta("us_companyfacts_period_label_factor_targets.csv")
DEFAULT_FACTOR_STATUS_PATH = DATA_LAKE.meta(
    "us_companyfacts_period_label_factor_rebuild_status.json"
)
DEFAULT_STATUS_PATH = DATA_LAKE.meta(
    "us_companyfacts_period_label_snapshot_rebuild_status.json"
)
MEMORY_SAFE_SETTINGS = {
    "max_bytes_before_external_group_by": 512 * 1024 * 1024,
    "max_bytes_before_external_sort": 512 * 1024 * 1024,
    "max_threads": 4,
}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
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


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _command_with_retry(client: Any, query: str, params: dict[str, Any]) -> None:
    from clickhouse_connect.driver.exceptions import OperationalError

    for attempt in range(5):
        try:
            client.command(query, parameters=params, settings=MEMORY_SAFE_SETTINGS)
            return
        except OperationalError:
            if attempt == 4:
                raise
            wait_seconds = (attempt + 1) * 5
            print(
                f"[WARN] snapshot insert connection failed; "
                f"retry={attempt + 1}/4 in {wait_seconds}s",
                flush=True,
            )
            time.sleep(wait_seconds)


def _targets(path: Path, market: str) -> tuple[list[str], list[str], str]:
    frame = pd.read_csv(path, dtype={"symbol": str, "security_id": str})
    symbols = sorted({str(value).strip().upper() for value in frame["symbol"].dropna()})
    security_ids = sorted(
        {str(value).strip() for value in frame["security_id"].dropna() if str(value).strip()}
    )
    prefix = f"SEC_{market.upper()}_"
    if not symbols or not security_ids or any(not value.startswith(prefix) for value in security_ids):
        raise ValueError(f"refusing rebuild: expected non-empty {prefix} target ids")
    digest = hashlib.sha256("\n".join(security_ids).encode("utf-8")).hexdigest()
    return symbols, security_ids, digest


def _validate_factor_rebuild(
    factor_status_path: Path,
    *,
    symbols: list[str],
) -> None:
    status = _read_json(factor_status_path)
    if not bool(status.get("deleted")):
        raise RuntimeError("affected factor deletion/rebuild has not been authorized and completed")
    completed = status.get("completed", {})
    expected = set(symbols)
    for basis in BASES:
        actual = {str(value).strip().upper() for value in completed.get(basis, [])}
        if actual != expected:
            raise RuntimeError(
                f"raw factor rebuild incomplete for {basis}: {len(actual)}/{len(expected)}"
            )


def _basis_metadata(
    client: Any,
    *,
    security_ids: list[str],
    basis: str,
    market: str,
    start_date: str | None,
    end_date: str | None,
) -> tuple[list[str], list[str]]:
    params = {"security_ids": security_ids, "financial_basis": basis}
    if start_date and end_date:
        resolved_start = start_date
        resolved_end = end_date
    else:
        bounds = client.query(
            """
SELECT min(trade_date), max(trade_date)
FROM fact_daily_factors
WHERE security_id IN {security_ids:Array(String)}
    AND financial_basis = {financial_basis:String}
    AND isFinite(factor_value)
""".strip(),
            parameters=params,
        ).result_rows[0]
        if bounds[0] is None or bounds[1] is None:
            return [], []
        resolved_start = max(str(bounds[0]), start_date) if start_date else str(bounds[0])
        resolved_end = min(str(bounds[1]), end_date) if end_date else str(bounds[1])
    if resolved_start > resolved_end:
        return [], []
    params.update(
        {
            "start_date": resolved_start,
            "end_date": resolved_end,
            "security_prefix": f"SEC_{market.upper()}_",
        }
    )
    dates = [
        str(row[0])
        for row in client.query(
            """
SELECT DISTINCT trade_date
FROM price_daily
WHERE trade_date >= {start_date:Date}
    AND trade_date <= {end_date:Date}
    AND startsWith(security_id, {security_prefix:String})
ORDER BY trade_date
""".strip(),
            parameters=params,
        ).result_rows
    ]
    factor_ids = sorted(preferred_factor_columns())
    return dates, factor_ids


def rebuild_snapshots(
    *,
    market: str,
    bases: tuple[str, ...],
    target_path: Path,
    factor_status_path: Path,
    status_path: Path,
    factor_chunk_size: int,
    factor_shard_index: int,
    factor_shard_count: int,
    resume_after: str | None,
    start_date: str | None,
    end_date: str | None,
) -> None:
    market = str(market).strip().lower()
    if market not in {"us", "kr"}:
        raise ValueError("market must be 'us' or 'kr'")
    invalid_bases = sorted(set(bases) - set(BASES))
    if not bases or invalid_bases:
        raise ValueError(f"invalid financial bases: {invalid_bases}")
    if factor_shard_count < 1 or not 0 <= factor_shard_index < factor_shard_count:
        raise ValueError("factor shard index must be within factor shard count")
    if resume_after and len(bases) != 1:
        raise ValueError("resume-after requires exactly one financial basis")
    symbols, security_ids, target_digest = _targets(target_path, market)
    _validate_factor_rebuild(factor_status_path, symbols=symbols)
    status = _read_json(status_path)
    if status and status.get("target_sha256") != target_digest:
        raise ValueError("snapshot status belongs to a different target set")
    status.setdefault("market", market)
    status.setdefault("target_count", len(security_ids))
    status.setdefault("target_sha256", target_digest)
    status.setdefault("start_date", start_date)
    status.setdefault("end_date", end_date)
    status.setdefault("completed_through", {})
    shard_metadata = {"index": factor_shard_index, "count": factor_shard_count}
    status.setdefault("factor_shard", shard_metadata)
    if status.get("factor_shard") != shard_metadata:
        raise ValueError("snapshot status belongs to a different factor shard")
    if resume_after:
        checkpoint = status["completed_through"].get(bases[0])
        if checkpoint is None:
            status["completed_through"][bases[0]] = resume_after
        elif checkpoint != resume_after:
            resume_after = checkpoint
    if status.get("start_date") != start_date or status.get("end_date") != end_date:
        raise ValueError("snapshot status belongs to a different date range")

    client = get_clickhouse_client(send_receive_timeout=3_600)
    started_at = time.monotonic()
    try:
        for basis in bases:
            snapshot_dates, factor_ids = _basis_metadata(
                client,
                security_ids=security_ids,
                basis=basis,
                market=market,
                start_date=start_date,
                end_date=end_date,
            )
            if not snapshot_dates or not factor_ids:
                raise RuntimeError(f"no rebuilt raw factor data found for basis={basis}")
            factor_ids = factor_ids[factor_shard_index::factor_shard_count]
            if not factor_ids:
                raise RuntimeError("factor shard is empty")
            previous_date = status["completed_through"].get(basis)
            if previous_date and previous_date not in snapshot_dates:
                raise RuntimeError(f"snapshot checkpoint date is invalid for basis={basis}")
            start_index = snapshot_dates.index(previous_date) + 1 if previous_date else 0
            pending_dates = snapshot_dates[start_index:]
            factor_chunks = list(_chunks(factor_ids, max(1, factor_chunk_size)))
            print(
                f"[INFO] snapshot basis={basis}, dates={len(snapshot_dates)}, "
                f"pending={len(pending_dates)}, factors={len(factor_ids)}, "
                f"chunks={len(factor_chunks)}",
                flush=True,
            )
            for date_index, snapshot_date in enumerate(pending_dates, start=1):
                for factor_chunk in factor_chunks:
                    if previous_date:
                        query, params = build_incremental_factor_snapshot_insert_query(
                            market=market,
                            snapshot_date=snapshot_date,
                            previous_snapshot_date=previous_date,
                            financial_basis=basis,
                            factor_ids=factor_chunk,
                            security_ids=security_ids,
                        )
                    else:
                        query, params = build_factor_snapshot_insert_query(
                            market=market,
                            financial_basis=basis,
                            factor_ids=factor_chunk,
                            carry_forward=True,
                            snapshot_dates=[snapshot_date],
                            security_ids=security_ids,
                        )
                    _command_with_retry(client, query, params)
                previous_date = snapshot_date
                status["completed_through"][basis] = snapshot_date
                _write_json(status_path, status)
                if date_index == 1 or date_index % 20 == 0 or date_index == len(pending_dates):
                    print(
                        f"[PROGRESS] snapshot basis={basis}, "
                        f"date={snapshot_date}, completed={start_index + date_index}/"
                        f"{len(snapshot_dates)}, elapsed={time.monotonic() - started_at:.1f}s",
                        flush=True,
                    )
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild carry-forward snapshots only for audited affected securities."
    )
    parser.add_argument("--market", choices=("us", "kr"), default="us")
    parser.add_argument("--basis", action="append", choices=BASES)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--factor-status", type=Path, default=DEFAULT_FACTOR_STATUS_PATH)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--factor-chunk-size", type=int, default=64)
    parser.add_argument("--factor-shard-index", type=int, default=0)
    parser.add_argument("--factor-shard-count", type=int, default=1)
    parser.add_argument("--resume-after")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    args = parser.parse_args()
    rebuild_snapshots(
        market=args.market,
        bases=tuple(args.basis or BASES),
        target_path=args.targets,
        factor_status_path=args.factor_status,
        status_path=args.status,
        factor_chunk_size=max(1, args.factor_chunk_size),
        factor_shard_index=args.factor_shard_index,
        factor_shard_count=args.factor_shard_count,
        resume_after=args.resume_after,
        start_date=args.start_date,
        end_date=args.end_date,
    )


if __name__ == "__main__":
    main()
