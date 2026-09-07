from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any, Iterable

import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.extractors.filings import (
    DartRequestThrottle,
    REPORT_METADATA_COLUMNS,
    deduplicate_report_metadata,
    fetch_dart_report_metadata,
    fetch_dart_search,
)
from engine.extractors._internal.marcap_market_prices import (
    MARCAP_CACHE_DIR,
    normalize_marcap_shares_frame,
)
from engine.transformers._internal.factor_metrics import SHARES_PATH
from engine.workflows._internal.normalize_workflow import normalize_all_statements


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET_PATH = DATA_LAKE.meta("kr_historical_2002_2012_factor_targets.csv")
DEFAULT_METADATA_PATH = DATA_LAKE.silver("dart", "kr_report_metadata.csv")
DEFAULT_PART_DIR = DATA_LAKE.meta("kr_historical_report_metadata_parts_v2")
DEFAULT_STATEMENT_MANIFEST_DIR = DATA_LAKE.meta(
    "kr_historical_statement_manifests_v2"
)
DEFAULT_STATUS_PATH = DATA_LAKE.meta(
    "kr_historical_disclosure_recovery_status_v2.json"
)
DEFAULT_HISTORICAL_SHARES_PATH = DATA_LAKE.silver(
    "krx", "shares", "kr_historical_2002_2012_shares.csv"
)
DEFAULT_START_DATE = "2002-01-01"
DEFAULT_END_DATE = "2012-12-31"
DEFAULT_NORMALIZE_START_YEAR = 2000
RECOVERY_VERSION = 2


def _digest(values: Iterable[str]) -> str:
    return sha256("\n".join(values).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def _write_csv_atomic(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temp_path, index=False, encoding="utf-8-sig")
    temp_path.replace(path)


@dataclass(frozen=True)
class RecoveryContract:
    security_ids: tuple[str, ...]
    symbols: tuple[str, ...]
    start_date: str
    end_date: str
    target_sha256: str

    @classmethod
    def create(
        cls,
        security_ids: Iterable[str],
        *,
        start_date: str,
        end_date: str,
    ) -> "RecoveryContract":
        normalized = tuple(
            sorted({str(value).strip() for value in security_ids if str(value).strip()})
        )
        if not normalized or any(
            not value.startswith("SEC_KR_") for value in normalized
        ):
            raise ValueError("historical disclosure recovery requires SEC_KR_ ids")
        if date.fromisoformat(start_date) > date.fromisoformat(end_date):
            raise ValueError("start_date must not be after end_date")
        symbols = tuple(value.removeprefix("SEC_KR_") for value in normalized)
        return cls(
            security_ids=normalized,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            target_sha256=_digest(normalized),
        )

    @property
    def source_start_date(self) -> str:
        return f"{date.fromisoformat(self.start_date).year - 1:04d}0101"

    @property
    def source_end_date(self) -> str:
        return date.fromisoformat(self.end_date).strftime("%Y%m%d")

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": RECOVERY_VERSION,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "source_start_date": self.source_start_date,
            "source_end_date": self.source_end_date,
            "target_count": len(self.security_ids),
            "target_sha256": self.target_sha256,
        }


def pending_statement_years(
    symbol: str,
    *,
    years: Iterable[int],
    marker_dir: str | Path,
) -> list[int]:
    marker_dir = Path(marker_dir)
    return [
        int(year)
        for year in years
        if not (marker_dir / f"{symbol}_{int(year)}.json").exists()
    ]


def merge_report_metadata_frames(
    frames: Iterable[pd.DataFrame],
) -> pd.DataFrame:
    available = [frame for frame in frames if frame is not None and not frame.empty]
    if not available:
        return pd.DataFrame(columns=REPORT_METADATA_COLUMNS)
    return deduplicate_report_metadata(pd.concat(available, ignore_index=True))


