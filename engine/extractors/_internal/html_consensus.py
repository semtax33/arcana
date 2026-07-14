from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from engine.core.paths import DATA_LAKE


VALUEFINDER_CONSENSUS_BASE_URL = "https://valuefinder.co.kr/bbs/board.php"
EQUITY_CONSENSUS_BASE_URL = "https://www.equity.co.kr/research/researchList.do"
EQUITY_RESEARCH_POP_URL = "https://www.equity.co.kr/research/researchPop.do"
BRONZE_VALUEFINDER_CONSENSUS_DIR = DATA_LAKE.bronze("consensus", "valuefinder")
BRONZE_EQUITY_CONSENSUS_DIR = DATA_LAKE.bronze("consensus", "equity")
DEFAULT_HTML_CONSENSUS_PAGES = 1
DEFAULT_PAGE_SLEEP_MIN_SECONDS = 2.0
DEFAULT_PAGE_SLEEP_MAX_SECONDS = 3.0

TextGetter = Callable[[str, dict[str, str]], str]
TextPoster = Callable[[str, dict[str, str], dict[str, str]], str]
Sleeper = Callable[[float], None]
Uniform = Callable[[float, float], float]


def download_valuefinder_consensus_reports(
    *,
    start_page: int = 1,
    pages: int = DEFAULT_HTML_CONSENSUS_PAGES,
    output_dir: str | Path = BRONZE_VALUEFINDER_CONSENSUS_DIR,
    cookie: str | None = None,
    force: bool = False,
    sleep_seconds: float | None = None,
    http_get_text: TextGetter | None = None,
    sleeper: Sleeper = time.sleep,
    uniform: Uniform = random.uniform,
) -> dict[str, int]:
    """Download ValueFinder analyst opinion rows into bronze JSON files."""

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    headers = _headers(
        cookie=_resolve_cookie(cookie, "VALUEFINDER_CONSENSUS_COOKIE"),
        referer="https://valuefinder.co.kr/bbs/board.php?bo_table=report",
    )
    get_text = http_get_text or _get_text
    sleep_min, sleep_max = _sleep_range(sleep_seconds)
    counts = _empty_counts()

    for page in _page_numbers(start_page=start_page, pages=pages):
        url = _valuefinder_page_url(page)
        html = get_text(url, headers)
        rows = parse_valuefinder_consensus_html(html, page=page, source_url=url)
        counts["pages"] += 1
        _add_counts(counts, _write_report_rows(rows, output_path, force=force))
        _sleep_between_pages(sleep_min, sleep_max, sleeper=sleeper, uniform=uniform)

    return counts


def download_equity_consensus_reports(
    *,
    start_page: int = 1,
    pages: int = DEFAULT_HTML_CONSENSUS_PAGES,
    output_dir: str | Path = BRONZE_EQUITY_CONSENSUS_DIR,
    cookie: str | None = None,
    force: bool = False,
    sleep_seconds: float | None = None,
    http_post_text: TextPoster | None = None,
    sleeper: Sleeper = time.sleep,
    uniform: Uniform = random.uniform,
) -> dict[str, int]:
    """Download EQUITY analyst opinion rows into bronze JSON files."""

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    headers = _headers(
        cookie=_resolve_cookie(cookie, "EQUITY_CONSENSUS_COOKIE"),
        content_type="application/x-www-form-urlencoded",
        referer="https://www.equity.co.kr/research/researchList.do",
    )
    post_text = http_post_text or _post_form_text
    sleep_min, sleep_max = _sleep_range(sleep_seconds)
    counts = _empty_counts()

    for page in _page_numbers(start_page=start_page, pages=pages):
        html = post_text(EQUITY_CONSENSUS_BASE_URL, headers, {"curPageNo": str(page)})
        rows = parse_equity_consensus_html(html, page=page, source_url=EQUITY_CONSENSUS_BASE_URL)
        counts["pages"] += 1
        _add_counts(counts, _write_report_rows(rows, output_path, force=force))
        _sleep_between_pages(sleep_min, sleep_max, sleeper=sleeper, uniform=uniform)

    return counts


