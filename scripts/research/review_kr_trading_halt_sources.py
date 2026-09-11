"""Pin dated halt facts independently of listing and settlement approval."""
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sys
from zipfile import ZipFile

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.transformers._internal.dart_document import _decode


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    base = DATA_LAKE.silver("survivorship", "financial_research")
    output = base / "kr_trading_halt_source_review_20260911"
    if (output / "source_review.json").exists():
        raise ValueError("Preserve the existing source review")
    old_path = base / "kr_remaining_missing_membership_20260911/source_review.json"
    prior = json.loads(old_path.read_bytes())
    documents = {row["receipt"]: row for row in prior["documents"]}
    supplements = json.loads((output / "supplementary_documents.json").read_bytes())
    documents.update({row["receipt"]: row for row in supplements["documents"]})
    inspected = {}

    def require(receipt, symbol, needles):
        record = documents[receipt]
        if record["symbol"] != symbol or digest(record["source_path"]) != record["source_sha256"]:
            raise ValueError("Original issuer document differs from its reviewed record")
        archive_path = Path(record["response_path"])
        if digest(archive_path) != record["response_sha256"]:
            raise ValueError("Retained original DART archive changed")
        raw = Path(record["source_path"]).read_bytes()
        with ZipFile(io.BytesIO(archive_path.read_bytes())) as archive:
            members = [name for name in archive.namelist() if Path(name).name == receipt+".xml"]
            if len(members) != 1 or archive.read(members[0]) != raw:
                raise ValueError("DART original member mismatch")
        index_path = Path(record.get("index_path") or record["index_source"])
        if digest(index_path) != record["index_sha256"]:
            raise ValueError("Retained DART index changed")
        entries = [row for row in json.loads(index_path.read_bytes())["list"] if row["rcept_no"] == receipt]
        if len(entries) != 1 or entries[0]["stock_code"] != symbol or entries[0]["corp_code"] != record["corp_code"]:
            raise ValueError("DART index issuer association mismatch")
        soup = BeautifulSoup(_decode(raw)[0], "lxml-xml")
        for tag in soup.find_all(lambda tag: tag.name.lower() in {"style", "script"}):
            tag.decompose()
        lines = soup.get_text("\n", strip=True).splitlines()
        text = re.sub(r"\s+", "", "".join(lines))
        if any(needle not in text for needle in needles):
            raise ValueError("Dated halt statement is missing from the selected original")
        day = entries[0]["rcept_dt"]
        inspected[receipt] = {**record, "published_date": f"{day[:4]}-{day[4:6]}-{day[6:]}",
            "evidence": [{"line": i+1, "text": line} for i, line in enumerate(lines)
                         if any(needle in re.sub(r"\s+", "", line) for needle in needles)]}
        return inspected[receipt]["published_date"]

    definitions = [
        ("204210", "2025-02-12", "2026-06-02", [("20260814003527", ["2025년2월12일", "2026년6월2일"])], "confirmed_after_event"),
        ("230980", "2024-03-21", "2026-05-26", [("20260331004396", ["2024년03월21일09:12:00"]),
                                                ("20260521900408", ["해제일시", "2026-05-26"])], "exchange_release_instruction"),
        ("464440", "2026-06-08", "2026-06-09", [("20260605900930", ["정지일시", "2026-06-08", "2026.06.09"])], "exchange_halt_and_release_instruction"),
        ("464680", "2026-05-27", "2026-05-28", [("20260526900858", ["정지일시", "2026-05-27", "2026.05.28"])], "exchange_halt_and_release_instruction"),
    ]
    intervals = []
    for symbol, start, end, refs, source_status in definitions:
        published = [require(receipt, symbol, needles) for receipt, needles in refs]
        intervals.append(dict(halt_id=f"kr-{symbol}-{start}", security_id="SEC_KR_"+symbol,
            start_date=start, end_date=end, published_date=max(published), source_ids=[row[0] for row in refs],
            source_statement_status=source_status, production_registered=False))
    require("20260323000749", "140910", ["2024.02.02.", "매매거래가정지"])
    require("20240202800801", "140910", ["2024.02.02", "매매거래가정지됨"])
    report = dict(status="four_dated_halt_intervals_reviewed_registration_pending", as_of="2026-09-10",
        intervals=intervals, unresolved=[dict(security_id="SEC_KR_140910", first_non_executable_close_date=None,
            announced_date="2024-02-02", reason="DART establishes the halt after the Feb 2 disclosure, but the selected original does not resolve its time relative to the closing session.",
            source_ids=["20260323000749", "20240202800801"])], sources=list(inspected.values()),
        previous_membership_review=str(old_path), previous_membership_review_sha256=digest(old_path),
        policy="Intervals use inclusive first blocked closing session and exclusive first resumed closing session. Intraday trades before a halt are not modeled as executable closing orders. Listing lifetime, issuer financial continuity and terminal settlement remain separate reviews.",
        production_changed=False, registered_halts=0, coverage_complete=False)
    implementation = output / "implementation"
    implementation.mkdir(exist_ok=True)
    shutil.copy2(__file__, implementation / Path(__file__).name)
    report["implementation_sha256"] = digest(__file__)
    artifact = export_json(output / "source_review.json", report)
    export_json(DATA_LAKE.gold("survivorship", "kr", "trading_halt_reviews", "20260911", "summary.json"),
                {**report, "silver_review": artifact})
    print(dict(status=report["status"], intervals=len(intervals), unresolved=1, source_documents=len(inspected)))


if __name__ == "__main__":
    main()
