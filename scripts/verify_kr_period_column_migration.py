from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Any

import pandas as pd

from engine.core.paths import DATA_LAKE, statement_symbol_name
from engine.transformers._internal.dart_filings import (
    amount_to_int,
    normalize_account_name,
    normalize_statement_type,
    safe_str,
)


DEFAULT_AUDIT_PATH = DATA_LAKE.meta("kr_dart_period_column_audit.csv")
DEFAULT_STATUS_PATH = DATA_LAKE.meta(
    "kr_dart_period_column_amount_migration_status.json"
)
DEFAULT_OUTPUT_PATH = DATA_LAKE.meta(
    "kr_dart_period_column_migration_validation.csv"
)
NORMALIZED_ROOT = DATA_LAKE.silver("dart", "normalized")
SNAPSHOT_ROOT = DATA_LAKE.silver("dart", "normalized-snapshots")
_ROW_SCALE_RE = re.compile(r"row unit-scale repaired: divided by ([0-9.]+)")


def _is_true(value: Any) -> bool:
    return safe_str(value).strip().lower() in {"1", "true", "yes"}


def _statement_group(value: Any) -> str:
    normalized = normalize_statement_type(value)
    return "IS" if normalized in {"IS", "CIS"} else normalized


def _verify_row(task: tuple[dict[str, str], str, str]) -> dict[str, Any]:
    row, normalized_root, snapshot_root = task
    symbol = safe_str(row.get("symbol")).strip().zfill(6)
    expected_amount = amount_to_int(row.get("corrected_ytd_amount"))
    result: dict[str, Any] = {
        "symbol": symbol,
        "period": safe_str(row.get("period")),
        "statement_type": normalize_statement_type(row.get("statement_type")),
        "account_name": safe_str(row.get("account_name")),
        "expected_ytd_amount": safe_str(expected_amount),
        "matched_rows": 0,
        "stored_amounts": "",
        "stored_match": False,
        "match_type": "",
        "source_amount_raw": "",
        "error": "",
    }
    try:
        path = Path(normalized_root) / statement_symbol_name(symbol, market="kr")
        if not path.is_file():
            raise FileNotFoundError(path)
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        required = {
            "period",
            "statement_type",
            "original_account_name",
            "raw_amount",
        }
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"missing columns: {sorted(missing)}")

        wanted_period = safe_str(row.get("period"))
        wanted_statement = _statement_group(row.get("statement_type"))
        wanted_account = normalize_account_name(row.get("account_name"))
        candidates = frame[
            frame["period"].map(safe_str).eq(wanted_period)
            & frame["statement_type"].map(_statement_group).eq(wanted_statement)
            & frame["original_account_name"].map(normalize_account_name).eq(wanted_account)
        ]
        stored_amounts = sorted(
            {amount_to_int(value) for value in candidates["raw_amount"].tolist()}
        )
        matches = sum(
            amount_to_int(value) == expected_amount
            for value in candidates["raw_amount"].tolist()
        )
        result["matched_rows"] = int(matches)
        result["stored_amounts"] = ";".join(map(str, stored_amounts))
        result["stored_match"] = bool(matches)
        if matches:
            result["match_type"] = "exact_amount"
            return result

        year_text, month_text = wanted_period.split(".", 1)
        debug_name = (
            f"kr_normalized_{symbol}_{int(year_text):04d}.{int(month_text):02d}.debug.csv"
        )
        debug_path = Path(snapshot_root) / debug_name
        debug = pd.read_csv(debug_path, dtype=str, keep_default_na=False)
        debug_candidates = debug[
            debug["period"].map(safe_str).eq(wanted_period)
            & debug["statement_type"].map(_statement_group).eq(wanted_statement)
            & debug["original_account_name"].map(normalize_account_name).eq(wanted_account)
        ]
        scaled_matches = 0
        source_values: list[str] = []
        for _, candidate in debug_candidates.iterrows():
            scale_match = _ROW_SCALE_RE.search(safe_str(candidate.get("reason")))
            if scale_match is None:
                continue
            source_raw = safe_str(candidate.get("amount_raw"))
            source_values.append(source_raw)
            try:
                repaired_unit = Decimal(safe_str(candidate.get("unit_factor")))
                scale_factor = Decimal(scale_match.group(1))
                original_unit = repaired_unit * scale_factor
                if original_unit < 1 or original_unit != original_unit.to_integral_value():
                    continue
                source_amount = amount_to_int(source_raw) * int(original_unit)
            except (InvalidOperation, ValueError, OverflowError):
                continue
            final_amount = amount_to_int(candidate.get("raw_amount"))
            if source_amount == expected_amount and final_amount in stored_amounts:
                scaled_matches += 1
        if scaled_matches:
            result["matched_rows"] = scaled_matches
            result["stored_match"] = True
            result["match_type"] = "row_unit_scale_repair"
            result["source_amount_raw"] = ";".join(sorted(set(source_values)))
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def verify_migration(
    *,
    audit_path: str | Path,
    status_path: str | Path,
    output_path: str | Path,
    normalized_root: str | Path = NORMALIZED_ROOT,
    snapshot_root: str | Path = SNAPSHOT_ROOT,
    workers: int = 2,
) -> pd.DataFrame:
    audit = pd.read_csv(audit_path, dtype=str, keep_default_na=False)
    affected = audit[audit["affected"].map(_is_true)].copy()
    if affected.empty:
        raise ValueError("KR audit contains no affected symbols")

    status = json.loads(Path(status_path).read_text(encoding="utf-8"))
    completed = {
        safe_str(symbol).strip().zfill(6)
        for symbol in status.get("completed_symbols", [])
    }
    targets = {
        safe_str(symbol).strip().zfill(6) for symbol in affected["symbol"].tolist()
    }
    missing_symbols = sorted(targets.difference(completed))
    if missing_symbols:
        raise RuntimeError(
            "KR amount migration is incomplete: "
            f"completed={len(completed)}/{len(targets)}, "
            f"first_missing={missing_symbols[0]}"
        )
    status_errors = status.get("errors") or {}
    if status_errors:
        raise RuntimeError(f"KR amount migration has {len(status_errors)} errors")

    rows = affected.to_dict(orient="records")
    tasks = [(row, str(normalized_root), str(snapshot_root)) for row in rows]
    worker_count = min(max(1, int(workers)), len(tasks))
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(_verify_row, tasks, chunksize=1))

    frame = pd.DataFrame(results).sort_values("symbol").reset_index(drop=True)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_path, index=False, encoding="utf-8-sig")

    mismatches = int((~frame["stored_match"]).sum())
    errors = int(frame["error"].astype(bool).sum())
    print(
        f"[DONE] KR period-column validation rows={len(frame)}, "
        f"mismatches={mismatches}, errors={errors}, output={output_path}",
        flush=True,
    )
    if mismatches or errors:
        raise RuntimeError(
            f"KR period-column validation failed: mismatches={mismatches}, errors={errors}"
        )
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify audited KR YTD amounts in consolidated Silver statements."
    )
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT_PATH)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--normalized-root", type=Path, default=NORMALIZED_ROOT)
    parser.add_argument("--snapshot-root", type=Path, default=SNAPSHOT_ROOT)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    verify_migration(
        audit_path=args.audit,
        status_path=args.status,
        output_path=args.output,
        normalized_root=args.normalized_root,
        snapshot_root=args.snapshot_root,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
