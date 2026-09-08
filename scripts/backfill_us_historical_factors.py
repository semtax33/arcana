from __future__ import annotations

"""Resumable, exact-scope US factor and snapshot backfill for 2006-2016."""

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Any, Iterable

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE


BASES = ("annual", "quarterly", "ttm")
DEFAULT_START_DATE = "2006-01-01"
DEFAULT_END_DATE = "2016-12-31"
DEFAULT_TARGET_PATH = DATA_LAKE.meta("us_historical_2006_2016_factor_targets.csv")
DEFAULT_STATUS_PATH = DATA_LAKE.meta("us_historical_2006_2016_factor_backfill_status.json")
DEFAULT_REPORT_PATH = Path(__file__).resolve().parents[1] / "deliverables" / "us_historical_2006_2016_factor_load_report.json"
DEFAULT_PRICE_PATH = DATA_LAKE.silver(
    "us", "price", "us_normalized_price.csv"
)
FINANCIAL_DIR = DATA_LAKE.silver("sec", "normalized")
REPORT_METADATA_PATH = DATA_LAKE.silver("sec", "us_report_metadata.csv")
LIVE_TABLES = ("fact_daily_factors", "fact_daily_factor_snapshot")


def _digest(values: Iterable[str]) -> str:
    return sha256("\n".join(values).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


@dataclass(frozen=True)
class HistoricalBackfillContract:
    security_ids: tuple[str, ...]
    factor_ids: tuple[str, ...]
    start_date: str
    end_date: str
    bases: tuple[str, ...]
    target_sha256: str
    factor_sha256: str

    @classmethod
    def create(
        cls,
        *,
        security_ids: Iterable[str],
        factor_ids: Iterable[str],
        start_date: str = DEFAULT_START_DATE,
        end_date: str = DEFAULT_END_DATE,
        bases: Iterable[str] = BASES,
    ) -> "HistoricalBackfillContract":
        normalized_security_ids = tuple(
            sorted({str(value).strip() for value in security_ids if str(value).strip()})
        )
        if not normalized_security_ids or any(
            not value.startswith("SEC_US_") for value in normalized_security_ids
        ):
            raise ValueError("US historical backfill requires non-empty SEC_US_ ids")
        normalized_factor_ids = tuple(
            sorted(
                {
                    str(value).strip().lower()
                    for value in factor_ids
                    if str(value).strip()
                }
            )
        )
        if not normalized_factor_ids:
            raise ValueError("factor ids must not be empty")
        requested_bases = {str(value).strip().lower() for value in bases}
        normalized_bases = tuple(basis for basis in BASES if basis in requested_bases)
        if not normalized_bases:
            raise ValueError("at least one supported financial basis is required")
        if date.fromisoformat(start_date) > date.fromisoformat(end_date):
            raise ValueError("start_date must not be after end_date")
        return cls(
            security_ids=normalized_security_ids,
            factor_ids=normalized_factor_ids,
            start_date=start_date,
            end_date=end_date,
            bases=normalized_bases,
            target_sha256=_digest(normalized_security_ids),
            factor_sha256=_digest(normalized_factor_ids),
        )

    @property
    def years(self) -> tuple[int, ...]:
        return tuple(
            range(
                date.fromisoformat(self.start_date).year,
                date.fromisoformat(self.end_date).year + 1,
            )
        )

    @property
    def _table_tag(self) -> str:
        return (
            f"{self.start_date[:4]}_{self.end_date[:4]}_"
            f"{self.target_sha256[:12]}_{self.factor_sha256[:8]}"
        )

    @property
    def raw_backup_table(self) -> str:
        return f"fact_daily_factors_us_hist_backup_{self._table_tag}"

    @property
    def snapshot_backup_table(self) -> str:
        return f"fact_daily_factor_snapshot_us_hist_backup_{self._table_tag}"

    def metadata(self) -> dict[str, Any]:
        return {
            "market": "us",
            "start_date": self.start_date,
            "end_date": self.end_date,
            "bases": list(self.bases),
            "target_count": len(self.security_ids),
            "target_sha256": self.target_sha256,
            "factor_count": len(self.factor_ids),
            "factor_sha256": self.factor_sha256,
            "raw_backup_table": self.raw_backup_table,
            "snapshot_backup_table": self.snapshot_backup_table,
        }


def build_universe_query(
    *, start_date: str, end_date: str
) -> tuple[str, dict[str, object]]:
    if date.fromisoformat(start_date) > date.fromisoformat(end_date):
        raise ValueError("start_date must not be after end_date")
    return (
        """
SELECT
    security_id,
    replaceOne(security_id, {security_prefix:String}, '') AS symbol,
    min(trade_date) AS min_price_date,
    max(trade_date) AS max_price_date,
    count() AS price_row_count,
    sum(toInt64(dateDiff('day', toDate('1970-01-01'), trade_date)))
        AS price_date_sum,
    sum(
        toInt64(dateDiff('day', toDate('1970-01-01'), trade_date))
        * toInt64(dateDiff('day', toDate('1970-01-01'), trade_date))
    ) AS price_date_square_sum
FROM price_daily FINAL
WHERE startsWith(security_id, {security_prefix:String})
    AND trade_date >= {start_date:Date}
    AND trade_date <= {end_date:Date}
GROUP BY security_id
ORDER BY security_id
""".strip(),
        {
            "security_prefix": "SEC_US_",
            "start_date": start_date,
            "end_date": end_date,
        },
    )


PRICE_COVERAGE_COLUMNS = [
    "security_id",
    "min_price_date",
    "max_price_date",
    "price_row_count",
    "price_date_sum",
    "price_date_square_sum",
]


def read_local_price_coverage(
    path: Path,
    *,
    security_ids: Iterable[str],
    start_date: str,
    end_date: str,
    chunksize: int = 500_000,
) -> Any:
    """Read only price identity columns and summarize the exact factor scope."""

    import pandas as pd

    targets = {str(value) for value in security_ids}
    if not path.exists():
        raise RuntimeError(f"Silver price input does not exist: {path}")
    epoch = pd.Timestamp("1970-01-01")
    totals: dict[str, dict[str, Any]] = {}
    try:
        chunks = pd.read_csv(
            path,
            usecols=["security_id", "trade_date"],
            dtype={"security_id": "string", "trade_date": "string"},
            chunksize=max(1, int(chunksize)),
        )
        for chunk in chunks:
            trade_dates = pd.to_datetime(chunk["trade_date"], errors="coerce").dt.tz_localize(None)
            selected = chunk["security_id"].astype(str).isin(targets)
            selected &= trade_dates.between(start_date, end_date, inclusive="both")
            if not selected.any():
                continue
            scoped = pd.DataFrame(
                {
                    "security_id": chunk.loc[selected, "security_id"].astype(str),
                    "trade_date": trade_dates.loc[selected].dt.normalize(),
                }
            ).dropna(subset=["trade_date"])
            scoped["price_day"] = (
                (scoped["trade_date"] - epoch) // pd.Timedelta(days=1)
            ).astype("int64")
            for security_id, group in scoped.groupby("security_id", sort=False):
                current = totals.setdefault(
                    security_id,
                    {
                        "security_id": security_id,
                        "min_price_date": group["trade_date"].min(),
                        "max_price_date": group["trade_date"].max(),
                        "price_row_count": 0,
                        "price_date_sum": 0,
                        "price_date_square_sum": 0,
                    },
                )
                current["min_price_date"] = min(
                    current["min_price_date"], group["trade_date"].min()
                )
                current["max_price_date"] = max(
                    current["max_price_date"], group["trade_date"].max()
                )
                days = group["price_day"]
                current["price_row_count"] += int(len(group))
                current["price_date_sum"] += int(days.sum())
                current["price_date_square_sum"] += int((days * days).sum())
    except (OSError, ValueError, pd.errors.EmptyDataError) as exc:
        raise RuntimeError(f"cannot audit Silver price input: {path}") from exc
    return pd.DataFrame(totals.values(), columns=PRICE_COVERAGE_COLUMNS)


def _normalized_price_coverage(frame: Any) -> Any:
    import pandas as pd

    missing = set(PRICE_COVERAGE_COLUMNS).difference(frame.columns)
    if missing:
        raise RuntimeError(f"price coverage is missing columns: {sorted(missing)}")
    normalized = frame[PRICE_COVERAGE_COLUMNS].copy()
    normalized["security_id"] = normalized["security_id"].astype(str)
    for column in ("min_price_date", "max_price_date"):
        normalized[column] = (
            pd.to_datetime(normalized[column], errors="coerce")
            .dt.tz_localize(None)
            .dt.strftime("%Y-%m-%d")
        )
    for column in (
        "price_row_count",
        "price_date_sum",
        "price_date_square_sum",
    ):
        normalized[column] = pd.to_numeric(
            normalized[column], errors="raise"
        ).astype("int64")
    if normalized["security_id"].duplicated().any():
        raise RuntimeError("price coverage contains duplicate security ids")
    return normalized.sort_values("security_id").reset_index(drop=True)


def compare_price_coverage(expected: Any, actual: Any) -> dict[str, int]:
    """Fail closed when the local factor input differs from ClickHouse prices."""

    expected = _normalized_price_coverage(expected)
    actual = _normalized_price_coverage(actual)
    if not expected.equals(actual):
        expected_by_id = expected.set_index("security_id")
        actual_by_id = actual.set_index("security_id")
        ids = sorted(set(expected_by_id.index) | set(actual_by_id.index))
        mismatches = [
            security_id
            for security_id in ids
            if security_id not in expected_by_id.index
            or security_id not in actual_by_id.index
            or not expected_by_id.loc[security_id].equals(
                actual_by_id.loc[security_id]
            )
        ]
        raise RuntimeError(
            "Silver price input differs from ClickHouse price_daily for "
            f"{len(mismatches):,} securities; examples={mismatches[:10]}"
        )
    return {
        "price_security_count": int(len(expected)),
        "price_row_count": int(expected["price_row_count"].sum()),
    }


def _price_coverage_sha256(frame: Any) -> str:
    normalized = _normalized_price_coverage(frame)
    payload = normalized.to_csv(index=False, lineterminator="\n")
    return sha256(payload.encode("utf-8")).hexdigest()


def ensure_price_input_parity(
    client: Any,
    contract: HistoricalBackfillContract,
    status: dict[str, Any],
    *,
    status_path: Path,
    price_path: Path = DEFAULT_PRICE_PATH,
) -> dict[str, Any]:
    """Prove that the Silver rows used by the calculator match frozen DB scope."""

    import pandas as pd

    query, parameters = build_universe_query(
        start_date=contract.start_date, end_date=contract.end_date
    )
    result = client.query(query, parameters=parameters)
    expected = pd.DataFrame(result.result_rows, columns=result.column_names)
    expected_ids = set(expected.get("security_id", pd.Series(dtype="string")).astype(str))
    if expected_ids != set(contract.security_ids):
        raise RuntimeError(
            "current ClickHouse price universe differs from the frozen target contract"
        )
    stat = price_path.stat() if price_path.exists() else None
    fingerprint = {
        "path": str(price_path.resolve()),
        "size": int(stat.st_size) if stat else None,
        "mtime_ns": int(stat.st_mtime_ns) if stat else None,
        "clickhouse_coverage_sha256": _price_coverage_sha256(expected),
    }
    cached = status.get("price_input_parity")
    if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint:
        return dict(cached)
    actual = read_local_price_coverage(
        price_path,
        security_ids=contract.security_ids,
        start_date=contract.start_date,
        end_date=contract.end_date,
    )
    counts = compare_price_coverage(expected, actual)
    parity = {
        "verified_at": _now(),
        "fingerprint": fingerprint,
        **counts,
    }
    status["price_input_parity"] = parity
    status["updated_at"] = _now()
    _write_json(status_path, status)
    print(
        f"[PRICE-PARITY] securities={counts['price_security_count']:,}, "
        f"rows={counts['price_row_count']:,}",
        flush=True,
    )
    return parity


def _year_bounds(
    contract: HistoricalBackfillContract, year: int
) -> tuple[str, str]:
    return (
        max(date(year, 1, 1), date.fromisoformat(contract.start_date)).isoformat(),
        min(date(year, 12, 31), date.fromisoformat(contract.end_date)).isoformat(),
    )


def _scope_parameters(
    contract: HistoricalBackfillContract,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    financial_basis: str | None = None,
    security_ids: Iterable[str] | None = None,
) -> dict[str, object]:
    parameters: dict[str, object] = {
        "security_ids": list(security_ids or contract.security_ids),
        "factor_ids": list(contract.factor_ids),
        "bases": list(contract.bases),
        "start_date": start_date or contract.start_date,
        "end_date": end_date or contract.end_date,
    }
    if financial_basis is not None:
        parameters["financial_basis"] = financial_basis
    return parameters


def _scope_filter(*, one_basis: bool = False) -> str:
    basis_filter = (
        "financial_basis = {financial_basis:String}"
        if one_basis
        else "has({bases:Array(String)}, financial_basis)"
    )
    return (
        "security_id IN {security_ids:Array(String)} "
        "AND trade_date >= {start_date:Date} "
        "AND trade_date <= {end_date:Date} "
        f"AND {basis_filter} "
        "AND has({factor_ids:Array(String)}, factor_id)"
    )


def build_delete_query(
    contract: HistoricalBackfillContract,
    *,
    table: str,
    start_date: str | None = None,
    end_date: str | None = None,
    financial_basis: str | None = None,
    security_ids: Iterable[str] | None = None,
) -> tuple[str, dict[str, object]]:
    if table not in LIVE_TABLES and table not in {
        contract.raw_backup_table,
        contract.snapshot_backup_table,
    }:
        raise ValueError(f"unsupported table: {table}")
    one_basis = financial_basis is not None
    return (
        f"ALTER TABLE {table} DELETE WHERE {_scope_filter(one_basis=one_basis)} "
        "SETTINGS mutations_sync = 2",
        _scope_parameters(
            contract,
            start_date=start_date,
            end_date=end_date,
            financial_basis=financial_basis,
            security_ids=security_ids,
        ),
    )


def build_snapshot_copy_query(
    contract: HistoricalBackfillContract,
    *,
    year: int,
    financial_basis: str,
) -> tuple[str, dict[str, object]]:
    if financial_basis not in contract.bases:
        raise ValueError(f"financial basis is outside contract: {financial_basis}")
    start_date, end_date = _year_bounds(contract, year)
    parameters = _scope_parameters(
        contract,
        start_date=start_date,
        end_date=end_date,
        financial_basis=financial_basis,
    )
    return (
        f"""
INSERT INTO fact_daily_factor_snapshot
    (trade_date, security_id, factor_id, financial_basis, factor_value,
     source_trade_date, fiscal_year, financial_period, currency, updated_at)
SELECT
    trade_date,
    security_id,
    factor_id,
    financial_basis,
    factor_value,
    trade_date AS source_trade_date,
    fiscal_year,
    financial_period,
    currency,
    updated_at
FROM fact_daily_factors FINAL
WHERE {_scope_filter(one_basis=True)}
    AND isFinite(factor_value)
""".strip(),
        parameters,
    )


def _initial_status(contract: HistoricalBackfillContract) -> dict[str, Any]:
    return {
        "contract": "us_historical_all_factors/v1",
        **contract.metadata(),
        "created_at": _now(),
        "updated_at": _now(),
        "point_in_time_policy": (
            "financial facts require observed SEC report_date; period-end fallback is "
            "disabled; undefined values are abstained and never imputed"
        ),
        "backup_rows": {table: {} for table in LIVE_TABLES},
        "backup_complete": {table: False for table in LIVE_TABLES},
        "delete_complete": {table: False for table in LIVE_TABLES},
        "factor_in_progress": None,
        "factor_completed": {basis: [] for basis in contract.bases},
        "factor_inserted_rows": {basis: 0 for basis in contract.bases},
        "snapshot_in_progress": None,
        "snapshot_completed": {basis: [] for basis in contract.bases},
        "snapshot_rows": {},
    }


def _validated_status(
    path: Path, contract: HistoricalBackfillContract
) -> dict[str, Any]:
    status = _read_json(path)
    if not status:
        status = _initial_status(contract)
    for key, expected in contract.metadata().items():
        if status.get(key) != expected:
            raise ValueError(f"status contract mismatch for {key}")
    return status


def _load_contract(
    target_path: Path, *, start_date: str, end_date: str
) -> HistoricalBackfillContract:
    import pandas as pd

    from engine.transformers._internal.factor_metrics import preferred_factor_columns

    target = pd.read_csv(target_path, dtype={"security_id": "string"})
    return HistoricalBackfillContract.create(
        security_ids=target["security_id"].dropna().astype(str),
        factor_ids=preferred_factor_columns(),
        start_date=start_date,
        end_date=end_date,
        bases=BASES,
    )


def prepare_targets(
    client: Any,
    *,
    target_path: Path,
    status_path: Path,
    start_date: str,
    end_date: str,
    force: bool = False,
) -> HistoricalBackfillContract:
    import pandas as pd

    if target_path.exists() and not force:
        contract = _load_contract(
            target_path, start_date=start_date, end_date=end_date
        )
        _validated_status(status_path, contract)
        return contract
    if force and status_path.exists():
        raise RuntimeError("refusing to replace targets while a status file exists")
    query, parameters = build_universe_query(
        start_date=start_date, end_date=end_date
    )
    result = client.query(query, parameters=parameters)
    frame = pd.DataFrame(result.result_rows, columns=result.column_names)
    if frame.empty:
        raise RuntimeError("no US price universe found in the requested date range")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = target_path.with_suffix(target_path.suffix + ".tmp")
    try:
        frame.to_csv(temporary, index=False, encoding="utf-8")
        contract = _load_contract(
            temporary, start_date=start_date, end_date=end_date
        )
        if status_path.exists():
            status = _validated_status(status_path, contract)
            price_rows = int(frame["price_row_count"].sum())
            expected_rows = status.get("initial_price_rows")
            if expected_rows is not None and int(expected_rows) != price_rows:
                raise RuntimeError(
                    "recreated target price row count differs from frozen status"
                )
            cached_parity = status.get("price_input_parity")
            expected_coverage_sha = None
            if isinstance(cached_parity, dict):
                fingerprint = cached_parity.get("fingerprint")
                if isinstance(fingerprint, dict):
                    expected_coverage_sha = fingerprint.get(
                        "clickhouse_coverage_sha256"
                    )
            if (
                expected_coverage_sha
                and expected_coverage_sha != _price_coverage_sha256(frame)
            ):
                raise RuntimeError(
                    "recreated target price coverage differs from frozen status"
                )
            temporary.replace(target_path)
            print(
                f"[RECREATED] targets={len(frame):,}, price_rows={price_rows:,}, "
                f"hash={contract.target_sha256}",
                flush=True,
            )
            return contract
        temporary.replace(target_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    status = _initial_status(contract)
    status["initial_price_rows"] = int(frame["price_row_count"].sum())
    _write_json(status_path, status)
    print(
        f"[PREPARED] targets={len(frame):,}, price_rows="
        f"{status['initial_price_rows']:,}, hash={contract.target_sha256}",
        flush=True,
    )
    return contract


def _backup_table_name(
    contract: HistoricalBackfillContract, table: str
) -> str:
    if table == "fact_daily_factors":
        return contract.raw_backup_table
    if table == "fact_daily_factor_snapshot":
        return contract.snapshot_backup_table
    raise ValueError(f"unsupported live table: {table}")


def backup_and_delete_scope(
    client: Any,
    contract: HistoricalBackfillContract,
    status: dict[str, Any],
    *,
    table: str,
    status_path: Path,
) -> None:
    backup_table = _backup_table_name(contract, table)
    client.command(f"CREATE TABLE IF NOT EXISTS {backup_table} AS {table}")
    completed = status["backup_rows"].setdefault(table, {})
    for year in contract.years:
        key = str(year)
        if key in completed:
            continue
        start_date, end_date = _year_bounds(contract, year)
        parameters = _scope_parameters(
            contract, start_date=start_date, end_date=end_date
        )
        source_count = int(
            client.query(
                f"SELECT count() FROM {table} FINAL WHERE {_scope_filter()}",
                parameters=parameters,
            ).result_rows[0][0]
        )
        delete_query, delete_parameters = build_delete_query(
            contract,
            table=backup_table,
            start_date=start_date,
            end_date=end_date,
        )
        client.command(delete_query, parameters=delete_parameters)
        client.command(
            f"INSERT INTO {backup_table} SELECT * FROM {table} FINAL "
            f"WHERE {_scope_filter()}",
            parameters=parameters,
            settings={"max_partitions_per_insert_block": 100, "max_threads": 4},
        )
        backup_count = int(
            client.query(
                f"SELECT count() FROM {backup_table} FINAL WHERE {_scope_filter()}",
                parameters=parameters,
            ).result_rows[0][0]
        )
        if backup_count != source_count:
            raise RuntimeError(
                f"backup mismatch table={table}, year={year}: "
                f"{backup_count:,}/{source_count:,}"
            )
        completed[key] = source_count
        status["updated_at"] = _now()
        _write_json(status_path, status)
        print(
            f"[BACKUP] table={table}, year={year}, rows={source_count:,}",
            flush=True,
        )
    status["backup_complete"][table] = True
    _write_json(status_path, status)
    if status["delete_complete"].get(table):
        return
    delete_query, delete_parameters = build_delete_query(contract, table=table)
    client.command(delete_query, parameters=delete_parameters)
    remaining = int(
        client.query(
            f"SELECT count() FROM {table} FINAL WHERE {_scope_filter()}",
            parameters=_scope_parameters(contract),
        ).result_rows[0][0]
    )
    if remaining:
        raise RuntimeError(
            f"exact-scope delete incomplete table={table}, remaining={remaining:,}"
        )
    status["delete_complete"][table] = True
    status["updated_at"] = _now()
    _write_json(status_path, status)
    print(f"[DELETE] table={table}, exact scope removed", flush=True)


def _recover_factor_batch(
    client: Any,
    contract: HistoricalBackfillContract,
    status: dict[str, Any],
) -> bool:
    in_progress = status.get("factor_in_progress")
    if not isinstance(in_progress, dict):
        return False
    security_ids = [str(value) for value in in_progress.get("security_ids", [])]
    financial_basis = str(in_progress.get("financial_basis", ""))
    query, parameters = build_delete_query(
        contract,
        table="fact_daily_factors",
        financial_basis=financial_basis,
        security_ids=security_ids,
    )
    client.command(query, parameters=parameters)
    status["factor_in_progress"] = None
    return True


def backfill_factors(
    client: Any,
    contract: HistoricalBackfillContract,
    status: dict[str, Any],
    *,
    status_path: Path,
    batch_size: int,
    parallel_workers: int,
) -> None:
    import warnings

    import pandas as pd

    from engine.loaders._internal.clickhouse_factors import (
        insert_daily_factors,
        insert_factor_catalog,
    )
    from engine.transformers._internal.factor_metrics import FactorMarketDataCache

    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)

    table = "fact_daily_factors"
    if not status["backup_complete"].get(table) or not status[
        "delete_complete"
    ].get(table):
        raise RuntimeError("raw factor backup and exact-scope delete must finish first")
    if _recover_factor_batch(client, contract, status):
        _write_json(status_path, status)
        print("[RECOVERED] incomplete raw-factor batch removed", flush=True)
    insert_factor_catalog(client, factor_ids=list(contract.factor_ids))
    cache = FactorMarketDataCache(
        market="us",
        start_date=contract.start_date,
        end_date=contract.end_date,
    )
    started_at = time.monotonic()
    for basis in contract.bases:
        completed = {
            str(value).strip().upper()
            for value in status["factor_completed"].get(basis, [])
        }
        pending = [
            security_id.removeprefix("SEC_US_")
            for security_id in contract.security_ids
            if security_id.removeprefix("SEC_US_") not in completed
        ]
        print(
            f"[FACTOR] basis={basis}, completed={len(completed):,}/"
            f"{len(contract.security_ids):,}, pending={len(pending):,}",
            flush=True,
        )
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset : offset + batch_size]
            status["factor_in_progress"] = {
                "financial_basis": basis,
                "security_ids": [f"SEC_US_{symbol}" for symbol in batch],
            }
            status["updated_at"] = _now()
            _write_json(status_path, status)
            result = insert_daily_factors(
                stock_codes=batch,
                financial_basis=basis,
                start_date=contract.start_date,
                end_date=contract.end_date,
                market="us",
                insert_catalog=False,
                client=client,
                insert_batch_size=max(1, min(8, len(batch))),
                insert_max_rows=1_500_000,
                progress_interval=max(1, min(4, len(batch))),
                reader_mode="cached",
                parallel_workers=parallel_workers,
                market_data_cache=cache,
                factor_ids=list(contract.factor_ids),
                financial_dir=FINANCIAL_DIR,
                report_metadata_path=REPORT_METADATA_PATH,
                require_report_metadata=True,
                use_edgartools=False,
                split_insert_by_partition=False,
            )
            inserted = int(result.attrs.get("inserted_rows", 0))
            completed.update(batch)
            status["factor_completed"][basis] = sorted(completed)
            status["factor_inserted_rows"][basis] = int(
                status["factor_inserted_rows"].get(basis, 0)
            ) + inserted
            status["factor_in_progress"] = None
            status["updated_at"] = _now()
            _write_json(status_path, status)
            print(
                f"[FACTOR-DONE] basis={basis}, batch={len(batch)}, "
                f"rows={inserted:,}, completed={len(completed):,}/"
                f"{len(contract.security_ids):,}, "
                f"elapsed={(time.monotonic() - started_at) / 60:.1f}m",
                flush=True,
            )


