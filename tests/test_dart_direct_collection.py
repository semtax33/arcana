"""The direct DART collector preserves failures without approving error pages."""
from pathlib import Path
import json
from urllib.parse import parse_qs, urlparse

import requests
import pytest
import subprocess
import sys

from engine.extractors.stock_splits import download_dart_splits


FIXTURES = Path(__file__).resolve().parents[1] / "data-lake/bronze/fixtures/dart_public_document"
AMENDED = "20180316800856"
ORIGINAL = "20180131800068"


def replay(request, *, amended_error=True, amended_status=200, empty=False, actual_index=False):
    url = urlparse(request.url)
    query = parse_qs(url.query)
    if url.path == "/dsab007/detailSearch.ax":
        body = request.body.decode() if isinstance(request.body, bytes) else request.body
        if parse_qs(body)["reportName"] == ["주식분할결정"]:
            # Minimal synthetic index for an actual retained split disclosure.
            content = f'''<div>총 1 건</div><table><tr><td>
            <a onclick="openCorpInfoNew('00126380')">삼성전자</a></td><td>
            <a href="/dsaf001/main.do?rcpNo={AMENDED}">주식분할결정</a>
            </td></tr></table>'''.encode()
        else:
            content = b"<div>\xec\xb4\x9d 0 \xea\xb1\xb4</div>"
        if actual_index:
            name = "split.html" if parse_qs(body)["reportName"] == ["주식분할결정"] else "reverse_empty.html"
            content = (FIXTURES / "direct_search" / name).read_bytes()
    elif url.path == "/dsae001/selectPopup.ax":
        content = "<table><tr><td>종목코드</td><td>005930</td></tr></table>".encode()
    elif url.path == "/dsaf001/main.do":
        content = (FIXTURES / ("html/main.html" if query["rcpNo"] == [AMENDED] else "samsung_original/main.html")).read_bytes()
    elif url.path == "/report/viewer.do":
        name = "samsung_original/full.html"
        if query["rcpNo"] == [AMENDED]:
            name = "html/service_message.html" if amended_error else "html/full.html"
        content = (FIXTURES / name).read_bytes()
        if query["rcpNo"] == [AMENDED] and empty:
            content = b""
    else:
        raise AssertionError("Unexpected external DART endpoint")
    response = requests.Response()
    response.status_code = amended_status if url.path == "/report/viewer.do" and query["rcpNo"] == [AMENDED] else 200
    response._content = content
    response.url = request.url
    response.headers["Content-Type"] = "text/html"
    return response


