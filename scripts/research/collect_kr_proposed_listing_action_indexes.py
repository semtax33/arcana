"""Retain complete dated DART indexes for the three proposed listing histories."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlencode

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_frame, export_json
from engine.core.source_storage import write_source_bytes
from engine.extractors.opendart_stock_splits import OpenDartClient


def main():
    bronze = DATA_LAKE.bronze("dart", "corporate_actions", "proposed_listings_20260911", "indexes")
    silver = DATA_LAKE.silver("survivorship", "financial_research", "kr_proposed_listing_action_indexes_20260911")
    summary_path = silver / "summary.json"
    if summary_path.exists():
        raise ValueError("Preserve the prior collection report; cached originals remain reusable")
    proposal_path = DATA_LAKE.silver("survivorship", "financial_research",
        "kr_missing_listing_price_preparation_20260911", "merged_price_review_proposal.json")
    proposal = json.loads(proposal_path.read_bytes())
    client = OpenDartClient()
    report = {"status": "collecting", "as_of": "2026-09-10", "windows": [], "sources": [],
        "production_changed": False, "coverage_complete": False,
        "proposal_path": str(proposal_path), "proposal_sha256": sha256(proposal_path.read_bytes()).hexdigest()}
    all_rows = []
    try:
        for symbol, corp in {"204210": "01035289", "464440": "01775952", "464680": "01785551"}.items():
            episodes = [row for row in proposal["listing_episodes"] if row["security_id"] == "SEC_KR_" + symbol]
            if len(episodes) != 1:
                raise ValueError("Expected a single reviewed proposed listing episode")
            first_year = int(episodes[0]["valid_from"][:4])
            for year in range(first_year, 2027):
                start, end = f"{year}0101", f"{year}1231" if year < 2026 else "20260910"
                page, expected, seen = 1, None, set()
                while True:
                    params = {"corp_code": corp, "bgn_de": start, "end_de": end,
                        "last_reprt_at": "N", "page_count": 100, "page_no": page,
                        "sort": "date", "sort_mth": "asc"}
                    name = sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]
                    path = bronze / symbol / (name + ".json")
                    metadata_path = path.with_suffix(".metadata.json")
                    if path.exists():
                        raw = path.read_bytes()
                        metadata = json.loads(metadata_path.read_bytes())
                        if metadata["request"] != params or metadata["source_sha256"] != sha256(raw).hexdigest():
                            raise ValueError("Original index or request changed")
                    else:
                        response = client.get("list.json", **params)
                        raw = response.content
                        write_source_bytes(path, raw, source="OpenDART-proposed-listing-full-action-index")
                        metadata = {"provider": "DART", "symbol": symbol, "corp_code": corp,
                            "request": params, "source_url": "https://opendart.fss.or.kr/api/list.json?" + urlencode(params),
                            "source_path": str(path), "source_sha256": sha256(raw).hexdigest(),
                            "http_status": response.status_code, "retrieved_at": datetime.now(timezone.utc).isoformat()}
                        export_json(metadata_path, metadata)
                    report["sources"].append(metadata)
                    export_json(summary_path, report)
                    payload = json.loads(raw)
                    if metadata["http_status"] != 200 or payload.get("status") not in {"000", "013"}:
                        raise ValueError("DART index response unavailable; original retained")
                    if payload["status"] == "013":
                        if page != 1:
                            raise ValueError("Unexpected empty page in original index")
                        expected = 0
                        break
                    count, pages = int(payload["total_count"]), int(payload["total_page"])
                    if expected is not None and expected != count:
                        raise ValueError("Issuer index changed across pages")
                    if pages != (count + 99) // 100 or int(payload["page_no"]) != page:
                        raise ValueError("Inconsistent original pagination")
                    expected = count
                    for row in payload["list"]:
                        if row["corp_code"] != corp or row.get("stock_code", "") not in {"", symbol}:
                            raise ValueError("Another issuer appeared in a scoped index")
                        if row["rcept_no"] in seen or not start <= row["rcept_dt"] <= end:
                            raise ValueError("Duplicate or out-of-window filing")
                        seen.add(row["rcept_no"])
                        all_rows.append({**row, "observed_symbol": symbol, "index_source": str(path),
                            "index_sha256": metadata["source_sha256"]})
                    if page == pages:
                        break
                    page += 1
                if len(seen) != expected:
                    raise ValueError("Incomplete issuer index")
                report["windows"].append({"symbol": symbol, "corp_code": corp, "start": start, "end": end,
                    "pages": page, "rows": len(seen), "status": "all_reported_pages_retained"})
                export_json(summary_path, report)
                print(json.dumps(report["windows"][-1]), flush=True)
        frame = pd.DataFrame(all_rows)
        if frame.duplicated(["observed_symbol", "rcept_no"]).any():
            raise ValueError("Duplicate receipt across yearly index windows")
        compact = frame.report_nm.str.replace(r"\s+", "", regex=True)
        actions = frame.loc[compact.str.contains(r"분할|병합|합병|감자|자본감소|액면|해산|청산|주식교환|주식이전", regex=True)]
        annual = frame.loc[compact.str.contains("사업보고서", regex=False)]
        report["artifacts"] = {
            "all_filings": export_frame(silver / "all_filings.parquet", frame),
            "action_title_candidates": export_frame(silver / "action_title_candidates.parquet", actions),
            "annual_reports": export_frame(silver / "annual_reports.parquet", annual),
        }
        for source in report["sources"]:
            if sha256(Path(source["source_path"]).read_bytes()).hexdigest() != source["source_sha256"]:
                raise ValueError("Original index changed during collection")
        if sha256(proposal_path.read_bytes()).hexdigest() != report["proposal_sha256"]:
            raise ValueError("Proposed listing scope changed during collection")
        report.update(status="full_issuer_indexes_retained_content_review_pending", rows=len(frame),
            action_title_candidates=len(actions), annual_reports=len(annual),
            limitation="Every reported page is retained from January of each IPO year to the fixed cutoff. Title matches are candidates; absence of a matching title is not approval that no corporate action occurred. Annual capital histories and event outcomes require original-document review.")
    except BaseException as error:
        report.update(status="collection_failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        silver.mkdir(parents=True, exist_ok=True)
        Path(silver / Path(__file__).name).write_bytes(Path(__file__).read_bytes())
        export_json(summary_path, report)
    print(json.dumps({key: report[key] for key in ("status", "rows", "action_title_candidates", "annual_reports")}), flush=True)


if __name__ == "__main__":
    main()
