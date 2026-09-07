from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Any, Iterable
import warnings

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.loaders._internal.clickhouse_factors import (
    insert_daily_factors,
    insert_factor_catalog,
)
from engine.transformers._internal.factor_metrics import (
    FactorMarketDataCache,
    market_applicable_factor_columns,
    preferred_factor_columns,
)
from engine.transformers._internal.filing_periods import (
    REPORT_METADATA_PATH,
    load_report_metadata,
)


BASES = ("annual", "quarterly", "ttm")
DEFAULT_START_DATE = "2002-01-01"
DEFAULT_END_DATE = "2012-12-31"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET_PATH = DATA_LAKE.meta("kr_historical_2002_2012_factor_targets.csv")
DEFAULT_STATUS_PATH = DATA_LAKE.meta("kr_historical_2002_2012_factor_backfill_status_v2.json")
DEFAULT_REPORT_PATH = ROOT / "deliverables" / "kr_historical_2002_2012_factor_load_report_v2.json"
PIT_DIVIDEND_PATH = DATA_LAKE.silver(
    "dart", "dividend", "kr_dividend_pit_events.csv"
)
HISTORICAL_SHARES_PATH = DATA_LAKE.silver(
    "krx", "shares", "kr_historical_2002_2012_shares.csv"
)

warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


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
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


@dataclass(frozen=True)
class BackfillContract:
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
    ) -> "BackfillContract":
        normalized_security_ids = tuple(
            sorted({str(value).strip() for value in security_ids if str(value).strip()})
        )
        if not normalized_security_ids or any(
            not value.startswith("SEC_KR_") for value in normalized_security_ids
        ):
            raise ValueError("KR historical backfill requires non-empty SEC_KR_ ids")
        normalized_factor_ids = tuple(
            sorted({str(value).strip().lower() for value in factor_ids if str(value).strip()})
        )
        if not normalized_factor_ids:
            raise ValueError("factor ids must not be empty")
        requested_bases = {str(value).strip() for value in bases}
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
    def backup_table(self) -> str:
        return (
            "fact_daily_factors_kr_hist_v2_backup_"
            f"{self.target_sha256[:12]}_{self.factor_sha256[:8]}"
        )

    @property
    def annual_stage_table(self) -> str:
        return (
            "fact_daily_factors_kr_hist_v2_stage_"
            f"{self.target_sha256[:12]}_{self.factor_sha256[:8]}"
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "market": "kr",
            "start_date": self.start_date,
            "end_date": self.end_date,
            "bases": list(self.bases),
            "target_count": len(self.security_ids),
            "target_sha256": self.target_sha256,
            "factor_count": len(self.factor_ids),
            "factor_sha256": self.factor_sha256,
            "backup_table": self.backup_table,
        }


def build_missing_target_query(
    *,
    start_date: str,
    end_date: str,
    bases: Iterable[str] = BASES,
) -> tuple[str, dict[str, object]]:
    normalized_bases = [basis for basis in BASES if basis in set(bases)]
    if not normalized_bases:
        raise ValueError("at least one supported financial basis is required")
    if date.fromisoformat(start_date) > date.fromisoformat(end_date):
        raise ValueError("start_date must not be after end_date")
    query = """
WITH
prices AS
(
    SELECT
        security_id,
        min(trade_date) AS min_price_date,
        max(trade_date) AS max_price_date,
        count() AS price_row_count
    FROM price_daily
    WHERE startsWith(security_id, 'SEC_KR_')
        AND trade_date >= {start_date:Date}
        AND trade_date <= {end_date:Date}
    GROUP BY security_id
),
existing AS
(
    SELECT DISTINCT security_id
    FROM fact_daily_factors
    WHERE startsWith(security_id, 'SEC_KR_')
        AND trade_date >= {start_date:Date}
        AND trade_date <= {end_date:Date}
        AND has({bases:Array(String)}, financial_basis)
)
SELECT
    prices.security_id,
    replaceOne(prices.security_id, 'SEC_KR_', '') AS symbol,
    prices.min_price_date,
    prices.max_price_date,
    prices.price_row_count
FROM prices
LEFT ANTI JOIN existing USING (security_id)
ORDER BY security_id
""".strip()
    return query, {
        "start_date": start_date,
        "end_date": end_date,
        "bases": normalized_bases,
    }


def build_universe_query(
    *,
    start_date: str,
    end_date: str,
    bases: Iterable[str] = BASES,
) -> tuple[str, dict[str, object]]:
    normalized_bases = [basis for basis in BASES if basis in set(bases)]
    query = """
WITH existing AS
(
    SELECT security_id, uniqExact(financial_basis) AS existing_basis_count
    FROM fact_daily_factors
    WHERE startsWith(security_id, 'SEC_KR_')
        AND trade_date >= {start_date:Date}
        AND trade_date <= {end_date:Date}
        AND has({bases:Array(String)}, financial_basis)
    GROUP BY security_id
)
SELECT
    prices.security_id,
    replaceOne(prices.security_id, 'SEC_KR_', '') AS symbol,
    prices.min_price_date,
    prices.max_price_date,
    prices.price_row_count,
    coalesce(existing.existing_basis_count, 0) AS initial_existing_basis_count
FROM
(
    SELECT
        security_id,
        min(trade_date) AS min_price_date,
        max(trade_date) AS max_price_date,
        count() AS price_row_count
    FROM price_daily
    WHERE startsWith(security_id, 'SEC_KR_')
        AND trade_date >= {start_date:Date}
        AND trade_date <= {end_date:Date}
    GROUP BY security_id
) AS prices
LEFT JOIN existing USING (security_id)
ORDER BY security_id
""".strip()
    return query, {
        "start_date": start_date,
        "end_date": end_date,
        "bases": normalized_bases,
    }


