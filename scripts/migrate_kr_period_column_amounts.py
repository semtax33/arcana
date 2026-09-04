from __future__ import annotations

import argparse
from collections import Counter
from contextlib import redirect_stdout
import csv
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

from engine.core.paths import DATA_LAKE, statement_snapshot_name, statement_symbol_name
from engine.transformers._internal.dart_filings import (
    EXPECTED_HEADER,
    amount_to_int,
    apply_amount_policy,
    apply_cash_direction,
    extract_rows_from_dart_html,
    normalize_account_name,
    normalize_financial_statement_rule_based,
    safe_str,
)
from engine.transformers._internal.statement_files import consolidate_statement_snapshots
from engine.workflows._internal import normalize_workflow as workflow


DEFAULT_AUDIT_PATH = DATA_LAKE.meta("kr_dart_period_column_audit.csv")
DEFAULT_STATUS_PATH = DATA_LAKE.meta("kr_dart_period_column_amount_migration_status.json")
BRONZE_ROOT = DATA_LAKE.bronze("dart", "finance-statement")
SNAPSHOT_ROOT = DATA_LAKE.silver("dart", "normalized-snapshots")
NORMALIZED_ROOT = DATA_LAKE.silver("dart", "normalized")
_PERIOD_RE = re.compile(r"finance_statement_\((?P<year>\d{4})[.](?P<month>06|09)\)[.]html$")


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


def _write_frame(path: Path, frame: pd.DataFrame) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(
        temp_path,
        index=False,
        encoding="utf-8-sig",
        quoting=csv.QUOTE_ALL,
    )
    temp_path.replace(path)


def _affected_symbols(path: Path) -> tuple[list[str], str]:
    frame = pd.read_csv(path, dtype={"symbol": str})
    errors = frame["error"].fillna("").astype(str).str.strip()
    if errors.astype(bool).any():
        raise RuntimeError(f"KR audit has {int(errors.astype(bool).sum())} errors")
    affected = frame["affected"].astype(str).str.strip().str.lower().isin({"1", "true"})
    symbols = sorted(
        {
            str(value).strip().zfill(6)
            for value in frame.loc[affected, "symbol"].dropna()
            if str(value).strip()
        }
    )
    digest = hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest()
    return symbols, digest


def _period_paths(symbol: str, start_year: int, end_year: int) -> list[Path]:
    paths: list[Path] = []
    stock_dir = BRONZE_ROOT / symbol
    for path in stock_dir.glob("finance_statement_(*).html"):
        match = _PERIOD_RE.fullmatch(path.name)
        if match is None:
            continue
        year = int(match.group("year"))
        if start_year <= year <= end_year:
            paths.append(path)
    return sorted(paths)


def _period_meta(path: Path) -> tuple[int, int, str]:
    match = _PERIOD_RE.fullmatch(path.name)
    if match is None:
        raise ValueError(f"unsupported interim statement path: {path}")
    year = int(match.group("year"))
    month = int(match.group("month"))
    return year, month, f"{year}.{month}"


def _account_sequence(rows: list[dict[str, Any]]) -> list[str]:
    return [normalize_account_name(row.get("original_account_name")) for row in rows]


def _frame_account_sequence(frame: pd.DataFrame) -> list[str]:
    return [normalize_account_name(value) for value in frame["original_account_name"].tolist()]


def _apply_amounts(frame: pd.DataFrame, rows: list[dict[str, Any]]) -> tuple[pd.DataFrame, int, bool]:
    if len(frame) != len(rows) or _frame_account_sequence(frame) != _account_sequence(rows):
        return frame, 0, True

    result = frame.copy()
    changed = 0
    zero_state_changed = False
    for index, parsed in enumerate(rows):
        old_raw = amount_to_int(result.iat[index, result.columns.get_loc("raw_amount")])
        new_raw = amount_to_int(parsed.get("raw_amount", parsed.get("amount")))
        if (old_raw == 0) != (new_raw == 0):
            zero_state_changed = True
        policy = safe_str(result.iat[index, result.columns.get_loc("amount_policy")])
        direction = safe_str(result.iat[index, result.columns.get_loc("cash_direction")])
        normalized = apply_amount_policy(new_raw, policy)
        cash_effect = apply_cash_direction(normalized, direction)
        new_values = {
            "amount": safe_str(normalized),
            "raw_amount": safe_str(new_raw),
            "normalized_amount": safe_str(normalized),
            "cash_effect_amount": safe_str(cash_effect),
        }
        if any(safe_str(result.at[result.index[index], column]) != value for column, value in new_values.items()):
            changed += 1
        for column, value in new_values.items():
            result.at[result.index[index], column] = value
        if "amount_raw" in result.columns:
            result.at[result.index[index], "amount_raw"] = safe_str(parsed.get("amount_raw"))
        if "unit_factor" in result.columns:
            result.at[result.index[index], "unit_factor"] = safe_str(parsed.get("unit_factor"))
    return result, changed, zero_state_changed