def merge_historical_shares(
    existing: pd.DataFrame,
    historical: pd.DataFrame,
) -> pd.DataFrame:
    required = ["security_id", "trade_date", "shares", "market_cap"]
    frames: list[pd.DataFrame] = []
    for priority, frame in enumerate((historical, existing)):
        if frame is None or frame.empty:
            continue
        missing = sorted(set(required) - set(frame.columns))
        if missing:
            raise ValueError(f"shares frame is missing columns: {missing}")
        prepared = frame[required].copy()
        prepared["security_id"] = prepared["security_id"].astype(str)
        prepared["trade_date"] = pd.to_datetime(
            prepared["trade_date"], errors="coerce"
        ).dt.strftime("%Y-%m-%d")
        prepared["shares"] = pd.to_numeric(prepared["shares"], errors="coerce")
        prepared["market_cap"] = pd.to_numeric(
            prepared["market_cap"], errors="coerce"
        )
        prepared["_priority"] = priority
        frames.append(prepared)
    if not frames:
        return pd.DataFrame(columns=required)
    merged = pd.concat(frames, ignore_index=True)
    return (
        merged.dropna(subset=["security_id", "trade_date", "shares"])
        .loc[
            lambda frame: frame["shares"].gt(0)
            & (frame["market_cap"].isna() | frame["market_cap"].gt(0))
        ]
        .sort_values(["security_id", "trade_date", "_priority"], kind="stable")
        .drop_duplicates(["security_id", "trade_date"], keep="last")
        .drop(columns="_priority")
        .sort_values(["security_id", "trade_date"], kind="stable")
        .reset_index(drop=True)
    )


def recover_historical_marcap_shares(
    contract: RecoveryContract,
    *,
    output_path: Path,
    existing_path: Path = SHARES_PATH,
    cache_dir: Path = MARCAP_CACHE_DIR,
) -> dict[str, Any]:
    target_ids = set(contract.security_ids)
    historical_frames: list[pd.DataFrame] = []
    start_year = date.fromisoformat(contract.start_date).year
    end_year = date.fromisoformat(contract.end_date).year
    for year in range(start_year, end_year + 1):
        source_path = cache_dir / f"marcap-{year}.parquet"
        source = pd.read_parquet(
            source_path,
            columns=["Date", "Code", "Stocks", "Marcap"],
        )
        frame = normalize_marcap_shares_frame(
            source,
            start_date=contract.start_date,
            end_date=contract.end_date,
        )
        frame = frame.loc[frame["security_id"].isin(target_ids)].copy()
        historical_frames.append(frame)
        print(f"[SHARES] year={year} rows={len(frame):,}", flush=True)
    historical = pd.concat(historical_frames, ignore_index=True)

    existing = pd.DataFrame(columns=historical.columns)
    if existing_path.exists():
        existing = pd.read_csv(existing_path, dtype={"security_id": str})
        existing["trade_date"] = pd.to_datetime(
            existing["trade_date"], errors="coerce"
        )
        existing = existing.loc[
            existing["security_id"].isin(target_ids)
            & existing["trade_date"].between(contract.start_date, contract.end_date)
        ].copy()
    merged = merge_historical_shares(existing, historical)
    _write_csv_atomic(output_path, merged)
    return {
        "output_path": str(output_path),
        "historical_source_rows": len(historical),
        "existing_overlap_rows": len(existing),
        "merged_rows": len(merged),
        "security_count": int(merged["security_id"].nunique()),
        "min_trade_date": merged["trade_date"].min() if not merged.empty else None,
        "max_trade_date": merged["trade_date"].max() if not merged.empty else None,
    }


def load_contract(
    target_path: Path,
    *,
    start_date: str,
    end_date: str,
) -> RecoveryContract:
    targets = pd.read_csv(target_path, dtype=str)
    if "security_id" not in targets.columns:
        raise ValueError(f"target manifest has no security_id column: {target_path}")
    return RecoveryContract.create(
        targets["security_id"].dropna(),
        start_date=start_date,
        end_date=end_date,
    )


def _metadata_part_path(part_dir: Path, symbol: str) -> Path:
    return part_dir / f"{symbol}.csv"