def build_incomplete_batch_cleanup_query(
    contract: BackfillContract,
    *,
    financial_basis: str,
    security_ids: Iterable[str],
) -> tuple[str, dict[str, object]]:
    if financial_basis not in contract.bases:
        raise ValueError(f"basis is outside the backfill contract: {financial_basis}")
    normalized_security_ids = sorted(
        {str(value).strip() for value in security_ids if str(value).strip()}
    )
    if not normalized_security_ids or not set(normalized_security_ids).issubset(
        contract.security_ids
    ):
        raise ValueError("cleanup security ids must be a non-empty contract subset")
    query = """
ALTER TABLE fact_daily_factors DELETE WHERE
    security_id IN {security_ids:Array(String)}
    AND trade_date >= {start_date:Date}
    AND trade_date <= {end_date:Date}
    AND financial_basis = {financial_basis:String}
    AND has({factor_ids:Array(String)}, factor_id)
SETTINGS mutations_sync = 2
""".strip()
    return query, {
        "security_ids": normalized_security_ids,
        "start_date": contract.start_date,
        "end_date": contract.end_date,
        "financial_basis": financial_basis,
        "factor_ids": list(contract.factor_ids),
    }


def basis_invariant_security_ids(
    contract: BackfillContract,
    report_metadata: pd.DataFrame,
) -> tuple[str, ...]:
    if report_metadata.empty:
        return contract.security_ids
    if not {"stock_code", "report_date"}.issubset(report_metadata.columns):
        return ()
    report_dates = pd.to_datetime(report_metadata["report_date"], errors="coerce")
    usable = report_metadata.loc[
        report_dates.notna() & report_dates.le(pd.Timestamp(contract.end_date)),
        "stock_code",
    ]
    sensitive_ids = {
        f"SEC_KR_{str(value).strip().zfill(6)}"
        for value in usable.dropna().astype(str)
    }
    return tuple(
        security_id
        for security_id in contract.security_ids
        if security_id not in sensitive_ids
    )


def build_basis_invariant_copy_query(
    contract: BackfillContract,
    *,
    financial_basis: str,
    security_ids: Iterable[str],
) -> tuple[str, dict[str, object]]:
    if financial_basis not in contract.bases or financial_basis == "annual":
        raise ValueError("basis-invariant copy target must be quarterly or ttm")
    normalized_security_ids = tuple(
        sorted({str(value).strip() for value in security_ids if str(value).strip()})
    )
    if not normalized_security_ids or not set(normalized_security_ids).issubset(
        contract.security_ids
    ):
        raise ValueError("copy security ids must be a non-empty contract subset")
    query = """
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
SELECT
    source_rows.security_id,
    source_rows.trade_date,
    source_rows.factor_id,
    {target_basis:String} AS financial_basis,
    source_rows.factor_value,
    source_rows.fiscal_year,
    source_rows.financial_period,
    source_rows.currency,
    source_rows.updated_at
FROM {annual_stage_table} AS source_rows FINAL
WHERE source_rows.security_id IN {security_ids:Array(String)}
    AND source_rows.trade_date >= {start_date:Date}
    AND source_rows.trade_date <= {end_date:Date}
    AND source_rows.financial_basis = {source_basis:String}
    AND has({factor_ids:Array(String)}, source_rows.factor_id)
    AND isFinite(source_rows.factor_value)
""".strip().replace("{annual_stage_table}", contract.annual_stage_table)
    return query, {
        "security_ids": list(normalized_security_ids),
        "start_date": contract.start_date,
        "end_date": contract.end_date,
        "source_basis": "annual",
        "target_basis": financial_basis,
        "factor_ids": list(contract.factor_ids),
    }


def recover_incomplete_batch(
    client,
    contract: BackfillContract,
    status: dict[str, object],
) -> bool:
    in_progress = status.get("in_progress")
    if not isinstance(in_progress, dict):
        return False
    financial_basis = str(in_progress.get("financial_basis") or "")
    security_ids = in_progress.get("security_ids")
    if not isinstance(security_ids, list):
        raise ValueError("invalid in-progress security ids")
    query, parameters = build_incomplete_batch_cleanup_query(
        contract,
        financial_basis=financial_basis,
        security_ids=security_ids,
    )
    client.command(query, parameters=parameters)
    status["in_progress"] = None
    return True


def build_snapshot_copy_query(
    contract: BackfillContract,
    *,
    year: int,
    financial_basis: str,
) -> tuple[str, dict[str, object]]:
    if financial_basis not in contract.bases:
        raise ValueError(f"basis is outside the backfill contract: {financial_basis}")
    year_start = max(date(year, 1, 1), date.fromisoformat(contract.start_date))
    year_end = min(date(year, 12, 31), date.fromisoformat(contract.end_date))
    if year_start > year_end:
        raise ValueError(f"year is outside the backfill contract: {year}")
    parameters: dict[str, object] = {
        "start_date": year_start.isoformat(),
        "end_date": year_end.isoformat(),
        "financial_basis": financial_basis,
        "factor_ids": list(contract.factor_ids),
        "security_ids": list(contract.security_ids),
    }
    query = """
INSERT INTO fact_daily_factor_snapshot
(
    trade_date,
    security_id,
    factor_id,
    financial_basis,
    factor_value,
    source_trade_date,
    fiscal_year,
    financial_period,
    currency,
    updated_at
)
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
WHERE security_id IN {security_ids:Array(String)}
    AND trade_date >= {start_date:Date}
    AND trade_date <= {end_date:Date}
    AND financial_basis = {financial_basis:String}
    AND has({factor_ids:Array(String)}, factor_id)
    AND isFinite(factor_value)
""".strip()
    return query, parameters


