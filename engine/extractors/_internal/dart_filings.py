from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import random
import threading
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlencode
import json

import pandas as pd

from engine.core.paths import DATA_LAKE, market_csv_name

RETRY_STATUS = {429, 500, 502, 503, 504}
REPORT_METADATA_COLUMNS = [
    "security_id",
    "stock_code",
    "fiscal_year",
    "fiscal_month",
    "period_end_date",
    "report_date",
    "rcept_no",
    "report_name",
    "source_type",
    "source_url",
    "updated_at",
]

DART_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)
DART_ACCEPT_LANGUAGES = (
    "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "ko-KR,ko;q=0.9,en;q=0.8,en-US;q=0.7",
    "ko,en-US;q=0.9,en;q=0.8",
)


def _dart_html_headers() -> dict[str, str]:
    return {
        "Accept": "text/html, */*; q=0.01",
        "Accept-Language": random.choice(DART_ACCEPT_LANGUAGES),
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": "https://dart.fss.or.kr/",
        "User-Agent": random.choice(DART_USER_AGENTS),
    }


def _sleep_with_jitter(seconds: float) -> None:
    if seconds <= 0:
        return
    time.sleep(random.uniform(seconds * 0.5, seconds * 1.5))


class DartRequestThrottle:
    def __init__(self, interval_seconds: float):
        self.interval_seconds = max(0.0, float(interval_seconds or 0.0))
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait(self) -> None:
        if self.interval_seconds <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed_at:
                time.sleep(self._next_allowed_at - now)
                now = time.monotonic()
            self._next_allowed_at = now + random.uniform(self.interval_seconds * 0.5, self.interval_seconds * 1.5)

    def cooldown(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            self._next_allowed_at = max(self._next_allowed_at, time.monotonic() + seconds)


def _wait_for_dart_request(throttle: DartRequestThrottle | None, seconds: float) -> None:
    if throttle is not None:
        throttle.wait()
    else:
        _sleep_with_jitter(seconds)


def _default_dart_start_date() -> str:
    return (datetime.now() - timedelta(1) * 3650).strftime("%Y%m%d")


def _default_dart_end_date() -> str:
    return datetime.now().strftime("%Y%m%d")


def _parse_dart_date(value: str) -> datetime:
    return datetime.strptime(str(value), "%Y%m%d")


def _add_years(value: datetime, years: int) -> datetime:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, month=2, day=28)


def iter_dart_search_date_windows(
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    years_per_window: int = 10,
) -> list[tuple[str, str]]:
    start = _parse_dart_date(start_date or _default_dart_start_date())
    end = _parse_dart_date(end_date or _default_dart_end_date())
    if start > end:
        raise ValueError("start_date must be earlier than or equal to end_date")

    windows: list[tuple[str, str]] = []
    window_start = start
    while window_start <= end:
        window_end = min(_add_years(window_start, years_per_window) - timedelta(days=1), end)
        windows.append((window_start.strftime("%Y%m%d"), window_end.strftime("%Y%m%d")))
        window_start = window_end + timedelta(days=1)
    return windows


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_retries: int = 6,
    timeout: int | float = 30,
    base_backoff: float = 0.7,
    max_backoff: float = 20.0,
    retry_statuses: set[int] = RETRY_STATUS,
    **kwargs,
) -> requests.Response:
    for attempt in range(max_retries + 1):
        sleep_s = None  # ✅ 매 attempt마다 초기화

        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)

            # 재시도 상태코드면 backoff 후 재시도
            if resp.status_code in retry_statuses:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        sleep_s = float(retry_after)
                    except ValueError:
                        sleep_s = None

                if attempt == max_retries:
                    resp.raise_for_status()
                # else: 재시도 계속

            else:
                # 재시도 대상이 아니면 성공/실패 확정
                resp.raise_for_status()
                return resp

        except (requests.Timeout, requests.ConnectionError):
            if attempt == max_retries:
                raise

        except requests.HTTPError:
            # 403/404 등은 재시도 의미 적으므로 즉시 raise
            raise

        # 여기까지 왔으면 재시도해야 함
        if attempt == max_retries:
            raise RuntimeError(f"Failed after {max_retries} retries: {method} {url}")

        if sleep_s is None:
            sleep_s = min(max_backoff, base_backoff * (2 ** attempt)) + random.uniform(0, 0.3)

        time.sleep(sleep_s)

    raise RuntimeError("request_with_retry: unexpected fallthrough")


def _safe_filename(name: str) -> str:
    # 윈도우/리눅스에서 파일명으로 못 쓰는 문자들 치환
    name = re.sub(r'[\\/*?:"<>|]+', "_", name).strip()
    return name or "output.html"


def _write_text(content: str, save_dir: str, filename: str) -> Path:
    out_path = Path(save_dir) / _safe_filename(filename)
    out_path.parent.mkdir(parents=True, exist_ok=True)  # 폴더 없으면 생성
    out_path.write_text(content, encoding="utf-8")      # HTML은 보통 utf-8로 저장하면 무난
    return out_path


def _normalize_stock_code(value: Any) -> str:
    return str(value).strip().zfill(6)


def _security_id_of(stock_code: Any) -> str:
    return f"SEC_KR_{_normalize_stock_code(stock_code)}"


def _period_end_date(year: int, month: int) -> str:
    return (pd.Timestamp(year=int(year), month=int(month), day=1) + pd.offsets.MonthEnd(0)).date().isoformat()


def _extract_rcept_no_from_href(href: str) -> str | None:
    parsed = urlparse(href)
    values = parse_qs(parsed.query).get("rcpNo")
    if values and re.fullmatch(r"\d{14}", values[0]):
        return values[0]

    match = re.search(r"(?:rcpNo=|['\"])(\d{14})(?:['\"]|&|$)", href)
    return match.group(1) if match else None


def report_date_from_rcept_no(rcept_no: str) -> str:
    rcept_no = str(rcept_no).strip()
    if not re.fullmatch(r"\d{14}", rcept_no):
        raise ValueError(f"invalid rcept_no: {rcept_no}")
    return f"{rcept_no[:4]}-{rcept_no[4:6]}-{rcept_no[6:8]}"


def parse_report_period_from_title(title: str) -> tuple[int, int] | None:
    match = re.search(r"\((\d{4})[./-]\s*(\d{1,2})\)", str(title))
    if not match:
        match = re.search(r"(\d{4})[./-]\s*(\d{1,2})", str(title))
    if not match:
        return None

    month = int(match.group(2))
    if month not in {3, 6, 9, 12}:
        return None
    return int(match.group(1)), month


