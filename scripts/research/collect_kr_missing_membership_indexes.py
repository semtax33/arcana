"""Collect issuer-specific original DART indexes for five observed universe gaps."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.source_storage import write_source_bytes
from engine.core.serving_storage import export_json
from engine.extractors.opendart_stock_splits import OpenDartClient

CORPS = {"140910": "00860730", "204210": "01035289", "230980": "01110076",
         "464440": "01775952", "464680": "01785551"}


def main():
    output = DATA_LAKE.silver("survivorship", "financial_research", "kr_remaining_missing_membership_20260911")
    bronze = DATA_LAKE.bronze("dart", "listings", "issuer_review_20260911", "missing_membership", "indexes")
    client = OpenDartClient()
    report = dict(status="collecting", sources=[], rows=[], production_changed=False)
    for symbol, corp in CORPS.items():
        for label, options in (("recent_all", {"bgn_de": "20250101"}),
                               ("annual_history", {"bgn_de": "20100101", "pblntf_detail_ty": "A001"})):
            page, received, expected = 1, 0, None
            while True:
                params = dict(corp_code=corp, end_de="20260910", last_reprt_at="N", page_count=100,
                    page_no=page, sort="date", sort_mth="asc", **options)
                digest = sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]
                path = bronze / symbol / (digest + ".json")
                metadata_path = path.with_suffix(".metadata.json")
                if path.exists():
                    metadata = json.loads(metadata_path.read_bytes())
                    raw = path.read_bytes()
                    if sha256(raw).hexdigest() != metadata["source_sha256"]:
                        raise ValueError("Retained index changed")
                else:
                    response = client.get("list.json", **params)
                    raw = response.content
                    write_source_bytes(path, raw, source="OpenDART-issuer-membership-index")
                    metadata = dict(provider="DART", symbol=symbol, corp_code=corp, scope=label,
                        source_path=str(path), source_sha256=sha256(raw).hexdigest(), request=params,
                        source_url="https://opendart.fss.or.kr/api/list.json?" + urlencode(params),
                        retrieved_at=datetime.now(timezone.utc).isoformat(), http_status=response.status_code)
                    export_json(metadata_path, metadata)
                payload = json.loads(raw)
                report["sources"].append(metadata)
                if payload.get("status") == "013":
                    if page != 1:
                        raise ValueError("Unexpected end of DART pagination")
                    break
                if payload.get("status") != "000":
                    export_json(output / "issuer_index_collection.json", report)
                    raise ValueError("Retained DART response did not report successful collection")
                total = int(payload["total_count"])
                if expected is not None and expected != total:
                    raise ValueError("DART index changed while collecting pages")
                expected = total
                for row in payload.get("list", []):
                    if row["corp_code"] != corp:
                        raise ValueError("DART returned another issuer")
                    report["rows"].append(dict(**row, observed_symbol=symbol, query_scope=label,
                        index_source=str(path), index_sha256=metadata["source_sha256"]))
                    received += 1
                if page >= int(payload["total_page"]):
                    if received != expected:
                        raise ValueError("DART index row count is incomplete")
                    break
                page += 1
            export_json(output / "issuer_index_collection.json", report)
            print(dict(symbol=symbol, scope=label, rows=received), flush=True)
    report.update(status="issuer_indexes_downloaded_pending_review", unique_receipts=len({r["rcept_no"] for r in report["rows"]}))
    export_json(output / "issuer_index_collection.json", report)


if __name__ == "__main__":
    main()