def _load_contract(target_path: Path) -> BackfillContract:
    target = pd.read_csv(
        target_path,
        dtype={"security_id": "string", "symbol": "string"},
    )
    return BackfillContract.create(
        security_ids=target["security_id"].dropna().astype(str),
        factor_ids=preferred_factor_columns(),
        start_date=DEFAULT_START_DATE,
        end_date=DEFAULT_END_DATE,
        bases=BASES,
    )


def _initial_status(contract: BackfillContract) -> dict[str, Any]:
    return {
        "contract": "kr_historical_all_factors/v2",
        **contract.metadata(),
        "created_at": _now(),
        "updated_at": _now(),
        "point_in_time_policy": (
            "all financial bases require an observed report_date; no period-end "
            "fallback; dividends use DART PIT receipt-date events"
        ),
        "backup_complete": False,
        "backup_years": {},
        "delete_complete": False,
        "in_progress": None,
        "completed": {basis: [] for basis in contract.bases},
        "inserted_rows": {basis: 0 for basis in contract.bases},
        "annual_stage_years": {},
        "annual_stage_complete": False,
        "basis_invariant_copy": {},
        "snapshot_in_progress": None,
        "snapshot_scope_deleted": False,
        "snapshot_completed": {basis: [] for basis in contract.bases},
        "snapshot_rows": {},
    }


def _validated_status(path: Path, contract: BackfillContract) -> dict[str, Any]:
    status = _read_json(path)
    if not status:
        status = _initial_status(contract)
    for key, expected in contract.metadata().items():
        if status.get(key) != expected:
            raise ValueError(f"status contract mismatch for {key}")
    return status


def prepare_targets(
    client,
    *,
    target_path: Path,
    status_path: Path,
    force: bool = False,
) -> BackfillContract:
    if target_path.exists() and not force:
        contract = _load_contract(target_path)
        _validated_status(status_path, contract)
        return contract
    if status_path.exists() and force:
        raise RuntimeError("refusing to replace a target manifest with an existing status")
    query, parameters = build_universe_query(
        start_date=DEFAULT_START_DATE,
        end_date=DEFAULT_END_DATE,
        bases=BASES,
    )
    result = client.query(query, parameters=parameters)
    frame = pd.DataFrame(result.result_rows, columns=result.column_names)
    if frame.empty:
        raise RuntimeError("no KR price universe found for 2002-2012")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target_path, index=False, encoding="utf-8")
    contract = _load_contract(target_path)
    status = _initial_status(contract)
    status["initial_universe_rows"] = int(len(frame))
    status["initial_no_factor_security_count"] = int(
        pd.to_numeric(frame["initial_existing_basis_count"], errors="coerce")
        .fillna(0)
        .eq(0)
        .sum()
    )
    _write_json(status_path, status)
    print(
        f"[PREPARED] targets={len(frame):,}, no-existing-factors="
        f"{status['initial_no_factor_security_count']:,}, hash={contract.target_sha256}",
        flush=True,
    )
    return contract


def _year_bounds(contract: BackfillContract, year: int) -> tuple[str, str]:
    return (
        max(date(year, 1, 1), date.fromisoformat(contract.start_date)).isoformat(),
        min(date(year, 12, 31), date.fromisoformat(contract.end_date)).isoformat(),
    )


def _factor_scope_parameters(
    contract: BackfillContract,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    financial_basis: str | None = None,
) -> dict[str, object]:
    parameters: dict[str, object] = {
        "security_ids": list(contract.security_ids),
        "factor_ids": list(contract.factor_ids),
        "bases": list(contract.bases),
        "start_date": start_date or contract.start_date,
        "end_date": end_date or contract.end_date,
    }
    if financial_basis:
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


def backup_and_delete_scope(
    client,
    contract: BackfillContract,
    status: dict[str, Any],
    *,
    status_path: Path,
) -> None:
    client.command(
        f"CREATE TABLE IF NOT EXISTS {contract.backup_table} AS fact_daily_factors"
    )
    for year in contract.years:
        key = str(year)
        if key in status["backup_years"]:
            continue
        start_date, end_date = _year_bounds(contract, year)
        parameters = _factor_scope_parameters(
            contract,
            start_date=start_date,
            end_date=end_date,
        )
        source_count = int(
            client.query(
                f"SELECT count() FROM fact_daily_factors FINAL WHERE {_scope_filter()}",
                parameters=parameters,
            ).result_rows[0][0]
        )
        client.command(
            f"ALTER TABLE {contract.backup_table} DELETE WHERE {_scope_filter()} "
            "SETTINGS mutations_sync = 2",
            parameters=parameters,
        )
        client.command(
            f"INSERT INTO {contract.backup_table} SELECT * FROM fact_daily_factors "
            f"FINAL WHERE {_scope_filter()}",
            parameters=parameters,
            settings={"max_partitions_per_insert_block": 100},
        )
        backup_count = int(
            client.query(
                f"SELECT count() FROM {contract.backup_table} WHERE {_scope_filter()}",
                parameters=parameters,
            ).result_rows[0][0]
        )
        if backup_count != source_count:
            raise RuntimeError(
                f"backup mismatch year={year}: {backup_count:,}/{source_count:,}"
            )
        status["backup_years"][key] = source_count
        status["updated_at"] = _now()
        _write_json(status_path, status)
        print(f"[BACKUP] year={year}, rows={source_count:,}", flush=True)
    status["backup_complete"] = True
    _write_json(status_path, status)
    if status.get("delete_complete"):
        return
    parameters = _factor_scope_parameters(contract)
    client.command(
        f"ALTER TABLE fact_daily_factors DELETE WHERE {_scope_filter()} "
        "SETTINGS mutations_sync = 2",
        parameters=parameters,
    )
    remaining = int(
        client.query(
            f"SELECT count() FROM fact_daily_factors FINAL WHERE {_scope_filter()}",
            parameters=parameters,
        ).result_rows[0][0]
    )
    if remaining:
        raise RuntimeError(f"factor scope delete incomplete: remaining={remaining:,}")
    status["delete_complete"] = True
    status["updated_at"] = _now()
    _write_json(status_path, status)
    print("[DELETE] exact historical factor scope removed and verified", flush=True)


