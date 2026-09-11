"""Prepare complete registry proposals from verified DART listing facts.

This writes only review and validation artifacts. It never replaces the default
registry, publishes a ClickHouse generation, or approves financial histories.
"""
import argparse
from copy import deepcopy
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
from engine.workflows.survivorship import run_survivorship_refresh


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compact(text):
    return re.sub(r"\s+", "", text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gold-output", type=Path, required=True)
    args = parser.parse_args()
    output, gold = args.output.resolve(), args.gold_output.resolve()
    if not output.is_relative_to((DATA_LAKE.root / "silver").resolve()) or not gold.is_relative_to((DATA_LAKE.root / "gold").resolve()):
        raise ValueError("Review artifacts belong in Silver and user-facing summaries in Gold")
    output.mkdir(parents=True, exist_ok=False)
    base = DATA_LAKE.silver("survivorship", "financial_research")
    review_path = base / "kr_remaining_missing_membership_20260911/source_review.json"
    halt_path = base / "kr_trading_halt_source_review_20260911/source_review.json"
    registry_path = DATA_LAKE.meta("survivorship", "kr_reviewed.json")
    pinned = {str(p.resolve()): digest(p) for p in (review_path, halt_path, registry_path, Path(__file__))}
    review, halt_review, current = [json.loads(p.read_bytes()) for p in (review_path, halt_path, registry_path)]
    if halt_review["previous_membership_review_sha256"] != digest(review_path):
        raise ValueError("Halt and listing source reviews are from different generations")
    documents = {row["receipt"]: row for row in review["documents"]}
    for row in halt_review["sources"]:
        if row["receipt"] in documents and row["source_sha256"] != documents[row["receipt"]]["source_sha256"]:
            raise ValueError("Conflicting originals in the two source reviews")
        documents.setdefault(row["receipt"], row)
    inspected, texts = {}, {}

    def require(receipt, symbol, corp_code):
        if receipt in inspected:
            return inspected[receipt]
        record = documents[receipt]
        if (record["symbol"], record["corp_code"]) != (symbol, corp_code):
            raise ValueError("Source review issuer mismatch")
        for field in ("source", "response", "index"):
            path = Path(record[field + "_path"]).resolve()
            if not path.is_relative_to((DATA_LAKE.root / "bronze").resolve()):
                raise ValueError("Original source evidence must be in Bronze")
            expected = record[field + "_sha256"]
            if digest(path) != expected:
                raise ValueError("Original bytes no longer match the source review")
            pinned[str(path)] = expected
        raw = Path(record["source_path"]).read_bytes()
        with ZipFile(io.BytesIO(Path(record["response_path"]).read_bytes())) as archive:
            names = [name for name in archive.namelist() if Path(name).name == receipt + ".xml"]
            if len(names) != 1 or archive.read(names[0]) != raw:
                raise ValueError("DART document does not match its original archive member")
        index = json.loads(Path(record["index_path"]).read_bytes())
        rows = [row for row in index["list"] if row["rcept_no"] == receipt]
        if len(rows) != 1 or (rows[0]["stock_code"], rows[0]["corp_code"]) != (symbol, corp_code):
            raise ValueError("DART index does not confirm the same security and corporation")
        day = rows[0]["rcept_dt"]
        published = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        if published != record["published_date"] or published > review["as_of"]:
            raise ValueError("Original publication date differs or exceeds the review cutoff")
        soup = BeautifulSoup(_decode(raw)[0], "lxml-xml")
        header = soup.find("COMPANY-NAME")
        if header is not None and header.get("AREGCIK") and header["AREGCIK"] != corp_code:
            raise ValueError("Original document names a different corporate identity")
        texts[receipt] = soup.get_text("\n", strip=True)
        inspected[receipt] = dict(record, company_header=str(header), published_date=published)
        return inspected[receipt]

    # The other two reviewed cases remain in this proposal's unresolved list.
    # No inferred halt time or unconfirmed administrative removal is registered.
    definitions = {
        "204210": ("KOSPI", "유가증권시장상장2016년09월22일", "보통주발행주식총수7,826,815"),
        "464440": ("KOSDAQ", "코스닥시장상장2023년11월13일", "보통주발행주식총수4,320,000"),
        "464680": ("KOSDAQ", "코스닥시장상장2023년11월03일", "보통주발행주식총수12,905,000"),
    }
    proposal = deepcopy(current)
    proposal["source_root"] = str((registry_path.parent / current.get("source_root", ".")).resolve())
    proposal.setdefault("trading_halts", [])
    proposal["coverage_complete"] = False
    additions, deferred = [], []
    for case in review["cases"]:
        symbol, sid = case["symbol"], case["security_id"]
        if symbol not in definitions:
            reason = ("First blocked closing session remains unresolved for the February 2024 halt."
                      if symbol == "140910" else case["special_review"])
            item = dict(security_id=sid, reason=reason, status="registration_deferred", source_ids=case["sources"])
            deferred.append(item)
            proposal["unresolved"].append(item)
            continue
        if any(row["security_id"] == sid for row in current["listing_episodes"]):
            raise ValueError("The base registry already contains a proposed security")
        if case["delisting"]["status"] not in {"confirmed_after_event", "confirmed_on_event_date"}:
            raise ValueError("An announced removal schedule is not an executed removal")
        exchange, ipo_statement, share_statement = definitions[symbol]
        halt = next(row for row in halt_review["intervals"] if row["security_id"] == sid)
        refs = sorted(set(case["sources"] + halt["source_ids"]))
        for receipt in refs:
            require(receipt, symbol, case["corp_code"])
        annual = compact(texts[case["ipo"]["receipt"]])
        if ipo_statement not in annual or share_statement not in annual:
            raise ValueError("Original IPO venue or common-share capital statement is missing")
        for event in (case["ipo"], case["delisting"]):
            original = compact(texts[event["receipt"]])
            if any(compact(line["text"]) not in original for line in event["evidence_lines"]):
                raise ValueError("Reviewed dated statement is not present in the original")
        source_id = lambda receipt: f"dart-{symbol}-{receipt}"
        listing_refs = [source_id(case[key]["receipt"]) for key in ("ipo", "delisting")]
        episode = dict(episode_id=f"{symbol}-listed-{case['ipo']['date']}", security_id=sid,
            issuer_id="ISSUER_ID_" + symbol, corp_code=case["corp_code"], symbol=symbol,
            country="KR", exchange_code=exchange, security_type="common_stock", status="confirmed",
            valid_from=case["ipo"]["date"], valid_until=case["delisting"]["date"],
            published_date=max(case[key]["published_date"] for key in ("ipo", "delisting")),
            source_ids=listing_refs)
        event = dict(event_id=f"{symbol}-delisting-{case['delisting']['date']}", security_id=sid,
            event_type="delisting", effective_date=case["delisting"]["date"],
            status="confirmed", currency="KRW", cash_per_share=None, cash_payment_date=None,
            entitlements_complete=False, published_date=case["delisting"]["published_date"],
            source_ids=[source_id(case["delisting"]["receipt"])], reason=case["remaining_right"])
        proposed_halt = {key: halt[key] for key in ("halt_id", "security_id", "start_date", "end_date", "published_date")}
        proposed_halt.update(status="confirmed", source_ids=[source_id(ref) for ref in halt["source_ids"]])
        proposal["listing_episodes"].append(episode)
        proposal["events"].append(event)
        proposal["trading_halts"].append(proposed_halt)
        proposal["unresolved"].append(dict(security_id=sid, event_id=event["event_id"],
            reason=case["remaining_right"], cash_per_share=None, actual_payment_date=None,
            distribution_plan=case.get("distribution"), financial_history_approved=False))
        for receipt in refs:
            record = inspected[receipt]
            proposal["sources"].append(dict(source_id=source_id(receipt), provider="DART",
                published_date=record["published_date"], path=record["source_path"],
                source_url=record["source_url"], source_sha256=record["source_sha256"]))
        additions.append(dict(episode=episode, event=event, halt=proposed_halt,
            common_share_evidence=dict(receipt=case["ipo"]["receipt"], ipo_statement=ipo_statement,
                common_share_statement=share_statement, header=inspected[case["ipo"]["receipt"]]["company_header"])))
    if len({row["source_id"] for row in proposal["sources"]}) != len(proposal["sources"]):
        raise ValueError("Proposed source identifiers collide with the existing registry")
    proposal_file = output / "merged_review_proposal.json"
    export_json(proposal_file, proposal)
    # Exercise the public transformation/export contract with all prior identity
    # rows preserved. Price restoration is a separate pending verification.
    validation = deepcopy(proposal)
    for name in ("price_sources", "share_sources", "market_data_sources"):
        validation[name] = []
    validation_file = output / "identity_validation_input.json"
    export_json(validation_file, validation)
    result = run_survivorship_refresh(market="kr", end_date=review["as_of"], manifest_path=validation_file,
        output_dir=output / "canonical", gold_dir=gold / "identity_preview", download=False, load_clickhouse=False)
    for path, expected in pinned.items():
        if digest(path) != expected:
            raise ValueError("A source or default registry changed during preparation")
    implementation = output / "implementation" / Path(__file__).name
    implementation.parent.mkdir()
    shutil.copyfile(__file__, implementation)
    report = dict(status="complete_registry_proposal_identity_contract_verified_not_published",
        as_of=review["as_of"], additions=additions, deferred=deferred, original_documents=list(inspected.values()),
        base_registry=str(registry_path), base_registry_sha256=pinned[str(registry_path.resolve())],
        proposal=str(proposal_file), proposal_sha256=digest(proposal_file), pinned_inputs=pinned,
        prior_listing_count=len(current["listing_episodes"]), proposed_listing_count=len(proposal["listing_episodes"]),
        canonical_refresh=result, production_changed=False, prices_verified_for_publication=False,
        financial_histories_approved=False, terminal_rights_complete=False, coverage_complete=False,
        publication_requirements=["Finish active native and snapshot audits before changing the dated universe.",
            "Revalidate the base registry hash and all current sources; preserve every existing market input.",
            "Verify each added security's price, split, shares, halt and dated consumer boundaries in isolation.",
            "Use the complete merged proposal; the identity-only validation input is not a production price manifest."],
        implementation_sha256=digest(implementation))
    export_json(output / "summary.json", report)
    export_json(gold / "summary.json", {**report, "silver_summary": str(output / "summary.json"),
        "silver_summary_sha256": digest(output / "summary.json")})
    print(dict(status=report["status"], added_listings=len(additions), deferred=len(deferred),
        total_listings=report["proposed_listing_count"], documents=len(inspected), production_changed=False), flush=True)


if __name__ == "__main__":
    main()
