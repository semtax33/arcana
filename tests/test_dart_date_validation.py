"""Undated or contradictory official publications cannot become approved inputs."""
from urllib.parse import urlparse
from pathlib import Path
import json
import re
import subprocess
import sys
from io import BytesIO
from zipfile import ZipFile

import requests
import pytest

from engine.extractors.stock_splits import download_dart_splits
from engine.extractors.opendart_stock_splits import OpenDartClient, download_document
from test_dart_direct_collection import AMENDED, ORIGINAL, FIXTURES, replay
from test_dart_previewer_failures import seed_known_original
from test_dart_publication_dates import shifted_publication


@pytest.mark.parametrize("route", ["direct", "authenticated"])
def test_conflicting_official_dates_do_not_publish_a_disclosure(tmp_path, monkeypatch, route):
    lake = tmp_path / "data-lake"
    bronze = lake / "bronze/dart/stock_splits"
    seed_known_original(lake)

    def send(self, request, **kwargs):
        if urlparse(request.url).path == "/api/document.xml":
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response._content = (FIXTURES / "api_014.response").read_bytes()
            return response
        response = replay(request, amended_error=False, actual_index=True)
        if urlparse(request.url).path == "/dsab007/detailSearch.ax":
            response._content = response.content.replace(b"2018.03.16", b"2018.03.19")
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    if route == "direct":
        report = download_dart_splits(symbols=["005930"], start_date="20180319", end_date="20180319",
                                      output_dir=bronze, sleep_seconds=0)
        assert any(item.get("source_id") == AMENDED and "Conflicting DART publication dates" in item["error"]
                   for item in report["errors"])
    else:
        assert download_document(OpenDartClient(key="date-conflict-test"), bronze,
                                 {"source_id": AMENDED, "published_date": "2018-03-19"}, "005930") == "unavailable"
    assert not (bronze / "disclosures/005930" / f"{AMENDED}.html").exists()
    failed = [json.loads(path.read_bytes()) for path in (bronze / "public_documents" / AMENDED).glob("*.metadata.json")]
    failed = [item for item in failed if item.get("provider") == "DART"]
    assert failed and failed[0]["published_date"] is None
    assert {item["published_date"] for item in failed[0]["publication_date_conflict"]} == {"2018-03-16", "2018-03-19"}
    # March 18 is inside the contradictory interval. The original must not silently resume.
    refreshed = refresh_process(lake, "2018-03-18")
    assert refreshed.returncode != 0 and "Unresolved DART publication date" in refreshed.stderr
    monkeypatch.setattr(requests.Session, "send", lambda self, request, **kwargs: shifted_publication(request))
    recovered = download_dart_splits(symbols=["005930"], start_date="20180319", end_date="20180319",
                                     output_dir=bronze, sleep_seconds=0, force=True)
    assert not recovered["errors"]
    for day in ["2018-03-18", "2018-03-19"]:
        refreshed = refresh_process(lake, day)
        assert refreshed.returncode == 0, refreshed.stderr
        gold = json.loads((lake / "gold/corporate_actions/kr/stock_splits.json").read_bytes())
        assert [event["source_id"] for item in gold["review"] for event in item.get("candidate_events", [])] == [ORIGINAL if day == "2018-03-18" else AMENDED]


def refresh_process(lake, as_of="2018-03-19"):
    program = """
import runpy,sys
from pathlib import Path
from engine.core import paths
paths.DATA_LAKE=paths.DataLakePaths(Path(sys.argv.pop(1)))
runpy.run_module('engine.workflows.stock_splits',run_name='__main__')
"""
    return subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(lake),
        "--market", "kr", "--symbols", "005930", "--end-date", as_of,
        "--skip-download", "--skip-prices"], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_missing_date_is_retained_without_inventing_availability(tmp_path, monkeypatch):
    lake = tmp_path / "data-lake"
    bronze = lake / "bronze/dart/stock_splits"

    def send(self, request, **kwargs):
        response = replay(request, amended_error=False, actual_index=True)
        if urlparse(request.url).path in {"/dsab007/detailSearch.ax", "/dsaf001/main.do"}:
            response._content = re.sub(rb"2018\.\d{2}\.\d{2}", b"", response.content)
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    report = download_dart_splits(symbols=["005930"], start_date="20180319", end_date="20180319",
                                  output_dir=bronze, sleep_seconds=0)

    assert any(item.get("source_id") == AMENDED and "Missing evidenced DART publication date" in item["error"]
               for item in report["errors"])
    assert not (bronze / "disclosures/005930" / f"{AMENDED}.html").exists()
    failed = [json.loads(path.read_bytes()) for path in (bronze / "public_documents" / AMENDED).glob("*.metadata.json")]
    assert failed and all(item["published_date"] is None for item in failed)
    refreshed = refresh_process(lake)
    assert refreshed.returncode != 0 and "Unresolved DART publication date" in refreshed.stderr
    assert not (lake / "gold/corporate_actions/kr/stock_splits.json").exists()