def collect_metadata_parts(
    contract: RecoveryContract,
    *,
    part_dir: Path,
    workers: int,
    request_interval: float,
) -> dict[str, Any]:
    part_dir.mkdir(parents=True, exist_ok=True)
    pending = [
        symbol
        for symbol in contract.symbols
        if not _metadata_part_path(part_dir, symbol).exists()
    ]
    throttle = DartRequestThrottle(request_interval)
    failures: dict[str, str] = {}
    completed = len(contract.symbols) - len(pending)
    discovered_rows = 0

    def recover_one(symbol: str) -> tuple[str, int]:
        frame = fetch_dart_report_metadata(
            symbol,
            source_type="statement",
            start_date=contract.source_start_date,
            end_date=contract.source_end_date,
            years_per_window=10,
            throttle=throttle,
        )
        _write_csv_atomic(_metadata_part_path(part_dir, symbol), frame)
        return symbol, len(frame)

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        future_map = {
            executor.submit(recover_one, symbol): symbol for symbol in pending
        }
        for future in as_completed(future_map):
            symbol = future_map[future]
            try:
                _, row_count = future.result()
            except Exception as error:
                failures[symbol] = repr(error)
            else:
                completed += 1
                discovered_rows += row_count
            if (completed + len(failures)) % 25 == 0:
                print(
                    f"[METADATA] completed={completed}/{len(contract.symbols)} "
                    f"failures={len(failures)} new_rows={discovered_rows:,}",
                    flush=True,
                )

    return {
        "completed": completed,
        "pending_before_run": len(pending),
        "new_rows": discovered_rows,
        "failures": failures,
    }


def merge_metadata_parts(
    *,
    part_dir: Path,
    metadata_path: Path,
) -> dict[str, Any]:
    frames: list[pd.DataFrame] = []
    if metadata_path.exists():
        frames.append(pd.read_csv(metadata_path, dtype=str))
    part_paths = sorted(part_dir.glob("*.csv"))
    for path in part_paths:
        frames.append(pd.read_csv(path, dtype=str))
    merged = merge_report_metadata_frames(frames)
    _write_csv_atomic(metadata_path, merged)
    report_dates = pd.to_datetime(merged.get("report_date"), errors="coerce")
    return {
        "part_count": len(part_paths),
        "merged_row_count": len(merged),
        "historical_row_count": int(
            report_dates.between("2001-01-01", "2012-12-31").sum()
        ),
        "min_report_date": str(report_dates.min().date())
        if report_dates.notna().any()
        else None,
        "max_report_date": str(report_dates.max().date())
        if report_dates.notna().any()
        else None,
    }


def collect_statement_manifests(
    contract: RecoveryContract,
    *,
    manifest_dir: Path,
    workers: int,
    request_interval: float,
) -> dict[str, Any]:
    manifest_dir.mkdir(parents=True, exist_ok=True)
    pending = [
        symbol
        for symbol in contract.symbols
        if not (manifest_dir / f"{symbol}.json").exists()
    ]
    throttle = DartRequestThrottle(request_interval)
    failures: dict[str, str] = {}
    status_counts: dict[str, int] = {}
    completed = len(contract.symbols) - len(pending)

    def recover_one(symbol: str) -> tuple[str, list[dict[str, str]]]:
        output_dir = DATA_LAKE.bronze("dart", "finance-statement", symbol)
        output_dir.mkdir(parents=True, exist_ok=True)
        results = fetch_dart_search(
            symbol,
            str(output_dir),
            start_date=contract.source_start_date,
            end_date=contract.source_end_date,
            force=False,
            sleep_seconds=request_interval,
            throttle=throttle,
        )
        manifest = {
            "version": RECOVERY_VERSION,
            "target_sha256": contract.target_sha256,
            "symbol": symbol,
            "source_start_date": contract.source_start_date,
            "source_end_date": contract.source_end_date,
            "completed_at": _now(),
            "results": results,
        }
        _write_json_atomic(manifest_dir / f"{symbol}.json", manifest)
        return symbol, results

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        future_map = {
            executor.submit(recover_one, symbol): symbol for symbol in pending
        }
        for future in as_completed(future_map):
            symbol = future_map[future]
            try:
                _, results = future.result()
            except Exception as error:
                failures[symbol] = repr(error)
            else:
                completed += 1
                for result in results:
                    key = str(result.get("status") or "unknown")
                    status_counts[key] = status_counts.get(key, 0) + 1
            if (completed + len(failures)) % 10 == 0:
                print(
                    f"[STATEMENTS] completed={completed}/{len(contract.symbols)} "
                    f"failures={len(failures)} statuses={status_counts}",
                    flush=True,
                )

    return {
        "completed": completed,
        "pending_before_run": len(pending),
        "status_counts": dict(sorted(status_counts.items())),
        "failures": failures,
    }


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="backslashreplace")