def _count_security_basis(
    client,
    contract: BackfillContract,
    *,
    security_ids: Iterable[str],
    financial_basis: str,
    table: str = "fact_daily_factors",
    start_date: str | None = None,
    end_date: str | None = None,
) -> int:
    security_ids = list(security_ids)
    if not security_ids:
        return 0
    parameters = _factor_scope_parameters(
        contract,
        start_date=start_date,
        end_date=end_date,
        financial_basis=financial_basis,
    )
    parameters["security_ids"] = security_ids
    return int(
        client.query(
            f"SELECT count() FROM {table} FINAL WHERE "
            f"{_scope_filter(one_basis=True)} AND isFinite(factor_value)",
            parameters=parameters,
        ).result_rows[0][0]
    )


def ensure_annual_stage(
    client,
    contract: BackfillContract,
    status: dict[str, Any],
    *,
    status_path: Path,
) -> None:
    client.command(
        f"CREATE TABLE IF NOT EXISTS {contract.annual_stage_table} "
        "AS fact_daily_factors"
    )
    completed_years = status.setdefault("annual_stage_years", {})
    for year in contract.years:
        key = str(year)
        if key in completed_years:
            continue
        start_date, end_date = _year_bounds(contract, year)
        parameters = _factor_scope_parameters(
            contract,
            start_date=start_date,
            end_date=end_date,
            financial_basis="annual",
        )
        client.command(
            f"ALTER TABLE {contract.annual_stage_table} DELETE WHERE "
            f"{_scope_filter(one_basis=True)} SETTINGS mutations_sync = 2",
            parameters=parameters,
        )
        client.command(
            f"INSERT INTO {contract.annual_stage_table} "
            "SELECT * FROM fact_daily_factors FINAL WHERE "
            f"{_scope_filter(one_basis=True)} AND isFinite(factor_value)",
            parameters=parameters,
            settings={"max_partitions_per_insert_block": 100, "max_threads": 4},
        )
        source_count = _count_security_basis(
            client,
            contract,
            security_ids=contract.security_ids,
            financial_basis="annual",
            start_date=start_date,
            end_date=end_date,
        )
        stage_count = _count_security_basis(
            client,
            contract,
            security_ids=contract.security_ids,
            financial_basis="annual",
            table=contract.annual_stage_table,
            start_date=start_date,
            end_date=end_date,
        )
        if stage_count != source_count:
            raise RuntimeError(
                f"annual stage mismatch year={year}: "
                f"{stage_count:,}/{source_count:,}"
            )
        completed_years[key] = stage_count
        status["updated_at"] = _now()
        _write_json(status_path, status)
        print(f"[ANNUAL-STAGE] year={year}, rows={stage_count:,}", flush=True)
    status["annual_stage_complete"] = True
    status["updated_at"] = _now()
    _write_json(status_path, status)


def copy_basis_invariant_rows(
    client,
    contract: BackfillContract,
    status: dict[str, Any],
    *,
    financial_basis: str,
    security_ids: Iterable[str],
    status_path: Path,
) -> int:
    completed = set(status["completed"].get(financial_basis, []))
    pending_ids = tuple(
        security_id
        for security_id in security_ids
        if security_id.removeprefix("SEC_KR_") not in completed
    )
    if not pending_ids:
        return 0
    ensure_annual_stage(
        client,
        contract,
        status,
        status_path=status_path,
    )
    status["in_progress"] = {
        "financial_basis": financial_basis,
        "security_ids": list(pending_ids),
        "mode": "basis_invariant_copy",
    }
    status["updated_at"] = _now()
    _write_json(status_path, status)
    cleanup_query, cleanup_parameters = build_incomplete_batch_cleanup_query(
        contract,
        financial_basis=financial_basis,
        security_ids=pending_ids,
    )
    client.command(cleanup_query, parameters=cleanup_parameters)
    copied_count = 0
    for year in contract.years:
        start_date, end_date = _year_bounds(contract, year)
        query, parameters = build_basis_invariant_copy_query(
            contract,
            financial_basis=financial_basis,
            security_ids=pending_ids,
        )
        parameters["start_date"], parameters["end_date"] = start_date, end_date
        client.command(
            query,
            parameters=parameters,
            settings={"max_partitions_per_insert_block": 100, "max_threads": 4},
        )
        annual_count = _count_security_basis(
            client,
            contract,
            security_ids=pending_ids,
            financial_basis="annual",
            table=contract.annual_stage_table,
            start_date=start_date,
            end_date=end_date,
        )
        year_copy_count = _count_security_basis(
            client,
            contract,
            security_ids=pending_ids,
            financial_basis=financial_basis,
            start_date=start_date,
            end_date=end_date,
        )
        if year_copy_count != annual_count:
            raise RuntimeError(
                f"basis-invariant copy mismatch basis={financial_basis}, "
                f"year={year}: {year_copy_count:,}/{annual_count:,}"
            )
        copied_count += year_copy_count
        print(
            f"[BASIS-COPY] basis={financial_basis}, year={year}, "
            f"securities={len(pending_ids):,}, rows={year_copy_count:,}",
            flush=True,
        )
    completed.update(
        security_id.removeprefix("SEC_KR_") for security_id in pending_ids
    )
    status["completed"][financial_basis] = sorted(completed)
    status["inserted_rows"][financial_basis] = int(
        status["inserted_rows"].get(financial_basis, 0)
    ) + copied_count
    status.setdefault("basis_invariant_copy", {})[financial_basis] = {
        "security_count": len(pending_ids),
        "row_count": copied_count,
    }
    status["in_progress"] = None
    status["updated_at"] = _now()
    _write_json(status_path, status)
    print(
        f"[BASIS-COPY-DONE] basis={financial_basis}, "
        f"securities={len(pending_ids):,}, rows={copied_count:,}",
        flush=True,
    )
    return copied_count