def deduplicate_report_metadata(records: pd.DataFrame | list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(records, columns=REPORT_METADATA_COLUMNS)
    if df.empty:
        return pd.DataFrame(columns=REPORT_METADATA_COLUMNS)

    for column in REPORT_METADATA_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA

    df = df[REPORT_METADATA_COLUMNS].copy()
    df["stock_code"] = df["stock_code"].map(_normalize_stock_code)
    df["security_id"] = df["stock_code"].map(_security_id_of)
    df["fiscal_year"] = pd.to_numeric(df["fiscal_year"], errors="coerce").astype("Int64")
    df["fiscal_month"] = pd.to_numeric(df["fiscal_month"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["stock_code", "fiscal_year", "fiscal_month", "source_type", "report_date"])
    if df.empty:
        return pd.DataFrame(columns=REPORT_METADATA_COLUMNS)

    df["_report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
    df["_rcept_no"] = pd.to_numeric(df["rcept_no"], errors="coerce")
    df = (
        df.sort_values(["_report_date", "_rcept_no"], kind="stable")
        .drop_duplicates(["stock_code", "fiscal_year", "fiscal_month", "source_type"], keep="last")
        .drop(columns=["_report_date", "_rcept_no"])
        .sort_values(["stock_code", "fiscal_year", "fiscal_month", "source_type"])
        .reset_index(drop=True)
    )
    df["fiscal_year"] = df["fiscal_year"].astype(int)
    df["fiscal_month"] = df["fiscal_month"].astype(int)
    return df[REPORT_METADATA_COLUMNS]


def extract_dart_report_metadata_from_search_html(
    html: str,
    stock_code: str,
    *,
    source_type: str = "statement",
    updated_at: datetime | None = None,
) -> pd.DataFrame:
    soup = BeautifulSoup(html, "lxml")
    stock_code = _normalize_stock_code(stock_code)
    updated_at = updated_at or datetime.now()
    rows: list[dict[str, Any]] = []
    seen_hrefs: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if not href or href in seen_hrefs or "dsaf001" not in href:
            continue

        title = _safe_title_from_anchor(anchor)
        period = parse_report_period_from_title(title)
        rcept_no = _extract_rcept_no_from_href(href)
        if period is None or rcept_no is None:
            continue

        seen_hrefs.add(href)
        fiscal_year, fiscal_month = period
        report_date = report_date_from_rcept_no(rcept_no)
        source_url = urljoin("https://dart.fss.or.kr", href)

        rows.append(
            {
                "security_id": _security_id_of(stock_code),
                "stock_code": stock_code,
                "fiscal_year": fiscal_year,
                "fiscal_month": fiscal_month,
                "period_end_date": _period_end_date(fiscal_year, fiscal_month),
                "report_date": report_date,
                "rcept_no": rcept_no,
                "report_name": title,
                "source_type": source_type,
                "source_url": source_url,
                "updated_at": updated_at,
            }
        )

    return deduplicate_report_metadata(pd.DataFrame(rows, columns=REPORT_METADATA_COLUMNS))


@dataclass
class NodeMatch:
    text: str
    rcpNo: Optional[str]
    dcmNo: Optional[str]
    eleId: Optional[str]
    offset: Optional[str]
    length: Optional[str]
    dtd: Optional[str] = None


# node['xxx'] = "value";  또는 node2["xxx"] = 123; 형태 둘 다 처리
_FIELD_RE_TEMPLATE = r"""
node\d*\[\s*['"]{field}['"]\s*\]\s*=\s*
(?:
    (?P<q>['"])(?P<valq>(?:\\.|(?!\1).)*) (?P=q)
  | (?P<valn>-?\d+)
)
\s*;
"""


_NODE_BLOCK_RE = re.compile(r"\bvar\s+node\d*\s*=\s*\{\}\s*;", flags=re.DOTALL)


def _find_field(block: str, field: str) -> Optional[str]:
    pattern = _FIELD_RE_TEMPLATE.format(field=re.escape(field))
    m = re.search(pattern, block, flags=re.VERBOSE | re.DOTALL)
    if not m:
        return None
    return m.group("valq") if m.group("valq") is not None else m.group("valn")


def _iter_node_blocks(html_or_js: str):
    starts = [m.start() for m in _NODE_BLOCK_RE.finditer(html_or_js)]
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(html_or_js)
        yield html_or_js[start:end]


def _node_match_from_block(block: str) -> NodeMatch | None:
    text = _find_field(block, "text")
    if not text:
        return None
    return NodeMatch(
        text=text,
        rcpNo=_find_field(block, "rcpNo"),
        dcmNo=_find_field(block, "dcmNo"),
        eleId=_find_field(block, "eleId"),
        offset=_find_field(block, "offset"),
        length=_find_field(block, "length"),
        dtd=_find_field(block, "dtd"),
    )


def parse_node2_financial_note(html_or_js: str) -> List[NodeMatch]:
    """
    JS/HTML 텍스트에서
    - var node = {}; 로 시작하는 블록들을 찾고
    - node2['text'] 값에 '재무제표'와 '주석'이 동시에 들어간 블록만 골라
    - eleId, offset, length를 파싱해서 반환
    """
    start_pat = re.compile(r"\bvar\s+node(?:2|3)\s*=\s*\{\}\s*;", flags=re.DOTALL)
    starts = [m.start() for m in start_pat.finditer(html_or_js)]
    if not starts:
        return []

    matches: List[NodeMatch] = []

    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(html_or_js)
        block = html_or_js[s:e]

        text = _find_field(block, "text")
        if not text:
            continue

        # "재무제표" + "주석" 동시 포함 필터
        if ("연결재무제표" in text):
            rcpNo = _find_field(block, "rcpNo")
            dcmNo = _find_field(block, "dcmNo")
            eleId = _find_field(block, "eleId")
            offset = _find_field(block, "offset")
            length = _find_field(block, "length")

            matches.append(NodeMatch(text=text, rcpNo=rcpNo, dcmNo=dcmNo, eleId=eleId, offset=offset, length=length))

    return matches


def parse_node2_financial_individual_note(html_or_js: str) -> List[NodeMatch]:
    """
    JS/HTML 텍스트에서
    - var node = {}; 로 시작하는 블록들을 찾고
    - node2['text'] 값에 '재무제표'와 '주석'이 동시에 들어간 블록만 골라
    - eleId, offset, length를 파싱해서 반환
    """
    start_pat = re.compile(r"\bvar\s+node(?:2|3)\s*=\s*\{\}\s*;", flags=re.DOTALL)
    starts = [m.start() for m in start_pat.finditer(html_or_js)]
    if not starts:
        return []

    matches: List[NodeMatch] = []

    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(html_or_js)
        block = html_or_js[s:e]

        text = _find_field(block, "text")
        if not text:
            continue

        if not ("연결재무제표" in text) and ("재무제표" in text):
            rcpNo = _find_field(block, "rcpNo")
            dcmNo = _find_field(block, "dcmNo")
            eleId = _find_field(block, "eleId")
            offset = _find_field(block, "offset")
            length = _find_field(block, "length")

            matches.append(NodeMatch(text=text, rcpNo=rcpNo, dcmNo=dcmNo, eleId=eleId, offset=offset, length=length))

    return matches


def parse_node2_financial_comment_note(html_or_js: str) -> List[NodeMatch]:
    """
    JS/HTML 텍스트에서
    - var node = {}; 로 시작하는 블록들을 찾고
    - node2['text'] 값에 '재무제표'와 '주석'이 동시에 들어간 블록만 골라
    - eleId, offset, length를 파싱해서 반환
    """
    start_pat = re.compile(r"\bvar\s+node(?:2|3)\s*=\s*\{\}\s*;", flags=re.DOTALL)
    starts = [m.start() for m in start_pat.finditer(html_or_js)]
    if not starts:
        return []

    matches: List[NodeMatch] = []

    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(html_or_js)
        block = html_or_js[s:e]

        text = _find_field(block, "text")
        if not text:
            continue
        
        # "재무제표" + "주석" 동시 포함 필터
        if ("재무제표" in text) and ("주석" in text):
            rcpNo = _find_field(block, "rcpNo")
            dcmNo = _find_field(block, "dcmNo")
            eleId = _find_field(block, "eleId")
            offset = _find_field(block, "offset")
            length = _find_field(block, "length")

            matches.append(NodeMatch(text=text, rcpNo=rcpNo, dcmNo=dcmNo, eleId=eleId, offset=offset, length=length))

    return matches


def _node_length(node: NodeMatch) -> int:
    try:
        return int(node.length or 0)
    except ValueError:
        return 0


def _is_valid_node(node: NodeMatch) -> bool:
    return all([node.rcpNo, node.dcmNo, node.eleId, node.offset, node.length])


def _is_statement_body_node(node: NodeMatch) -> bool:
    return _node_length(node) > 1024 and "주석" not in node.text


def select_financial_statement_position(html_or_js: str) -> Optional[NodeMatch]:
    """
    DART 목차 script에서 재무제표 본문 위치를 고른다.
    연결재무제표가 있으면 우선 사용하고, 없거나 목차성 짧은 블록이면 개별 재무제표로 fallback한다.
    """
    consolidated_positions = [
        node for node in parse_node2_financial_note(html_or_js)
        if _is_valid_node(node)
    ]
    individual_positions = [
        node for node in parse_node2_financial_individual_note(html_or_js)
        if _is_valid_node(node)
    ]

    for node in consolidated_positions:
        if _is_statement_body_node(node):
            return node

    for node in individual_positions:
        if _is_statement_body_node(node):
            return node

    return None


_BUSINESS_EXCLUDE_KEYWORDS = (
    "재무제표",
    "주석",
    "감사의견",
    "감사보고서",
    "배당",
    "임원",
    "주주",
    "계열회사",
    "이사회",
    "감사인",
    "내부회계",
)
_BUSINESS_DIRECT_KEYWORDS = ("사업의내용", "사업내용", "사업의 내용", "II. 사업의 내용")
_BUSINESS_FALLBACK_KEYWORDS = (
    "사업개요",
    "영업의개황",
    "주요제품",
    "주요서비스",
    "매출및수주상황",
    "매출및수주",
    "주요매출",
    "영업개황",
    "회사의개요",
)


def _normalize_toc_text(text: str) -> str:
    return re.sub(r"[\s\.\-_/()\[\]ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVXLCDMivxlcdm0-9II]+", "", str(text or ""))


def _business_info_score(text: str) -> int:
    normalized = _normalize_toc_text(text)
    if not normalized:
        return 0
    if any(keyword in normalized for keyword in _BUSINESS_EXCLUDE_KEYWORDS):
        return 0
    if any(keyword in normalized for keyword in _BUSINESS_DIRECT_KEYWORDS):
        return 100
    if any(keyword in normalized for keyword in _BUSINESS_FALLBACK_KEYWORDS):
        return 50
    return 0


def parse_node_business_info(html_or_js: str) -> List[NodeMatch]:
    matches: List[NodeMatch] = []
    for block in _iter_node_blocks(html_or_js):
        node = _node_match_from_block(block)
        if not node or not _is_valid_node(node):
            continue
        if _business_info_score(node.text) > 0:
            matches.append(node)
    return matches


def select_business_info_position(html_or_js: str) -> Optional[NodeMatch]:
    candidates = parse_node_business_info(html_or_js)
    if not candidates:
        return None
    return max(candidates, key=lambda node: (_business_info_score(node.text), _node_length(node)))


def _business_info_output_name(title: str, save_filename: str | None = None) -> str:
    if save_filename:
        return save_filename
    safe_title = str(title or "").strip()
    if len(safe_title.split("\n")) >= 2:
        safe_title_line = safe_title.split("\n")[1].strip()
    else:
        if not safe_title:
            print("WARNING! title line is omitted!\n")
        safe_title_line = safe_title or "untitled"
    return f"business_info_{safe_title_line}.html"


def _safe_title_from_anchor(a) -> str:
    # "보고서명" 텍스트가 통째로 붙는 경우가 많아서 strip 기반으로 안전하게
    txt = a.get_text(" ", strip=True)
    return txt if txt else "untitled"


def fetch_dart_comment_search(
    ticker: str,
    save_dir: str,
    comment_prop_name: str | None = "",
    *,
    start_date: str | None = None,
    end_date: str | None = None,
):
    for window_start, window_end in iter_dart_search_date_windows(start_date, end_date):
        print(f"searching DART comments {ticker}: {window_start}-{window_end}")
        _fetch_dart_comment_search_window(
            ticker,
            save_dir,
            comment_prop_name,
            start_date=window_start,
            end_date=window_end,
        )


def _fetch_dart_comment_search_window(
    ticker: str,
    save_dir: str,
    comment_prop_name: str | None = "",
    *,
    start_date: str | None = None,
    end_date: str | None = None,
):
    url = "https://dart.fss.or.kr/dsab001/search.ax"

    headers = _dart_html_headers()

    data = [
        ("currentPage", "1"),
        ("maxResults", "100"),
        ("maxLinks", "10"),
        ("sort", "date"),
        ("series", "desc"),
        ("pageGubun", "corp"),
        ("attachDocNmPopYn", ""),
        ("textCrpNm", ticker),
        ("startDate", start_date or _default_dart_start_date()),
        ("endDate", end_date or _default_dart_end_date()),
        ("decadeType", ""),
        ("publicType", "A001"),
        ("publicType", "A002"),
        ("publicType", "A003"),
        ("publicType", "A005"),
        ("publicType", "A004"),
    ]

    with requests.Session() as s:
        # 1) 검색 POST (재시도 적용)
        resp = request_with_retry(s, "POST", url, headers=headers, data=data, timeout=10)
        resp.encoding = resp.apparent_encoding
        html = resp.text

        soup = BeautifulSoup(html, "lxml")
        anchors = soup.find_all("a", href=True)

        # ✅ anchor↔href 매칭 깨짐 방지: anchor를 그대로 순회하면서 href 중복만 skip
        seen_hrefs: set[str] = set()

        for a in anchors:
            href = a.get("href")
            if not href:
                continue
            if href in seen_hrefs:
                continue
            if not "dsaf001" in href:
                continue

            seen_hrefs.add(href)

            title = _safe_title_from_anchor(a)
            page_url = f"https://dart.fss.or.kr{href}"

            # 2) 공시 페이지 GET
            page_resp = request_with_retry(s, "GET", page_url, timeout=10)
            page_resp.encoding = page_resp.apparent_encoding
            page_soup = BeautifulSoup(page_resp.text, "lxml")

            scripts = page_soup.find_all("script")

            # 기존 함수 활용 (너가 이미 가진 함수라고 가정)
            meta_scripts = [sc for sc in scripts if parse_node2_financial_comment_note(sc.get_text()) != []]
            if not meta_scripts:
                continue

            statement_comment_positions = parse_node2_financial_comment_note(meta_scripts[0].get_text())
            statement_position = statement_comment_positions[0]

            params = {
                "rcpNo": statement_position.rcpNo,
                "dcmNo": statement_position.dcmNo,
                "eleId": statement_position.eleId,
                "offset": statement_position.offset,
                "length": statement_position.length,
                "dtd": "dart4.xsd",
            }
            report_viewer_url = f"https://dart.fss.or.kr/report/viewer.do?{urlencode(params)}"

            # 3) viewer GET
            time.sleep(1)
            viewer_resp = request_with_retry(s, "GET", report_viewer_url, timeout=30)
            viewer_resp.encoding = viewer_resp.apparent_encoding
            statement_content = viewer_resp.text

            # 파일명 안전화(윈도우 파일명 금지문자 제거)
            safe_title = title.strip()

            if len(safe_title.split("\n")) >= 2:
                safe_title_line = safe_title.split("\n")[1].strip()
            else:
                print("WARNING! title line is omitted!\n")
                safe_title_line = safe_title #진원생명과학 말고는 다른 케이스 없음으로 보임 
            out_name = f"finance_statement_comment_{safe_title_line}.html"

            _write_text(statement_content, save_dir, out_name)

def fetch_dart_recent_comment_search(
    ticker: str,
    save_dir: str,
    comment_prop_name: str | None = "",
    *,
    start_date: str | None = None,
    end_date: str | None = None,
):
    url = "https://dart.fss.or.kr/dsab001/search.ax"

    headers = _dart_html_headers()

    data = [
        ("currentPage", "1"),
        ("maxResults", "100"),
        ("maxLinks", "10"),
        ("sort", "date"),
        ("series", "desc"),
        ("pageGubun", "corp"),
        ("attachDocNmPopYn", ""),
        ("textCrpNm", ticker),
        ("startDate", start_date or _default_dart_start_date()),
        ("endDate", end_date or _default_dart_end_date()),
        ("decadeType", ""),
        ("publicType", "A001"),
        ("publicType", "A002"),
        ("publicType", "A003"),
        ("publicType", "A005"),
        ("publicType", "A004"),
    ]

    with requests.Session() as s:
        # 1) 검색 POST (재시도 적용)
        resp = request_with_retry(s, "POST", url, headers=headers, data=data, timeout=10)
        resp.encoding = resp.apparent_encoding
        html = resp.text

        soup = BeautifulSoup(html, "lxml")
        anchors = soup.find_all("a", href=True)

        # ✅ anchor↔href 매칭 깨짐 방지: anchor를 그대로 순회하면서 href 중복만 skip
        seen_hrefs: set[str] = set()

        for a in anchors:
            href = a.get("href")
            if not href:
                continue 
            if href in seen_hrefs:
                continue
            if not "dsaf001" in href:
                continue

            seen_hrefs.add(href)

            title = _safe_title_from_anchor(a)
            page_url = f"https://dart.fss.or.kr{href}"

            # 2) 공시 페이지 GET
            page_resp = request_with_retry(s, "GET", page_url, timeout=10)
            page_resp.encoding = page_resp.apparent_encoding
            page_soup = BeautifulSoup(page_resp.text, "lxml")

            scripts = page_soup.find_all("script")

            # 기존 함수 활용 (너가 이미 가진 함수라고 가정)
            meta_scripts = [sc for sc in scripts if parse_node2_financial_comment_note(sc.get_text()) != []]
            if not meta_scripts:
                return

            statement_comment_positions = parse_node2_financial_comment_note(meta_scripts[0].get_text())
            statement_position = statement_comment_positions[0]

            params = {
                "rcpNo": statement_position.rcpNo,
                "dcmNo": statement_position.dcmNo,
                "eleId": statement_position.eleId,
                "offset": statement_position.offset,
                "length": statement_position.length,
                "dtd": "dart4.xsd",
            }
            report_viewer_url = f"https://dart.fss.or.kr/report/viewer.do?{urlencode(params)}"

            # 3) viewer GET
            time.sleep(1)
            viewer_resp = request_with_retry(s, "GET", report_viewer_url, timeout=30)
            viewer_resp.encoding = viewer_resp.apparent_encoding
            statement_content = viewer_resp.text

            # 파일명 안전화(윈도우 파일명 금지문자 제거)
            safe_title = title.strip()

            if len(safe_title.split("\n")) >= 2:
                safe_title_line = safe_title.split("\n")[1].strip()
            else:
                print("WARNING! title line is omitted!\n")
                safe_title_line = safe_title #진원생명과학 말고는 다른 케이스 없음으로 보임
            
            out_name = f"finance_statement_comment_{safe_title_line}.html"

            _write_text(statement_content, save_dir, out_name)
            
            if not "기재정정" in safe_title_line:
                return


def fetch_dart_search(
    ticker: str,
    save_dir: str,
    save_filename: str | None = None,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
):
    for window_start, window_end in iter_dart_search_date_windows(start_date, end_date):
        print(f"searching DART statements {ticker}: {window_start}-{window_end}")
        _fetch_dart_search_window(
            ticker,
            save_dir,
            save_filename,
            start_date=window_start,
            end_date=window_end,
            force=force,
        )


def _fetch_dart_search_window(
    ticker: str,
    save_dir: str,
    save_filename: str | None = None,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
):
    url = "https://dart.fss.or.kr/dsab001/search.ax"

    headers = _dart_html_headers()

    data = [
        ("currentPage", "1"),
        ("maxResults", "100"),
        ("maxLinks", "10"),
        ("sort", "date"),
        ("series", "desc"),
        ("pageGubun", "corp"),
        ("attachDocNmPopYn", ""),
        ("textCrpNm", ticker),
        ("startDate", start_date or _default_dart_start_date()),
        ("endDate", end_date or _default_dart_end_date()),
        ("decadeType", ""),
        ("publicType", "A001"),
        ("publicType", "A002"),
        ("publicType", "A003"),
        ("publicType", "A005"),
        ("publicType", "A004"),
    ]

    with requests.Session() as s:
        # 1) 검색 POST (재시도 적용)
        resp = request_with_retry(s, "POST", url, headers=headers, data=data, timeout=30)
        resp.encoding = resp.apparent_encoding
        html = resp.text

        soup = BeautifulSoup(html, "lxml")
        anchors = soup.find_all("a", href=True)

        # ✅ anchor↔href 매칭 깨짐 방지: anchor를 그대로 순회하면서 href 중복만 skip
        seen_hrefs: set[str] = set()

        for a in anchors:
            href = a.get("href")
            if not href:
                continue
            if href in seen_hrefs:
                continue
            if not "dsaf001" in href:
                continue

            seen_hrefs.add(href)

            title = _safe_title_from_anchor(a)
            safe_title = title.strip()
            if len(safe_title.split("\n")) >= 2:
                safe_title_line = safe_title.split("\n")[1].strip()
            else:
                print("WARNING! title line is omitted!\n")
                safe_title_line = safe_title
            out_name = f"finance_statement_{safe_title_line}.html"
            out_path = Path(save_dir) / _safe_filename(out_name)
            if out_path.exists() and not force:
                print(f"[SKIP] statement exists: {out_path}")
                continue

            page_url = f"https://dart.fss.or.kr{href}"

            # 2) 공시 페이지 GET
            page_resp = request_with_retry(s, "GET", page_url, timeout=30)
            page_resp.encoding = page_resp.apparent_encoding
            page_soup = BeautifulSoup(page_resp.text, "lxml")

            scripts = page_soup.find_all("script")

            statement_position = None
            for sc in scripts:
                statement_position = select_financial_statement_position(sc.get_text())
                if statement_position:
                    break

            if not statement_position:
                continue

            params = {
                "rcpNo": statement_position.rcpNo,
                "dcmNo": statement_position.dcmNo,
                "eleId": statement_position.eleId,
                "offset": statement_position.offset,
                "length": statement_position.length,
                "dtd": "dart4.xsd",
            }
            report_viewer_url = f"https://dart.fss.or.kr/report/viewer.do?{urlencode(params)}"

            # 3) viewer GET
            time.sleep(0.5)
            viewer_resp = request_with_retry(s, "GET", report_viewer_url, timeout=30)
            viewer_resp.encoding = viewer_resp.apparent_encoding
            statement_content = viewer_resp.text

            # 파일명 안전화(윈도우 파일명 금지문자 제거)
            safe_title = title.strip()

            if len(safe_title.split("\n")) >= 2:
                safe_title_line = safe_title.split("\n")[1].strip()
            else:
                print("WARNING! title line is omitted!\n")
                safe_title_line = safe_title #진원생명과학 말고는 다른 케이스 없음으로 보임 
            out_name = f"finance_statement_{safe_title_line}.html"

            _write_text(statement_content, save_dir, out_name)


def fetch_dart_business_info_search(
    ticker: str,
    save_dir: str,
    save_filename: str | None = None,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
    sleep_seconds: float = 5.0,
    throttle: DartRequestThrottle | None = None,
):
    for window_start, window_end in iter_dart_search_date_windows(start_date, end_date):
        print(f"searching DART business info {ticker}: {window_start}-{window_end}")
        _fetch_dart_business_info_search_window(
            ticker,
            save_dir,
            save_filename,
            start_date=window_start,
            end_date=window_end,
            force=force,
            sleep_seconds=sleep_seconds,
            throttle=throttle,
        )


def _fetch_dart_business_info_search_window(
    ticker: str,
    save_dir: str,
    save_filename: str | None = None,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
    sleep_seconds: float = 5.0,
    throttle: DartRequestThrottle | None = None,
):
    url = "https://dart.fss.or.kr/dsab001/search.ax"

    headers = _dart_html_headers()

    data = [
        ("currentPage", "1"),
        ("maxResults", "100"),
        ("maxLinks", "10"),
        ("sort", "date"),
        ("series", "desc"),
        ("pageGubun", "corp"),
        ("attachDocNmPopYn", ""),
        ("textCrpNm", ticker),
        ("startDate", start_date or _default_dart_start_date()),
        ("endDate", end_date or _default_dart_end_date()),
        ("decadeType", ""),
        ("publicType", "A001"),
        ("publicType", "A002"),
        ("publicType", "A003"),
        ("publicType", "A005"),
        ("publicType", "A004"),
    ]

    if save_filename:
        out_path = Path(save_dir) / _safe_filename(save_filename)
        if out_path.exists() and not force:
            print(f"[SKIP] business info exists: {out_path}")
            return

    with requests.Session() as s:
        _wait_for_dart_request(throttle, sleep_seconds)
        resp = request_with_retry(s, "POST", url, headers=headers, data=data, timeout=30)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "lxml")
        anchors = soup.find_all("a", href=True)
        seen_hrefs: set[str] = set()

        for a in anchors:
            href = a.get("href")
            if not href:
                continue
            if href in seen_hrefs:
                continue
            if "dsaf001" not in href:
                continue

            seen_hrefs.add(href)
            title = _safe_title_from_anchor(a)
            out_name = _business_info_output_name(title, save_filename)
            out_path = Path(save_dir) / _safe_filename(out_name)
            if out_path.exists() and not force:
                print(f"[SKIP] business info exists: {out_path}")
                continue

            page_url = urljoin("https://dart.fss.or.kr", href)

            _wait_for_dart_request(throttle, sleep_seconds)
            page_resp = request_with_retry(s, "GET", page_url, timeout=30)
            page_resp.encoding = page_resp.apparent_encoding
            page_soup = BeautifulSoup(page_resp.text, "lxml")

            business_position = None
            for sc in page_soup.find_all("script"):
                script_text = sc.get_text()
                if "makeToc" not in script_text and "node" not in script_text:
                    continue
                business_position = select_business_info_position(script_text)
                if business_position:
                    break

            if not business_position:
                business_position = select_business_info_position(page_resp.text)
            if not business_position:
                print(f"[WARN] business info section not found: ticker={ticker}, title={title}")
                continue

            params = {
                "rcpNo": business_position.rcpNo,
                "dcmNo": business_position.dcmNo,
                "eleId": business_position.eleId,
                "offset": business_position.offset,
                "length": business_position.length,
                "dtd": business_position.dtd or "dart4.xsd",
            }
            report_viewer_url = f"https://dart.fss.or.kr/report/viewer.do?{urlencode(params)}"

            _wait_for_dart_request(throttle, sleep_seconds)
            viewer_resp = request_with_retry(s, "GET", report_viewer_url, timeout=30)
            viewer_resp.encoding = viewer_resp.apparent_encoding
            business_content = viewer_resp.text

            _write_text(business_content, save_dir, out_name)


def fetch_dart_report_metadata(
    ticker: str,
    *,
    source_type: str = "statement",
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    url = "https://dart.fss.or.kr/dsab001/search.ax"

    headers = _dart_html_headers()

    data = [
        ("currentPage", "1"),
        ("maxResults", "100"),
        ("maxLinks", "10"),
        ("sort", "date"),
        ("series", "desc"),
        ("pageGubun", "corp"),
        ("attachDocNmPopYn", ""),
        ("textCrpNm", ticker),
        ("startDate", start_date or _default_dart_start_date()),
        ("endDate", end_date or _default_dart_end_date()),
        ("decadeType", ""),
        ("publicType", "A001"),
        ("publicType", "A002"),
        ("publicType", "A003"),
        ("publicType", "A005"),
        ("publicType", "A004"),
    ]

    with requests.Session() as session:
        resp = request_with_retry(session, "POST", url, headers=headers, data=data, timeout=30)
        resp.encoding = resp.apparent_encoding
        return extract_dart_report_metadata_from_search_html(
            resp.text,
            ticker,
            source_type=source_type,
        )


def collect_dart_report_metadata(
    stock_codes: list[str],
    download_offset: int = 0,
    *,
    source_types: tuple[str, ...] = ("statement", "comment"),
    output_csv_path: str | Path = DATA_LAKE.silver("dart", market_csv_name("report_metadata")),
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []

    for offset, stock_code in enumerate(sorted(stock_codes)[download_offset:], start=download_offset):
        print(f"downloading report metadata {stock_code} (download_offset : {offset})....")
        for source_type in source_types:
            try:
                metadata_df = fetch_dart_report_metadata(
                    stock_code,
                    source_type=source_type,
                    start_date=start_date,
                    end_date=end_date,
                )
            except Exception as e:
                print(f"[WARN] report metadata failed: stock_code={stock_code}, source_type={source_type}, error={repr(e)}")
                continue
            if not metadata_df.empty:
                frames.append(metadata_df)
        time.sleep(0.01)

    if frames:
        result = deduplicate_report_metadata(pd.concat(frames, ignore_index=True))
    else:
        result = pd.DataFrame(columns=REPORT_METADATA_COLUMNS)

    output_csv_path = Path(output_csv_path)
    output_csv_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_csv_path, index=False, encoding="utf-8-sig")
    print(f"[SAVED] {output_csv_path} rows={len(result):,}")
    return result


def _ensure_text(html: Union[str, bytes]) -> str:
    """DART HTML은 euc-kr인 경우가 있어 bytes면 디코딩 시도."""
    if isinstance(html, str):
        return html
    for enc in ("euc-kr", "cp949", "utf-8"):
        try:
            return html.decode(enc)
        except UnicodeDecodeError:
            continue
    return html.decode("utf-8", errors="ignore")


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _extract_unit(label_text: str) -> Optional[str]:
    """
    '배당금총액(원)' 같은 라벨에서 괄호 단위 추출 -> '원'
    """
    m = re.search(r"\(([^)]+)\)", label_text)
    return m.group(1).strip() if m else None


def _add_unit(value: str, unit: Optional[str]) -> str:
    value = _clean_text(value)
    if not unit or value == "-" or value == "":
        return value
    # 이미 value에 단위가 붙어 있으면 중복 방지
    if unit in value:
        return value
    return f"{value} {unit}"


def _find_main_table(soup: BeautifulSoup):
    """
    페이지에 테이블이 여러 개(종류주식 상세표 등) 있으니,
    '배당구분' + '배당금총액' 같은 키워드가 같이 있는 테이블을 메인으로 본다.
    """
    for tbl in soup.find_all("table"):
        t = tbl.get_text(" ", strip=True)
        if ("배당구분" in t) and ("배당금총액" in t) and ("배당기준일" in t):
            return tbl
    return None


def _find_value_by_label(table, label_keyword: str) -> Tuple[Optional[str], Optional[str]]:
    """
    메인 테이블에서 특정 라벨(예: '배당구분', '배당금총액')이 있는 행을 찾아,
    같은 행의 '오른쪽 셀' 값을 반환한다. + 라벨 괄호 단위도 같이 반환.
    """
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        for i, td in enumerate(tds):
            label_text = _clean_text(td.get_text(" ", strip=True))
            if label_keyword in label_text:
                unit = _extract_unit(label_text)
                # 보통 라벨 다음 td가 값
                if i + 1 < len(tds):
                    val_td = tds[i + 1]
                    val_text = _clean_text(val_td.get_text(" ", strip=True))
                    return val_text, unit
    return None, None


def _extract_dps_common_and_preferred(table) -> Dict[str, str]:
    """
    '3. 1주당 배당금(원)' 블록에서
    보통주식 / 종류주식 DPS를 각각 뽑는다.
    """
    trs = table.find_all("tr")
    for idx, tr in enumerate(trs):
        row_text = tr.get_text(" ", strip=True)
        if "1주당 배당금" not in row_text:
            continue

        # 첫 td가 라벨(대개 rowspan=2)이라 단위는 여기서 뽑는 게 제일 안정적
        first_td = tr.find("td")
        unit = _extract_unit(first_td.get_text(" ", strip=True)) if first_td else None

        dps: Dict[str, str] = {}
        # 현재 행 + 다음 행까지 훑어서 보통주/종류주를 채움(대개 2행)
        for j in range(idx, min(idx + 4, len(trs))):
            tds = trs[j].find_all("td")
            if not tds:
                continue
            cells = [_clean_text(td.get_text(" ", strip=True)) for td in tds]

            kind = None
            for c in cells:
                if "보통주식" in c:
                    kind = "보통주식"
                    break
                if "종류주식" in c:
                    kind = "종류주식"
                    break

            if kind:
                value = cells[-1]  # 값은 보통 오른쪽 끝 셀
                dps[kind] = _add_unit(value, unit)

            if ("보통주식" in dps) and ("종류주식" in dps):
                return dps

        # 둘 중 하나만 있어도 반환
        if dps:
            return dps

    return {}


def parse_dividend_decision_html(html: Union[str, bytes], report_date: str) -> Dict[str, Any]:
    html_text = _ensure_text(html)
    soup = BeautifulSoup(html_text, "lxml")  # lxml 없으면 "html.parser"로 바꿔도 됨

    table = _find_main_table(soup)
    if table is None:
        raise ValueError("메인 배당결정 테이블을 찾지 못했습니다. (배당구분/배당금총액/배당기준일 키워드 기준)")

    dividend_class, _ = _find_value_by_label(table, "배당구분")

    total_raw, total_unit = _find_value_by_label(table, "배당금총액")
    total_with_unit = _add_unit(total_raw or "", total_unit)

    record_date, _ = _find_value_by_label(table, "배당기준일")

    # 문서마다 라벨이 살짝 달라질 수 있어 '배당금지급'으로 먼저 찾고,
    # 실패하면 '지급 예정일자' 변형도 한 번 더 시도
    pay_date, _ = _find_value_by_label(table, "배당금지급")
    if not pay_date:
        pay_date, _ = _find_value_by_label(table, "지급 예정일자")

    dps = _extract_dps_common_and_preferred(table)

    return {
        "배당구분": dividend_class,
        "1주당배당금": {
            "보통주식": dps.get("보통주식"),
            "종류주식": dps.get("종류주식"),
        },
        "배당금총액": total_with_unit if total_with_unit else None,
        "배당기준일": record_date,
        "배당지급일": pay_date,  # = 배당금지급 예정일자
        "배당공시일": report_date 
    }

def fetch_dart_dividend_search(
    ticker: str,
    save_dir: str,
    save_filename: str | None = None,
    *,
    corp_code: str | None = None,
    corp_name: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    force: bool = False,
):
    url = "https://dart.fss.or.kr/dsab007/detailSearch.ax"

    headers = _dart_html_headers()

    data = [
        ("currentPage", "1"),
        ("maxResults", "100"),
        ("maxLinks", "10"),
        ("sort", "date"),
        ("series", "desc"),
        ("pageGubun", "corp"),
        ("attachDocNmPopYn", ""),
        ("textCrpNm", corp_name or ticker),
        ("textCrpCik", str(corp_code or "").strip()),
        ("startDate", start_date or _default_dart_start_date()),
        ("endDate", end_date or _default_dart_end_date()),
        ("autoSearch", "N"),
        ("autoSearchCorp", "N" if corp_code else "Y"),
        ("option", "corp"),
        ("decadeType", ""),
        ("businessCode", "all"),
        ("businessNm", "전체"),
        ("corporationType", "all"),
        ("closingAccountsMonth", "all"),
        ("reportName", "현금ㆍ현물배당결정"),
        ("reportName2", "현금ㆍ현물배당결정"),
        ("tocSrch2", "")
    ]

    PAT_VIEWDOC = re.compile(r"""
        viewDoc\(\s*
            (?P<q1>['"])(?P<rcept_no>\d{14})(?P=q1)   # 1) 접수번호
            \s*,\s*
            (?P<q2>['"])(?P<doc_no>\d+)(?P=q2)        # 2) 문서번호
            (?:\s*,\s*(?:'[^']*'|"[^"]*"))*           # 나머지 인자들(전부 문자열이라고 가정) 무시
        \s*\)\s*;?
    """, re.VERBOSE)

    with requests.Session() as s:
        # 1) 검색 POST (재시도 적용)
        resp = request_with_retry(s, "POST", url, headers=headers, data=data, timeout=30)
        resp.encoding = resp.apparent_encoding
        html = resp.text

        soup = BeautifulSoup(html, "lxml")
        anchors = soup.find_all("a", href=True)

        # ✅ anchor↔href 매칭 깨짐 방지: anchor를 그대로 순회하면서 href 중복만 skip
        seen_hrefs: set[str] = set()

        for a in anchors:
            href = a.get("href")
            text = a.get_text()
            
            if not href:
                continue
            if href in seen_hrefs:
                continue
            if not "dsaf001" in href:
                continue
            if not "현금ㆍ현물배당결정" in text:
                continue

            seen_hrefs.add(href)

            title = _safe_title_from_anchor(a)
            if "자회사의주요경영사항" in title.replace(" ", ""):
                continue
            page_url = f"https://dart.fss.or.kr{href}"

            # 2) 공시 페이지 GET
            page_resp = request_with_retry(s, "GET", page_url, timeout=30)
            page_resp.encoding = page_resp.apparent_encoding
            content = page_resp.text

            # 사용 예시:
            m = PAT_VIEWDOC.search(content)
            rcept_no = m.group("rcept_no")
            doc_no = m.group("doc_no")
            report_date = f"{rcept_no[0:4]}-{rcept_no[4:6]}-{rcept_no[6:8]}"
            safe_title = f"dividend_{report_date}_{rcept_no}".strip()
            out_name = f"finance_statement_{safe_title}.json"
            out_path = Path(save_dir) / _safe_filename(out_name)
            if out_path.exists() and not force:
                print(f"[SKIP] dividend exists: {out_path}")
                continue
            time.sleep(0.3)

            dividend_frame_url = f"https://dart.fss.or.kr/report/viewer.do?rcpNo={rcept_no}&dcmNo={doc_no}&dtd=HTML"
            dividend_page_resp = request_with_retry(s, "GET", dividend_frame_url, timeout=30)
            dividend_page_resp.encoding = dividend_page_resp.apparent_encoding

            report_date = f"{rcept_no[0:4]}-{rcept_no[4:6]}-{rcept_no[6:8]}"
            safe_title = f"dividend_{report_date}_{rcept_no}".strip()
            out_name = f"finance_statement_{safe_title}.json"

            try:
                data = parse_dividend_decision_html(dividend_page_resp.content, report_date)
                data.update(
                    {
                        "stock_code": str(ticker).strip().zfill(6),
                        "corp_code": str(corp_code or "").strip(),
                        "corp_name": str(corp_name or "").strip(),
                        "rcept_no": rcept_no,
                        "source_report_name": title,
                    }
                )
                json_data = json.dumps(data, ensure_ascii=False)
                _write_text(json_data, save_dir, out_name)
                time.sleep(1.0)
            except ValueError as e:
                print("[WARN] 배당 내역이 존재하지 않는 문서입니다.", e)


def download_statements(
    stock_codes,
    download_offset,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    display_offset_base: int | None = None,
    force: bool = False,
):
    download_stock_codes = stock_codes[download_offset:]
    offset_base = download_offset if display_offset_base is None else display_offset_base

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+offset_base})....")
        ticker = stock_code
        dir = str(DATA_LAKE.bronze("dart", "finance-statement", ticker))
        fetch_dart_search(ticker, dir, start_date=start_date, end_date=end_date, force=force)


def download_statement_comments(
    stock_codes,
    download_offset,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    display_offset_base: int | None = None,
):
    download_stock_codes = sorted(stock_codes)[download_offset:]
    offset_base = download_offset if display_offset_base is None else display_offset_base

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+offset_base})....")
        ticker = stock_code
        dir = str(DATA_LAKE.bronze("dart", "finance-comment", ticker))
        fetch_dart_comment_search(ticker, dir, start_date=start_date, end_date=end_date)


