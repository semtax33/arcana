from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup
import pandas as pd
import requests

from engine.core.paths import DATA_LAKE
from engine.core.source_storage import write_source_text
from engine.extractors._internal.dart_filings import (
    DartRequestThrottle,
    _dart_html_headers,
    _wait_for_dart_request,
    request_with_retry,
    select_financial_statement_position,
)


SOURCE_START_DATE = "2011-01-01"
SOURCE_END_DATE = "2016-12-31"
DEFAULT_TARGET_PATH = DATA_LAKE.meta("kr_pvgo_2012_2016_factor_targets.csv")
DEFAULT_PART_DIR = DATA_LAKE.meta("kr_pvgo_2012_2016_report_metadata_parts")
DEFAULT_STATUS_PATH = DATA_LAKE.meta(
    "kr_pvgo_2012_2016_statement_recovery_status.json"
)
STATEMENT_ROOT = DATA_LAKE.bronze("dart", "finance-statement")


def statement_filename(fiscal_year: int, fiscal_month: int) -> str:
    return f"finance_statement_({int(fiscal_year):04d}.{int(fiscal_month):02d}).html"


def load_missing_statement_jobs(
    *,
    target_path: Path = DEFAULT_TARGET_PATH,
    part_dir: Path = DEFAULT_PART_DIR,
    statement_root: Path = STATEMENT_ROOT,
) -> list[dict[str, Any]]:
    targets = pd.read_csv(target_path, dtype={"symbol": str})
    target_symbols = set(targets["symbol"].dropna().astype(str).str.zfill(6))
    jobs: list[dict[str, Any]] = []
    for path in sorted(part_dir.glob("*.csv")):
        symbol = path.stem.zfill(6)
        if symbol not in target_symbols:
            continue
        frame = pd.read_csv(path, dtype=str)
        if frame.empty:
            continue
        frame["period_end_date"] = pd.to_datetime(
            frame["period_end_date"], errors="coerce"
        )
        frame["report_date"] = pd.to_datetime(frame["report_date"], errors="coerce")
        frame = frame.loc[
            frame["period_end_date"].between(SOURCE_START_DATE, SOURCE_END_DATE)
            & frame["report_date"].le(SOURCE_END_DATE)
        ].copy()
        frame = frame.sort_values(["report_date", "rcept_no"]).drop_duplicates(
            ["fiscal_year", "fiscal_month"], keep="last"
        )
        for row in frame.itertuples(index=False):
            output_path = statement_root / symbol / statement_filename(
                int(row.fiscal_year), int(row.fiscal_month)
            )
            if output_path.is_file() and output_path.stat().st_size > 1_000:
                continue
            jobs.append(
                {
                    "symbol": symbol,
                    "fiscal_year": int(row.fiscal_year),
                    "fiscal_month": int(row.fiscal_month),
                    "rcept_no": str(row.rcept_no),
                    "source_url": str(row.source_url),
                    "output_path": output_path,
                }
            )
    return jobs


def fetch_statement_job(
    job: dict[str, Any],
    *,
    throttle: DartRequestThrottle,
) -> dict[str, Any]:
    output_path = Path(job["output_path"])
    if output_path.is_file() and output_path.stat().st_size > 1_000:
        return {**job, "status": "skipped_existing"}

    source_url = str(job["source_url"] or "").strip()
    if not source_url.startswith("https://dart.fss.or.kr/"):
        source_url = (
            "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="
            f"{job['rcept_no']}"
        )
    with requests.Session() as session:
        _wait_for_dart_request(throttle, 0)
        page = request_with_retry(
            session,
            "GET",
            source_url,
            headers=_dart_html_headers(),
            timeout=30,
            throttle=throttle,
        )
        page.encoding = page.apparent_encoding
        soup = BeautifulSoup(page.text, "lxml")
        position = None
        for script in soup.find_all("script"):
            position = select_financial_statement_position(script.get_text())
            if position:
                break
        if position is None:
            return {**job, "status": "statement_section_not_found"}

        viewer_url = "https://dart.fss.or.kr/report/viewer.do?" + urlencode(
            {
                "rcpNo": position.rcpNo,
                "dcmNo": position.dcmNo,
                "eleId": position.eleId,
                "offset": position.offset,
                "length": position.length,
                "dtd": position.dtd or "dart4.xsd",
            }
        )
        _wait_for_dart_request(throttle, 0)
        viewer = request_with_retry(
            session,
            "GET",
            viewer_url,
            headers=_dart_html_headers(),
            timeout=30,
            throttle=throttle,
        )
        viewer.encoding = viewer.apparent_encoding
        if len(viewer.text) <= 1_000:
            return {**job, "status": "empty_statement_response"}
        write_source_text(
            output_path,
            viewer.text,
            source="dart-filing",
            encoding="utf-8",
            metadata={
                "rcept_no": job["rcept_no"],
                "source_url": source_url,
            },
        )
    return {**job, "status": "downloaded"}


def recover(
    *,
    target_path: Path = DEFAULT_TARGET_PATH,
    part_dir: Path = DEFAULT_PART_DIR,
    status_path: Path = DEFAULT_STATUS_PATH,
    workers: int = 24,
    request_interval: float = 0.2,
) -> dict[str, Any]:
    jobs = load_missing_statement_jobs(
        target_path=target_path,
        part_dir=part_dir,
    )
    throttle = DartRequestThrottle(request_interval)
    counts: dict[str, int] = {}
    failures: list[dict[str, Any]] = []
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        future_map = {
            executor.submit(fetch_statement_job, job, throttle=throttle): job
            for job in jobs
        }
        for future in as_completed(future_map):
            job = future_map[future]
            try:
                result = future.result()
            except Exception as error:
                status = "failed"
                failures.append(
                    {
                        "symbol": job["symbol"],
                        "fiscal_year": job["fiscal_year"],
                        "fiscal_month": job["fiscal_month"],
                        "error": repr(error),
                    }
                )
            else:
                status = str(result["status"])
            counts[status] = counts.get(status, 0) + 1
            completed += 1
            if completed % 100 == 0:
                print(
                    f"[STATEMENTS] completed={completed:,}/{len(jobs):,} "
                    f"statuses={dict(sorted(counts.items()))}",
                    flush=True,
                )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_period": [SOURCE_START_DATE, SOURCE_END_DATE],
        "job_count": len(jobs),
        "status_counts": dict(sorted(counts.items())),
        "failure_count": len(failures),
        "failures": failures,
    }
    write_source_text(
        status_path,
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        source="arcana-kr-pvgo-statement-recovery",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str), flush=True)
    if failures:
        raise RuntimeError(f"statement recovery has {len(failures)} retryable failures")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recover only missing 2011-2016 DART statements from PIT metadata."
    )
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--part-dir", type=Path, default=DEFAULT_PART_DIR)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--request-interval", type=float, default=0.2)
    args = parser.parse_args()
    recover(
        target_path=args.targets,
        part_dir=args.part_dir,
        status_path=args.status,
        workers=args.workers,
        request_interval=args.request_interval,
    )


if __name__ == "__main__":
    main()