def _count_year_basis(
    client: Any,
    contract: HistoricalBackfillContract,
    *,
    table: str,
    year: int,
    financial_basis: str,
) -> int:
    start_date, end_date = _year_bounds(contract, year)
    return int(
        client.query(
            f"SELECT count() FROM {table} FINAL WHERE "
            f"{_scope_filter(one_basis=True)} AND isFinite(factor_value)",
            parameters=_scope_parameters(
                contract,
                start_date=start_date,
                end_date=end_date,
                financial_basis=financial_basis,
            ),
        ).result_rows[0][0]
    )


def _recover_snapshot_year(
    client: Any,
    contract: HistoricalBackfillContract,
    status: dict[str, Any],
) -> bool:
    in_progress = status.get("snapshot_in_progress")
    if not isinstance(in_progress, dict):
        return False
    year = int(in_progress["year"])
    basis = str(in_progress["financial_basis"])
    start_date, end_date = _year_bounds(contract, year)
    query, parameters = build_delete_query(
        contract,
        table="fact_daily_factor_snapshot",
        start_date=start_date,
        end_date=end_date,
        financial_basis=basis,
    )
    client.command(query, parameters=parameters)
    status["snapshot_in_progress"] = None
    return True


def build_snapshots(
    client: Any,
    contract: HistoricalBackfillContract,
    status: dict[str, Any],
    *,
    status_path: Path,
) -> None:
    expected = {
        security_id.removeprefix("SEC_US_") for security_id in contract.security_ids
    }
    for basis in contract.bases:
        if set(status["factor_completed"].get(basis, [])) != expected:
            raise RuntimeError(f"raw factor backfill is incomplete for basis={basis}")
    table = "fact_daily_factor_snapshot"
    if not status["backup_complete"].get(table) or not status[
        "delete_complete"
    ].get(table):
        raise RuntimeError("snapshot backup and exact-scope delete must finish first")
    if _recover_snapshot_year(client, contract, status):
        _write_json(status_path, status)
        print("[RECOVERED] incomplete snapshot year removed", flush=True)
    started_at = time.monotonic()
    for basis in contract.bases:
        completed_years = {
            int(value) for value in status["snapshot_completed"].get(basis, [])
        }
        for year in contract.years:
            if year in completed_years:
                continue
            status["snapshot_in_progress"] = {
                "financial_basis": basis,
                "year": year,
            }
            status["updated_at"] = _now()
            _write_json(status_path, status)
            query, parameters = build_snapshot_copy_query(
                contract, year=year, financial_basis=basis
            )
            client.command(
                query,
                parameters=parameters,
                settings={"max_partitions_per_insert_block": 100, "max_threads": 4},
            )
            source_count = _count_year_basis(
                client,
                contract,
                table="fact_daily_factors",
                year=year,
                financial_basis=basis,
            )
            snapshot_count = _count_year_basis(
                client,
                contract,
                table=table,
                year=year,
                financial_basis=basis,
            )
            if source_count != snapshot_count:
                raise RuntimeError(
                    f"snapshot mismatch year={year}, basis={basis}: "
                    f"{snapshot_count:,}/{source_count:,}"
                )
            completed_years.add(year)
            status["snapshot_completed"][basis] = sorted(completed_years)
            status["snapshot_rows"][f"{year}:{basis}"] = snapshot_count
            status["snapshot_in_progress"] = None
            status["updated_at"] = _now()
            _write_json(status_path, status)
            print(
                f"[SNAPSHOT] year={year}, basis={basis}, rows={snapshot_count:,}, "
                f"elapsed={(time.monotonic() - started_at) / 60:.1f}m",
                flush=True,
            )


