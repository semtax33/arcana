"""Official download results retain the response evidence, including failures."""
from hashlib import sha256
import json
from pathlib import Path

import requests
import pytest

from engine.extractors.opendart_stock_splits import OpenDartClient, download_document


FIXTURES = Path(__file__).resolve().parents[1] / "data-lake/bronze/fixtures/opendart_source_retention"
RECEIPT = "20170518000045"
RECORD = {"source_id": RECEIPT, "published_date": "2017-05-18"}
TEST_KEY = "source-retention-transport-test"


class RecordedDartResponse(requests.adapters.BaseAdapter):
    """Replay the external HTTP response through the real OpenDartClient."""

    def __init__(self, content):
        self.content = content

    def send(self, request, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content = self.content
        response.headers["Content-Type"] = "application/xml;charset=UTF-8"
        response.request = request
        response.url = request.url
        return response

    def close(self):
        pass


def client_for(content):
    client = OpenDartClient(key=TEST_KEY)
    client.session.session.mount("https://", RecordedDartResponse(content))
    return client


def test_unavailable_document_retains_exact_official_response_and_safe_provenance(tmp_path):
    raw = (FIXTURES / "document_014.response").read_bytes()
    assert sha256(raw).hexdigest() == "03f4385d883fd756de28e791dde6f153baad5771898af36fcd2f07b479a84a8f"
    bronze = tmp_path / "data-lake/bronze/dart/stock_splits"

    assert download_document(client_for(raw), bronze, RECORD, "204210") == "unavailable"

    outcome = json.loads((bronze / "document_archives" / f"{RECEIPT}.unavailable.json").read_bytes())
    source = Path(outcome["response_path"])
    assert source.is_relative_to(bronze)
    assert source.read_bytes() == raw
    assert outcome["response_sha256"] == "03f4385d883fd756de28e791dde6f153baad5771898af36fcd2f07b479a84a8f"
    assert outcome["http_status"] == 200
    assert outcome["source_url"] == "https://opendart.fss.or.kr/api/document.xml?rcept_no=20170518000045"
    assert outcome["retrieved_at"]
    assert outcome["status"] == "014"
    assert not (bronze / "disclosures/204210" / f"{RECEIPT}.html").exists()
    for path in bronze.rglob("*"):
        if path.is_file():
            assert TEST_KEY.encode() not in path.read_bytes()


def test_unrecognized_response_is_retained_even_when_document_download_fails(tmp_path):
    # A synthetic HTTP 200 error body is an external-service failure, not a filing.
    raw = b"<html><body>Service unavailable</body></html>"
    bronze = tmp_path / "data-lake/bronze/dart/stock_splits"

    with pytest.raises(ValueError, match="OpenDART document status=unknown"):
        download_document(client_for(raw), bronze, RECORD, "204210")

    originals = list(bronze.rglob("*.response"))
    assert len(originals) == 1
    assert originals[0].read_bytes() == raw
    assert not (bronze / "document_archives" / f"{RECEIPT}.unavailable.json").exists()
    assert not (bronze / "disclosures/204210" / f"{RECEIPT}.html").exists()


def test_empty_response_records_its_provenance_without_inventing_a_document(tmp_path):
    bronze = tmp_path / "data-lake/bronze/dart/stock_splits"

    with pytest.raises(ValueError):
        download_document(client_for(b""), bronze, RECORD, "204210")

    metadata = list((bronze / "document_responses").rglob("*.metadata.json"))
    assert len(metadata) == 1
    outcome = json.loads(metadata[0].read_bytes())
    assert outcome["response_path"] is None
    assert outcome["byte_count"] == 0
    assert outcome["response_sha256"] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert outcome["http_status"] == 200
    assert not list(bronze.rglob("*.response"))
    assert not (bronze / "disclosures").exists()