def test_authenticated_fallback_resolves_omitted_date_from_the_official_family(tmp_path, monkeypatch):
    bronze = tmp_path / "data-lake/bronze/dart/stock_splits"

    def send(self, request, **kwargs):
        if urlparse(request.url).path == "/api/document.xml":
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response._content = (FIXTURES / "api_014.response").read_bytes()
            return response
        return shifted_publication(request)

    monkeypatch.setattr(requests.Session, "send", send)
    assert download_document(OpenDartClient(key="omitted-date-test"), bronze, {"source_id": AMENDED}, "005930") == "downloaded"
    metadata = json.loads((bronze / "disclosures/005930" / f"{AMENDED}.html.metadata.json").read_bytes())
    assert metadata["published_date"] == "2018-03-19"
    assert metadata["publication_date_source"]["kind"] == "DART_family_receipt_date"


def test_archive_without_receipt_date_is_retained_but_cannot_publish(tmp_path, monkeypatch):
    lake = tmp_path / "data-lake"
    bronze = lake / "bronze/dart/stock_splits"
    seed_known_original(lake)
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(f"{AMENDED}.xml", b"<DOCUMENT><TITLE>Synthetic filing</TITLE></DOCUMENT>")
    raw = buffer.getvalue()

    def send(self, request, **kwargs):
        assert urlparse(request.url).path == "/api/document.xml"
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response._content = raw
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    record = {"source_id": AMENDED, "family": [ORIGINAL, AMENDED]}
    with pytest.raises(ValueError, match="Missing evidenced DART publication date"):
        download_document(OpenDartClient(key="archive-date-test"), bronze, record, "005930")
    assert (bronze / "document_archives" / f"{AMENDED}.zip").read_bytes() == raw
    assert not (bronze / "disclosures/005930" / f"{AMENDED}.html").exists()
    refreshed = refresh_process(lake)
    assert refreshed.returncode != 0 and "Unresolved DART publication date" in refreshed.stderr

    def no_network(self, request, **kwargs):
        raise AssertionError("The original archive must be reused after its date is supplied")

    monkeypatch.setattr(requests.Session, "send", no_network)
    assert download_document(OpenDartClient(key="archive-date-test"), bronze,
                             {**record, "published_date": "2018-03-19"}, "005930") == "downloaded"
    assert (bronze / "document_archives" / f"{AMENDED}.zip").read_bytes() == raw
    refreshed = refresh_process(lake, "2018-03-18")
    assert refreshed.returncode == 0, refreshed.stderr


def test_unrelated_future_document_does_not_change_a_past_refresh(tmp_path):
    lake = tmp_path / "data-lake"
    seed_known_original(lake)
    future = "20190102000001"
    body = lake / "bronze/dart/stock_splits/disclosures/005930" / f"{future}.html"
    body.write_bytes(b"Synthetic future source with intentionally invalid integrity metadata")
    body.with_suffix(".html.metadata.json").write_text(json.dumps({
        "provider": "DART", "source_id": future, "security_id": "SEC_KR_005930",
        "family_id": future, "published_date": "2019-01-02", "source_sha256": "0" * 64,
        "source_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={future}"}), encoding="utf-8")

    refreshed = refresh_process(lake, "2018-03-15")

    assert refreshed.returncode == 0, refreshed.stderr
    gold = json.loads((lake / "gold/corporate_actions/kr/stock_splits.json").read_bytes())
    assert [event["source_id"] for item in gold["review"] for event in item.get("candidate_events", [])] == [ORIGINAL]


def test_a_new_date_conflict_cannot_be_cleared_by_an_older_approved_copy(tmp_path, monkeypatch):
    lake = tmp_path / "data-lake"
    bronze = lake / "bronze/dart/stock_splits"
    monkeypatch.setattr(requests.Session, "send", lambda self, request, **kwargs:
                        replay(request, amended_error=False, actual_index=True))
    original = download_dart_splits(symbols=["005930"], start_date="20180316", end_date="20180316",
                                   output_dir=bronze, sleep_seconds=0)
    assert not original["errors"]

    def conflicting_send(self, request, **kwargs):
        response = replay(request, amended_error=False, actual_index=True)
        if urlparse(request.url).path == "/dsab007/detailSearch.ax":
            response._content = response.content.replace(b"2018.03.16", b"2018.03.19")
        return response

    monkeypatch.setattr(requests.Session, "send", conflicting_send)
    conflict = download_dart_splits(symbols=["005930"], start_date="20180319", end_date="20180319",
                                   output_dir=bronze, sleep_seconds=0, force=True)
    assert conflict["errors"]
    refreshed = refresh_process(lake, "2018-03-18")
    assert refreshed.returncode != 0 and "Unresolved DART publication date" in refreshed.stderr
