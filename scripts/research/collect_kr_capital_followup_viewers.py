"""Retain DART public originals for capital-history documents unavailable in OpenDART."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from urllib.parse import urlencode

from bs4 import BeautifulSoup
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import write_source_bytes
from engine.extractors.stock_splits import dart_family, dart_viewer_parameters


def main():
    bronze = DATA_LAKE.bronze("dart", "corporate_actions", "proposed_listings_20260911", "public_viewers", "204210")
    silver = DATA_LAKE.silver("survivorship", "financial_research", "kr_capital_followup_viewers_20260911")
    report = {"status": "collecting", "documents": [], "production_changed": False}
    index_path = DATA_LAKE.silver("survivorship", "financial_research", "kr_proposed_listing_action_indexes_20260911", "all_filings.parquet")
    frame = pd.read_parquet(index_path)
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Arcana Research",
        "Referer": "https://dart.fss.or.kr/", "Accept-Language": "ko-KR,ko;q=0.9"})
    try:
        for receipt in ("20160819000146", "20170518000045"):
            selected = frame.loc[frame.rcept_no.eq(receipt) & frame.observed_symbol.eq("204210")]
            if len(selected) != 1:
                raise ValueError("Receipt lacks a unique retained issuer index")
            row = selected.iloc[0].to_dict()
            original_index = Path(row["index_source"])
            if sha256(original_index.read_bytes()).hexdigest() != row["index_sha256"]:
                raise ValueError("Original issuer index changed")
            record = {"receipt": receipt, "index_record": row, "sources": []}
            report["documents"].append(record)
            main_url = "https://dart.fss.or.kr/dsaf001/main.do?" + urlencode({"rcpNo": receipt})
            for label in ("main", "viewer"):
                url = main_url if label == "main" else "https://dart.fss.or.kr/report/viewer.do?" + urlencode(params)
                path = bronze / (receipt + "." + label + ".html")
                meta_path = path.with_suffix(path.suffix + ".metadata.json")
                if path.exists():
                    raw = path.read_bytes()
                    meta = json.loads(meta_path.read_bytes())
                    if meta["source_sha256"] != sha256(raw).hexdigest() or meta["source_url"] != url:
                        raise ValueError("Cached original changed")
                else:
                    response = session.get(url, timeout=25)
                    raw = response.content
                    write_source_bytes(path, raw, source="DART-proposed-listing-capital-public-original")
                    meta = {"source_url": url, "source_path": str(path), "source_sha256": sha256(raw).hexdigest(),
                        "http_status": response.status_code, "retrieved_at": datetime.now(timezone.utc).isoformat()}
                    export_json(meta_path, meta)
                record["sources"].append(meta)
                export_json(silver / "summary.json", report)
                if meta["http_status"] != 200:
                    raise ValueError("DART response unavailable; original retained")
                if label == "main":
                    record["family"] = dart_family(raw)
                    params = dart_viewer_parameters(raw, receipt)
                else:
                    soup = BeautifulSoup(raw, "lxml")
                    for node in soup.find_all(["style", "script"]):
                        node.decompose()
                    text_path = silver / (receipt + ".txt")
                    text_path.write_text(soup.get_text("\n", strip=True), "utf-8")
                    record["review_text_path"] = str(text_path)
                    record["review_text_sha256"] = sha256(text_path.read_bytes()).hexdigest()
            print(json.dumps({"receipt": receipt, "status": "public_original_retained_pending_review"}), flush=True)
        report["status"] = "public_originals_retained_pending_review"
    except BaseException as error:
        report.update(status="collection_failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        silver.mkdir(parents=True, exist_ok=True)
        Path(silver / Path(__file__).name).write_bytes(Path(__file__).read_bytes())
        export_json(silver / "summary.json", report)


if __name__ == "__main__":
    main()