def parse_valuefinder_consensus_html(
    html: str,
    *,
    page: int = 1,
    source_url: str = VALUEFINDER_CONSENSUS_BASE_URL,
) -> list[dict[str, Any]]:
    """Parse ValueFinder report list HTML into Hankyung-compatible raw rows."""

    soup = BeautifulSoup(html or "", "html.parser")
    rows: list[dict[str, Any]] = []
    for row_number, tr in enumerate(soup.select("table tr"), start=1):
        if tr.find("th"):
            continue
        report_date = _date_text_from_display(_cell_text(tr.select_one(".td_datetime")))
        stock_name = _cell_text(tr.select_one(".td_cate"))
        title_link = tr.select_one(".td_subject .bo_tit > a[href]") or tr.select_one("a[href*='wr_id=']")
        title = _cell_text(title_link) or _cell_text(tr.select_one(".td_subject"))
        writer = _cell_text(tr.select_one(".td_name"))
        target_price = _price_text(_cell_text(tr.select_one(".td_aim span") or tr.select_one(".td_aim")))
        opinion = _visible_cell_text(tr.select_one(".td_opinion"))
        detail_url = urljoin(source_url, title_link.get("href", "")) if title_link else ""
        report_idx = _query_value(detail_url, "wr_id")
        stock_code = _stock_code_from_text(f"{stock_name} {title}")
        if not (report_date and title):
            continue
        rows.append(
            _report_row(
                source_provider="valuefinder",
                page=page,
                row_number=row_number,
                report_idx=report_idx,
                publish_code="VALUEFINDER",
                office_name="ValueFinder",
                stock_code=stock_code,
                stock_name=stock_name,
                report_title=title,
                report_writer=writer,
                report_date=report_date,
                grade_value=opinion,
                target_stock_prices=target_price,
                opinion_end_prices="",
                report_filepath=detail_url,
                report_filename=f"valuefinder_{report_idx}" if report_idx else "",
                source_url=source_url,
            )
        )
    return rows


def parse_equity_consensus_html(
    html: str,
    *,
    page: int = 1,
    source_url: str = EQUITY_CONSENSUS_BASE_URL,
) -> list[dict[str, Any]]:
    """Parse EQUITY report list HTML into Hankyung-compatible raw rows."""

    soup = BeautifulSoup(html or "", "html.parser")
    rows: list[dict[str, Any]] = []
    for row_number, tr in enumerate(soup.select("table tr"), start=1):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 12:
            continue
        title_link = cells[5].find("a")
        title = _cell_text(title_link) or _cell_text(cells[5])
        report_date = _date_text_from_display(_cell_text(cells[0]))
        report_idx = _equity_report_id(title_link)
        stock_code = _stock_code_from_text(title)
        stock_name = _stock_name_from_equity_title(title)
        detail_url = _equity_detail_url(report_idx, title_link, source_url)
        if not (report_date and title):
            continue
        rows.append(
            _report_row(
                source_provider="equity",
                page=page,
                row_number=row_number,
                report_idx=report_idx,
                publish_code="EQUITY",
                office_name=_cell_text(cells[7]),
                stock_code=stock_code,
                stock_name=stock_name,
                report_title=title,
                report_writer=_cell_text(cells[6]),
                report_date=report_date,
                grade_value=_cell_text(cells[4]),
                old_grade_value=_cell_text(cells[2]),
                target_stock_prices=_price_text(_cell_text(cells[9])),
                opinion_end_prices=_price_text(_cell_text(cells[10])),
                report_type=_cell_text(cells[1]) or "CO",
                report_filepath=detail_url,
                report_filename=f"equity_{report_idx}" if report_idx else "",
                source_url=source_url,
            )
        )
    return rows


def _report_row(
    *,
    source_provider: str,
    page: int,
    row_number: int,
    report_idx: str,
    publish_code: str,
    office_name: str,
    stock_code: str,
    stock_name: str,
    report_title: str,
    report_writer: str,
    report_date: str,
    grade_value: str,
    target_stock_prices: str,
    opinion_end_prices: str,
    source_url: str,
    old_grade_value: str = "",
    report_type: str = "CO",
    report_filepath: str = "",
    report_filename: str = "",
) -> dict[str, Any]:
    flags = []
    if not stock_code:
        flags.append("missing_stock_code")
    return {
        "SOURCE_PROVIDER": source_provider,
        "SOURCE_URL": source_url,
        "SOURCE_PAGE": page,
        "SOURCE_ROW": row_number,
        "REPORT_IDX": report_idx,
        "PUBLISH_CODE": publish_code,
        "OFFICE_NAME": office_name,
        "BUSINESS_CODE": stock_code,
        "BUSINESS_NAME": stock_name,
        "REPORT_TYPE": report_type,
        "REPORT_TITLE": report_title,
        "REPORT_WRITER": report_writer,
        "REPORT_CONTENT": "",
        "REPORT_FILEPATH": report_filepath,
        "REPORT_FILENAME": report_filename,
        "REPORT_DATE": report_date,
        "GRADE_CODE": grade_value,
        "GRADE_VALUE": grade_value,
        "OLD_GRADE_CODE": old_grade_value,
        "OLD_GRADE_VALUE": old_grade_value,
        "OPINON_END_PRICES": opinion_end_prices,
        "TARGET_STOCK_PRICES": target_stock_prices,
        "REGISTER_DATE": _register_date_from_report_date(report_date),
        "UPDATE_DATE": _register_date_from_report_date(report_date),
        "QUALITY_FLAGS": "|".join(flags),
    }


