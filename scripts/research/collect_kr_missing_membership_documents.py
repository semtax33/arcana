"""Retain missing DART liquidation documents for observed historical-universe gaps."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import sys
from zipfile import ZipFile

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import write_source_bytes
from engine.extractors.opendart_stock_splits import OpenDartClient
from engine.transformers._internal.dart_document import _decode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", nargs="+", default=[])
    parser.add_argument("--report-name", default="new_document_downloads.json")
    args = parser.parse_args()
    if Path(args.report_name).name != args.report_name or not args.report_name.endswith(".json"):
        raise ValueError("Report name must be a JSON file name")
    output = DATA_LAKE.silver("survivorship", "financial_research", "kr_remaining_missing_membership_20260911")
    inventory_path = output / ("issuer_index_collection.json" if args.receipts else "document_inventory.json")
    inventory = json.loads(inventory_path.read_bytes())
    if args.receipts:
        selected = {}
        for row in inventory["rows"]:
            if row["rcept_no"] not in args.receipts:
                continue
            index_path = Path(row["index_source"])
            raw_index = index_path.read_bytes()
            if sha256(raw_index).hexdigest() != row["index_sha256"]:
                raise ValueError("Original DART index changed")
            if not any(r == {k: row[k] for k in r} for r in json.loads(raw_index).get("list", [])
                       if r.get("rcept_no") == row["rcept_no"]):
                raise ValueError("Selected document is absent from original DART index")
            selected[row["rcept_no"]] = dict(receipt=row["rcept_no"], symbol=row["observed_symbol"],
                title=row["report_nm"], published_date=row["rcept_dt"], index_source=row["index_source"],
                status="needs_original_download")
        if set(selected) != set(args.receipts):
            raise ValueError("A requested receipt has no retained issuer index")
        items = list(selected.values())
    else:
        items = inventory["documents"]
    bronze = DATA_LAKE.bronze("dart", "listings", "issuer_review_20260911", "missing_membership")
    client = OpenDartClient()
    records = []
    for item in items:
        if item["status"] != "needs_original_download":
            continue
        receipt, symbol = item["receipt"], item["symbol"]
        if not re.fullmatch(r"\d{14}", receipt) or not re.fullmatch(r"[A-Z0-9]{6}", symbol):
            raise ValueError("Invalid document identity")
        path = bronze / symbol / (receipt + ".response")
        metadata_path = path.with_suffix(".metadata.json")
        if path.exists():
            metadata = json.loads(metadata_path.read_bytes())
            raw = path.read_bytes()
            if sha256(raw).hexdigest() != metadata["response_sha256"]:
                raise ValueError("Retained response changed")
        else:
            response = client.get("document.xml", rcept_no=receipt)
            raw = response.content
            write_source_bytes(path, raw, source="OpenDART-missing-membership-document-response")
            metadata = dict(receipt=receipt, symbol=symbol, provider="DART", title=item["title"],
                published_date=item["published_date"], index_source=item["index_source"],
                source_url=f"https://opendart.fss.or.kr/api/document.xml?rcept_no={receipt}",
                http_status=response.status_code, retrieved_at=datetime.now(timezone.utc).isoformat(),
                response_path=str(path), response_sha256=sha256(raw).hexdigest())
            export_json(metadata_path, metadata)
        if raw.startswith(b"PK"):
            with ZipFile(io.BytesIO(raw)) as archive:
                names = [n for n in archive.namelist() if Path(n).name == receipt + ".xml"]
                if len(names) != 1:
                    raise ValueError("Ambiguous main document in retained DART archive")
                document = archive.read(names[0])
            source = path.with_suffix(".xml")
            write_source_bytes(source, document, source="OpenDART-original-main-document")
            soup = BeautifulSoup(_decode(document)[0], "lxml-xml")
            for node in soup.find_all(lambda node: node.name.lower() in {"style", "script"}):
                node.decompose()
            text_path = output / "extracted" / symbol / (receipt + ".txt")
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(soup.get_text("\n", strip=True), "utf-8")
            metadata.update(status="available_pending_content_review", source_path=str(source),
                source_sha256=sha256(document).hexdigest(), review_text_path=str(text_path))
        else:
            status = BeautifulSoup(raw, "xml").find("status")
            metadata.update(status=status.get_text() if status else "unrecognized_response")
        export_json(metadata_path, metadata)
        records.append(metadata)
        export_json(output / args.report_name, dict(status="originals_retained_pending_review", documents=records,
            inventory_path=str(inventory_path), inventory_sha256=sha256(inventory_path.read_bytes()).hexdigest(),
            new_listing_episodes_registered=0))
        print({key: metadata[key] for key in ("symbol", "receipt", "status")}, flush=True)


if __name__ == "__main__":
    main()