def backfill_factors(
    client,
    contract: BackfillContract,
    status: dict[str, Any],
    *,
    status_path: Path,
    batch_size: int,
    parallel_workers: int,
    shares_path: Path | None = None,
    financial_dir: Path | None = None,
    report_metadata_path: Path = REPORT_METADATA_PATH,
    start_warmup_days: int = 366 * 11,
) -> None:
    if not status.get("backup_complete") or not status.get("delete_complete"):
        raise RuntimeError("backup and exact-scope deletion must complete first")
    if recover_incomplete_batch(client, contract, status):
        _write_json(status_path, status)
        print("[RECOVERED] incomplete factor batch removed", flush=True)
    insert_factor_catalog(client, factor_ids=list(contract.factor_ids))
    cache = FactorMarketDataCache(
        market="kr",
        start_date=contract.start_date,
        end_date=contract.end_date,
        start_warmup_days=start_warmup_days,
        shares_path=(
            shares_path
            if shares_path is not None
            else HISTORICAL_SHARES_PATH
            if HISTORICAL_SHARES_PATH.exists()
            else None
        ),
        dividend_path=PIT_DIVIDEND_PATH,
    )
    invariant_ids = basis_invariant_security_ids(
        contract,
        load_report_metadata(report_metadata_path),
    )
    print(
        f"[PIT-SCOPE] basis-invariant={len(invariant_ids):,}, "
        f"basis-sensitive={len(contract.security_ids) - len(invariant_ids):,}",
        flush=True,
    )
    started_at = time.monotonic()
    for basis in contract.bases:
        if basis != "annual":
            copy_basis_invariant_rows(
                client,
                contract,
                status,
                financial_basis=basis,
                security_ids=invariant_ids,
                status_path=status_path,
            )
        completed = set(status["completed"].get(basis, []))
        pending = [
            security_id.removeprefix("SEC_KR_")
            for security_id in contract.security_ids
            if security_id.removeprefix("SEC_KR_") not in completed
        ]
        print(
            f"[FACTOR] basis={basis}, completed={len(completed):,}/"
            f"{len(contract.security_ids):,}, pending={len(pending):,}",
            flush=True,
        )
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset : offset + batch_size]
            status["in_progress"] = {
                "financial_basis": basis,
                "security_ids": [f"SEC_KR_{symbol}" for symbol in batch],
            }
            status["updated_at"] = _now()
            _write_json(status_path, status)
            factor_kwargs: dict[str, Any] = {
                "factor_ids": list(contract.factor_ids),
                "require_report_metadata": True,
                "report_metadata_path": report_metadata_path,
            }
            if financial_dir is not None:
                factor_kwargs["financial_dir"] = financial_dir
            result = insert_daily_factors(
                stock_codes=batch,
                financial_basis=basis,
                start_date=contract.start_date,
                end_date=contract.end_date,
                market="kr",
                insert_catalog=False,
                client=client,
                insert_batch_size=max(1, min(8, len(batch))),
                insert_max_rows=1_500_000,
                progress_interval=max(1, min(4, len(batch))),
                reader_mode="cached",
                parallel_workers=parallel_workers,
                market_data_cache=cache,
                use_edgartools=False,
                split_insert_by_partition=False,
                **factor_kwargs,
            )
            inserted = int(result.attrs.get("inserted_rows", 0))
            completed.update(batch)
            status["completed"][basis] = sorted(completed)
            status["inserted_rows"][basis] = int(
                status["inserted_rows"].get(basis, 0)
            ) + inserted
            status["in_progress"] = None
            status["updated_at"] = _now()
            _write_json(status_path, status)
            print(
                f"[FACTOR-DONE] basis={basis}, batch={len(batch)}, rows={inserted:,}, "
                f"completed={len(completed):,}/{len(contract.security_ids):,}, "
                f"elapsed={(time.monotonic() - started_at) / 60:.1f}m",
                flush=True,
            )


def _snapshot_delete_query(
    contract: BackfillContract,
    *,
    year: int,
    financial_basis: str,
) -> tuple[str, dict[str, object]]:
    start_date, end_date = _year_bounds(contract, year)
    parameters = _factor_scope_parameters(
        contract,
        start_date=start_date,
        end_date=end_date,
        financial_basis=financial_basis,
    )
    return (
        "ALTER TABLE fact_daily_factor_snapshot DELETE WHERE "
        f"{_scope_filter(one_basis=True)} SETTINGS mutations_sync = 2",
        parameters,
    )


def recover_incomplete_snapshot(
    client,
    contract: BackfillContract,
    status: dict[str, Any],
) -> bool:
    in_progress = status.get("snapshot_in_progress")
    if not isinstance(in_progress, dict):
        return False
    financial_basis = str(in_progress.get("financial_basis", ""))
    year = int(in_progress.get("year", 0))
    query, parameters = _snapshot_delete_query(
        contract,
        year=year,
        financial_basis=financial_basis,
    )
    client.command(query, parameters=parameters)
    status["snapshot_in_progress"] = None
    return True


def _count_year_basis(
    client,
    contract: BackfillContract,
    *,
    table: str,
    year: int,
    financial_basis: str,
) -> int:
    start_date, end_date = _year_bounds(contract, year)
    parameters = _factor_scope_parameters(
        contract,
        start_date=start_date,
        end_date=end_date,
        financial_basis=financial_basis,
    )
    return int(
        client.query(
            f"SELECT count() FROM {table} FINAL WHERE "
            f"{_scope_filter(one_basis=True)} AND isFinite(factor_value)",
            parameters=parameters,
        ).result_rows[0][0]
    )


