from __future__ import annotations

import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.transformers.sec_filings import normalize_us_sec_filings


def main() -> None:
    universe = pd.read_csv(
        DATA_LAKE.bronze("yfinance", "universe", "us_equity_universe.csv"),
        dtype=str,
    )
    symbols = sorted(
        set(universe["ticker"].dropna().astype(str).str.strip().str.upper())
    )
    written = normalize_us_sec_filings(
        symbols=symbols,
        start_year=2015,
        end_year=2026,
        use_notes=False,
        use_edgartools=False,
        workers=0,
        progress_interval=250,
    )
    print(
        f"[DONE] corrected_us_statements={len(written)} "
        f"universe_symbols={len(symbols)}"
    )


if __name__ == "__main__":
    main()
