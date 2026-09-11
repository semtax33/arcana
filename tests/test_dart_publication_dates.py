"""DART availability follows the reported receipt date rather than its identifier."""
from pathlib import Path
from io import BytesIO
import json
from urllib.parse import urlparse
from zipfile import ZipFile

import requests
import pytest

from engine.extractors.stock_splits import download_dart_splits
from engine.extractors.opendart_stock_splits import OpenDartClient, download_document, download_opendart_splits
from test_dart_direct_collection import AMENDED, ORIGINAL, FIXTURES, replay, refresh_review


def shifted_publication(request):
    # A synthetic publication-date variant of the retained Samsung source family.
    # The identifier stays March 16 while all published-date displays say March 19.
    response = replay(request, amended_error=False, actual_index=True)
    response._content = response.content.replace(b"2018.03.16", b"2018.03.19")
    return response


def test_public_index_date_controls_availability_and_related_dates(tmp_path, monkeypatch):
    lake = tmp_path / "data-lake"
    bronze = lake / "bronze/dart/stock_splits"
    monkeypatch.setattr(requests.Session, "send", lambda self, request, **kwargs: shifted_publication(request))

    result = download_dart_splits(symbols=["005930"], start_date="20180319", end_date="20180319",
                                  output_dir=bronze, sleep_seconds=0)

    assert not result["errors"] and result["downloaded_or_cached"] == 2
    folder = bronze / "disclosures/005930"
    amended = json.loads((folder / f"{AMENDED}.html.metadata.json").read_bytes())
    original = json.loads((folder / f"{ORIGINAL}.html.metadata.json").read_bytes())
    assert amended["published_date"] == "2018-03-19"
    assert original["published_date"] == "2018-01-31"
    before = refresh_review(lake, "2018-03-18")
    after = refresh_review(lake, "2018-03-19")
    assert [event["source_id"] for item in before["review"] for event in item.get("candidate_events", [])] == [ORIGINAL]
    assert [event["source_id"] for item in after["review"] for event in item.get("candidate_events", [])] == [AMENDED]


def test_authenticated_cached_search_uses_the_displayed_receipt_date(tmp_path, monkeypatch):
    lake = tmp_path / "data-lake"
    bronze = lake / "bronze/dart/stock_splits"
    search = bronze / "search"
    search.mkdir(parents=True)
    for filename, title in [("split", "주식분할결정"), ("reverse_empty", "주식병합결정")]:
        path = search / f"{filename}.html"
        path.write_bytes((FIXTURES / "direct_search" / path.name).read_bytes().replace(b"2018.03.16", b"2018.03.19"))
        path.with_suffix(".html.metadata.json").write_text(json.dumps({"request": {
            "reportName": title, "startDate": "20180319", "endDate": "20180319", "currentPage": "1"}}), encoding="utf-8")
    archive = BytesIO()
    with ZipFile(archive, "w") as zipped:
        zipped.writestr("CORPCODE.XML", "<result><list><corp_code>00126380</corp_code><stock_code>005930</stock_code></list></result>")

    def send(self, request, **kwargs):
        path = urlparse(request.url).path
        if path in {"/api/corpCode.xml", "/api/document.xml"}:
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response._content = archive.getvalue() if path.endswith("corpCode.xml") else (FIXTURES / "api_014.response").read_bytes()
            return response
        return shifted_publication(request)

    monkeypatch.setenv("DART_API_KEY", "publication-date-transport-test")
    monkeypatch.setattr(requests.Session, "send", send)
    report = download_opendart_splits(symbols=["005930"], start_date="20180319", end_date="20180319", output_dir=bronze)

    assert not report["errors"] and report["indexed_disclosures"] == 1 and report["downloaded_or_cached"] == 1
    metadata = json.loads((bronze / "disclosures/005930" / f"{AMENDED}.html.metadata.json").read_bytes())
    assert metadata["published_date"] == "2018-03-19"
    assert Path(metadata["publication_date_source"]["path"]).is_relative_to(bronze)
    before = refresh_review(lake, "2018-03-18")
    after = refresh_review(lake, "2018-03-19")
    assert not before["review"]
    assert [event["source_id"] for item in after["review"] for event in item.get("candidate_events", [])] == [AMENDED]


@pytest.mark.parametrize("route", ["direct", "authenticated"])
def test_cached_disclosure_repairs_legacy_date_without_replacing_originals(tmp_path, monkeypatch, route):
    lake = tmp_path / "data-lake"
    bronze = lake / "bronze/dart/stock_splits"
    monkeypatch.setattr(requests.Session, "send", lambda self, request, **kwargs: shifted_publication(request))
    download_dart_splits(symbols=["005930"], start_date="20180319", end_date="20180319",
                         output_dir=bronze, sleep_seconds=0)
    sidecar = bronze / "disclosures/005930" / f"{AMENDED}.html.metadata.json"
    legacy = json.loads(sidecar.read_bytes())
    record = {key: legacy[key] for key in ["source_id", "published_date", "publication_date_source", "publication_date_text"]}
    legacy["published_date"] = "2018-03-16"
    legacy.pop("publication_date_source", None)
    legacy.pop("publication_date_text", None)
    sidecar.write_text(json.dumps(legacy), encoding="utf-8")
    legacy_bytes = sidecar.read_bytes()
    raw_before = {path: path.read_bytes() for path in bronze.rglob("*.html")}

    def no_network(self, request, **kwargs):
        raise AssertionError("An available index and disclosure must be reused")

    monkeypatch.setattr(requests.Session, "send", no_network)

    def refresh_cached():
        if route == "direct":
            result = download_dart_splits(symbols=["005930"], start_date="20180319", end_date="20180319",
                                          output_dir=bronze, sleep_seconds=0)
            assert not result["errors"]
        else:
            assert download_document(OpenDartClient(key="cached-date-test"), bronze, record, "005930") == "cached"

    refresh_cached()
    corrected = json.loads(sidecar.read_bytes())
    assert corrected["published_date"] == "2018-03-19"
    assert Path(corrected["publication_date_prior_metadata"]["path"]).read_bytes() == legacy_bytes
    assert all(path.read_bytes() == raw for path, raw in raw_before.items())
    before = refresh_review(lake, "2018-03-18")
    assert [event["source_id"] for item in before["review"] for event in item.get("candidate_events", [])] == [ORIGINAL]
    first = {path: path.read_bytes() for path in bronze.rglob("*") if path.is_file()}
    refresh_cached()
    assert first == {path: path.read_bytes() for path in bronze.rglob("*") if path.is_file()}