def build_snapshots(
    client,
    contract: BackfillContract,
    status: dict[str, Any],
    *,
    status_path: Path,
) -> None:
    expected = {value.removeprefix("SEC_KR_") for value in contract.security_ids}
    for basis in contract.bases:
        if set(status["completed"].get(basis, [])) != expected:
            raise RuntimeError(f"factor backfill is incomplete for basis={basis}")
    client.command(
        """
CREATE TABLE IF NOT EXISTS fact_daily_factor_snapshot
(
    trade_date Date,
    security_id String,
    factor_id LowCardinality(String),
    financial_basis LowCardinality(String) DEFAULT 'annual',
    factor_value Nullable(Float64),
    source_trade_date Date,
    fiscal_year Nullable(UInt16),
    financial_period Nullable(Date),
    currency LowCardinality(String) DEFAULT 'KRW',
    updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(trade_date)
ORDER BY (trade_date, factor_id, financial_basis, security_id)
SETTINGS index_granularity = 8192
""".strip()
    )
    if not status.get("snapshot_scope_deleted"):
        if any(status["snapshot_completed"].get(basis) for basis in contract.bases):
            raise RuntimeError(
                "snapshot completion exists without an initial scope-delete checkpoint"
            )
        parameters = _factor_scope_parameters(contract)
        client.command(
            "ALTER TABLE fact_daily_factor_snapshot DELETE WHERE "
            f"{_scope_filter()} SETTINGS mutations_sync = 2",
            parameters=parameters,
        )
        remaining = int(
            client.query(
                "SELECT count() FROM fact_daily_factor_snapshot FINAL WHERE "
                f"{_scope_filter()}",
                parameters=parameters,
            ).result_rows[0][0]
        )
        if remaining:
            raise RuntimeError(
                f"snapshot scope delete incomplete: remaining={remaining:,}"
            )
        status["snapshot_scope_deleted"] = True
        status["updated_at"] = _now()
        _write_json(status_path, status)
        print("[SNAPSHOT-DELETE] exact scope removed and verified", flush=True)
    elif recover_incomplete_snapshot(client, contract, status):
        status["updated_at"] = _now()
        _write_json(status_path, status)
        print("[SNAPSHOT-RECOVERED] incomplete year/basis removed", flush=True)
    started_at = time.monotonic()
    for basis in contract.bases:
        completed_years = {int(value) for value in status["snapshot_completed"][basis]}
        for year in contract.years:
            if year in completed_years:
                continue
            status["snapshot_in_progress"] = {
                "financial_basis": basis,
                "year": year,
            }
            _write_json(status_path, status)
            insert_query, insert_parameters = build_snapshot_copy_query(
                contract,
                year=year,
                financial_basis=basis,
            )
            client.command(
                insert_query,
                parameters=insert_parameters,
                settings={
                    "max_partitions_per_insert_block": 100,
                    "max_threads": 4,
                },
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
                table="fact_daily_factor_snapshot",
                year=year,
                financial_basis=basis,
            )
            if snapshot_count != source_count:
                raise RuntimeError(
                    f"snapshot mismatch {year}/{basis}: "
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


def _coverage_query(table: str, *, one_basis: bool = False) -> str:
    source_trade_date = (
        "source_trade_date"
        if table == "fact_daily_factor_snapshot"
        else "trade_date"
    )
    return f"""
SELECT
    toYear(trade_date) AS year,
    financial_basis,
    uniqExact(security_id) AS security_count,
    uniqExact(factor_id) AS factor_count,
    arraySort(groupUniqArray(factor_id)) AS factor_ids,
    count() AS row_count,
    sum(cityHash64(toString(tuple(
        trade_date, security_id, factor_id, financial_basis,
        factor_value, {source_trade_date}, fiscal_year,
        financial_period, currency, updated_at
    )))) AS checksum
FROM {table} FINAL
WHERE {{scope_filter}}
    AND isFinite(factor_value)
GROUP BY year, financial_basis
ORDER BY year, financial_basis
""".strip().replace(
        "{scope_filter}", _scope_filter(one_basis=one_basis)
    )


def _fetch_coverage_rows(
    client,
    contract: BackfillContract,
    *,
    table: str,
) -> list[tuple[Any, ...]]:
    """Scan one partition/basis at a time to keep verification restartable."""
    rows: list[tuple[Any, ...]] = []
    query = _coverage_query(table, one_basis=True)
    for year in contract.years:
        start_date, end_date = _year_bounds(contract, year)
        for basis in contract.bases:
            parameters = _factor_scope_parameters(
                contract,
                start_date=start_date,
                end_date=end_date,
                financial_basis=basis,
            )
            result = client.query(query, parameters=parameters)
            rows.extend(result.result_rows)
            print(
                f"[VERIFY-SCAN] table={table}, year={year}, basis={basis}",
                flush=True,
            )
    return rows


def _price_coverage_query() -> str:
    return """
SELECT
    toYear(trade_date) AS year,
    uniqExact(security_id) AS security_count,
    count() AS price_row_count
FROM price_daily
WHERE security_id IN {security_ids:Array(String)}
    AND trade_date >= {start_date:Date}
    AND trade_date <= {end_date:Date}
GROUP BY year
ORDER BY year
""".strip()


def calculate_factor_coverage(
    *,
    source_rows: Iterable[dict[str, Any]],
    price_rows: Iterable[dict[str, Any]],
    years: Iterable[int],
    bases: Iterable[str],
    contracted_factor_count: int,
) -> list[dict[str, Any]]:
    if contracted_factor_count <= 0:
        raise ValueError("contracted_factor_count must be positive")
    sources = {
        (int(row["year"]), str(row["financial_basis"])): row
        for row in source_rows
    }
    prices = {int(row["year"]): row for row in price_rows}
    coverage: list[dict[str, Any]] = []
    for year in sorted({int(value) for value in years}):
        price = prices.get(year, {})
        price_security_count = int(price.get("security_count", 0))
        price_row_count = int(price.get("price_row_count", 0))
        possible_cell_count = price_row_count * contracted_factor_count
        for basis in bases:
            source = sources.get((year, str(basis)), {})
            factor_security_count = int(source.get("security_count", 0))
            materialized_factor_count = int(source.get("factor_count", 0))
            finite_cell_count = int(source.get("row_count", 0))
            coverage.append(
                {
                    "year": year,
                    "financial_basis": str(basis),
                    "price_security_count": price_security_count,
                    "factor_security_count": factor_security_count,
                    "security_coverage_pct": round(
                        100.0 * factor_security_count / price_security_count, 6
                    )
                    if price_security_count
                    else 0.0,
                    "materialized_factor_count": materialized_factor_count,
                    "contracted_factor_count": contracted_factor_count,
                    "factor_id_coverage_pct": round(
                        100.0 * materialized_factor_count / contracted_factor_count, 6
                    ),
                    "finite_factor_cell_count": finite_cell_count,
                    "possible_factor_cell_count": possible_cell_count,
                    "factor_cell_coverage_pct": round(
                        100.0 * finite_cell_count / possible_cell_count, 6
                    )
                    if possible_cell_count
                    else 0.0,
                    "checksum": int(source.get("checksum", 0)),
                }
            )
    return coverage


def calculate_market_applicable_factor_coverage(
    *,
    source_rows: Iterable[dict[str, Any]],
    price_rows: Iterable[dict[str, Any]],
    years: Iterable[int],
    bases: Iterable[str],
    global_factor_ids: Iterable[str],
    applicable_factor_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Rebase coverage only when excluded factors contributed no numerator rows."""
    sources = list(source_rows)
    global_ids = {str(value) for value in global_factor_ids}
    applicable_ids = {str(value) for value in applicable_factor_ids}
    if not applicable_ids or not applicable_ids.issubset(global_ids):
        raise ValueError("applicable factors must be a non-empty global-contract subset")
    excluded_ids = global_ids - applicable_ids
    materialized_excluded = {
        str(factor_id)
        for row in sources
        for factor_id in row.get("factor_ids", [])
        if str(factor_id) in excluded_ids
    }
    if materialized_excluded:
        raise ValueError(
            "excluded factor rows require a scoped numerator query: "
            + ", ".join(sorted(materialized_excluded))
        )
    return calculate_factor_coverage(
        source_rows=sources,
        price_rows=price_rows,
        years=years,
        bases=bases,
        contracted_factor_count=len(applicable_ids),
    )


def aggregate_factor_coverage(
    *,
    source_rows: Iterable[dict[str, Any]],
    price_rows: Iterable[dict[str, Any]],
    bases: Iterable[str],
    contracted_factor_count: int,
) -> list[dict[str, Any]]:
    if contracted_factor_count <= 0:
        raise ValueError("contracted_factor_count must be positive")
    sources = list(source_rows)
    prices = list(price_rows)
    price_security_year_count = sum(int(row.get("security_count", 0)) for row in prices)
    price_row_count = sum(int(row.get("price_row_count", 0)) for row in prices)
    possible_cell_count = price_row_count * contracted_factor_count
    result: list[dict[str, Any]] = []
    for basis in bases:
        basis_rows = [
            row for row in sources if str(row.get("financial_basis")) == str(basis)
        ]
        factor_security_year_count = sum(
            int(row.get("security_count", 0)) for row in basis_rows
        )
        finite_cell_count = sum(int(row.get("row_count", 0)) for row in basis_rows)
        materialized_factor_ids = {
            str(factor_id)
            for row in basis_rows
            for factor_id in row.get("factor_ids", [])
        }
        materialized_factor_count = len(materialized_factor_ids)
        if not materialized_factor_ids:
            materialized_factor_count = max(
                (int(row.get("factor_count", 0)) for row in basis_rows),
                default=0,
            )
        result.append(
            {
                "financial_basis": str(basis),
                "price_security_year_count": price_security_year_count,
                "factor_security_year_count": factor_security_year_count,
                "security_year_coverage_pct": round(
                    100.0
                    * factor_security_year_count
                    / price_security_year_count,
                    6,
                )
                if price_security_year_count
                else 0.0,
                "materialized_factor_count": materialized_factor_count,
                "contracted_factor_count": contracted_factor_count,
                "factor_id_coverage_pct": round(
                    100.0 * materialized_factor_count / contracted_factor_count,
                    6,
                ),
                "finite_factor_cell_count": finite_cell_count,
                "possible_factor_cell_count": possible_cell_count,
                "factor_cell_coverage_pct": round(
                    100.0 * finite_cell_count / possible_cell_count,
                    6,
                )
                if possible_cell_count
                else 0.0,
            }
        )
    return result


def verify(
    client,
    contract: BackfillContract,
    status: dict[str, Any],
    *,
    report_path: Path,
) -> dict[str, Any]:
    parameters = _factor_scope_parameters(contract)
    source_rows = _fetch_coverage_rows(
        client, contract, table="fact_daily_factors"
    )
    snapshot_rows = _fetch_coverage_rows(
        client, contract, table="fact_daily_factor_snapshot"
    )
    price_result = client.query(_price_coverage_query(), parameters=parameters)
    columns = (
        "year",
        "financial_basis",
        "security_count",
        "factor_count",
        "factor_ids",
        "row_count",
        "checksum",
    )
    source = [dict(zip(columns, row)) for row in source_rows]
    snapshots = [dict(zip(columns, row)) for row in snapshot_rows]
    price_columns = ("year", "security_count", "price_row_count")
    price = [dict(zip(price_columns, row)) for row in price_result.result_rows]
    source_coverage = calculate_factor_coverage(
        source_rows=source,
        price_rows=price,
        years=contract.years,
        bases=contract.bases,
        contracted_factor_count=len(contract.factor_ids),
    )
    snapshot_coverage = calculate_factor_coverage(
        source_rows=snapshots,
        price_rows=price,
        years=contract.years,
        bases=contract.bases,
        contracted_factor_count=len(contract.factor_ids),
    )
    source_coverage_summary = aggregate_factor_coverage(
        source_rows=source,
        price_rows=price,
        bases=contract.bases,
        contracted_factor_count=len(contract.factor_ids),
    )
    snapshot_coverage_summary = aggregate_factor_coverage(
        source_rows=snapshots,
        price_rows=price,
        bases=contract.bases,
        contracted_factor_count=len(contract.factor_ids),
    )
    market_applicable = set(market_applicable_factor_columns("kr"))
    kr_applicable_factor_ids = tuple(
        factor_id
        for factor_id in contract.factor_ids
        if factor_id in market_applicable
    )
    source_kr_applicable_coverage = calculate_market_applicable_factor_coverage(
        source_rows=source,
        price_rows=price,
        years=contract.years,
        bases=contract.bases,
        global_factor_ids=contract.factor_ids,
        applicable_factor_ids=kr_applicable_factor_ids,
    )
    snapshot_kr_applicable_coverage = calculate_market_applicable_factor_coverage(
        source_rows=snapshots,
        price_rows=price,
        years=contract.years,
        bases=contract.bases,
        global_factor_ids=contract.factor_ids,
        applicable_factor_ids=kr_applicable_factor_ids,
    )
    source_kr_applicable_coverage_summary = aggregate_factor_coverage(
        source_rows=source,
        price_rows=price,
        bases=contract.bases,
        contracted_factor_count=len(kr_applicable_factor_ids),
    )
    snapshot_kr_applicable_coverage_summary = aggregate_factor_coverage(
        source_rows=snapshots,
        price_rows=price,
        bases=contract.bases,
        contracted_factor_count=len(kr_applicable_factor_ids),
    )
    source_by_key = {
        (int(row["year"]), str(row["financial_basis"])): row for row in source
    }
    snapshot_by_key = {
        (int(row["year"]), str(row["financial_basis"])): row for row in snapshots
    }
    expected_keys = {(year, basis) for year in contract.years for basis in contract.bases}
    equality = {
        f"{year}:{basis}": (
            source_by_key.get((year, basis)) == snapshot_by_key.get((year, basis))
        )
        for year, basis in sorted(expected_keys)
    }
    processed = {
        basis: len(status["completed"].get(basis, [])) for basis in contract.bases
    }
    report = {
        "contract": "kr_historical_all_factors/v2",
        "generated_at": _now(),
        **contract.metadata(),
        "point_in_time_policy": status["point_in_time_policy"],
        "processed_security_counts": processed,
        "all_targets_processed": all(
            count == len(contract.security_ids) for count in processed.values()
        ),
        "source": source,
        "snapshots": snapshots,
        "price_universe_by_year": price,
        "source_coverage_by_year_basis": source_coverage,
        "snapshot_coverage_by_year_basis": snapshot_coverage,
        "source_coverage_summary": source_coverage_summary,
        "snapshot_coverage_summary": snapshot_coverage_summary,
        "factor_contracts": {
            "global_factor_count": len(contract.factor_ids),
            "kr_applicable_factor_count": len(kr_applicable_factor_ids),
            "kr_excluded_factor_ids": sorted(
                set(contract.factor_ids) - set(kr_applicable_factor_ids)
            ),
        },
        "source_kr_applicable_coverage_by_year_basis": source_kr_applicable_coverage,
        "snapshot_kr_applicable_coverage_by_year_basis": snapshot_kr_applicable_coverage,
        "source_kr_applicable_coverage_summary": source_kr_applicable_coverage_summary,
        "snapshot_kr_applicable_coverage_summary": snapshot_kr_applicable_coverage_summary,
        "basis_invariant_copy": status.get("basis_invariant_copy", {}),
        "snapshot_source_equal": equality,
        "all_year_basis_snapshots_equal": (
            set(source_by_key) == expected_keys
            and set(snapshot_by_key) == expected_keys
            and all(equality.values())
        ),
        "undefined_values_policy": (
            f"all {len(contract.factor_ids)} contracted factors are attempted; "
            "undefined/non-finite values are abstained and never imputed as zero"
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
        description="Backfill all calculable KR factors and exact daily snapshots for 2002-2012."
    )
    parser.add_argument(
        "command", choices=("prepare", "backfill", "snapshots", "verify", "all")
    )
    parser.add_argument("--target-path", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--status-path", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--parallel-workers", type=int, default=12)
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
                force=args.force_prepare,
            )
        else:
            contract = _load_contract(args.target_path)
        status = _validated_status(args.status_path, contract)
        if args.command in {"backfill", "all"}:
            if not args.apply:
                raise RuntimeError("backfill requires --apply")
            backup_and_delete_scope(
                client,
                contract,
                status,
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
                raise RuntimeError("snapshot build requires --apply")
            build_snapshots(
                client,
                contract,
                status,
                status_path=args.status_path,
            )
        if args.command in {"verify", "all"}:
            verify(
                client,
                contract,
                status,
                report_path=args.report_path,
            )
    finally:
        client.close()


if __name__ == "__main__":
    main()