def test_error_page_is_retained_but_not_published_as_a_disclosure(tmp_path, monkeypatch):
    monkeypatch.setattr(requests.Session, "send", lambda self, request, **kwargs: replay(request))
    bronze = tmp_path / "data-lake/bronze/dart/stock_splits"

    result = download_dart_splits(symbols=["005930"], start_date="20180316", end_date="20180316",
                                  output_dir=bronze, sleep_seconds=0)

    assert [item["source_id"] for item in result["errors"]] == [AMENDED]
    assert result["downloaded_or_cached"] == 1
    assert not (bronze / "disclosures/005930" / f"{AMENDED}.html").exists()
    assert (bronze / "disclosures/005930" / f"{ORIGINAL}.html").read_bytes() == (FIXTURES / "samsung_original/full.html").read_bytes()
    retained = list((bronze / "public_documents" / AMENDED).glob("*.viewer.html"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == (FIXTURES / "html/service_message.html").read_bytes()
    metadata = json.loads(retained[0].with_suffix(".html.metadata.json").read_bytes())
    assert metadata["source_validation"] == "failed"


def test_real_empty_reverse_search_does_not_cancel_valid_split_collection(tmp_path, monkeypatch):
    monkeypatch.setattr(requests.Session, "send", lambda self, request, **kwargs:
                        replay(request, amended_error=False, actual_index=True))
    bronze = tmp_path / "data-lake/bronze/dart/stock_splits"
    result = download_dart_splits(symbols=["005930"], start_date="20180316", end_date="20180316",
                                  output_dir=bronze, sleep_seconds=0)
    assert result["disclosures_found"] == 1 and result["downloaded_or_cached"] == 2
    assert not result["errors"]
    assert (bronze / "disclosures/005930" / f"{AMENDED}.html").read_bytes() == (FIXTURES / "html/full.html").read_bytes()


def refresh_review(lake, as_of="2018-03-16"):
    program = """
import runpy,sys
from pathlib import Path
from engine.core import paths
paths.DATA_LAKE=paths.DataLakePaths(Path(sys.argv.pop(1)))
runpy.run_module('engine.workflows.stock_splits',run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(lake),
        "--market", "kr", "--symbols", "005930", "--end-date", as_of,
        "--skip-download", "--skip-prices"], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((lake / "gold/corporate_actions/kr/stock_splits.json").read_bytes())


def test_failed_latest_correction_blocks_only_its_family_until_recovered(tmp_path, monkeypatch):
    from test_split_correction_refresh import disclosure, DECISION
    monkeypatch.setattr(requests.Session, "send", lambda self, request, **kwargs: replay(request))
    lake = tmp_path / "data-lake"
    bronze = lake / "bronze/dart/stock_splits"
    download_dart_splits(symbols=["005930"], start_date="20180316", end_date="20180316",
                        output_dir=bronze, sleep_seconds=0)
    # An independent synthetic issuer must retain its own active proposal.
    other = "20180202000001"
    disclosure(lake, other, DECISION.replace("2020", "2018").replace("04.01", "02.01"))

    pending = refresh_review(lake)
    assert not any(candidate["source_id"] == ORIGINAL for item in pending["review"]
                   for candidate in item.get("candidate_events", []))
    assert any(item.get("source_id") == AMENDED and "official_document_unavailable" in item.get("reason", [])
               for item in pending["review"])
    assert any(candidate["source_id"] == other for item in pending["review"]
               for candidate in item.get("candidate_events", []))

    before = refresh_review(lake, "2018-03-15")
    assert any(candidate["source_id"] == ORIGINAL for item in before["review"]
               for candidate in item.get("candidate_events", []))
    monkeypatch.setattr(requests.Session, "send", lambda self, request, **kwargs: replay(request, amended_error=False))
    result = download_dart_splits(symbols=["005930"], start_date="20180316", end_date="20180316",
                                 output_dir=bronze, sleep_seconds=0, force=True)
    assert not result["errors"]
    recovered = refresh_review(lake)
    candidates = [candidate["source_id"] for item in recovered["review"]
                  for candidate in item.get("candidate_events", []) if candidate["security_id"] == "SEC_KR_005930"]
    assert candidates == [AMENDED]


@pytest.mark.parametrize("http_status,empty", [(400, False), (200, True)])
def test_http_rejection_or_empty_body_remains_a_visible_family_gap(tmp_path, monkeypatch, http_status, empty):
    monkeypatch.setattr(requests.Session, "send", lambda self, request, **kwargs:
                        replay(request, amended_status=http_status, empty=empty))
    lake = tmp_path / "data-lake"
    bronze = lake / "bronze/dart/stock_splits"
    result = download_dart_splits(symbols=["005930"], start_date="20180316", end_date="20180316",
                                 output_dir=bronze, sleep_seconds=0)
    assert [item["source_id"] for item in result["errors"]] == [AMENDED]
    files = list((bronze / "public_documents" / AMENDED).glob("*.metadata.json"))
    assert len(files) == 1
    metadata = json.loads(files[0].read_bytes())
    assert metadata["http_status"] == http_status
    assert metadata["source_validation"] == "failed"
    if empty:
        assert metadata["byte_count"] == 0 and metadata["path"] is None
        assert not list((bronze / "public_documents" / AMENDED).glob("*.viewer.html"))
    report = refresh_review(lake)
    assert not any(candidate["source_id"] == ORIGINAL for item in report["review"]
                   for candidate in item.get("candidate_events", []))
    assert any(item.get("source_id") == AMENDED and "official_document_unavailable" in item.get("reason", [])
               for item in report["review"])
