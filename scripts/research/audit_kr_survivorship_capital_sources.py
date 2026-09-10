"""Review additional operating-capital facts against retained DART statements.

Pure trade receivables/payables may extend an existing reviewed receipt.
Borrowing components are inventoried separately; this script never sums or
publishes them without a reviewed definition of the complete, disjoint set.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from decimal import Decimal
from hashlib import sha256
import inspect
import json
from pathlib import Path
import re
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.transformers.filings import extract_rows_from_dart_html

PILOT = ROOT / "deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot"
DATE = re.compile(r"(\d{4})\s*[.년/-]\s*(\d{1,2})\s*[.월/-]\s*(\d{1,2})(?:일)?")
UNITS = {"원": 1, "천원": 1000, "백만원": 1000000}
PURE = {"TRADE_RECEIVABLES": "매출채권", "TRADE_PAYABLES": "매입채무"}


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", "utf-8")


def label(text):
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"^(?:[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫIVX]+\.?|\(\d+\)|\d+\.)", "", text)
    return re.sub(r"[():：]", "", text)


def review_source(task):
    symbol, receipt, folder = task
    output = Path(folder) / symbol / receipt["rcept_no"]
    for kind in ("source", "normalized", "debug"):
        assert digest(receipt[kind + "_path"]) == receipt[kind + "_sha256"]
    rows = extract_rows_from_dart_html(receipt["source_path"], symbol,
        pd.Timestamp(receipt["period_end_date"]).strftime("%Y.%m"))
    output.mkdir(parents=True)
    pd.DataFrame(rows).to_parquet(output / "primary_rows.parquet", index=False)
    normalized = pd.read_csv(receipt["normalized_path"], dtype=str).fillna("")
    accepted, excluded, borrowing = [], [], []
    for row in rows:
        if row["statement_type"] != "BS":
            continue
        if re.search("차입|사채|리스|금융부채", row["original_account_name"]):
            borrowing.append({key: row.get(key) for key in ("table_index", "row_index", "indent_level",
                "original_account_name", "amount_raw", "raw_amount", "unit_factor", "table_title",
                "source_financial_scope", "parse_alignment_complete", "amount_is_missing")})
    for account, allowed in PURE.items():
        try:
            found = [row for row in rows if row["statement_type"] == "BS"
                and label(row["original_account_name"]) == allowed]
            assert len(found) == 1, "Pure trade account is absent or ambiguous; combined other balances are not substituted"
            row = found[0]
            assert row["source_financial_scope"] == receipt["financial_scope"]
            assert row["accounting_regime"] == receipt["accounting_regime"]
            assert row["parse_alignment_complete"] and not row["amount_is_missing"]
            caption = re.split(r"재\s*무\s*상\s*태\s*표|대\s*차\s*대\s*조\s*표", row["table_title"])[-1]
            dates = [date(*map(int, match)) for match in DATE.findall(caption)]
            assert dates and dates[0] == date.fromisoformat(receipt["period_end_date"])
            units = re.findall(r"단위\s*[:：]\s*(백만원|천원|원)", row["table_title"])
            assert units and len(set(units)) == 1
            unit = UNITS[units[0]]
            display = re.sub(r"\s+", "", row["amount_raw"])
            assert re.fullmatch(r"\d[\d,]*(?:\.\d+)?", display), "Unreviewed signed, blank or annotated trade balance"
            amount = Decimal(display.replace(",", "")) * unit
            assert amount == Decimal(row["raw_amount"]) and Decimal(row["unit_factor"]) == unit
            candidates = normalized.loc[normalized.canonical_account_id.eq(account)]
            assert len(candidates) == 1, "Normalized account is absent or duplicated"
            candidate = candidates.iloc[0]
            assert candidate.original_account_name == row["original_account_name"]
            assert candidate.statement_type == "BS" and candidate.period == pd.Timestamp(dates[0]).strftime("%Y.%m")
            assert candidate.amount_policy == "as_reported"
            assert Decimal(candidate.normalized_amount) == amount
            accepted.append(dict(canonical_account_id=account, value=str(amount),
                original_account_name=row["original_account_name"], table_index=row["table_index"], row_index=row["row_index"],
                table_title=row["table_title"], amount_raw=row["amount_raw"], unit_factor=unit,
                financial_scope=row["source_financial_scope"], already_accepted=account in receipt["accepted_canonical_account_ids"]))
        except AssertionError as error:
            excluded.append(dict(canonical_account_id=account, reason=str(error) or "Source period, scope, unit or normalization does not match"))
    result = dict(symbol=symbol, receipt=receipt["rcept_no"], report_date=receipt["report_date"],
        period=receipt["period_end_date"], source_path=receipt["source_path"], source_sha256=receipt["source_sha256"],
        normalized_sha256=receipt["normalized_sha256"], accepted=accepted, excluded=excluded,
        borrowing_components=borrowing, borrowing_status="requires_component_and_completeness_review",
        primary_rows_sha256=digest(output / "primary_rows.parquet"))
    save(output / "review.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["003410", "035480"])
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists(), "Preserve the earlier source review"
    implementations = {str(path): digest(path) for path in [Path(__file__), Path(inspect.getsourcefile(extract_rows_from_dart_html))]}
    reviews, tasks = {}, []
    for symbol in args.symbols:
        directory = "kr_receipt_history_v5_jtech" if symbol == "035480" else "kr_receipt_history_v7_review_input"
        source = PILOT / directory / "reviewed" / symbol / "review.json"
        review = json.loads(source.read_text("utf-8"))
        reviews[symbol] = dict(path=str(source), sha256=digest(source))
        tasks.extend((symbol, receipt, str(args.output)) for receipt in review["receipts"])
    report = dict(status="running", source_reviews=reviews, implementations=implementations,
        planned_receipts=len(tasks), results=[], production_published=False, coverage_complete=False)
    save(args.output / "summary.json", report)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for completed in as_completed([pool.submit(review_source, task) for task in tasks]):
            result = completed.result()
            report["results"].append(result)
            save(args.output / "summary.json", report)
            print(result["symbol"], result["receipt"], "accepted", len(result["accepted"]), "completed", len(report["results"]), flush=True)
    for path, checksum in implementations.items():
        assert digest(path) == checksum
    for source in reviews.values():
        assert digest(source["path"]) == source["sha256"]
    report.update(status="reviewed", accepted_facts=sum(len(r["accepted"]) for r in report["results"]))
    save(args.output / "summary.json", report)


if __name__ == "__main__":
    main()
