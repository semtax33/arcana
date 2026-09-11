"""Retain original DART notices around two already identified trading halts."""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
from urllib.parse import urlencode
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
    bronze = DATA_LAKE.bronze("dart", "listings", "issuer_review_20260911", "trading_halts")
    silver = DATA_LAKE.silver("survivorship", "financial_research", "kr_trading_halt_source_review_20260911")
    report_path = silver / "collection.json"
    if report_path.exists():
        raise ValueError("Retain the previous collection report")
    client = OpenDartClient()
    report = dict(status="collecting", indexes=[], documents=[], production_changed=False)
    for symbol, corp, begin, end in (
        ("140910", "00860730", "20240201", "20240206"),
        ("204210", "01035289", "20250211", "20250213"),
    ):
        page, total, received, candidates = 1, None, 0, {}
        while True:
            params = dict(corp_code=corp, bgn_de=begin, end_de=end, last_reprt_at="N", page_count=100,
                          page_no=page, sort="date", sort_mth="asc")
            response = client.get("list.json", **params)
            raw = response.content
            path = bronze / symbol / f"index-{begin}-{end}-{page}.json"
            write_source_bytes(path, raw, source="DART-trading-halt-index")
            metadata = dict(provider="DART", symbol=symbol, corp_code=corp, request=params,
                source_path=str(path.resolve()), source_sha256=hashlib.sha256(raw).hexdigest(),
                source_url="https://opendart.fss.or.kr/api/list.json?"+urlencode(params),
                retrieved_at=datetime.now(timezone.utc).isoformat(), http_status=response.status_code)
            export_json(path.with_suffix(".metadata.json"), metadata)
            report["indexes"].append(metadata)
            export_json(report_path, report)
            payload = json.loads(raw)
            if payload.get("status") == "013" and page == 1:
                break
            if payload.get("status") != "000":
                raise ValueError("DART index did not complete")
            count = int(payload["total_count"])
            if total is not None and total != count:
                raise ValueError("DART index changed during pagination")
            total = count
            for row in payload["list"]:
                if row["corp_code"] != corp or row["stock_code"] != symbol:
                    raise ValueError("DART index issuer identity mismatch")
                received += 1
                if any(word in row["report_nm"] for word in ("매매거래", "시장안내")):
                    candidates[row["rcept_no"]] = dict(row, index_source=str(path.resolve()),
                        index_sha256=metadata["source_sha256"])
            if page >= int(payload["total_page"]):
                if received != total:
                    raise ValueError("DART index row count mismatch")
                break
            page += 1
        for receipt, row in sorted(candidates.items()):
            response = client.get("document.xml", rcept_no=receipt)
            raw = response.content
            path = bronze / symbol / f"{receipt}.response"
            write_source_bytes(path, raw, source="DART-trading-halt-response")
            metadata = dict(provider="DART", symbol=symbol, corp_code=corp, receipt=receipt,
                title=row["report_nm"], published_date=row["rcept_dt"], index_source=row["index_source"],
                index_sha256=row["index_sha256"], response_path=str(path.resolve()),
                response_sha256=hashlib.sha256(raw).hexdigest(),
                source_url=f"https://opendart.fss.or.kr/api/document.xml?rcept_no={receipt}",
                retrieved_at=datetime.now(timezone.utc).isoformat(), http_status=response.status_code)
            if raw.startswith(b"PK"):
                with ZipFile(io.BytesIO(raw)) as archive:
                    members = [name for name in archive.namelist() if Path(name).name == receipt+".xml"]
                    if len(members) != 1:
                        raise ValueError("Ambiguous DART main document")
                    document = archive.read(members[0])
                original = path.with_suffix(".xml")
                write_source_bytes(original, document, source="DART-trading-halt-main-document")
                soup = BeautifulSoup(_decode(document)[0], "lxml-xml")
                for tag in soup.find_all(lambda tag: tag.name.lower() in {"style", "script"}):
                    tag.decompose()
                text_path = silver / "extracted" / symbol / f"{receipt}.txt"
                text_path.parent.mkdir(parents=True, exist_ok=True)
                text_path.write_text(soup.get_text("\n", strip=True), "utf-8")
                metadata.update(status="original_retained_pending_review", source_path=str(original.resolve()),
                    source_sha256=hashlib.sha256(document).hexdigest(), extracted_text_path=str(text_path.resolve()))
            else:
                metadata["status"] = "document_unavailable_retained_response"
            export_json(path.with_suffix(".metadata.json"), metadata)
            report["documents"].append(metadata)
            export_json(report_path, report)
            print(dict(symbol=symbol, receipt=receipt, title=row["report_nm"], status=metadata["status"]), flush=True)
    report["status"] = "originals_collected_pending_review"
    export_json(report_path, report)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Transport exception messages may contain credential-bearing URLs.
        print({"status": "failed", "error_type": type(exc).__name__})
        sys.exit(1)
