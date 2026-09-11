"""Pin reviewed DART facts for five missing identities without registering them."""
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import shutil
import sys
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json


def main():
    output = DATA_LAKE.silver("survivorship", "financial_research", "kr_remaining_missing_membership_20260911")
    report_path = output / "source_review.json"
    if report_path.exists():
        raise ValueError("Keep the previous source review immutable")
    documents, inventories = {}, {}
    for name in ["document_inventory.json", "new_document_downloads.json", "ipo_liquidation_document_downloads.json",
                 "post_delisting_document_downloads.json"]:
        path = output / name
        inventory = json.loads(path.read_bytes())
        inventories[str(path)] = sha256(path.read_bytes()).hexdigest()
        for record in inventory["documents"]:
            if record["status"] == "needs_original_download":
                continue
            source = Path(record.get("source_path") or record["document_path"])
            original = source.read_bytes()
            digest = record.get("source_sha256") or record["document_sha256"]
            archive_path = Path(record.get("response_path") or record["archive_path"])
            archive_bytes = archive_path.read_bytes()
            archive_digest = record.get("response_sha256") or record["archive_sha256"]
            if sha256(original).hexdigest() != digest or sha256(archive_bytes).hexdigest() != archive_digest:
                raise ValueError("A retained original changed")
            with ZipFile(io.BytesIO(archive_bytes)) as archive:
                names = [n for n in archive.namelist() if Path(n).name == record["receipt"] + ".xml"]
                if len(names) != 1 or archive.read(names[0]) != original:
                    raise ValueError("Extracted original differs from its retained DART response")
            soup = BeautifulSoup(original, "lxml-xml")
            for tag in soup.find_all(lambda tag: tag.name.lower() in {"style", "script"}):
                tag.decompose()
            lines = soup.get_text("\n", strip=True).splitlines()
            date_text = re.sub(r"\s+", "", "".join(lines))
            index_source = Path(record["index_source"])
            index = json.loads(index_source.read_bytes())
            entries = [r for r in index["list"] if r["rcept_no"] == record["receipt"]]
            if len(entries) != 1 or entries[0]["stock_code"] != record["symbol"]:
                raise ValueError("Original DART index does not establish the document identity")
            day = entries[0]["rcept_dt"]
            documents[record["receipt"]] = dict(receipt=record["receipt"], symbol=record["symbol"],
                corp_code=entries[0]["corp_code"], title=entries[0]["report_nm"],
                published_date=f"{day[:4]}-{day[4:6]}-{day[6:]}", source_path=str(source), source_sha256=digest,
                response_path=str(archive_path), response_sha256=archive_digest,
                source_url=record["source_url"], index_path=str(index_source),
                index_sha256=sha256(index_source.read_bytes()).hexdigest(), lines=lines, date_text=date_text)

    cases = {
        "140910": dict(corp_code="00860730", ipo=("2011-07-14", "20260323000749", "2011년07월14일"),
            delisting=("2026-06-10", "20260814000770", "2026년6월10일", "confirmed_after_event"),
            final_trading=["2026-05-29", "2026-06-09"], final_trading_receipt="20260522800677",
            remaining_right="private_equity_not_valued", missing_cells=21),
        "204210": dict(corp_code="01035289", ipo=("2016-09-22", "20260323001430", "2016년09월22일"),
            delisting=("2026-06-12", "20260814003527", "2026년6월12일", "confirmed_after_event"),
            final_trading=["2026-06-02", "2026-06-11"], final_trading_receipt="20260527800223",
            remaining_right="private_equity_not_valued", missing_cells=3),
        "230980": dict(corp_code="01110076", ipo=("2016-03-02", "20260331004396", "2016년03월02일"),
            delisting=("2026-06-05", "20260521900408", "2026.06.05", "announced_schedule"),
            final_trading=["2026-05-26", "2026-06-04"], final_trading_receipt="20260521900408",
            remaining_right="private_equity_not_valued", missing_cells=21,
            special_review="The listed SPAC and the operating issuer merged in 2016. The report's listing date does not authorize joining their earlier financial histories. The August report retains stale listed-company front matter; obtain post-event removal corroboration."),
        "464440": dict(corp_code="01775952", ipo=("2023-11-13", "20260311003586", "2023년11월13일"),
            delisting=("2026-06-18", "20260618000252", "2026년6월18일", "confirmed_on_event_date"),
            final_trading=["2026-06-09", "2026-06-17"], final_trading_receipt="20260605900930",
            remaining_right="unsettled_liquidation_claim", missing_cells=6,
            distribution=("2026-09-30", "20260722000020", "2026년9월30일")),
        "464680": dict(corp_code="01785551", ipo=("2023-11-03", "20260318001684", "2023년11월03일"),
            delisting=("2026-06-09", "20260609000084", "2026년6월9일", "confirmed_on_event_date"),
            final_trading=["2026-05-28", "2026-06-08"], final_trading_receipt="20260526900858",
            remaining_right="unsettled_liquidation_claim", missing_cells=18,
            distribution=("2026-09-28", "20260907000070", "2026년9월28일")),
    }
    facts = []
    for symbol, case in cases.items():
        references = {case["final_trading_receipt"]}
        for name in ["ipo", "delisting", "distribution"]:
            if name not in case:
                continue
            day, receipt, needle, *status = case.pop(name)
            document = documents[receipt]
            if document["symbol"] != symbol or document["corp_code"] != case["corp_code"] or needle not in document["date_text"]:
                raise ValueError(f"Reviewed date lacks the selected original issuer evidence: {symbol}/{name}")
            evidence_lines = [{"line": i+1, "text": line} for i, line in enumerate(document["lines"])
                if needle in re.sub(r"\s+", "", line)]
            case[name] = dict(date=day, receipt=receipt, published_date=document["published_date"],
                status=(status[0] if status else ("planned_unpaid" if name == "distribution" else "reported_historical_listing_date")),
                evidence_lines=evidence_lines)
            references.add(receipt)
        case.update(symbol=symbol, security_id="SEC_KR_"+symbol, registered=False,
            confirmed_cash_per_share=None, actual_payment_date=None,
            unresolved=["Historical trading halts and resumption intervals must be retained separately from administrative listing dates.",
                        "Publication dates above must not be backdated; financial issuer continuity requires separate evidence.",
                        "Price corrections alone do not restore this security to the dated universe."],
            sources=sorted(references))
        if "distribution" in case:
            case["unresolved"].append("Escrow proceeds exclude pre-IPO holders and other assets follow charter allocations. A future planned date and an EPS figure do not establish a paid liquidation amount.")
        facts.append(case)
    for doc in documents.values():
        doc.pop("date_text")
        doc.pop("lines")
    implementation = output / "implementation"
    implementation.mkdir(exist_ok=True)
    code = {}
    for file in [__file__, "scripts/research/collect_kr_missing_membership_documents.py", "scripts/research/collect_kr_missing_membership_indexes.py"]:
        path = Path(file).resolve()
        destination = implementation / path.name
        shutil.copy2(path, destination)
        code[str(path)] = dict(path=str(destination), sha256=sha256(destination.read_bytes()).hexdigest())
    report = dict(status="source_facts_reviewed_registration_pending", as_of="2026-09-10",
        cases=facts, documents=list(documents.values()), input_inventories=inventories, implementation=code,
        missing_factorlab_cells=69, new_listing_episodes_registered=0, production_changed=False, coverage_complete=False)
    export_json(report_path, report)
    export_json(DATA_LAKE.gold("survivorship", "kr", "membership_gaps", "20260911", "summary.json"),
        dict(status=report["status"], as_of=report["as_of"], cases=facts, missing_factorlab_cells=69,
            new_listing_episodes_registered=0, coverage_complete=False,
            review_path=str(report_path), review_sha256=sha256(report_path.read_bytes()).hexdigest()))
    print(dict(status=report["status"], issuers=len(facts), original_documents=len(documents)), flush=True)


if __name__ == "__main__":
    main()
