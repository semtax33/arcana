from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.transformers.sec_filings import normalize_us_sec_filings


DEFAULT_AFFECTED_PATH = DATA_LAKE.meta("us_companyfacts_period_label_affected.csv")
DEFAULT_STATUS_PATH = DATA_LAKE.meta("us_companyfacts_period_label_rebuild_status.json")


def _load_completed(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(symbol).strip().upper() for symbol in payload.get("completed_symbols", [])}


def _write_status(
    path: Path,
    *,
    completed: set[str],
    total: int,
    start_year: int,
    end_year: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "completed_count": len(completed),
                "total_count": total,
                "start_year": start_year,
                "end_year": end_year,
                "completed_symbols": sorted(completed),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def rebuild_affected_statements(
    *,
    affected_path: str | Path,
    status_path: str | Path,
    start_year: int,
    end_year: int,
    batch_size: int,
    workers: int,
) -> None:
    affected_path = Path(affected_path)
    status_path = Path(status_path)
    frame = pd.read_csv(affected_path, dtype={"symbol": str})
    symbols = sorted(
        {
            str(symbol).strip().upper()
            for symbol in frame.get("symbol", pd.Series(dtype=str))
            if str(symbol).strip()
        }
    )
    completed = _load_completed(status_path)
    pending = [symbol for symbol in symbols if symbol not in completed]
    print(
        "[INFO] affected US statement rebuild "
        f"total={len(symbols)}, completed={len(completed)}, pending={len(pending)}, "
        f"batch_size={batch_size}, workers={workers}, years={start_year}-{end_year}",
        flush=True,
    )

    started_at = time.monotonic()
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset : offset + batch_size]
        batch_number = offset // batch_size + 1
        print(
            f"[START] batch={batch_number}, symbols={len(batch)}, "
            f"first={batch[0]}, last={batch[-1]}",
            flush=True,
        )
        written = normalize_us_sec_filings(
            symbols=batch,
            start_year=start_year,
            end_year=end_year,
            use_notes=False,
            use_edgartools=False,
            workers=workers,
            progress_interval=50,
            replace_existing=True,
        )
        written_symbols = {
            path.stem.removeprefix("us_normalized_").upper()
            for path in written
        }
        missing_outputs = sorted(set(batch) - written_symbols)
        if missing_outputs:
            raise RuntimeError(
                "normalizer did not write every requested symbol: "
                + ",".join(missing_outputs[:20])
            )
        completed.update(batch)
        _write_status(
            status_path,
            completed=completed,
            total=len(symbols),
            start_year=start_year,
            end_year=end_year,
        )
        print(
            f"[DONE] batch={batch_number}, completed={len(completed)}/{len(symbols)}, "
            f"elapsed={time.monotonic() - started_at:.1f}s",
            flush=True,
        )

    print(
        f"[DONE] affected US statement rebuild completed={len(completed)}/{len(symbols)}, "
        f"elapsed={time.monotonic() - started_at:.1f}s, status={status_path}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild only the US symbols proven affected by period-label mapping."
    )
    parser.add_argument("--affected", default=str(DEFAULT_AFFECTED_PATH))
    parser.add_argument("--status", default=str(DEFAULT_STATUS_PATH))
    parser.add_argument("--start-year", type=int, default=1900)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    rebuild_affected_statements(
        affected_path=args.affected,
        status_path=args.status,
        start_year=args.start_year,
        end_year=args.end_year,
        batch_size=max(args.batch_size, 1),
        workers=max(args.workers, 1),
    )


if __name__ == "__main__":
    main()
