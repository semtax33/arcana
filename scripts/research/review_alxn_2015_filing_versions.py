"""Retain and reconcile ALXN's 2015 balance presentation across three filings.

This research command does not publish financial histories or change SEC source
authority. Full accession bundles and canonical period semantics remain separate
prerequisites for production normalization.
"""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import write_source_bytes
from engine.extractors._internal.sec_filings import DEFAULT_SEC_USER_AGENT

SCOPE = "us_alxn_2015_filing_versions_20260911"
BRONZE = DATA_LAKE.bronze("sec", "financial_history", SCOPE, "ALXN")
SILVER = DATA_LAKE.silver("survivorship", "financial_research", SCOPE)
FILINGS = (
    ("original", "0000899866-16-000226", "2016-02-08", "2015-12-31",
     "10-K", "alxn10k12312015.htm", "htm"),
    ("comparative", "0000899866-16-000290", "2016-04-29", "2016-03-31",
     "10-Q", "alxn3311610q.htm", "html"),
    ("amendment", "0000899866-17-000012", "2017-01-19", "2015-12-31",
     "10-K/A", "alxn10ka12312015.htm", "html"),
)


def pin(path):
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def retain(name, url):
    path = BRONZE / (name + Path(url).suffix)
    metadata_path = BRONZE / (name + ".metadata.json")
    if path.exists():
        raw = path.read_bytes()
        metadata = json.loads(metadata_path.read_bytes())
        if metadata["source_url"] != url or metadata["source_sha256"] != sha256(raw).hexdigest():
            raise ValueError("Retained SEC evidence changed")
    else:
        response = requests.get(url, headers={"User-Agent": DEFAULT_SEC_USER_AGENT,
            "Accept-Encoding": "identity"}, timeout=30)
        raw = response.content
        if raw:
            write_source_bytes(path, raw, source="SEC-ALXN-financial-version-review")
        metadata = {"provider": "SEC", "source_url": url, "source_path": str(path) if raw else None,
            "source_sha256": sha256(raw).hexdigest(), "http_status": response.status_code,
            "byte_count": len(raw), "retrieved_at": datetime.now(timezone.utc).isoformat()}
        export_json(metadata_path, metadata)
    if metadata["http_status"] != 200 or not raw:
        raise ValueError("Unavailable SEC response retained in Bronze")
    soup = BeautifulSoup(raw, "html.parser")
    for node in soup.find_all(["script", "style"]):
        node.decompose()
    return path, metadata_path, soup


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def main():
    SILVER.mkdir(parents=True, exist_ok=True)
    facts_path = DATA_LAKE.bronze("sec", "companyfacts", "CIK0000899866.json")
    facts_pin = pin(facts_path)
    facts = json.loads(facts_path.read_bytes())["facts"]["us-gaap"]
    expected = {
        "original": {"Assets": 13133230000, "LongTermDebtCurrent": 175000000,
            "LongTermDebtNoncurrent": 3281250000, "StockholdersEquity": 8258616000},
        "comparative": {"Assets": 13097881000, "LongTermDebtCurrent": 166365000,
            "LongTermDebtNoncurrent": 3254536000, "StockholdersEquity": 8258616000},
        "amendment": {"Assets": 13133230000, "LongTermDebtCurrent": 175000000,
            "LongTermDebtNoncurrent": 3281250000, "StockholdersEquity": 8258616000},
    }
    labels = {"Assets": "Total assets", "LongTermDebtCurrent": "Current portion of long-term debt",
        "LongTermDebtNoncurrent": "Long-term debt, less current portion",
        "StockholdersEquity": "Total stockholders' equity"}
    summary = {"status": "collecting", "symbol": "ALXN", "balance_date": "2015-12-31",
        "filings": {}, "companyfacts": facts_pin, "production_changed": False,
        "coverage_complete": False, "canonical_history_published": False}
    rows = []
    for name, accession, filed, period, form, document, index_suffix in FILINGS:
        base = f"https://www.sec.gov/Archives/edgar/data/899866/{accession.replace('-', '')}/"
        index_url = base + f"{accession}-index.{index_suffix}"
        index_path, index_metadata, index = retain(name + "_index", index_url)
        index_text = clean(index.get_text(" ", strip=True))
        for literal in (accession, filed, period, "ALEXION PHARMACEUTICALS"):
            if literal not in index_text:
                raise ValueError(f"Index identity/date evidence missing: {name}")
        filing_date_node = index.find(string=lambda value: value and value.strip() == "Filing Date")
        report_period_node = index.find(string=lambda value: value and value.strip() == "Period of Report")
        if not filing_date_node or not report_period_node:
            raise ValueError("Index date labels missing")
        observed_date = clean(filing_date_node.parent.find_next_sibling().get_text())
        observed_period = clean(report_period_node.parent.find_next_sibling().get_text())
        if (observed_date, observed_period) != (filed, period):
            raise ValueError("Actual SEC index date differs from expected")
        linked = [urljoin(index_url, a["href"]) for a in index.select("a[href]")
            if Path(a["href"]).name == document]
        if linked != [base + document]:
            raise ValueError("Primary filing is not unambiguously linked from its SEC index")
        path, metadata_path, soup = retain(name, linked[0])
        document_text = clean(soup.get_text(" ", strip=True))
        if "ALEXION PHARMACEUTICALS" not in document_text.upper():
            raise ValueError("Primary document identity missing")
        text_path = SILVER / (name + ".txt")
        text_path.write_text(soup.get_text("\n", strip=True), "utf-8")
        candidates = [table for table in soup.find_all("table")
            if all(label.lower() in clean(table.get_text(" ", strip=True)).lower()
                   for label in labels.values())]
        if len(candidates) != 1:
            raise ValueError(f"Balance sheet table ambiguous: {name} ({len(candidates)})")
        table = candidates[0]
        table_text = clean(table.get_text(" ", strip=True))
        unit_node = table.find_previous(string=re.compile("thousands", re.IGNORECASE))
        unit_text = clean(str(unit_node or ""))
        expected_header = ("March 31, December 31, 2016 2015 Assets" if name == "comparative"
                           else "December 31, 2015 2014 Assets")
        if not table_text.startswith(expected_header) or unit_text != "(amounts in thousands, except per share amounts)":
            raise ValueError("Expected balance unit/date columns missing")
        for tag, label in labels.items():
            selected = []
            for row in table.find_all("tr"):
                cells = [clean(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"], recursive=False)]
                if not cells or cells[0].lower() != label.lower():
                    continue
                numbers = [Decimal(cell.replace(",", "")) for cell in cells[1:]
                    if re.fullmatch(r"[\d,]+", cell)]
                selected.append((cells, numbers))
            if len(selected) != 1 or len(selected[0][1]) != 2:
                raise ValueError(f"Balance row/column ambiguity: {name}/{tag}")
            cells, numbers = selected[0]
            value = numbers[1 if name == "comparative" else 0] * 1000
            if value != expected[name][tag]:
                raise ValueError(f"HTML balance amount mismatch: {name}/{tag}")
            matches = [entry for entry in facts[tag]["units"]["USD"]
                if entry.get("accn") == accession and entry.get("end") == "2015-12-31"
                and not entry.get("start")]
            if not matches or {Decimal(str(entry["val"])) for entry in matches} != {value}:
                raise ValueError(f"CompanyFacts differs from primary balance: {name}/{tag}")
            if {entry.get("filed") for entry in matches} != {filed}:
                raise ValueError("CompanyFacts publication date differs from index")
            rows.append({"filing": name, "accession": accession, "filed": filed,
                "filing_period_end": period, "reported_period_end": "2015-12-31",
                "reported_period_semantic": "INSTANT", "tag": tag, "amount_usd": int(value),
                "reported_unit_heading": unit_text, "reported_column_header": expected_header,
                "source_row_cells": json.dumps(cells), "primary_document": str(path),
                "primary_document_sha256": pin(path)["sha256"]})
        assertions = []
        if name == "comparative":
            assertions = ["We have adopted the provisions of this standard in the first quarter 2016",
                "reclassified $8,635 of deferred financing costs", "$26,714 other non current assets"]
        elif name == "amendment":
            assertions = ["have not changed as a result of the material weakness",
                "does not purport to reflect any information or events subsequent to the filing date of the Original Filing"]
        if any(text not in document_text for text in assertions):
            raise ValueError(f"Expected source explanation missing: {name}")
        summary["filings"][name] = {"accession": accession, "filing_date": observed_date,
            "filing_period_end": observed_period, "form": form, "index": pin(index_path),
            "index_metadata": pin(index_metadata), "primary_document": pin(path),
            "primary_metadata": pin(metadata_path), "review_text": pin(text_path),
            "source_explanation_checked": bool(assertions)}
        export_json(SILVER / "collection.json", summary)
        print(json.dumps({"filing": name, "status": "primary_balance_and_filing_date_verified"}), flush=True)
    bridge = {
        "current_debt_reclassification_usd": 8635000,
        "noncurrent_debt_reclassification_usd": 26714000,
        "asset_reclassification_usd": 35349000,
        "equity_change_usd": 0,
    }
    if expected["original"]["LongTermDebtCurrent"] - expected["comparative"]["LongTermDebtCurrent"] != bridge["current_debt_reclassification_usd"]:
        raise ValueError("Current debt reclassification does not reconcile")
    if expected["original"]["LongTermDebtNoncurrent"] - expected["comparative"]["LongTermDebtNoncurrent"] != bridge["noncurrent_debt_reclassification_usd"]:
        raise ValueError("Noncurrent debt reclassification does not reconcile")
    if expected["original"]["Assets"] - expected["comparative"]["Assets"] != bridge["asset_reclassification_usd"]:
        raise ValueError("Asset reclassification does not reconcile")
    frame_path = SILVER / "balance_observations.parquet"
    pd.DataFrame(rows).to_parquet(frame_path, index=False)
    if pin(facts_path) != facts_pin:
        raise ValueError("CompanyFacts changed during review")
    metadata_path = DATA_LAKE.silver("sec", "us_report_metadata.csv")
    metadata = pd.read_csv(metadata_path, dtype=str).fillna("")
    metadata = metadata[(metadata.stock_code == "ALXN") & (metadata.fiscal_year == "2015")
        & (metadata.fiscal_month == "12") & (metadata.source_type == "statement")]
    metadata_rows = metadata.to_dict("records")
    if len(metadata_rows) != 1 or metadata_rows[0]["report_date"] != "2017-01-19":
        raise ValueError("Flattened period metadata changed; reassess the finding")
    normalized_path = DATA_LAKE.silver("sec", "normalized", "us_normalized_ALXN.csv")
    normalized = pd.read_csv(normalized_path, dtype=str).fillna("")
    normalized = normalized[(normalized.fiscal_year == "2015") & (normalized.fiscal_month == "12")]
    flattened_path = SILVER / "flattened_2015_period_observations.parquet"
    normalized.to_parquet(flattened_path, index=False)
    metadata_evidence = SILVER / "flattened_2015_period_metadata.json"
    export_json(metadata_evidence, {"source": pin(metadata_path), "rows": metadata_rows})
    summary.update(status="three_filing_balance_presentation_reconciled_history_pending",
        balance_observations=pin(frame_path), balance_observation_count=len(rows),
        flattened_normalized_source=pin(normalized_path),
        flattened_period_observations=pin(flattened_path),
        flattened_period_metadata=pin(metadata_evidence),
        reclassification=bridge,
        findings=[
            "The 2016 Q1 comparative balance applies the disclosed debt-issuance-cost presentation change to 2015-12-31.",
            "The 2017 10-K/A changes internal-control reporting and explicitly retains the original financial statements; it is not evidence that the 2016 presentation was reversed.",
            "The original 2015 balance was filed on 2016-02-08, before the flattened period metadata date of 2017-01-19.",
        ],
        limitations=[
            "Twelve instant balance observations only; not approval of all accounts, debt coverage, weighted shares, or all accession versions.",
            "Retained primary HTML/index documents are research evidence, not complete SEC Filing Bundles or a replacement for bundle authority.",
            "Comparative facts require filing-specific availability and presentation context; no retrospective adjustment has been published.",
        ], collector=pin(Path(__file__).resolve()))
    export_json(SILVER / "summary.json", summary)
    print(json.dumps({"status": summary["status"], "balance_observations": len(rows)}), flush=True)


if __name__ == "__main__":
    main()
