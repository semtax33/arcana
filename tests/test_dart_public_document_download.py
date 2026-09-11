"""Recover complete official documents when the OpenDART archive is missing."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from engine.extractors.opendart_stock_splits import OpenDartClient, download_document


FIXTURES = Path(__file__).resolve().parents[1] / "data-lake/bronze/fixtures/dart_public_document"
RECEIPT = "20170518000045"


class RecordedDartWebsite(requests.adapters.BaseAdapter):
    def __init__(self, viewer="full.html", dataset="", viewer_status=200):
        self.viewer = viewer
        self.fixtures = FIXTURES / dataset
        self.viewer_status = viewer_status

    def send(self, request, **kwargs):
        url = urlparse(request.url)
        query = parse_qs(url.query)
        if url.netloc == "opendart.fss.or.kr" and url.path == "/api/document.xml":
            path = FIXTURES / "api_014.response"
        elif url.netloc == "dart.fss.or.kr" and url.path == "/dsaf001/main.do":
            path = self.fixtures / "main.html"
        elif url.netloc == "dart.fss.or.kr" and url.path == "/report/viewer.do":
            expected = json.loads((self.fixtures / "full.metadata.json").read_bytes())["request"]
            path = self.fixtures / self.viewer if query == {key: [value] for key, value in expected.items()} else FIXTURES / "wrong_format.html"
        else:
            raise AssertionError("Unexpected external DART endpoint")
        response = requests.Response()
        response.status_code = self.viewer_status if url.path == "/report/viewer.do" else 200
        response._content = path.read_bytes()
        response.headers["Content-Type"] = "text/html;charset=UTF-8"
        response.request = request
        response.url = request.url
        return response

    def close(self):
        pass


def website_client(viewer="full.html", dataset="", viewer_status=200):
    client = OpenDartClient(key="public-document-transport-test")
    client.session.session.mount("https://", RecordedDartWebsite(viewer,dataset,viewer_status))
    return client


def test_archive_missing_recovers_the_complete_dart_document_in_bronze(tmp_path):
    bronze = tmp_path / "data-lake/bronze/dart/stock_splits"
    record = {"source_id": RECEIPT, "published_date": "2017-05-18"}

    assert download_document(website_client(), bronze, record, "204210") == "downloaded"

    path = bronze / "disclosures/204210" / f"{RECEIPT}.html"
    assert path.read_bytes() == (FIXTURES / "full.html").read_bytes()
    metadata = json.loads(path.with_suffix(".html.metadata.json").read_bytes())
    assert metadata["provider"] == "DART"
    assert metadata["representation"] == "dart_public_full_document_html"
    assert metadata["published_date"] == "2017-05-18"
    assert metadata["full_document_verified"] is True
    assert Path(metadata["main_path"]).is_relative_to(bronze)
    assert Path(metadata["main_path"]).read_bytes() == (FIXTURES / "main.html").read_bytes()
    assert (bronze / "document_archives" / f"{RECEIPT}.unavailable.json").exists()
    assert "public-document-transport-test" not in json.dumps(metadata)


def test_partial_viewer_is_not_published_and_forced_retry_preserves_it(tmp_path):
    bronze = tmp_path / "data-lake/bronze/dart/stock_splits"
    record = {"source_id": RECEIPT, "published_date": "2017-05-18"}
    path = bronze / "disclosures/204210" / f"{RECEIPT}.html"

    assert download_document(website_client("section.html"), bronze, record, "204210") == "unavailable"
    assert not path.exists()
    retained = list((bronze / "public_documents").rglob("*.viewer.html"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == (FIXTURES / "section.html").read_bytes()

    assert download_document(website_client(), bronze, record, "204210", force=True) == "downloaded"
    assert path.read_bytes() == (FIXTURES / "full.html").read_bytes()
    assert retained[0].read_bytes() == (FIXTURES / "section.html").read_bytes()
    assert len(list((bronze / "public_documents").rglob("*.viewer.html"))) == 2


def test_completed_public_download_reuses_the_same_source_and_provenance(tmp_path):
    bronze = tmp_path / "data-lake/bronze/dart/stock_splits"
    record = {"source_id": RECEIPT, "published_date": "2017-05-18"}
    assert download_document(website_client(), bronze, record, "204210") == "downloaded"
    before = {path: path.read_bytes() for path in bronze.rglob("*") if path.is_file()}

    assert download_document(website_client("section.html"), bronze, record, "204210") == "cached"

    assert before == {path: path.read_bytes() for path in bronze.rglob("*") if path.is_file()}


def test_html_service_message_cannot_replace_a_split_disclosure(tmp_path):
    bronze = tmp_path / "data-lake/bronze/dart/stock_splits"
    record = {"source_id": "20180316800856", "published_date": "2018-03-16"}

    result = download_document(website_client("service_message.html", "html"), bronze, record, "005930")

    assert result == "unavailable"
    assert not (bronze / "disclosures/005930/20180316800856.html").exists()
    retained = list((bronze / "public_documents").rglob("*.viewer.html"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == (FIXTURES / "html/service_message.html").read_bytes()


def test_html_split_disclosure_preserves_encoding_and_correction_family(tmp_path):
    bronze = tmp_path / "data-lake/bronze/dart/stock_splits"
    record = {"source_id": "20180316800856", "published_date": "2018-03-16"}

    assert download_document(website_client(dataset="html"), bronze, record, "005930") == "downloaded"

    path = bronze / "disclosures/005930/20180316800856.html"
    assert path.read_bytes() == (FIXTURES / "html/full.html").read_bytes()
    metadata = json.loads(path.with_suffix(".html.metadata.json").read_bytes())
    assert metadata["encoding"] == "cp949"
    assert metadata["family"] == ["20180131800068", "20180316800856"]
    assert metadata["family_id"] == "20180131800068"
    assert metadata["document_title"] == "삼성전자/주식분할결정/2018.03.16"


def test_rejected_http_response_is_retained_with_its_status(tmp_path):
    bronze = tmp_path / "data-lake/bronze/dart/stock_splits"
    record = {"source_id": "20180316800856", "published_date": "2018-03-16"}

    assert download_document(website_client("service_message.html", "html", 400), bronze, record, "005930") == "unavailable"

    originals = list((bronze / "public_documents").rglob("*.viewer.html"))
    assert len(originals) == 1
    assert originals[0].read_bytes() == (FIXTURES / "html/service_message.html").read_bytes()
    metadata = json.loads(originals[0].with_suffix(".html.metadata.json").read_bytes())
    assert metadata["http_status"] == 400
    assert not (bronze / "disclosures/005930/20180316800856.html").exists()
