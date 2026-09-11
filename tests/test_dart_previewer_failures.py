"""Official collection failures before the viewer remain visible to the public CLI."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from engine.extractors.stock_splits import download_dart_splits
from engine.extractors.opendart_stock_splits import OpenDartClient, download_document
from test_dart_direct_collection import FIXTURES, AMENDED, ORIGINAL, replay, refresh_review


def seed_known_original(lake):
    folder = lake / "bronze/dart/stock_splits/disclosures/005930"
    folder.mkdir(parents=True)
    (folder / f"{ORIGINAL}.html").write_bytes((FIXTURES / "samsung_original/full.html").read_bytes())
    (folder / f"{ORIGINAL}.html.metadata.json").write_bytes(
        (FIXTURES / "samsung_original/full.metadata.json").read_bytes())


@pytest.mark.parametrize("failure", ["http", "empty", "no_viewer_parameters", "network", "viewer_network"])
def test_failed_public_request_keeps_known_correction_family_on_hold(tmp_path, monkeypatch, failure):
    lake = tmp_path / "data-lake"
    bronze = lake / "bronze/dart/stock_splits"
    seed_known_original(lake)
    error_body = (FIXTURES / "html/service_message.html").read_bytes()

    def send(self, request, **kwargs):
        url = urlparse(request.url)
        target_path = "/report/viewer.do" if failure == "viewer_network" else "/dsaf001/main.do"
        if url.path == target_path and parse_qs(url.query)["rcpNo"] == [AMENDED]:
            if failure.endswith("network"):
                raise requests.ConnectionError("Synthetic connection interruption before an HTTP response")
            response = replay(request, amended_error=False, actual_index=True)
            response.status_code = 400 if failure == "http" else 200
            response._content = b"" if failure == "empty" else error_body
            return response
        return replay(request, amended_error=False, actual_index=True)

    monkeypatch.setattr(requests.Session, "send", send)
    result = download_dart_splits(symbols=["005930"], start_date="20180316", end_date="20180316",
                                  output_dir=bronze, sleep_seconds=0)
    assert [item["source_id"] for item in result["errors"]] == [AMENDED]
    failed = [json.loads(path.read_bytes()) for path in (bronze / "public_documents" / AMENDED).glob("*.metadata.json")]
    assert len(failed) == 1
    metadata = failed[0]
    assert metadata["source_validation"] == "failed"
    assert metadata["document_stage"] == ("viewer" if failure == "viewer_network" else "main")
    assert metadata["security_id"] == "SEC_KR_005930"
    if failure == "empty" or failure.endswith("network"):
        assert metadata["path"] is None and metadata["byte_count"] == 0
    else:
        assert Path(metadata["path"]).read_bytes() == error_body
    assert metadata["http_response_received"] is (not failure.endswith("network"))
    assert not (bronze / "disclosures/005930" / f"{AMENDED}.html").exists()
    pending = refresh_review(lake)
    assert any(item.get("source_id") == AMENDED and "official_document_unavailable" in item.get("reason", [])
               for item in pending["review"])
    assert not any(event["source_id"] == ORIGINAL for item in pending["review"]
                   for event in item.get("candidate_events", []))
    before = refresh_review(lake, "2018-03-15")
    assert any(event["source_id"] == ORIGINAL for item in before["review"]
               for event in item.get("candidate_events", []))
    monkeypatch.setattr(requests.Session, "send", lambda self, request, **kwargs:
                        replay(request, amended_error=False, actual_index=True))
    recovered = download_dart_splits(symbols=["005930"], start_date="20180316", end_date="20180316",
                                     output_dir=bronze, sleep_seconds=0, force=True)
    assert not recovered["errors"]
    report = refresh_review(lake)
    assert [event["source_id"] for item in report["review"] for event in item.get("candidate_events", [])] == [AMENDED]


@pytest.mark.parametrize("failure", ["main_http", "main_empty", "main_no_viewer_parameters", "main_network",
                                     "viewer_http", "viewer_empty", "viewer_service_message", "viewer_network"])
def test_authenticated_fallback_failure_is_visible_in_refresh(tmp_path, monkeypatch, failure):
    lake = tmp_path / "data-lake"
    bronze = lake / "bronze/dart/stock_splits"
    seed_known_original(lake)

    def send(self, request, **kwargs):
        url = urlparse(request.url)
        if url.netloc == "opendart.fss.or.kr":
            response = requests.Response()
            response.status_code = 200
            response._content = (FIXTURES / "api_014.response").read_bytes()
            response.url = request.url
            return response
        target_path = "/dsaf001/main.do" if failure.startswith("main_") else "/report/viewer.do"
        if url.path == target_path and failure.endswith("network"):
            raise requests.ConnectionError("Synthetic fallback connection interruption")
        response = replay(request, amended_error=False, actual_index=True)
        if url.path == target_path:
            response.status_code = 400 if failure.endswith("http") else 200
            response._content = b"" if failure.endswith("empty") else (FIXTURES / "html/service_message.html").read_bytes()
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    record = {"source_id": AMENDED, "published_date": "2018-03-16"}
    assert download_document(OpenDartClient(key="pre-viewer-transport-test"), bronze, record, "005930") == "unavailable"
    report = refresh_review(lake)
    assert not any(event["source_id"] == ORIGINAL for item in report["review"]
                   for event in item.get("candidate_events", []))
    assert any(item.get("source_id") == AMENDED and "official_document_unavailable" in item.get("reason", [])
               for item in report["review"])
    metadata = json.loads((bronze / "document_archives" / f"{AMENDED}.unavailable.json").read_bytes())["public_fallback"]["failure_source"]
    assert metadata["document_stage"] == failure.split("_")[0]
    assert metadata["http_status"] == (None if failure.endswith("network") else 400 if failure.endswith("http") else 200)
    assert metadata["source_validation"] == "failed"
    assert "pre-viewer-transport-test" not in json.dumps(metadata)
    before = refresh_review(lake, "2018-03-15")
    assert any(event["source_id"] == ORIGINAL for item in before["review"] for event in item.get("candidate_events", []))


def test_legacy_unavailable_cache_gains_a_gap_without_new_requests(tmp_path, monkeypatch):
    from test_dart_public_document_download import website_client
    lake = tmp_path / "data-lake"
    bronze = lake / "bronze/dart/stock_splits"
    seed_known_original(lake)
    record = {"source_id": AMENDED, "published_date": "2018-03-16"}
    assert download_document(website_client("service_message.html", "html"), bronze, record, "005930") == "unavailable"
    # A retained pre-upgrade cache has original bytes and outcome, without the new gap metadata.
    unavailable = bronze / "document_archives" / f"{AMENDED}.unavailable.json"
    cached = json.loads(unavailable.read_bytes())
    cached["public_fallback"].pop("failure_source", None)
    unavailable.write_text(json.dumps(cached), encoding="utf-8")
    for path in (bronze / "public_documents" / AMENDED).glob("*.metadata.json"):
        metadata = json.loads(path.read_bytes())
        metadata.pop("provider", None)
        metadata.pop("source_validation", None)
        path.write_text(json.dumps(metadata), encoding="utf-8")
    raw_before = {path: path.read_bytes() for path in bronze.rglob("*.html")}
    cache_before = unavailable.read_bytes()

    def no_network(self, request, **kwargs):
        raise AssertionError("A retained unavailable cache must not redownload implicitly")

    monkeypatch.setattr(requests.Session, "send", no_network)
    assert download_document(OpenDartClient(key="legacy-cache-test"), bronze, record, "005930") == "unavailable"
    report = refresh_review(lake)
    assert not any(event["source_id"] == ORIGINAL for item in report["review"] for event in item.get("candidate_events", []))
    assert any(item.get("source_id") == AMENDED and "official_document_unavailable" in item.get("reason", [])
               for item in report["review"])
    assert unavailable.read_bytes() == cache_before
    assert all(path.read_bytes() == value for path, value in raw_before.items())
    first = {path: path.read_bytes() for path in bronze.rglob("*") if path.is_file()}
    assert download_document(OpenDartClient(key="legacy-cache-test"), bronze, record, "005930") == "unavailable"
    assert first == {path: path.read_bytes() for path in bronze.rglob("*") if path.is_file()}
