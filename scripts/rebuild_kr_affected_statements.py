from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pandas as pd

from engine.core.paths import DATA_LAKE, statement_symbol_name
from engine.workflows._internal.normalize_workflow import normalize_all_statements


DEFAULT_AFFECTED_PATH = DATA_LAKE.meta("kr_dart_period_column_audit.csv")
DEFAULT_STATUS_PATH = DATA_LAKE.meta("kr_dart_period_column_rebuild_status.json")
NORMALIZED_ROOT = DATA_LAKE.silver("dart", "normalized")


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


def _affected_symbols(path: Path) -> tuple[list[str], str]:
    frame = pd.read_csv(path, dtype={"symbol": str})
    affected = frame["affected"].astype(str).str.strip().str.lower().isin({"1", "true"})
    symbols = sorted(
        {
            str(value).strip().zfill(6)
            for value in frame.loc[affected, "symbol"].dropna()
            if str(value).strip()
        }
    )
    if not symbols:
        raise ValueError("KR audit contains no affected symbols")
    digest = hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest()
    return symbols, digest


def rebuild_kr_affected_statements(
    *,
    affected_path: Path,
    status_path: Path,
    start_year: int,
    end_year: int,
    batch_size: int,
) -> None:
    symbols, target_digest = _affected_symbols(affected_path)
    status = _read_json(status_path)
    status.setdefault("target_count", len(symbols))
    status.setdefault("target_sha256", target_digest)
    status.setdefault("start_year", start_year)
    status.setdefault("end_year", end_year)
    status.setdefault("months", [6, 9])
    status.setdefault("save_debug", False)
    status.setdefault("completed_symbols", [])
    if (
        status.get("target_sha256") != target_digest
        or int(status.get("target_count", 0)) != len(symbols)
        or int(status.get("start_year", 0)) != start_year
        or int(status.get("end_year", 0)) != end_year
        or status.get("months") != [6, 9]
        or status.get("save_debug") is not False
    ):
        raise ValueError("KR statement status belongs to a different target set or year range")

    completed = {
        str(value).strip().zfill(6) for value in status["completed_symbols"]
    }
    pending = [symbol for symbol in symbols if symbol not in completed]
    started_at = time.monotonic()
    print(
        f"[INFO] affected KR statement rebuild total={len(symbols)}, "
        f"completed={len(completed)}, pending={len(pending)}, "
        f"years={start_year}-{end_year}",
        flush=True,
    )
    for offset in range(0, len(pending), max(1, batch_size)):
        batch = pending[offset : offset + max(1, batch_size)]
        result = normalize_all_statements(
            symbols=batch,
            start_year=start_year,
            end_year=end_year,
            months=(6, 9),
            save_debug=False,
        )
        if int(result.get("failed", 0)):
            raise RuntimeError(
                f"KR normalizer reported failed tasks for batch starting {batch[0]}: {result}"
            )
        missing_outputs = [
            symbol
            for symbol in batch
            if not (
                NORMALIZED_ROOT / statement_symbol_name(symbol, market="kr")
            ).is_file()
        ]
        if missing_outputs:
            raise RuntimeError(
                "KR normalizer did not produce outputs for: "
                + ",".join(missing_outputs[:20])
            )
        completed.update(batch)
        status["completed_symbols"] = sorted(completed)
        _write_json(status_path, status)
        print(
            f"[DONE] KR statement batch={len(batch)}, "
            f"completed={len(completed)}/{len(symbols)}, "
            f"elapsed={time.monotonic() - started_at:.1f}s",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild only KR statements affected by interim period-column mapping."
    )
    parser.add_argument("--affected", type=Path, default=DEFAULT_AFFECTED_PATH)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--start-year", type=int, default=1900)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--batch-size", type=int, default=250)
    args = parser.parse_args()
    rebuild_kr_affected_statements(
        affected_path=args.affected,
        status_path=args.status,
        start_year=args.start_year,
        end_year=args.end_year,
        batch_size=max(1, args.batch_size),
    )


if __name__ == "__main__":
    main()