def _full_rebuild(path: Path, symbol: str, period: str, output_path: Path) -> None:
    if workflow._WORKER_CANONICAL_DF is None:
        workflow._init_normalize_worker()
    comment_path = workflow.infer_comment_html_path(
        input_html_path=path,
        company_name=symbol,
        period=period,
    )
    with redirect_stdout(StringIO()):
        normalize_financial_statement_rule_based(
            input_html_path=path,
            company_name=symbol,
            period=period,
            output_csv_path=output_path,
            canonical_csv_path=workflow.CANONICAL_CSV_PATH,
            context_rule_path=workflow.CONTEXT_RULE_PATH,
            mapping_rule_paths=[workflow.MAPPING_RULE_PATH],
            sign_policy_path=workflow.SIGN_POLICY_PATH,
            save_debug=True,
            context_engine=workflow._WORKER_CONTEXT_ENGINE,
            mapping_engine=workflow._WORKER_MAPPING_ENGINE,
            canonical_df=workflow._WORKER_CANONICAL_DF,
            verbose=False,
            comment_rule_paths=[workflow.COMMENT_RULE_PATH],
            comment_html_path=comment_path if comment_path.exists() else None,
        )


def _migrate_file(path: Path, symbol: str) -> dict[str, int]:
    year, month, period = _period_meta(path)
    output_path = SNAPSHOT_ROOT / statement_snapshot_name(symbol, year, month)
    with redirect_stdout(StringIO()):
        rows = extract_rows_from_dart_html(path, symbol, period)
    if not output_path.exists():
        _full_rebuild(path, symbol, period, output_path)
        return {"files": 1, "changed_rows": 0, "fallback_files": 1, "debug_files": 0}

    frame = pd.read_csv(output_path, dtype=str).fillna("")
    old_amounts = Counter(
        (
            normalize_account_name(row["original_account_name"]),
            amount_to_int(row["raw_amount"]),
        )
        for _, row in frame.iterrows()
    )
    new_amounts = Counter(
        (
            normalize_account_name(row.get("original_account_name")),
            amount_to_int(row.get("raw_amount", row.get("amount"))),
        )
        for row in rows
    )
    if old_amounts == new_amounts:
        return {"files": 1, "changed_rows": 0, "fallback_files": 0, "debug_files": 0}

    changed_rows = sum((old_amounts - new_amounts).values())
    _full_rebuild(path, symbol, period, output_path)
    return {
        "files": 1,
        "changed_rows": changed_rows,
        "fallback_files": 1,
        "debug_files": 1,
    }


def _migrate_symbol(task: tuple[str, int, int]) -> tuple[str, dict[str, int], str]:
    symbol, start_year, end_year = task
    totals = {"files": 0, "changed_rows": 0, "fallback_files": 0, "debug_files": 0}
    try:
        for path in _period_paths(symbol, start_year, end_year):
            result = _migrate_file(path, symbol)
            for key, value in result.items():
                totals[key] += int(value)
        consolidated = consolidate_statement_snapshots(
            symbol,
            SNAPSHOT_ROOT,
            market="kr",
            columns=EXPECTED_HEADER,
            output_dir=NORMALIZED_ROOT,
        )
        if consolidated is None or not Path(consolidated).is_file():
            raise RuntimeError("consolidated statement output was not created")
        return symbol, totals, ""
    except Exception as exc:
        return symbol, totals, f"{type(exc).__name__}: {exc}"


def migrate(
    *,
    audit_path: Path,
    status_path: Path,
    start_year: int,
    end_year: int,
    workers: int,
) -> None:
    symbols, digest = _affected_symbols(audit_path)
    status = _read_json(status_path)
    status.setdefault("target_count", len(symbols))
    status.setdefault("target_sha256", digest)
    status.setdefault("start_year", start_year)
    status.setdefault("end_year", end_year)
    status.setdefault("completed_symbols", [])
    status.setdefault("results", {})
    if (
        status.get("target_sha256") != digest
        or int(status.get("target_count", 0)) != len(symbols)
        or int(status.get("start_year", 0)) != start_year
        or int(status.get("end_year", 0)) != end_year
    ):
        raise ValueError("KR amount migration status belongs to a different run")

    completed = {str(value).zfill(6) for value in status["completed_symbols"]}
    pending = [symbol for symbol in symbols if symbol not in completed]
    worker_count = min(max(1, workers), len(pending) or 1)
    started_at = time.monotonic()
    print(
        f"[INFO] KR amount migration total={len(symbols)}, completed={len(completed)}, "
        f"pending={len(pending)}, workers={worker_count}",
        flush=True,
    )
    tasks = [(symbol, start_year, end_year) for symbol in pending]
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        for index, (symbol, result, error) in enumerate(
            executor.map(_migrate_symbol, tasks, chunksize=1), start=1
        ):
            if error:
                status.setdefault("errors", {})[symbol] = error
                _write_json(status_path, status)
                raise RuntimeError(f"KR amount migration failed for {symbol}: {error}")
            completed.add(symbol)
            status["completed_symbols"] = sorted(completed)
            status["results"][symbol] = result
            _write_json(status_path, status)
            if index == 1 or index % 50 == 0 or index == len(tasks):
                changed_rows = sum(
                    int(value.get("changed_rows", 0)) for value in status["results"].values()
                )
                fallback_files = sum(
                    int(value.get("fallback_files", 0)) for value in status["results"].values()
                )
                print(
                    f"[PROGRESS] completed={len(completed)}/{len(symbols)}, "
                    f"changed_rows={changed_rows:,}, fallback_files={fallback_files:,}, "
                    f"elapsed={time.monotonic() - started_at:.1f}s",
                    flush=True,
                )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate KR Q2/Q3 statement amounts to the cumulative YTD column."
    )
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT_PATH)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--start-year", type=int, default=1900)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--workers", type=int, default=min(os.cpu_count() or 2, 12))
    args = parser.parse_args()
    migrate(
        audit_path=args.audit,
        status_path=args.status,
        start_year=args.start_year,
        end_year=args.end_year,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
