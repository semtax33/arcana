from __future__ import annotations

"""Build the marcap share-count warm-up needed by 2012 PVGO compression."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd

from engine.core.paths import DATA_LAKE


HISTORICAL_PATH = DATA_LAKE.silver(
    "krx", "shares", "kr_historical_2002_2012_shares.csv"
)
BRIDGE_PATH = DATA_LAKE.silver(
    "krx", "shares", "kr_pvgo_2012_2016_marcap_shares.csv"
)
OUTPUT_PATH = DATA_LAKE.silver(
    "krx", "shares", "kr_pvgo_2011_2016_marcap_shares.csv"
)
STATUS_PATH = DATA_LAKE.meta("kr_pvgo_2011_2016_marcap_shares_status.json")
START_DATE = "2011-01-01"
END_DATE = "2016-12-31"
KEYS = ["security_id", "trade_date"]
COLUMNS = ["security_id", "trade_date", "shares", "market_cap"]


def _read_scope(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for chunk in pd.read_csv(path, usecols=COLUMNS, chunksize=500_000):
        chunk["trade_date"] = pd.to_datetime(chunk["trade_date"], errors="coerce")
        chunk = chunk.loc[chunk["trade_date"].between(start_date, end_date)].copy()
        if not chunk.empty:
            frames.append(chunk)
    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat(frames, ignore_index=True)


def build_warmup_shares(
    historical_path: Path = HISTORICAL_PATH,
    bridge_path: Path = BRIDGE_PATH,
    *,
    output_path: Path = OUTPUT_PATH,
    status_path: Path = STATUS_PATH,
) -> dict[str, object]:
    historical = _read_scope(historical_path, START_DATE, "2012-12-31")
    bridge = _read_scope(bridge_path, "2012-01-01", END_DATE)
    historical["_priority"] = 0
    bridge["_priority"] = 1
    merged = pd.concat([historical, bridge], ignore_index=True)
    merged["shares"] = pd.to_numeric(merged["shares"], errors="coerce")
    merged["market_cap"] = pd.to_numeric(merged["market_cap"], errors="coerce")
    merged = (
        merged.dropna(subset=["security_id", "trade_date", "shares"])
        .loc[lambda frame: frame["shares"].gt(0) & frame["market_cap"].gt(0)]
        .sort_values([*KEYS, "_priority"], kind="stable")
        .drop_duplicates(KEYS, keep="last")
        .drop(columns="_priority")
        .sort_values(KEYS, kind="stable")
        .reset_index(drop=True)
    )
    if merged.empty or merged.duplicated(KEYS).any():
        raise RuntimeError("warm-up shares are empty or contain duplicate keys")
    if merged["trade_date"].min() > pd.Timestamp("2011-01-03"):
        raise RuntimeError("2011 marcap warm-up is missing")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    merged.to_csv(temporary, index=False, date_format="%Y-%m-%d")
    temporary.replace(output_path)
    by_year = {
        str(int(year)): int(count)
        for year, count in merged["trade_date"].dt.year.value_counts().sort_index().items()
    }
    status = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "marcap only",
        "historical_path": str(historical_path),
        "bridge_path": str(bridge_path),
        "output_path": str(output_path),
        "start_date": merged["trade_date"].min().date().isoformat(),
        "end_date": merged["trade_date"].max().date().isoformat(),
        "row_count": int(len(merged)),
        "security_count": int(merged["security_id"].nunique()),
        "duplicate_key_count": int(merged.duplicated(KEYS).sum()),
        "invalid_share_count": int((~merged["shares"].gt(0)).sum()),
        "rows_by_year": by_year,
    }
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return status


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--historical-path", type=Path, default=HISTORICAL_PATH)
    parser.add_argument("--bridge-path", type=Path, default=BRIDGE_PATH)
    parser.add_argument("--output-path", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--status-path", type=Path, default=STATUS_PATH)
    args = parser.parse_args()
    print(
        json.dumps(
            build_warmup_shares(
                args.historical_path,
                args.bridge_path,
                output_path=args.output_path,
                status_path=args.status_path,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
