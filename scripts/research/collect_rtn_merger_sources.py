"""Retain SEC originals explaining the terminal date of Alpha Vantage RTN quotes."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

from bs4 import BeautifulSoup
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import write_source_bytes
from engine.extractors._internal.sec_filings import DEFAULT_SEC_USER_AGENT


def main():
    bronze = DATA_LAKE.bronze("sec", "listings", "issuer_review_20260911", "RTN")
    silver = DATA_LAKE.silver("survivorship", "financial_research", "us_rtn_source_review_20260911")
    sources = [
        ("completion_8k", "https://www.sec.gov/Archives/edgar/data/1047122/000119312520097237/d827106d8k.htm"),
        ("completion_release", "https://www.sec.gov/Archives/edgar/data/1047122/000119312520097237/d827106dex991.htm"),
        ("merger_agreement", "https://www.sec.gov/Archives/edgar/data/1047122/000094787119000428/ss139705_ex0201.htm"),
        ("merger_amendment", "https://www.sec.gov/Archives/edgar/data/1047122/000119312520072473/d892051dex21.htm"),
        ("filing_index", "https://www.sec.gov/Archives/edgar/data/1047122/000119312520097237/0001193125-20-097237-index.html"),
    ]
    result = dict(status="collecting", sources=[], production_changed=False, coverage_complete=False)
    for name, url in sources:
        path = bronze / (name + ".html")
        metadata_path = path.with_suffix(".metadata.json")
        if path.exists():
            metadata = json.loads(metadata_path.read_bytes())
            raw = path.read_bytes()
            if sha256(raw).hexdigest() != metadata["source_sha256"] or metadata["source_url"] != url:
                raise ValueError("Retained SEC original changed")
        else:
            response = requests.get(url, headers={"User-Agent": DEFAULT_SEC_USER_AGENT, "Accept-Encoding": "identity"}, timeout=30)
            raw = response.content
            write_source_bytes(path, raw, source="SEC-RTN-merger-original")
            metadata = dict(provider="SEC", source_url=url, source_path=str(path), source_sha256=sha256(raw).hexdigest(),
                retrieved_at=datetime.now(timezone.utc).isoformat(), http_status=response.status_code)
            export_json(metadata_path, metadata)
        result["sources"].append(metadata)
        if metadata["http_status"] != 200:
            export_json(silver / "collection.json", result)
            raise ValueError("SEC response was retained but did not provide the requested document")
        soup = BeautifulSoup(raw, "html.parser")
        for node in soup.find_all(["style", "script"]):
            node.decompose()
        text_path = silver / (name + ".txt")
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(soup.get_text("\n", strip=True), "utf-8")
        metadata.update(review_text_path=str(text_path), review_text_sha256=sha256(text_path.read_bytes()).hexdigest())
        export_json(silver / "collection.json", result)
        print(dict(document=name, status="original_retained_pending_review"), flush=True)
    result.update(status="originals_retained_pending_review")
    export_json(silver / "collection.json", result)


if __name__ == "__main__":
    main()
