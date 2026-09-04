from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from engine.core.paths import DATA_LAKE


DEFAULT_PERIOD_TARGETS = DATA_LAKE.meta("kr_dart_period_column_factor_targets.csv")
DEFAULT_UNIT_TARGETS = DATA_LAKE.meta("kr_unit_scale_repair_targets.csv")
DEFAULT_OUTPUT = DATA_LAKE.meta("kr_financial_normalization_factor_targets.csv")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge KR period-column and unit-scale affected symbols."
    )
    parser.add_argument("--period-targets", type=Path, default=DEFAULT_PERIOD_TARGETS)
    parser.add_argument("--unit-targets", type=Path, default=DEFAULT_UNIT_TARGETS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    symbols: set[str] = set()
    for path in (args.period_targets, args.unit_targets):
        frame = pd.read_csv(path, dtype={"symbol": str})
        symbols.update(
            str(value).strip().zfill(6)
            for value in frame["symbol"].dropna()
            if str(value).strip()
        )
    output = pd.DataFrame(
        {
            "symbol": sorted(symbols),
            "security_id": [f"SEC_KR_{symbol}" for symbol in sorted(symbols)],
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"[DONE] KR merged factor targets={len(output)}, output={args.output}")


if __name__ == "__main__":
    main()
