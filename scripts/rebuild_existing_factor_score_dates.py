from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.workflows._internal.score_workflow import (
    build_factor_scores,
    build_style_scores,
)


DEFAULT_STATUS_PATH = DATA_LAKE.meta("period_label_score_rebuild_status.json")


def _read_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_status(path: Path, status: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True)
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


def _client_factory() -> Any:
    return get_clickhouse_client(send_receive_timeout=3_600)


def rebuild_existing_score_dates(
    *,
    status_path: Path,
    run_label: str,
    date_shard_index: int = 0,
    date_shard_count: int = 1,
    factor_asof_mode: str = "asof",
    source_dates: list[str] | None = None,
) -> None:
    if source_dates is None:
        client = _client_factory()
        try:
            dates = [
                str(row[0])
                for row in client.query(
                    """
SELECT trade_date
FROM
(
    SELECT DISTINCT trade_date FROM fact_daily_factor_score
    UNION DISTINCT
    SELECT DISTINCT trade_date FROM fact_daily_style_score
)
ORDER BY trade_date
""".strip()
                ).result_rows
            ]
        finally:
            client.close()
    else:
        dates = sorted({str(value).strip() for value in source_dates if str(value).strip()})
    if not dates:
        raise RuntimeError("no existing factor/style score dates found")
    if date_shard_count < 1:
        raise ValueError("date_shard_count must be at least 1")
    if not 0 <= date_shard_index < date_shard_count:
        raise ValueError("date_shard_index must be within the shard count")
    dates = [
        trade_date
        for index, trade_date in enumerate(dates)
        if index % date_shard_count == date_shard_index
    ]
    if not dates:
        raise RuntimeError("date shard contains no existing score dates")

    status = _read_status(status_path)
    if status and status.get("run_label") != run_label:
        raise ValueError("score status belongs to a different rebuild run")
    status.setdefault("run_label", run_label)
    status.setdefault("date_shard_index", date_shard_index)
    status.setdefault("date_shard_count", date_shard_count)
    status.setdefault("factor_asof_mode", factor_asof_mode)
    status.setdefault("dates", dates)
    status.setdefault("completed_dates", [])
    if (
        status["dates"] != dates
        or int(status.get("date_shard_index", -1)) != date_shard_index
        or int(status.get("date_shard_count", 0)) != date_shard_count
        or status.get("factor_asof_mode") != factor_asof_mode
    ):
        raise ValueError("existing score date shard changed after the rebuild started")

    completed = set(status["completed_dates"])
    started_at = time.monotonic()
    for trade_date in dates:
        if trade_date in completed:
            continue
        factor_scores, industry_snapshot = build_factor_scores(
            trade_date,
            factor_asof_mode=factor_asof_mode,
            exclude_financials=False,
            financial_basis="annual",
            client_factory=_client_factory,
        )
        style_scores = build_style_scores(
            trade_date,
            style_profile="DEFAULT",
            client_factory=_client_factory,
        )
        if factor_scores.empty or style_scores.empty:
            raise RuntimeError(f"empty rebuilt scores for trade_date={trade_date}")
        completed.add(trade_date)
        status["completed_dates"] = sorted(completed)
        status.setdefault("rows", {})[trade_date] = {
            "factor_score": len(factor_scores),
            "industry_snapshot": len(industry_snapshot),
            "style_score": len(style_scores),
        }
        _write_status(status_path, status)
        print(
            f"[DONE] score date={trade_date}, factor={len(factor_scores):,}, "
            f"industry={len(industry_snapshot):,}, style={len(style_scores):,}, "
            f"completed={len(completed)}/{len(dates)}, "
            f"elapsed={time.monotonic() - started_at:.1f}s",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild all existing cross-sectional factor/style score dates."
    )
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--date-shard-index", type=int, default=0)
    parser.add_argument("--date-shard-count", type=int, default=1)
    parser.add_argument(
        "--factor-asof-mode", choices=("exact", "asof"), default="asof"
    )
    parser.add_argument(
        "--dates-source-status",
        type=Path,
        help="Read the complete score-date list from another status JSON.",
    )
    args = parser.parse_args()
    source_dates = None
    if args.dates_source_status is not None:
        source_status = _read_status(args.dates_source_status)
        source_dates = source_status.get("dates")
        if not isinstance(source_dates, list):
            raise ValueError("dates source status does not contain a dates list")
    rebuild_existing_score_dates(
        status_path=args.status,
        run_label=args.run_label,
        date_shard_index=args.date_shard_index,
        date_shard_count=args.date_shard_count,
        factor_asof_mode=args.factor_asof_mode,
        source_dates=source_dates,
    )


if __name__ == "__main__":
    main()