def download_business_infos(
    stock_codes,
    download_offset,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    max_workers: int = 1,
    force: bool = False,
    sleep_seconds: float = 5.0,
    stock_retries: int = 3,
    stock_retry_backoff: float = 30.0,
    display_offset_base: int | None = None,
):
    download_stock_codes = sorted(stock_codes)[download_offset:]
    max_workers = max(1, int(max_workers or 1))
    stock_retries = max(0, int(stock_retries or 0))
    stock_retry_backoff = max(0.0, float(stock_retry_backoff or 0.0))
    throttle = DartRequestThrottle(sleep_seconds) if max_workers > 1 else None

    def _download_one(task: tuple[int, str]) -> bool:
        offset, stock_code = task
        ticker = stock_code
        dir = str(DATA_LAKE.bronze("dart", "business-info", ticker))
        for attempt in range(stock_retries + 1):
            print(f"downloading {stock_code} (download_offset : {offset}, attempt : {attempt + 1}/{stock_retries + 1})....")
            try:
                fetch_dart_business_info_search(
                    ticker,
                    dir,
                    start_date=start_date,
                    end_date=end_date,
                    force=force,
                    sleep_seconds=sleep_seconds,
                    throttle=throttle,
                )
                return True
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == stock_retries:
                    print(
                        f"[WARN] business info download failed: "
                        f"stock_code={stock_code}, offset={offset}, error={repr(e)}"
                    )
                    return False
                wait_s = stock_retry_backoff * (2 ** attempt) + random.uniform(0, stock_retry_backoff)
                if throttle is not None:
                    throttle.cooldown(wait_s)
                print(
                    f"[WARN] DART transient disconnect: stock_code={stock_code}, "
                    f"attempt={attempt + 1}/{stock_retries + 1}, sleep={wait_s:.1f}s, error={repr(e)}"
                )
                time.sleep(wait_s)
            except Exception as e:
                print(f"[WARN] business info download failed: stock_code={stock_code}, offset={offset}, error={repr(e)}")
                return False
        return False

    offset_base = download_offset if display_offset_base is None else display_offset_base
    tasks = [(offset + offset_base, stock_code) for offset, stock_code in enumerate(download_stock_codes)]
    if max_workers == 1:
        for task in tasks:
            _download_one(task)
        return

    print(f"downloading business info with max_workers={max_workers}")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {executor.submit(_download_one, task): task for task in tasks}
        for future in as_completed(future_to_task):
            offset, stock_code = future_to_task[future]
            try:
                future.result()
            except Exception as e:
                print(f"[WARN] business info download failed: stock_code={stock_code}, offset={offset}, error={repr(e)}")


