from __future__ import annotations

from datetime import date, datetime
import json
import os
import random
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from engine.core.paths import DATA_LAKE


HANKYUNG_CONSENSUS_BASE_URL = "https://markets.hankyung.com/api/v2/consensus/search/report"
BRONZE_HANKYUNG_CONSENSUS_DIR = DATA_LAKE.bronze("consensus", "hankyung")
DEFAULT_HANKYUNG_CONSENSUS_TOKEN = "0ZdNlr7LrQoawewqweq78k6usasBsqhqSIaUarSTf8mxnHuQVh9CvKAfpUy94LhBmZMg"
DEFAULT_START_DATE = date(2001, 1, 1)
DEFAULT_PAGE_SLEEP_MIN_SECONDS = 2.0
DEFAULT_PAGE_SLEEP_MAX_SECONDS = 3.0

JsonGetter = Callable[[str, dict[str, str]], dict[str, Any]]
Sleeper = Callable[[float], None]
Uniform = Callable[[float, float], float]


def download_hankyung_consensus_reports(
    *,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    output_dir: str | Path = BRONZE_HANKYUNG_CONSENSUS_DIR,
    token: str | None = None,
    force: bool = False,
    sleep_seconds: float | None = None,
    http_get_json: JsonGetter | None = None,
    sleeper: Sleeper = time.sleep,
    uniform: Uniform = random.uniform,
) -> dict[str, int]:
    """Download Hankyung consensus report rows into bronze JSON files."""

    resolved_token = _resolve_token(token)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    headers = {
        "Authorization": f"Bearer {resolved_token}",
        "Accept": "application/json",
        "User-Agent": "Arcana consensus downloader",
    }
    get_json = http_get_json or _get_json
    sleep_min, sleep_max = _sleep_range(sleep_seconds)

    counts = {
        "years": 0,
        "pages": 0,
        "rows": 0,
        "written": 0,
        "skipped": 0,
        "invalid": 0,
    }
    for from_date, to_date in _annual_windows(
        _parse_date(start_date) or DEFAULT_START_DATE,
        _parse_date(end_date) or date.today(),
    ):
        counts["years"] += 1
        first_payload = _request_page(
            get_json,
            headers,
            page=1,
            from_date=from_date,
            to_date=to_date,
        )
        counts["pages"] += 1
        _add_counts(counts, _write_payload_rows(first_payload, output_path, force=force))
        _sleep_between_pages(sleep_min, sleep_max, sleeper=sleeper, uniform=uniform)

        last_page = _positive_int(first_payload.get("last_page")) or 1
        for page in range(2, last_page + 1):
            payload = _request_page(
                get_json,
                headers,
                page=page,
                from_date=from_date,
                to_date=to_date,
            )
            counts["pages"] += 1
            _add_counts(counts, _write_payload_rows(payload, output_path, force=force))
            _sleep_between_pages(sleep_min, sleep_max, sleeper=sleeper, uniform=uniform)

        print(
            "[DONE] hankyung consensus "
            f"from={from_date.isoformat()}, to={to_date.isoformat()}, "
            f"last_page={last_page}, total={first_payload.get('total', 0)}",
            flush=True,
        )

    return counts


def _add_counts(total: dict[str, int], delta: dict[str, int]) -> None:
    for key, value in delta.items():
        total[key] = total.get(key, 0) + int(value)


def _request_page(
    get_json: JsonGetter,
    headers: dict[str, str],
    *,
    page: int,
    from_date: date,
    to_date: date,
) -> dict[str, Any]:
    url = _build_url(page=page, from_date=from_date, to_date=to_date)
    return get_json(url, headers)


def _build_url(*, page: int, from_date: date, to_date: date) -> str:
    query = {
        "page": str(page),
        "reportType": "CO",
        "fromDate": from_date.isoformat(),
        "toDate": to_date.isoformat(),
        "gradeCode": "ALL",
        "changePrices": "ALL",
        "searchType": "ALL",
        "reportRange": "1000",
    }
    return f"{HANKYUNG_CONSENSUS_BASE_URL}?{urlencode(query)}"


def _get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _write_payload_rows(payload: dict[str, Any], output_dir: Path, *, force: bool) -> dict[str, int]:
    rows = payload.get("data") or []
    counts = {"rows": 0, "written": 0, "skipped": 0, "invalid": 0}
    if not isinstance(rows, list):
        return counts

    for row in rows:
        if not isinstance(row, dict):
            counts["invalid"] += 1
            continue
        counts["rows"] += 1
        file_name = _row_file_name(row)
        if file_name is None:
            counts["invalid"] += 1
            continue
        path = output_dir / file_name
        if path.exists() and not force:
            counts["skipped"] += 1
            continue
        path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        counts["written"] += 1
    return counts


def _row_file_name(row: dict[str, Any]) -> str | None:
    business_code = _stock_code(row.get("BUSINESS_CODE"))
    register_date = str(row.get("REGISTER_DATE") or "").strip()
    if not business_code or not register_date:
        return None
    return f"{business_code}_{register_date}.json"


def _stock_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.zfill(6) if text.isdigit() else text


def _resolve_token(token: str | None) -> str:
    resolved = (token or os.getenv("HANKYUNG_CONSENSUS_TOKEN") or DEFAULT_HANKYUNG_CONSENSUS_TOKEN).strip()
    if not resolved:
        raise ValueError("HANKYUNG_CONSENSUS_TOKEN or --hankyung-token is required")
    return resolved


def _sleep_range(sleep_seconds: float | None) -> tuple[float, float]:
    if sleep_seconds is None or sleep_seconds <= 0:
        return DEFAULT_PAGE_SLEEP_MIN_SECONDS, DEFAULT_PAGE_SLEEP_MAX_SECONDS
    center = float(sleep_seconds)
    return max(0.0, center - 0.5), center + 0.5


def _sleep_between_pages(
    sleep_min: float,
    sleep_max: float,
    *,
    sleeper: Sleeper,
    uniform: Uniform,
) -> None:
    delay = max(0.0, uniform(sleep_min, sleep_max))
    if delay:
        sleeper(delay)


def _annual_windows(start_date: date, end_date: date) -> list[tuple[date, date]]:
    if start_date > end_date:
        raise ValueError("start_date must be earlier than or equal to end_date")
    windows: list[tuple[date, date]] = []
    year = start_date.year
    while year <= end_date.year:
        current_start = max(start_date, date(year, 1, 1))
        current_end = min(end_date, date(year + 1, 1, 1))
        windows.append((current_start, current_end))
        year += 1
    return windows


def _parse_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    cleaned = text.replace("-", "")
    return datetime.strptime(cleaned, "%Y%m%d").date()


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None
