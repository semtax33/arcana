from __future__ import annotations

"""Rebuild exact-scope US dividend events from matching yfinance price files."""

import argparse
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Any, Iterable

import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.extractors._internal.yfinance_market_prices import yfinance_price_storage_stem
from engine.markets.us import US_MARKET_CONFIG


DEFAULT_TARGET_PATH = DATA_LAKE.meta("us_historical_2006_2016_factor_targets.csv")
DEFAULT_PRICE_DIR = DATA_LAKE.bronze("yfinance", "price")
DEFAULT_OUTPUT_PATH = DATA_LAKE.silver(
    "us", "dividend", "us_dividend_normalized.csv"
)
DEFAULT_STATUS_PATH = DATA_LAKE.meta(
    "us_historical_2006_2016_dividend_rebuild_status.json"
)
DEFAULT_REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "deliverables"
    / "us_historical_2006_2016_dividend_rebuild_report.json"
)
OUTPUT_COLUMNS = [
    "security_id",
    "trade_date",
    "dividend",
    "payout_ratio",
    "dividend_percent",
    "currency",
    "updated_at",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(values: Iterable[str]) -> str:
    return sha256("\n".join(values).encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_target_symbols(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        symbols = {
            str(row.get("security_id") or "").strip().removeprefix("SEC_US_").upper()
            for row in csv.DictReader(stream)
        }
    return sorted(symbol for symbol in symbols if symbol)


def build_historical_dividend_rows(
    symbols: Iterable[str],
    *,
    price_dir: Path,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    for raw_symbol in symbols:
        symbol = US_MARKET_CONFIG.normalize_symbol(raw_symbol)
        path = price_dir / f"{yfinance_price_storage_stem(symbol)}.csv"
        if not path.exists():
            continue
        try:
            raw = pd.read_csv(
                path,
                usecols=lambda column: str(column).lower()
                in {
                    "date",
                    "datetime",
                    "trade_date",
                    "index",
                    "unnamed: 0",
                    "dividends",
                    "dividend",
                    "close",
                },
            )
        except (OSError, ValueError, pd.errors.EmptyDataError):
            continue
        if raw.empty:
            continue
        by_lower = {str(column).lower(): column for column in raw.columns}
        date_column = next(
            (
                by_lower[name]
                for name in (
                    "date",
                    "datetime",
                    "trade_date",
                    "index",
                    "unnamed: 0",
                )
                if name in by_lower
            ),
            None,
        )
        dividend_column = next(
            (by_lower[name] for name in ("dividends", "dividend") if name in by_lower),
            None,
        )
        if date_column is None or dividend_column is None:
            continue
        trade_date = pd.to_datetime(raw[date_column], errors="coerce").dt.tz_localize(None)
        dividend = pd.to_numeric(raw[dividend_column], errors="coerce")
        close = (
            pd.to_numeric(raw[by_lower["close"]], errors="coerce")
            if "close" in by_lower
            else pd.Series(pd.NA, index=raw.index, dtype="Float64")
        )
        frame = pd.DataFrame(
            {
                "security_id": f"SEC_US_{symbol}",
                "trade_date": trade_date,
                "dividend": dividend,
                "payout_ratio": pd.NA,
                "dividend_percent": dividend / close.where(close > 0) * 100,
                "currency": US_MARKET_CONFIG.currency,
                "updated_at": updated_at,
            }
        )
        frame = frame.loc[
            frame["trade_date"].between(start_date, end_date, inclusive="both")
            & (frame["dividend"] > 0)
        ].copy()
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    output = pd.concat(frames, ignore_index=True)
    output = output.drop_duplicates(["security_id", "trade_date"], keep="last")
    return output[OUTPUT_COLUMNS].sort_values(["security_id", "trade_date"]).reset_index(drop=True)


def merge_exact_scope(
    existing: pd.DataFrame,
    rebuilt: pd.DataFrame,
    *,
    security_ids: set[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    existing = existing.copy()
    if not existing.empty:
        existing["trade_date"] = pd.to_datetime(existing["trade_date"], errors="coerce").dt.tz_localize(None)
        in_scope = (
            existing["security_id"].fillna("").astype(str).isin(security_ids)
            & existing["trade_date"].between(start_date, end_date, inclusive="both")
        )
        existing = existing.loc[~in_scope].copy()
    frames = [frame for frame in (existing, rebuilt.copy()) if not frame.empty]
    if not frames:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    output = pd.concat(frames, ignore_index=True, sort=False)
    for column in OUTPUT_COLUMNS:
        if column not in output.columns:
            output[column] = pd.NA
    output["trade_date"] = pd.to_datetime(output["trade_date"], errors="coerce").dt.tz_localize(None)
    output = output.dropna(subset=["security_id", "trade_date", "dividend"])
    output = output.drop_duplicates(["security_id", "trade_date"], keep="last")
    return output[OUTPUT_COLUMNS].sort_values(["trade_date", "security_id"]).reset_index(drop=True)


def rebuild(
    *,
    symbols: list[str],
    price_dir: Path,
    output_path: Path,
    status_path: Path,
    report_path: Path,
    start_date: str,
    end_date: str,
    apply: bool,
) -> dict[str, Any]:
    security_ids = {f"SEC_US_{symbol}" for symbol in symbols}
    target_hash = _digest(sorted(security_ids))
    rebuilt = build_historical_dividend_rows(
        symbols,
        price_dir=price_dir,
        start_date=start_date,
        end_date=end_date,
    )
    existing = (
        pd.read_csv(output_path)
        if output_path.exists()
        else pd.DataFrame(columns=OUTPUT_COLUMNS)
    )
    merged = merge_exact_scope(
        existing,
        rebuilt,
        security_ids=security_ids,
        start_date=start_date,
        end_date=end_date,
    )
    scope_symbols = int(rebuilt["security_id"].nunique()) if not rebuilt.empty else 0
    report = {
        "contract": "us_historical_dividends/v1",
        "generated_at": _now(),
        "start_date": start_date,
        "end_date": end_date,
        "target_count": len(security_ids),
        "target_sha256": target_hash,
        "historical_event_rows": len(rebuilt),
        "historical_symbols_with_dividends": scope_symbols,
        "result_rows": len(merged),
        "applied": bool(apply),
    }
    if not apply:
        return report

    status: dict[str, Any]
    if status_path.exists():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        for key, expected in (
            ("start_date", start_date),
            ("end_date", end_date),
            ("target_sha256", target_hash),
        ):
            if status.get(key) != expected:
                raise ValueError(f"dividend rebuild status contract mismatch for {key}")
    else:
        backup_path = output_path.with_name(
            f"{output_path.stem}.pre_{start_date[:4]}_{end_date[:4]}_"
            f"{target_hash[:12]}.csv"
        )
        if output_path.exists():
            shutil.copy2(output_path, backup_path)
        status = {
            "start_date": start_date,
            "end_date": end_date,
            "target_sha256": target_hash,
            "backup_path": str(backup_path) if output_path.exists() else None,
            "backup_rows": len(existing),
            "complete": False,
            "updated_at": _now(),
        }
        _write_json(status_path, status)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    merged.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(output_path)
    verified = pd.read_csv(output_path)
    verified["trade_date"] = pd.to_datetime(verified["trade_date"], errors="coerce")
    verified_scope = verified.loc[
        verified["security_id"].astype(str).isin(security_ids)
        & verified["trade_date"].between(start_date, end_date, inclusive="both")
    ]
    if len(verified_scope) != len(rebuilt):
        raise RuntimeError(
            f"dividend scope verification failed: {len(verified_scope):,}/{len(rebuilt):,}"
        )
    status["complete"] = True
    status["historical_event_rows"] = len(rebuilt)
    status["updated_at"] = _now()
    _write_json(status_path, status)
    _write_json(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="2006-01-01")
    parser.add_argument("--end-date", default="2016-12-31")
    parser.add_argument("--target-path", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--price-dir", type=Path, default=DEFAULT_PRICE_DIR)
    parser.add_argument("--output-path", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--status-path", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.start_date > args.end_date:
        raise ValueError("start-date must not be after end-date")
    report = rebuild(
        symbols=read_target_symbols(args.target_path),
        price_dir=args.price_dir,
        output_path=args.output_path,
        status_path=args.status_path,
        report_path=args.report_path,
        start_date=args.start_date,
        end_date=args.end_date,
        apply=args.apply,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