def _write_report_rows(rows: list[dict[str, Any]], output_dir: Path, *, force: bool) -> dict[str, int]:
    counts = {"rows": 0, "written": 0, "skipped": 0, "invalid": 0}
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
    provider = _safe_filename_part(row.get("SOURCE_PROVIDER")) or "html"
    report_idx = _safe_filename_part(row.get("REPORT_IDX"))
    register_date = re.sub(r"\D", "", str(row.get("REGISTER_DATE") or ""))[:14]
    stock_code = _safe_filename_part(row.get("BUSINESS_CODE")) or "unknown"
    if not report_idx:
        seed = f"{provider}|{row.get('SOURCE_URL')}|{row.get('REPORT_TITLE')}|{row.get('SOURCE_ROW')}"
        report_idx = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    if not register_date:
        register_date = "00000000000000"
    return f"{provider}_{stock_code}_{register_date}_{report_idx}.json"


def _valuefinder_page_url(page: int) -> str:
    query = {"bo_table": "report", "page": str(int(page))}
    return f"{VALUEFINDER_CONSENSUS_BASE_URL}?{urlencode(query)}"


def _headers(*, cookie: str, referer: str, content_type: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": referer,
        "User-Agent": "Arcana consensus downloader",
    }
    if content_type:
        headers["Content-Type"] = content_type
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _get_text(url: str, headers: dict[str, str]) -> str:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=60) as response:
        return _decode_response(response.read(), response.headers.get_content_charset())


def _post_form_text(url: str, headers: dict[str, str], data: dict[str, str]) -> str:
    request = Request(url, data=urlencode(data).encode("utf-8"), headers=headers, method="POST")
    with urlopen(request, timeout=60) as response:
        return _decode_response(response.read(), response.headers.get_content_charset())


def _decode_response(body: bytes, charset: str | None) -> str:
    encoding = charset or "utf-8"
    try:
        return body.decode(encoding)
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


def _resolve_cookie(cookie: str | None, env_name: str) -> str:
    return str(cookie or os.getenv(env_name) or "").strip()


def _empty_counts() -> dict[str, int]:
    return {"pages": 0, "rows": 0, "written": 0, "skipped": 0, "invalid": 0}


def _add_counts(total: dict[str, int], delta: dict[str, int]) -> None:
    for key, value in delta.items():
        total[key] = total.get(key, 0) + int(value)


def _page_numbers(*, start_page: int, pages: int) -> range:
    first_page = max(1, int(start_page or 1))
    page_count = max(0, int(pages or 0))
    return range(first_page, first_page + page_count)


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


def _cell_text(node: Any) -> str:
    if node is None:
        return ""
    return _collapse_text(node.get_text(" ", strip=True))


def _visible_cell_text(node: Any) -> str:
    if node is None:
        return ""
    for hidden in node.select(".text_box, script, style"):
        hidden.extract()
    return _cell_text(node)


def _price_text(value: Any) -> str:
    text = _collapse_text(value)
    if text in {"", "-", "0"}:
        return ""
    return text


def _collapse_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _date_text_from_display(value: Any) -> str:
    text = _collapse_text(value)
    if not text:
        return ""
    match = re.search(r"(?P<year>\d{2,4})[./-](?P<month>\d{1,2})[./-](?P<day>\d{1,2})", text)
    if not match:
        return ""
    year = int(match.group("year"))
    if year < 100:
        year += 1900 if year >= 80 else 2000
    try:
        return datetime(year, int(match.group("month")), int(match.group("day"))).date().isoformat()
    except ValueError:
        return ""


def _register_date_from_report_date(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 8:
        return f"{digits}000000"
    return ""


def _stock_code_from_text(value: Any) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value or ""))
    return match.group(1) if match else ""


def _stock_name_from_equity_title(title: str) -> str:
    match = re.match(r"\s*(?P<name>.*?)\s*\(\d{6}\)", title or "")
    return _collapse_text(match.group("name")) if match else ""


def _equity_report_id(link: Any) -> str:
    if link is None:
        return ""
    onclick = str(link.get("onclick") or "")
    match = re.search(r"researchPop\(\s*['\"]?(\d+)['\"]?", onclick)
    if match:
        return match.group(1)
    href = str(link.get("href") or "")
    parsed = urlparse(href)
    for query_text in (parsed.query, parsed.fragment):
        value = (parse_qs(query_text).get("P_ID") or [""])[0]
        if value:
            return value
    return ""


def _equity_detail_url(report_idx: str, link: Any, source_url: str) -> str:
    if report_idx:
        return f"{EQUITY_RESEARCH_POP_URL}?{urlencode({'P_ID': report_idx})}"
    if link is None:
        return ""
    href = str(link.get("href") or "")
    return urljoin(source_url, href) if href else ""


def _query_value(url: str, key: str) -> str:
    return (parse_qs(urlparse(url).query).get(key) or [""])[0]


def _safe_filename_part(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r'[\\/*?:"<>|]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    return text[:80]