def _coverage_row(
    client: Any,
    contract: HistoricalBackfillContract,
    *,
    table: str,
    year: int,
    financial_basis: str,
) -> dict[str, Any]:
    start_date, end_date = _year_bounds(contract, year)
    source_date = "source_trade_date" if table.endswith("snapshot") else "trade_date"
    query = f"""
SELECT
    count() AS row_count,
    uniqExact(security_id) AS security_count,
    uniqExact(factor_id) AS factor_count,
    sum(cityHash64(toString(tuple(
        trade_date, security_id, factor_id, financial_basis, factor_value,
        fiscal_year, financial_period, currency
    )))) AS checksum,
    countIf({source_date} > trade_date) AS future_source_count
FROM {table} FINAL
WHERE {_scope_filter(one_basis=True)}
    AND isFinite(factor_value)
""".strip()
    row = client.query(
        query,
        parameters=_scope_parameters(
            contract,
            start_date=start_date,
            end_date=end_date,
            financial_basis=financial_basis,
        ),
    ).result_rows[0]
    return {
        "year": year,
        "financial_basis": financial_basis,
        "row_count": int(row[0]),
        "security_count": int(row[1]),
        "factor_count": int(row[2]),
        "checksum": int(row[3]),
        "future_source_count": int(row[4]),
    }


