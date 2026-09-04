from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.workflows._internal.normalize_workflow import normalize_all_statements


DEFAULT_TARGETS = DATA_LAKE.meta("kr_unit_scale_repair_targets.csv")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-normalize KR symbols affected by legacy report-wide unit repair."
    )
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--start-year", type=int, default=1900)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()

    frame = pd.read_csv(args.targets, dtype={"symbol": str})
    symbols = sorted(
        {
            str(value).strip().zfill(6)
            for value in frame["symbol"].dropna()
            if str(value).strip()
        }
    )
    if not symbols:
        raise ValueError("KR unit-scale target file contains no symbols")
    print(
        f"[INFO] KR unit-scale rebuild symbols={len(symbols)}, workers={args.workers}",
        flush=True,
    )
    result = normalize_all_statements(
        symbols=symbols,
        start_year=args.start_year,
        end_year=args.end_year,
        months=(3, 6, 9, 12),
        save_debug=True,
        workers=args.workers,
    )
    if result["failed"]:
        raise RuntimeError(f"KR unit-scale rebuild failed files={result['failed']}")


if __name__ == "__main__":
    main()
