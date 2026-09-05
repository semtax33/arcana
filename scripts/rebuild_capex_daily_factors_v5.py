from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Any
import warnings

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.loaders._internal.clickhouse_factors import insert_daily_factors
from engine.transformers._internal.factor_metrics import FactorMarketDataCache


CAPEX_FACTOR_IDS = (
    "capex_growth_2y_pct",
    "capx",
    "fc_to_ndr",
    "fcf",
    "fcf_yoy_pct",
    "fcfe",
    "fcff",
    "fcfpr",
)
REBUILD_SPLIT_INSERT_BY_PARTITION = True
DEFAULT_TARGETS = DATA_LAKE.meta("kr_capex_semantic_v5_factor_targets.csv")
DEFAULT_STATUS = DATA_LAKE.meta("kr_capex_semantic_v5_factor_rebuild_status.json")

# The factor builder intentionally materializes many derived columns. Its known
# fragmentation warning is diagnostic noise during a multi-process rebuild and
# can overwhelm the resumable progress channel.
warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp.replace(path)


def load_targets(path: Path) -> tuple[list[str], list[str], str]:
    frame = pd.read_csv(path, dtype={"symbol": str, "security_id": str})
    symbols = sorted(
        {
            str(value).strip().zfill(6)
            for value in frame["symbol"].dropna()
            if str(value).strip()
        }
    )
    security_ids = sorted(
        {
            str(value).strip()
            for value in frame["security_id"].dropna()
            if str(value).strip()
        }
    )
    if not symbols or set(security_ids) != {f"SEC_KR_{symbol}" for symbol in symbols}:
        raise ValueError("refusing CAPEX rebuild: target symbols and security IDs disagree")
    digest = sha256("\n".join(security_ids).encode("utf-8")).hexdigest()
    return symbols, security_ids, digest


def _backup_table(target_digest: str) -> str:
    return f"fact_daily_factors_semantic_v5_capex_backup_{target_digest[:12]}"


def _backup_years(start_date: str | None, end_date: str | None) -> list[int]:
    if not start_date:
        raise ValueError("CAPEX rebuild requires an explicit start date")
    start_year = int(str(start_date)[:4])
    end_year = (
        int(str(end_date)[:4])
        if end_date
        else datetime.now(timezone.utc).year
    )
    if end_year < start_year:
        raise ValueError("CAPEX rebuild end date precedes start date")
    return list(range(start_year, end_year + 1))


def _date_scope_sql(start_date: str | None, end_date: str | None) -> str:
    clauses = []
    if start_date:
        clauses.append("  AND trade_date >= {start_date:Date}")
    if end_date:
        clauses.append("  AND trade_date <= {end_date:Date}")
    return "\n".join(clauses)


def _scope_parameters(
    security_ids: list[str], start_date: str | None, end_date: str | None
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "factor_ids": list(CAPEX_FACTOR_IDS),
        "security_ids": security_ids,
    }
    if start_date:
        parameters["start_date"] = start_date
    if end_date:
        parameters["end_date"] = end_date
    return parameters