def verify(
    client: Any,
    contract: HistoricalBackfillContract,
    status: dict[str, Any],
    *,
    report_path: Path,
) -> dict[str, Any]:
    source: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    equality: dict[str, bool] = {}
    for year in contract.years:
        for basis in contract.bases:
            raw = _coverage_row(
                client,
                contract,
                table="fact_daily_factors",
                year=year,
                financial_basis=basis,
            )
            snapshot = _coverage_row(
                client,
                contract,
                table="fact_daily_factor_snapshot",
                year=year,
                financial_basis=basis,
            )
            source.append(raw)
            snapshots.append(snapshot)
            equality[f"{year}:{basis}"] = (
                raw["row_count"] == snapshot["row_count"]
                and raw["checksum"] == snapshot["checksum"]
                and snapshot["future_source_count"] == 0
            )
            print(f"[VERIFY] year={year}, basis={basis}", flush=True)
    price_query, price_parameters = build_universe_query(
        start_date=contract.start_date, end_date=contract.end_date
    )
    price_result = client.query(price_query, parameters=price_parameters)
    processed = {
        basis: len(status["factor_completed"].get(basis, []))
        for basis in contract.bases
    }
    report = {
        "contract": "us_historical_all_factors/v1",
        "generated_at": _now(),
        **contract.metadata(),
        "point_in_time_policy": status["point_in_time_policy"],
        "price_security_count": len(price_result.result_rows),
        "price_row_count": sum(int(row[4]) for row in price_result.result_rows),
        "price_input_parity": status.get("price_input_parity"),
        "processed_security_counts": processed,
        "all_targets_processed": all(
            count == len(contract.security_ids) for count in processed.values()
        ),
        "source": source,
        "snapshots": snapshots,
        "snapshot_source_equal": equality,
        "all_year_basis_snapshots_equal": bool(equality) and all(equality.values()),
        "undefined_values_policy": (
            "all contracted factors are attempted; undefined/non-finite values are "
            "abstained and never imputed as zero"
        ),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report": str(report_path),
                "all_targets_processed": report["all_targets_processed"],
                "all_year_basis_snapshots_equal": report[
                    "all_year_basis_snapshots_equal"
                ],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill all calculable US factors and exact daily snapshots for a "
            "historical date range."
        )
    )
    parser.add_argument(
        "command", choices=("prepare", "backfill", "snapshots", "verify", "all")
    )
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=DEFAULT_END_DATE)
    parser.add_argument("--target-path", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--price-path", type=Path, default=DEFAULT_PRICE_PATH)
    parser.add_argument("--status-path", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--parallel-workers", type=int, default=8)
    parser.add_argument("--force-prepare", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    client = get_clickhouse_client(send_receive_timeout=3_600)
    try:
        if args.command in {"prepare", "all"}:
            contract = prepare_targets(
                client,
                target_path=args.target_path,
                status_path=args.status_path,
                start_date=args.start_date,
                end_date=args.end_date,
                force=args.force_prepare,
            )
        else:
            contract = _load_contract(
                args.target_path,
                start_date=args.start_date,
                end_date=args.end_date,
            )
        status = _validated_status(args.status_path, contract)
        if args.command in {"backfill", "all"}:
            if not args.apply:
                raise RuntimeError("raw factor backfill requires --apply")
            ensure_price_input_parity(
                client,
                contract,
                status,
                status_path=args.status_path,
                price_path=args.price_path,
            )
            backup_and_delete_scope(
                client,
                contract,
                status,
                table="fact_daily_factors",
                status_path=args.status_path,
            )
            backfill_factors(
                client,
                contract,
                status,
                status_path=args.status_path,
                batch_size=max(1, args.batch_size),
                parallel_workers=max(1, args.parallel_workers),
            )
        if args.command in {"snapshots", "all"}:
            if not args.apply:
                raise RuntimeError("snapshot backfill requires --apply")
            backup_and_delete_scope(
                client,
                contract,
                status,
                table="fact_daily_factor_snapshot",
                status_path=args.status_path,
            )
            build_snapshots(
                client, contract, status, status_path=args.status_path
            )
        if args.command in {"verify", "all"}:
            verify(client, contract, status, report_path=args.report_path)
    finally:
        client.close()


if __name__ == "__main__":
    main()
