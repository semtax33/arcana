"""Follow-up disclosure collection uses observed dates rather than identifiers."""
import json
from copy import copy
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import pytest
from bs4 import BeautifulSoup

from engine.extractors.kind_stock_splits import download_kind_splits
from engine.extractors.stock_splits import download_dart_splits
from test_dart_direct_collection import AMENDED, FIXTURES, replay, refresh_review


def kind_replay(request):
    url = urlparse(request.url)
    query = parse_qs(url.query)
    if url.path == "/disclosure/details.do":
        params = parse_qs(request.body.decode() if isinstance(request.body, bytes) else request.body)
        if params["fromDate"] == params["toDate"] == ["2018-03-19"]:
            content = """<div>전체 1건</div><table><tr><td>2018-03-19</td><td>
            <a id="companysum" title="삼성전자">삼성전자</a>
            <a onclick="openDisclsViewer('20180319000001','')">주식분할결정</a>
            </td></tr></table>""".encode()
        else:
            content = "<div>전체 0건</div>".encode()
    elif url.path == "/common/disclsviewer.do" and query["method"] == ["search"]:
        content = """<h1>삼성전자 (005930)</h1><select id="mainDoc">
        <option selected value="20180319000199|Y">주식분할결정 (2018.03.19)</option></select>""".encode()
    elif url.path == "/common/disclsviewer.do" and query["method"] == ["searchContents"]:
        content = b"parent.setPath('document','https://kind.krx.co.kr/external/2018/03/19/000001.htm');"
    elif url.path == "/external/2018/03/19/000001.htm":
        content = (FIXTURES / "html/full.html").read_bytes().replace(b"2018.03.16", b"2018.03.19")
    else:
        raise AssertionError("Unexpected external KIND request")
    response = requests.Response()
    response.status_code = 200
    response.url = request.url
    response._content = content
    return response


@pytest.mark.parametrize("kind_receipt", ["20180319000001", "20180316000001"])
def test_kind_fallback_recovers_the_actual_publication_day(tmp_path, monkeypatch, kind_receipt):
    bronze = tmp_path / "data-lake/bronze/dart/stock_splits"
    def send(self, request, **kwargs):
        response = kind_replay(request)
        response._content = response.content.replace(b"20180319000001", kind_receipt.encode())
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    report = download_kind_splits(unavailable=[{"source_id": AMENDED, "published_date": "2018-03-19",
        "corp_code": "00126380", "title": "주식분할결정"}], corporation_mapping={"00126380": "005930"},
        symbols=["005930"], start_date="20180319", end_date="20180319", output_dir=bronze, search_titles=())

    assert not report["errors"]
    assert report["fallback_recovered"] == [{"dart_rcept_no": AMENDED, "matches": [kind_receipt]}]
    sidecar = bronze / f"disclosures/005930/kind_{kind_receipt}_20180319000199.html.metadata.json"
    metadata = json.loads(sidecar.read_bytes())
    assert metadata["published_date"] == "2018-03-19" and metadata["dart_rcept_no"] == AMENDED


def test_kind_fallback_retains_an_undated_request_as_unresolved(tmp_path, monkeypatch):
    bronze = tmp_path / "data-lake/bronze/dart/stock_splits"

    def no_network(self, request, **kwargs):
        raise AssertionError("No date was supplied for the KIND fallback search")

    monkeypatch.setattr(requests.Session, "send", no_network)
    report = download_kind_splits(unavailable=[{"source_id": AMENDED,
        "corp_code": "00126380", "title": "주식분할결정"}], corporation_mapping={"00126380": "005930"},
        symbols=["005930"], start_date="20180319", end_date="20180319", output_dir=bronze, search_titles=())

    assert not report["fallback_recovered"]
    assert report["errors"] == [{"dart_rcept_no": AMENDED, "error": "Missing evidenced DART publication date for KIND fallback"}]
    assert json.loads((bronze / "kind_download_report.json").read_bytes()) == report


def test_undated_related_receipt_is_classified_using_its_own_document(tmp_path, monkeypatch):
    lake = tmp_path / "data-lake"
    bronze = lake / "bronze/dart/stock_splits"
    followup = "20180402000001"

    def send(self, request, **kwargs):
        url = urlparse(request.url)
        query = parse_qs(url.query)
        if query.get("rcpNo") == [followup]:
            mapped = copy(request)
            mapped.url = request.url.replace(followup, AMENDED)
            response = replay(mapped, amended_error=False, actual_index=True)
            response.url = request.url
            response._content = response.content.replace(AMENDED.encode(), followup.encode()).replace(b"2018.03.16", b"2018.03.19")
            return response
        response = replay(request, amended_error=False, actual_index=True)
        if url.path == "/dsaf001/main.do" and query.get("rcpNo") == [AMENDED]:
            soup = BeautifulSoup(response.content, "lxml")
            option = soup.new_tag("option", value=f"rcpNo={followup}")
            option.string = "주식분할결정"
            soup.find("select", id="family").append(option)
            response._content = str(soup).encode()
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    report = download_dart_splits(symbols=["005930"], start_date="20180316", end_date="20180316",
                                  output_dir=bronze, sleep_seconds=0)

    assert not report["errors"]
    outside = report.get("outside_cutoff", [])
    assert len(outside) == 1 and outside[0]["source_id"] == followup
    assert outside[0]["published_date"] == "2018-03-19"
    assert Path(outside[0]["publication_date_source"]["path"]).is_file()
    assert not (bronze / "disclosures/005930" / f"{followup}.html").exists()
    result = refresh_review(lake)
    assert [event["source_id"] for item in result["review"] for event in item.get("candidate_events", [])] == [AMENDED]