def build_recovery_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recover strict-PIT KR historical DART metadata and statements."
    )
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--metadata-path", type=Path, default=DEFAULT_METADATA_PATH)
    parser.add_argument("--part-dir", type=Path, default=DEFAULT_PART_DIR)
    parser.add_argument(
        "--statement-manifest-dir",
        type=Path,
        default=DEFAULT_STATEMENT_MANIFEST_DIR,
    )
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=DEFAULT_END_DATE)
    parser.add_argument(
        "--stage",
        choices=("all", "shares", "metadata", "statements", "normalize"),
        default="all",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--normalize-workers", type=int, default=8)
    parser.add_argument(
        "--normalize-start-year",
        type=int,
        default=DEFAULT_NORMALIZE_START_YEAR,
        help="First raw filing year to normalize; defaults to the requested 2000 audit boundary.",
    )
    parser.add_argument(
        "--normalize-end-year",
        type=int,
        default=None,
        help="Last raw filing year to normalize; defaults to --end-date year.",
    )
    parser.add_argument("--request-interval", type=float, default=1.0)
    parser.add_argument(
        "--historical-shares-path",
        type=Path,
        default=DEFAULT_HISTORICAL_SHARES_PATH,
    )
    return parser


def main() -> None:
    args = build_recovery_parser().parse_args()

    _configure_console()
    contract = load_contract(
        args.targets,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    status: dict[str, Any] = {
        "contract": contract.as_dict(),
        "started_at": _now(),
    }
    _write_json_atomic(args.status, status)

    if args.stage in {"all", "shares"}:
        status["historical_shares"] = recover_historical_marcap_shares(
            contract,
            output_path=args.historical_shares_path,
        )
        status["updated_at"] = _now()
        _write_json_atomic(args.status, status)

    if args.stage in {"all", "metadata"}:
        status["metadata_collection"] = collect_metadata_parts(
            contract,
            part_dir=args.part_dir,
            workers=args.workers,
            request_interval=args.request_interval,
        )
        status["metadata_merge"] = merge_metadata_parts(
            part_dir=args.part_dir,
            metadata_path=args.metadata_path,
        )
        status["updated_at"] = _now()
        _write_json_atomic(args.status, status)
        if status["metadata_collection"]["failures"]:
            raise RuntimeError("metadata recovery has retryable failures")

    if args.stage in {"all", "statements"}:
        status["statement_collection"] = collect_statement_manifests(
            contract,
            manifest_dir=args.statement_manifest_dir,
            workers=args.workers,
            request_interval=args.request_interval,
        )
        status["updated_at"] = _now()
        _write_json_atomic(args.status, status)
        if status["statement_collection"]["failures"]:
            raise RuntimeError("statement recovery has retryable failures")

    if args.stage in {"all", "normalize"}:
        normalize_end_year = (
            args.normalize_end_year
            if args.normalize_end_year is not None
            else date.fromisoformat(args.end_date).year
        )
        if args.normalize_start_year > normalize_end_year:
            raise ValueError("normalize-start-year must not exceed normalize-end-year")
        status["normalization"] = normalize_all_statements(
            symbols=list(contract.symbols),
            start_year=args.normalize_start_year,
            end_year=normalize_end_year,
            save_debug=False,
            workers=args.normalize_workers,
        )
        status["updated_at"] = _now()
        _write_json_atomic(args.status, status)
        if status["normalization"]["failed"]:
            raise RuntimeError("historical normalization has failed documents")

    status["completed_at"] = _now()
    _write_json_atomic(args.status, status)
    print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
