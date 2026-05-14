from datetime import datetime, timedelta
from pathlib import Path
import random
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlencode
import json


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


def _write_text(content: str, save_dir: str, filename: str) -> Path:
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

            _write_text(statement_content, save_dir, out_name)

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

            _write_text(statement_content, save_dir, out_name)


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

def fetch_dart_dividend_search(ticker: str, save_dir: str, save_filename: str | None = None):
    url = "https://dart.fss.or.kr/dsab007/detailSearch.ax"

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
        ("autoSearch", "N"),
        ("autoSearchCorp", "Y"),
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
            page_url = f"https://dart.fss.or.kr{href}"

            # 2) 공시 페이지 GET
            page_resp = request_with_retry(s, "GET", page_url, timeout=30)
            page_resp.encoding = page_resp.apparent_encoding
            content = page_resp.text

            # 사용 예시:
            m = PAT_VIEWDOC.search(content)
            rcept_no = m.group("rcept_no")
            doc_no = m.group("doc_no")
            time.sleep(0.3)

            dividend_frame_url = f"https://dart.fss.or.kr/report/viewer.do?rcpNo={rcept_no}&dcmNo={doc_no}&dtd=HTML"
            dividend_page_resp = request_with_retry(s, "GET", dividend_frame_url, timeout=30)
            dividend_page_resp.encoding = dividend_page_resp.apparent_encoding

            report_date = f"{rcept_no[0:4]}-{rcept_no[4:6]}-{rcept_no[6:8]}"
            safe_title = f"dividend_{report_date}".strip()
            out_name = f"finance_statement_{safe_title}.json"

            try:
                data = parse_dividend_decision_html(dividend_page_resp.content, report_date)
                json_data = json.dumps(data, ensure_ascii=False)
                _write_text(json_data, save_dir, out_name)
                time.sleep(0.3)
            except ValueError as e:
                print("[WARN] 배당 내역이 존재하지 않는 문서입니다.", e)


def download_statements(stock_codes, download_offset):
    download_stock_codes = stock_codes[download_offset:]

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+download_offset})....")
        ticker = stock_code
        dir = f"../data-lake/bronze/dart/finance-statement/{ticker}"
        fetch_dart_search(ticker, dir)

def download_statement_comments(stock_codes, download_offset):
    download_stock_codes = sorted(stock_codes)[download_offset:]

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+download_offset})....")
        ticker = stock_code
        dir = f"../data-lake/bronze/dart/finance-comment/{ticker}"
        fetch_dart_comment_search(ticker, dir)

def download_recent_statement_comments(stock_codes, download_offset):
    download_stock_codes = sorted(stock_codes)[download_offset:]

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+download_offset})....")
        ticker = stock_code
        dir = f"../data-lake/bronze/dart/finance-comment/{ticker}"
        fetch_dart_recent_comment_search(ticker, dir)

def download_dividend_histories(stock_codes, download_offset):
    download_stock_codes = sorted(stock_codes)[download_offset:]

    for offset, stock_code in enumerate(download_stock_codes):
        print(f"downloading {stock_code} (download_offset : {offset+download_offset})....")
        ticker = stock_code
        dir = f"../data-lake/bronze/dart/dividend/{ticker}"
        fetch_dart_dividend_search(ticker, dir)
