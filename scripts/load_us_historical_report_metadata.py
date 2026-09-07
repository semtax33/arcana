from __future__ import annotations

"""Backup and replace the exact historical US SEC report-metadata scope."""

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.loaders._internal.clickhouse_filings import (
    DART_REPORT_METADATA_TABLE,
    insert_report_metadata,
    read_report_metadata,
)


DEFAULT_TARGET_PATH = DATA_LAKE.meta("us_historical_2006_2016_factor_targets.csv")
DEFAULT_METADATA_PATH = DATA_LAKE.silver("sec", "us_report_metadata.csv")
DEFAULT_STATUS_PATH = DATA_LAKE.meta("us_historical_2006_2016_metadata_load_status.json")
DEFAULT_REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "deliverables"
    / "us_historical_2006_2016_metadata_load_report.json"
)


def _digest(values: Iterable[str]) -> str:
    return sha256("\n".join(values).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


@dataclass(frozen=True)
class HistoricalMetadataContract:
    security_ids: tuple[str, ...]
    start_year: int
    end_year: int
    target_sha256: str

    @classmethod
    def create(
        cls,
        security_ids: Iterable[str],
        start_year: int,
        end_year: int,
    ) -> "HistoricalMetadataContract":
        resolved = tuple(
            sorted({str(value).strip() for value in security_ids if str(value).strip()})
        )
        if not resolved or any(not value.startswith("SEC_US_") for value in resolved):
            raise ValueError("historical US metadata requires non-empty SEC_US_ ids")
        if int(start_year) > int(end_year):
            raise ValueError("start_year must not be after end_year")
        return cls(
            security_ids=resolved,
            start_year=int(start_year),
            end_year=int(end_year),
            target_sha256=_digest(resolved),
        )

    @property
    def backup_table(self) -> str:
        return (
            "dart_report_metadata_us_hist_backup_"
            f"{self.start_year}_{self.end_year}_{self.target_sha256[:12]}"
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "market": "us",
            "start_year": self.start_year,
            "end_year": self.end_year,
            "target_count": len(self.security_ids),
            "target_sha256": self.target_sha256,
            "backup_table": self.backup_table,
        }


def contract_from_target(path: Path, start_year: int, end_year: int) -> HistoricalMetadataContract:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        security_ids = [
            str(row.get("security_id") or "").strip()
            for row in csv.DictReader(stream)
        ]
    return HistoricalMetadataContract.create(security_ids, start_year, end_year)


def build_scope_filter(
    contract: HistoricalMetadataContract,
) -> tuple[str, dict[str, object]]:
    return (
        "security_id IN {security_ids:Array(String)} "
        "AND fiscal_year >= {start_year:UInt16} "
        "AND fiscal_year <= {end_year:UInt16}",
        {
            "security_ids": list(contract.security_ids),
            "start_year": contract.start_year,
            "end_year": contract.end_year,
        },
    )


def _read_status(path: Path, contract: HistoricalMetadataContract) -> dict[str, Any]:
    if path.exists():
        status = json.loads(path.read_text(encoding="utf-8"))
    else:
        status = {
            **contract.metadata(),
            "backup_complete": False,
            "backup_rows": None,
            "delete_complete": False,
            "insert_in_progress": False,
            "insert_complete": False,
            "inserted_rows": None,
            "updated_at": _now(),
        }
    for key, expected in contract.metadata().items():
        if status.get(key) != expected:
            raise ValueError(f"metadata load status contract mismatch for {key}")
    return status


def _count_scope(client: Any, table: str, contract: HistoricalMetadataContract) -> int:
    clause, parameters = build_scope_filter(contract)
    return int(
        client.query(
            f"SELECT count() FROM {table} FINAL WHERE {clause}",
            parameters=parameters,
        ).result_rows[0][0]
    )


def _delete_scope(client: Any, table: str, contract: HistoricalMetadataContract) -> None:
    clause, parameters = build_scope_filter(contract)
    client.command(
        f"ALTER TABLE {table} DELETE WHERE {clause} SETTINGS mutations_sync = 2",
        parameters=parameters,
    )


def validate_source_rows(
    metadata_path: Path,
    contract: HistoricalMetadataContract,
) -> pd.DataFrame:
    frame = read_report_metadata(
        metadata_path,
        market="us",
        start_year=contract.start_year,
        end_year=contract.end_year,
        security_ids=contract.security_ids,
    )
    if frame.empty:
        raise RuntimeError("historical US report metadata scope is empty")
    key = ["security_id", "fiscal_year", "fiscal_month", "source_type"]
    if frame.duplicated(key, keep=False).any():
        raise RuntimeError("historical US report metadata has duplicate period keys")
    period_end = pd.to_datetime(frame["period_end_date"], errors="coerce")
    report_date = pd.to_datetime(frame["report_date"], errors="coerce")
    if period_end.isna().any() or report_date.isna().any():
        raise RuntimeError("historical US report metadata has missing disclosure dates")
    if ((report_date - period_end).dt.days < 0).any():
        raise RuntimeError("historical US report metadata violates point-in-time ordering")
    return frame


def load_exact_scope(
    client: Any,
    contract: HistoricalMetadataContract,
    *,
    metadata_path: Path,
    status_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    source = validate_source_rows(metadata_path, contract)
    expected_rows = len(source)
    status = _read_status(status_path, contract)
    clause, parameters = build_scope_filter(contract)

    if not status["backup_complete"]:
        client.command(
            f"CREATE TABLE IF NOT EXISTS {contract.backup_table} "
            f"AS {DART_REPORT_METADATA_TABLE}"
        )
        original_rows = _count_scope(client, DART_REPORT_METADATA_TABLE, contract)
        _delete_scope(client, contract.backup_table, contract)
        client.command(
            f"INSERT INTO {contract.backup_table} "
            f"SELECT * FROM {DART_REPORT_METADATA_TABLE} FINAL WHERE {clause}",
            parameters=parameters,
        )
        backup_rows = _count_scope(client, contract.backup_table, contract)
        if backup_rows != original_rows:
            raise RuntimeError(
                "metadata backup mismatch: "
                f"backup={backup_rows:,}, source={original_rows:,}"
            )
        status["backup_rows"] = original_rows
        status["backup_complete"] = True
        status["updated_at"] = _now()
        _write_json(status_path, status)
        print(f"[BACKUP] report metadata rows={original_rows:,}", flush=True)

    if not status["delete_complete"]:
        _delete_scope(client, DART_REPORT_METADATA_TABLE, contract)
        remaining = _count_scope(client, DART_REPORT_METADATA_TABLE, contract)
        if remaining:
            raise RuntimeError(f"metadata exact-scope delete incomplete: {remaining:,}")
        status["delete_complete"] = True
        status["updated_at"] = _now()
        _write_json(status_path, status)
        print("[DELETE] report metadata exact scope removed", flush=True)

    if not status["insert_complete"]:
        # A failed partitioned insert is recovered by deleting the exact scope
        # again before replaying the deterministic source CSV.
        status["insert_in_progress"] = True
        status["updated_at"] = _now()
        _write_json(status_path, status)
        _delete_scope(client, DART_REPORT_METADATA_TABLE, contract)
        inserted = insert_report_metadata(
            path=metadata_path,
            client=client,
            market="us",
            start_year=contract.start_year,
            end_year=contract.end_year,
            security_ids=contract.security_ids,
        )
        actual_rows = _count_scope(client, DART_REPORT_METADATA_TABLE, contract)
        if inserted != expected_rows or actual_rows != expected_rows:
            raise RuntimeError(
                "metadata insert mismatch: "
                f"loader={inserted:,}, db={actual_rows:,}, expected={expected_rows:,}"
            )
        status["inserted_rows"] = inserted
        status["insert_in_progress"] = False
        status["insert_complete"] = True
        status["updated_at"] = _now()
        _write_json(status_path, status)
        print(f"[INSERT] report metadata rows={inserted:,}", flush=True)
    else:
        actual_rows = _count_scope(client, DART_REPORT_METADATA_TABLE, contract)
        if actual_rows != expected_rows:
            raise RuntimeError(
                f"completed metadata scope drifted: {actual_rows:,}/{expected_rows:,}"
            )

    report = {
        "contract": "us_historical_report_metadata/v1",
        "generated_at": _now(),
        **contract.metadata(),
        "source_rows": expected_rows,
        "database_rows": _count_scope(client, DART_REPORT_METADATA_TABLE, contract),
        "backup_rows": status["backup_rows"],
        "passed": True,
    }
    _write_json(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2006)
    parser.add_argument("--end-year", type=int, default=2016)
    parser.add_argument("--target-path", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--metadata-path", type=Path, default=DEFAULT_METADATA_PATH)
    parser.add_argument("--status-path", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    contract = contract_from_target(args.target_path, args.start_year, args.end_year)
    if not args.apply:
        rows = validate_source_rows(args.metadata_path, contract)
        print(
            f"[DRY-RUN] targets={len(contract.security_ids):,}, rows={len(rows):,}, "
            f"backup={contract.backup_table}",
            flush=True,
        )
        return
    client = get_clickhouse_client(send_receive_timeout=3_600)
    try:
        report = load_exact_scope(
            client,
            contract,
            metadata_path=args.metadata_path,
            status_path=args.status_path,
            report_path=args.report_path,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