def _scoped_count(
    client,
    table: str,
    security_ids: list[str],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> int:
    date_scope = _date_scope_sql(start_date, end_date)
    return int(
        client.query(
            f"""
SELECT count()
FROM {table} FINAL
WHERE financial_basis = 'annual'
  AND factor_id IN {{factor_ids:Array(String)}}
  AND security_id IN {{security_ids:Array(String)}}
{date_scope}
""".strip(),
            parameters=_scope_parameters(security_ids, start_date, end_date),
        ).result_rows[0][0]
    )


def _source_count_for_run(
    client,
    status: dict[str, Any],
    security_ids: list[str],
    *,
    start_date: str | None,
    end_date: str | None,
) -> int:
    if status.get("delete_complete"):
        if status.get("source_row_count") is None:
            raise RuntimeError("completed delete is missing its verified source row count")
        return int(status["source_row_count"])
    return _scoped_count(
        client,
        "fact_daily_factors",
        security_ids,
        start_date=start_date,
        end_date=end_date,
    )


def rebuild(
    *,
    targets_path: Path,
    status_path: Path,
    apply: bool,
    start_date: str | None,
    end_date: str | None,
    batch_size: int,
    parallel_workers: int,
) -> dict[str, Any]:
    symbols, security_ids, target_digest = load_targets(targets_path)
    backup_table = _backup_table(target_digest)
    status = _read_json(status_path)
    contract = {
        "contract": "capex_daily_factor_rebuild/v5",
        "market": "kr",
        "financial_basis": "annual",
        "target_sha256": target_digest,
        "target_count": len(symbols),
        "factor_ids": list(CAPEX_FACTOR_IDS),
        "backup_table": backup_table,
        "start_date": start_date,
        "end_date": end_date,
        "point_in_time_policy": "require DART report metadata; no period-end fallback",
    }
    if status:
        for key, value in contract.items():
            if status.get(key) != value:
                raise ValueError(f"rebuild status contract mismatch for {key}")
    else:
        status = {
            **contract,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "backup_complete": False,
            "delete_complete": False,
            "completed_symbols": [],
        }
        _write_json(status_path, status)

    client = get_clickhouse_client(send_receive_timeout=3_600)
    try:
        source_count = _source_count_for_run(
            client,
            status,
            security_ids,
            start_date=start_date,
            end_date=end_date,
        )
        preflight = {**contract, "source_row_count": source_count, "apply": apply}
        if not apply:
            return preflight

        if not status["backup_complete"]:
            client.command(
                f"CREATE TABLE IF NOT EXISTS {backup_table} AS fact_daily_factors"
            )
            completed_backup_years = {
                int(value) for value in status.get("completed_backup_years", [])
            }
            backup_count = 0
            for backup_year in _backup_years(start_date, end_date):
                year_start = max(str(start_date), f"{backup_year:04d}-01-01")
                year_end = min(
                    str(end_date) if end_date else f"{backup_year:04d}-12-31",
                    f"{backup_year:04d}-12-31",
                )
                source_year_count = _scoped_count(
                    client,
                    "fact_daily_factors",
                    security_ids,
                    start_date=year_start,
                    end_date=year_end,
                )
                backup_year_count = _scoped_count(
                    client,
                    backup_table,
                    security_ids,
                    start_date=year_start,
                    end_date=year_end,
                )
                if backup_year_count not in {0, source_year_count}:
                    raise RuntimeError(
                        "backup year row count is neither empty nor complete: "
                        f"{backup_year} {backup_year_count}/{source_year_count}"
                    )
                if backup_year_count == 0 and source_year_count:
                    client.command(
                        f"""
INSERT INTO {backup_table}
SELECT *
FROM fact_daily_factors FINAL
WHERE financial_basis = 'annual'
  AND factor_id IN {{factor_ids:Array(String)}}
  AND security_id IN {{security_ids:Array(String)}}
{_date_scope_sql(year_start, year_end)}
""".strip(),
                        parameters=_scope_parameters(
                            security_ids, year_start, year_end
                        ),
                    )
                    backup_year_count = _scoped_count(
                        client,
                        backup_table,
                        security_ids,
                        start_date=year_start,
                        end_date=year_end,
                    )
                if backup_year_count != source_year_count:
                    raise RuntimeError(
                        f"backup year verification failed: {backup_year} "
                        f"{backup_year_count}/{source_year_count}"
                    )
                backup_count += backup_year_count
                completed_backup_years.add(backup_year)
                status["completed_backup_years"] = sorted(completed_backup_years)
                status["backup_row_count"] = backup_count
                status["updated_at"] = datetime.now(timezone.utc).isoformat()
                _write_json(status_path, status)
                print(
                    f"[CAPEX-v5] backup year={backup_year} "
                    f"rows={backup_year_count:,} total={backup_count:,}",
                    flush=True,
                )
            if backup_count != source_count:
                raise RuntimeError(
                    f"backup verification failed: {backup_count}/{source_count}"
                )
            status["source_row_count"] = source_count
            status["backup_row_count"] = backup_count
            status["backup_complete"] = True
            _write_json(status_path, status)

        if not status["delete_complete"]:
            date_scope = _date_scope_sql(start_date, end_date)
            client.command(
                f"""
ALTER TABLE fact_daily_factors DELETE
WHERE financial_basis = 'annual'
  AND factor_id IN {{factor_ids:Array(String)}}
  AND security_id IN {{security_ids:Array(String)}}
{date_scope}
SETTINGS mutations_sync = 2
""".strip(),
                parameters=_scope_parameters(security_ids, start_date, end_date),
            )
            if _scoped_count(
                client,
                "fact_daily_factors",
                security_ids,
                start_date=start_date,
                end_date=end_date,
            ) != 0:
                raise RuntimeError("scoped CAPEX factor delete did not complete")
            status["delete_complete"] = True
            _write_json(status_path, status)

        completed = {str(value) for value in status["completed_symbols"]}
        pending = [symbol for symbol in symbols if symbol not in completed]
        cache = FactorMarketDataCache(
            market="kr", start_date=start_date, end_date=end_date
        )
        started = time.monotonic()
        for offset in range(0, len(pending), max(1, batch_size)):
            batch = pending[offset : offset + max(1, batch_size)]
            result = insert_daily_factors(
                stock_codes=batch,
                market="kr",
                financial_basis="annual",
                start_date=start_date,
                end_date=end_date,
                factor_ids=list(CAPEX_FACTOR_IDS),
                require_report_metadata=True,
                use_edgartools=False,
                client=client,
                insert_catalog=False,
                reader_mode="cached",
                parallel_workers=max(1, parallel_workers),
                market_data_cache=cache,
                split_insert_by_partition=REBUILD_SPLIT_INSERT_BY_PARTITION,
                insert_batch_size=max(1, len(batch)),
            )
            completed.update(batch)
            status["completed_symbols"] = sorted(completed)
            status["inserted_row_count"] = int(status.get("inserted_row_count", 0)) + int(
                result.attrs.get("inserted_rows", 0)
            )
            status["updated_at"] = datetime.now(timezone.utc).isoformat()
            _write_json(status_path, status)
            print(
                f"[CAPEX-v5] completed={len(completed)}/{len(symbols)} "
                f"elapsed={time.monotonic() - started:.1f}s",
                flush=True,
            )
        status["rebuilt_row_count"] = _scoped_count(
            client,
            "fact_daily_factors",
            security_ids,
            start_date=start_date,
            end_date=end_date,
        )
        status["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(status_path, status)
        return status
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backup and rebuild only PIT-safe annual CAPEX-dependent daily factors."
    )
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--start-date", default="2015-01-01")
    parser.add_argument("--end-date")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--parallel-workers", type=int, default=4)
    args = parser.parse_args()
    print(
        json.dumps(
            rebuild(
                targets_path=args.targets,
                status_path=args.status,
                apply=args.apply,
                start_date=args.start_date,
                end_date=args.end_date,
                batch_size=args.batch_size,
                parallel_workers=args.parallel_workers,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
