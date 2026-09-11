"""Preserve annual capital histories and action notices for three proposed listings."""
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
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import write_source_bytes
from engine.extractors.opendart_stock_splits import OpenDartClient
from engine.transformers._internal.dart_document import _decode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", nargs="+")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    indexes = DATA_LAKE.silver("survivorship", "financial_research", "kr_proposed_listing_action_indexes_20260911")
    silver = args.output or DATA_LAKE.silver("survivorship", "financial_research", "kr_proposed_listing_capital_documents_20260911")
    if not silver.resolve().is_relative_to(DATA_LAKE.silver("survivorship", "financial_research").resolve()):
        raise ValueError("Review output must remain in Silver")
    bronze = DATA_LAKE.bronze("dart", "corporate_actions", "proposed_listings_20260911", "documents")
    summary_path = silver / "summary.json"
    if summary_path.exists():
        raise ValueError("Preserve the previous collection report")
    index_summary = json.loads((indexes / "summary.json").read_bytes())
    path = Path(index_summary["artifacts"]["all_filings"]["path"])
    if sha256(path.read_bytes()).hexdigest() != index_summary["artifacts"]["all_filings"]["sha256"]:
        raise ValueError("Reviewed index inventory changed")
    frame = pd.read_parquet(path)
    is_annual = frame.report_nm.str.match(r"^(?:\[[^\]]+\])?사업보고서\s*\(")
    is_latest_period = frame.rcept_dt.ge("20260101") & frame.report_nm.str.match(r"^(?:\[[^\]]+\])?(?:분기|반기)보고서\s*\(")
    is_action = frame.report_nm.str.contains(r"분할|병합|합병|감자|자본감소|액면|해산|청산|주식교환|주식이전")
    selected = frame.loc[is_annual | is_latest_period | is_action].sort_values(["observed_symbol", "rcept_dt", "rcept_no"])
    if args.receipts:
        selected = frame.loc[frame.rcept_no.isin(args.receipts)].sort_values(["observed_symbol", "rcept_dt", "rcept_no"])
        if set(selected.rcept_no) != set(args.receipts):
            raise ValueError("Requested receipts must all occur in the retained issuer index")
    report = {"status": "collecting", "as_of": "2026-09-10", "documents": [],
        "selected_receipts": selected.rcept_no.tolist(), "index_summary_path": str(indexes / "summary.json"),
        "index_summary_sha256": sha256((indexes / "summary.json").read_bytes()).hexdigest(),
        "production_changed": False, "coverage_complete": False,
        "selection": "All original and corrected annual reports, all 2026 quarterly/half-year reports, and every action-title candidate. Annual filing deadline extensions remain indexed but are not financial reports."}
    if args.receipts:
        report["selection"] = "Explicit source-review follow-up receipts, each verified against the retained full issuer index."
    client = OpenDartClient()
    try:
        for row in selected.to_dict("records"):
            symbol, receipt = row["observed_symbol"], row["rcept_no"]
            if not re.fullmatch(r"\d{6}", symbol) or not re.fullmatch(r"\d{14}", receipt):
                raise ValueError("Invalid selected identity")
            original_index = Path(row["index_source"])
            if sha256(original_index.read_bytes()).hexdigest() != row["index_sha256"]:
                raise ValueError("Retained index original changed")
            original_rows = [r for r in json.loads(original_index.read_bytes())["list"] if r["rcept_no"] == receipt]
            if len(original_rows) != 1 or any(row[key] != value for key, value in original_rows[0].items()):
                raise ValueError("Selected receipt differs from its original issuer index")
            old_response = DATA_LAKE.bronze("dart", "listings", "issuer_review_20260911", "missing_membership", symbol, receipt + ".response")
            response_path = bronze / symbol / (receipt + ".response")
            metadata_path = response_path.with_suffix(".metadata.json")
            if old_response.exists():
                metadata = json.loads(old_response.with_suffix(".metadata.json").read_bytes())
                response_path = old_response
                raw = response_path.read_bytes()
                if metadata["response_sha256"] != sha256(raw).hexdigest():
                    raise ValueError("Previously retained source response changed")
            elif response_path.exists():
                raw = response_path.read_bytes()
                metadata = json.loads(metadata_path.read_bytes())
                if metadata["response_sha256"] != sha256(raw).hexdigest():
                    raise ValueError("Cached source response changed")
            else:
                response = client.get("document.xml", rcept_no=receipt)
                raw = response.content
                write_source_bytes(response_path, raw, source="OpenDART-proposed-listing-capital-document")
                metadata = {"provider": "DART", "symbol": symbol, "receipt": receipt,
                    "source_url": f"https://opendart.fss.or.kr/api/document.xml?rcept_no={receipt}",
                    "response_path": str(response_path), "response_sha256": sha256(raw).hexdigest(),
                    "http_status": response.status_code, "retrieved_at": datetime.now(timezone.utc).isoformat()}
                export_json(metadata_path, metadata)
            record = {**row, "response_path": str(response_path), "response_sha256": sha256(raw).hexdigest(),
                "http_status": metadata["http_status"], "source_url": metadata["source_url"],
                "retrieved_at": metadata["retrieved_at"], "status": "response_retained"}
            report["documents"].append(record)
            export_json(summary_path, report)
            if record["http_status"] != 200 or not raw.startswith(b"PK"):
                raise ValueError("DART document unavailable; response retained")
            with ZipFile(io.BytesIO(raw)) as archive:
                members = [name for name in archive.namelist() if Path(name).name == receipt + ".xml"]
                if len(members) != 1:
                    raise ValueError("Ambiguous original main document")
                xml = archive.read(members[0])
            old_xml = old_response.with_suffix(".xml")
            if response_path == old_response and old_xml.exists():
                if old_xml.read_bytes() != xml:
                    raise ValueError("Previously retained XML differs from archive member")
                source = old_xml
            else:
                source = bronze / symbol / (receipt + ".xml")
                if source.exists() and source.read_bytes() != xml:
                    raise ValueError("Retained main XML changed")
                if not source.exists():
                    write_source_bytes(source, xml, source="OpenDART-proposed-listing-original-archive-member")
            decoded, encoding = _decode(xml)
            soup = BeautifulSoup(decoded, "lxml-xml")
            name_node = soup.find("COMPANY-NAME")
            if name_node is not None and name_node.get("AREGCIK") and name_node["AREGCIK"] != row["corp_code"]:
                raise ValueError("Main original document has another issuer")
            text_path = silver / "extracted" / symbol / (receipt + ".txt")
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(soup.get_text("\n", strip=True), "utf-8")
            record.update(status="original_retained_pending_capital_review", source_path=str(source),
                source_sha256=sha256(xml).hexdigest(), archive_member=members[0], encoding=encoding,
                review_text_path=str(text_path), review_text_sha256=sha256(text_path.read_bytes()).hexdigest())
            export_json(summary_path, report)
            print(json.dumps({key: record[key] for key in ("observed_symbol", "rcept_no", "status")}), flush=True)
        if len(report["documents"]) != len(selected):
            raise ValueError("Not all selected originals were retained")
        report.update(status="all_selected_originals_retained_capital_review_pending", selected_count=len(selected))
    except BaseException as error:
        report.update(status="collection_failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        silver.mkdir(parents=True, exist_ok=True)
        Path(silver / Path(__file__).name).write_bytes(Path(__file__).read_bytes())
        export_json(summary_path, report)
    print(json.dumps({key: report[key] for key in ("status", "selected_count")}), flush=True)


if __name__ == "__main__":
    main()