def download_recent_statement_comments(
    stock_codes,
    download_offset,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
):
    download_stock_codes = sorted(stock_codes)[download_offset:]

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+download_offset})....")
        ticker = stock_code
        dir = str(DATA_LAKE.bronze("dart", "finance-comment", ticker))
        fetch_dart_recent_comment_search(ticker, dir, start_date=start_date, end_date=end_date)


@lru_cache(maxsize=1)
def _dart_corp_identity_by_stock_code() -> dict[str, tuple[str, str]]:
    from engine.extractors._internal.krx_market_universe import fetch_corp_list

    frame = fetch_corp_list()
    result: dict[str, tuple[str, str]] = {}
    for row in frame.to_dict("records"):
        raw_stock_code = str(row.get("stock_code") or "").strip()
        if not raw_stock_code or not raw_stock_code.isdigit():
            continue
        stock_code = raw_stock_code.zfill(6)
        result[stock_code] = (
            str(row.get("corp_code") or "").strip(),
            str(row.get("corp_name") or "").strip(),
        )
    return result


def download_dividend_histories(
    stock_codes,
    download_offset,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    display_offset_base: int | None = None,
    force: bool = False,
):
    download_stock_codes = sorted(stock_codes)[download_offset:]
    offset_base = download_offset if display_offset_base is None else display_offset_base

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+offset_base})....")
        ticker = stock_code
        corp_code, corp_name = _dart_corp_identity_by_stock_code().get(
            str(stock_code).strip().zfill(6),
            ("", ""),
        )
        dir = str(DATA_LAKE.bronze("dart", "dividend", ticker))
        fetch_dart_dividend_search(
            ticker,
            dir,
            corp_code=corp_code or None,
            corp_name=corp_name or None,
            start_date=start_date,
            end_date=end_date,
            force=force,
        )
