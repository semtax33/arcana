from datetime import datetime, timedelta
from pathlib import Path
import random
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlencode


RETRY_STATUS = {429, 500, 502, 503, 504}


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


def _write_html_text(content: str, save_dir: str, filename: str) -> Path:
    out_path = Path(save_dir) / _safe_filename(filename)
    out_path.parent.mkdir(parents=True, exist_ok=True)  # 폴더 없으면 생성
    out_path.write_text(content, encoding="utf-8")      # HTML은 보통 utf-8로 저장하면 무난
    return out_path


@dataclass
class NodeMatch:
    text: str
    rcpNo: Optional[str]
    dcmNo: Optional[str]
    eleId: Optional[str]
    offset: Optional[str]
    length: Optional[str]


# node['xxx'] = "value";  또는 node2["xxx"] = 123; 형태 둘 다 처리
_FIELD_RE_TEMPLATE = r"""
node(?:2|3)\[\s*['"]{field}['"]\s*\]\s*=\s*
(?:
    (?P<q>['"])(?P<valq>(?:\\.|(?!\1).)*) (?P=q)
  | (?P<valn>-?\d+)
)
\s*;
"""


def _find_field(block: str, field: str) -> Optional[str]:
    pattern = _FIELD_RE_TEMPLATE.format(field=re.escape(field))
    m = re.search(pattern, block, flags=re.VERBOSE | re.DOTALL)
    if not m:
        return None
    return m.group("valq") if m.group("valq") is not None else m.group("valn")


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


def _safe_title_from_anchor(a) -> str:
    # "보고서명" 텍스트가 통째로 붙는 경우가 많아서 strip 기반으로 안전하게
    txt = a.get_text(" ", strip=True)
    return txt if txt else "untitled"

def fetch_dart_comment_search(ticker: str, save_dir: str, comment_prop_name: str | None = ""):
    url = "https://dart.fss.or.kr/dsab001/search.ax"

    headers = {
        "Accept": "text/html, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7,ar-IQ;q=0.6,ar-JO;q=0.5,ar;q=0.4,ja-JP;q=0.3,ja;q=0.2",
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/8.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/137.36",
    }

    data = [
        ("currentPage", "1"),
        ("maxResults", "100"),
        ("maxLinks", "10"),
        ("sort", "date"),
        ("series", "desc"),
        ("pageGubun", "corp"),
        ("attachDocNmPopYn", ""),
        ("textCrpNm", ticker),
        ("startDate", (datetime.now() - timedelta(1)*3650).strftime("%Y%m%d")),
        ("endDate", datetime.now().strftime("%Y%m%d")),
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

            _write_html_text(statement_content, save_dir, out_name)

def fetch_dart_recent_comment_search(ticker: str, save_dir: str, comment_prop_name: str | None = ""):
    url = "https://dart.fss.or.kr/dsab001/search.ax"

    headers = {
        "Accept": "text/html, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7,ar-IQ;q=0.6,ar-JO;q=0.5,ar;q=0.4,ja-JP;q=0.3,ja;q=0.2",
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/10.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/137.36",
    }

    data = [
        ("currentPage", "1"),
        ("maxResults", "100"),
        ("maxLinks", "10"),
        ("sort", "date"),
        ("series", "desc"),
        ("pageGubun", "corp"),
        ("attachDocNmPopYn", ""),
        ("textCrpNm", ticker),
        ("startDate", (datetime.now() - timedelta(1)*3650).strftime("%Y%m%d")),
        ("endDate", datetime.now().strftime("%Y%m%d")),
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

        print(anchors)

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

            _write_html_text(statement_content, save_dir, out_name)
            
            if not "기재정정" in safe_title_line:
                return


def fetch_dart_search(ticker: str, save_dir: str, save_filename: str | None = None):
    url = "https://dart.fss.or.kr/dsab001/search.ax"

    headers = {
        "Accept": "text/html, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7,ar-IQ;q=0.6,ar-JO;q=0.5,ar;q=0.4,ja-JP;q=0.3,ja;q=0.2",
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/8.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/137.36",
    }

    data = [
        ("currentPage", "1"),
        ("maxResults", "100"),
        ("maxLinks", "10"),
        ("sort", "date"),
        ("series", "desc"),
        ("pageGubun", "corp"),
        ("attachDocNmPopYn", ""),
        ("textCrpNm", ticker),
        ("startDate", (datetime.now() - timedelta(1)*3650).strftime("%Y%m%d")),
        ("endDate", datetime.now().strftime("%Y%m%d")),
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

            _write_html_text(statement_content, save_dir, out_name)

def download_statements(stock_codes, download_offset):
    download_stock_codes = stock_codes[download_offset:]

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+download_offset})....")
        ticker = stock_code
        dir = f"./data-lake/bronze/dart/finance-statement/{ticker}"
        fetch_dart_search(ticker, dir)

def download_statement_comments(stock_codes, download_offset):
    download_stock_codes = sorted(stock_codes)[download_offset:]

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+download_offset})....")
        ticker = stock_code
        dir = f"./data-lake/bronze/dart/finance-comment/{ticker}"
        fetch_dart_comment_search(ticker, dir)

def download_recent_statement_comments(stock_codes, download_offset):
    download_stock_codes = stock_codes[download_offset:]

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+download_offset})....")
        ticker = stock_code
        dir = f"./data-lake/bronze/dart/finance-comment/{ticker}"
        fetch_dart_recent_comment_search(ticker, dir)
