"""Reconcile reported borrowing components directly against retained DART cells.

This preserves current/noncurrent presentation and explicit blanks. It does not
infer complete debt totals, change canonical mappings, or publish financial data.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import re
import shutil
import sys
import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import sha256_file

DATE = re.compile(r"(\d{4})\s*[.년/-]\s*(\d{1,2})\s*[.월/-]\s*(\d{1,2})")
UNITS = {"원": 1, "천원": 1000, "백만원": 1000000}
COMPONENTS = {
    "단기차입금": "reported_short_term_borrowings", "유동차입금": "reported_current_borrowings",
    "장기차입금": "reported_long_term_borrowings", "단기사채": "reported_short_term_bonds",
    "유동성장기부채": "reported_current_portion_of_long_term_debt",
    "유동성장기차입금": "reported_current_portion_of_long_term_borrowings",
    "사채": "reported_bonds", "유동성사채": "reported_current_bonds",
    "전환사채": "reported_convertible_bonds", "유동성전환사채": "reported_current_convertible_bonds",
    "신주인수권부사채": "reported_bonds_with_warrants",
    "금융리스부채": "reported_finance_lease_liabilities",
    "유동성금융리스부채": "reported_current_finance_lease_liabilities",
    "비유동금융리스부채": "reported_noncurrent_finance_lease_liabilities",
}


def compact(value):
    return re.sub(r"\s+", "", str(value))


def label(value):
    text = compact(value)
    return re.sub(r"^(?:\(\d+\)|\d+\.|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVX]+\.)", "", text)


def amount(value, multiplier):
    text = compact(value)
    if not text:
        return None, "reported_blank"
    if text in {"-", "–", "—"}:
        return None, "reported_dash_requires_policy_review"
    if not re.fullmatch(r"(?:\d[\d,]*(?:\.\d+)?|\(\d[\d,]*(?:\.\d+)?\))", text):
        raise ValueError(f"Unsupported original amount syntax: {text}")
    negative = text.startswith("(")
    value = Decimal(text.strip("()").replace(",", "")) * multiplier
    return -value if negative else value, "reported_number"


def review(task):
    prior, receipt, destination = task
    source = Path(prior["source_path"])
    assert sha256_file(source) == prior["source_sha256"]
    assert sha256_file(receipt["normalized_path"]) == receipt["normalized_sha256"]
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(source.read_bytes(), "html.parser")
    captions = {compact(row["table_title"]) for row in prior["borrowing_components"]}
    assert len(captions) == 1
    candidates = []
    for index, table in enumerate(soup.find_all("table")):
        header = table.find_previous_sibling("table")
        if header is None:
            continue
        caption = header.get_text(" ", strip=True)
        local_title = header.find_previous_sibling("title")
        alternatives = {compact(caption)}
        if local_title is not None:
            alternatives.add(compact(local_title.get_text(" ", strip=True) + " " + caption))
        if alternatives & captions:
            candidates.append((index, table, caption))
    assert len(candidates) == 1, f"Original financial table ambiguous/absent: {prior['symbol']} {prior['receipt']} {len(candidates)}"
    table_index, table, caption = candidates[0]
    dates = [date(*map(int, match)) for match in DATE.findall(caption)]
    assert dates and dates[0].isoformat() == prior["period"]
    scope = "CFS" if "연결" in caption else "OFS"
    assert scope == receipt["financial_scope"]
    unit_names = re.findall(r"단위\s*[:：]\s*(백만원|천원|원)", caption)
    assert unit_names and len(set(unit_names)) == 1
    unit = UNITS[unit_names[0]]
    dom, maturity = [], None
    for index, row in enumerate(table.find_all("tr")):
        cells = row.find_all(["td", "th", "te", "tu"], recursive=False)
        if not cells:
            continue
        text = [cell.get_text(" ", strip=True) for cell in cells]
        name = label(text[0])
        if name == "유동부채":
            maturity = "current"
        elif name == "비유동부채":
            maturity = "noncurrent"
        elif name in {"부채총계", "자본"}:
            maturity = None
        dom.append(dict(row_index=index, original_account_name=text[0], label=name,
            current_cell=text[1] if len(text) > 1 else None, maturity=maturity,
            original_cells=text))
    normalized = pd.read_csv(receipt["normalized_path"], dtype=str).fillna("")
    normalized = normalized.loc[normalized.statement_type.eq("BS")].copy()
    normalized["label"] = normalized.original_account_name.map(label)
    components = []
    for old in prior["borrowing_components"]:
        matches = [r for r in dom if r["row_index"] == old["row_index"] and r["label"] == label(old["original_account_name"])]
        assert len(matches) == 1, "Original row/label alignment differs"
        observed = matches[0]
        assert observed["maturity"] in {"current", "noncurrent"}
        assert observed["current_cell"] is not None
        value, status = amount(observed["current_cell"], unit)
        assert compact(observed["current_cell"]) == compact(old["amount_raw"])
        assert Decimal(old["unit_factor"]) == unit
        if value is None:
            assert old["amount_is_missing"] and not old["raw_amount"]
        else:
            assert not old["amount_is_missing"] and Decimal(old["raw_amount"]) == value
        mapped = normalized.loc[normalized.label.eq(observed["label"])]
        mappings = mapped[["canonical_account_id", "original_account_name", "normalized_amount"]].to_dict("records")
        components.append(dict(symbol=prior["symbol"], receipt=prior["receipt"], period=prior["period"],
            report_date=prior["report_date"], financial_scope=scope, accounting_regime=receipt["accounting_regime"],
            source_path=str(source), source_sha256=prior["source_sha256"], dom_table_index=table_index,
            row_index=observed["row_index"], original_account_name=old["original_account_name"],
            maturity=observed["maturity"], component_description=COMPONENTS.get(observed["label"], "mixed_or_other_liability_requires_notes"),
            amount_raw=observed["current_cell"], value_krw=str(value) if value is not None else None,
            amount_status=status, unit_factor=unit, current_normalized_candidates=mappings,
            total_debt_approved=False))
    # Expose collisions without replacing the mapping by an inferred aggregate.
    collisions = []
    for canonical, group in normalized.groupby("canonical_account_id"):
        relevant = [r for r in components if label(r["original_account_name"]) in set(group.label)]
        if canonical in {"SHORT_TERM_DEBT", "LONG_TERM_DEBT"} and len(relevant) > 1:
            collisions.append(dict(canonical_account_id=canonical,
                components=[{k:r[k] for k in ("original_account_name", "maturity", "value_krw", "row_index")} for r in relevant],
                complete_disjoint_total_reviewed=False))
    folder = Path(destination) / prior["symbol"] / prior["receipt"]
    folder.mkdir(parents=True)
    pd.DataFrame(dom).to_parquet(folder / "original_dom_rows.parquet", index=False)
    result = dict(status="original_cells_verified", symbol=prior["symbol"], receipt=prior["receipt"],
        period=prior["period"], caption=caption, components=components, canonical_collisions=collisions,
        source_sha256=prior["source_sha256"], normalized_sha256=receipt["normalized_sha256"],
        dom_rows_sha256=sha256_file(folder / "original_dom_rows.parquet"), total_debt_approved=False)
    assert sha256_file(source) == prior["source_sha256"]
    assert sha256_file(receipt["normalized_path"]) == receipt["normalized_sha256"]
    export_json(folder / "review.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Derived source reviews must remain in Silver")
    args.output.mkdir(parents=True, exist_ok=False)
    source_review = json.loads(args.source_review.read_text("utf-8"))
    refs = {str(args.source_review.resolve()): sha256_file(args.source_review), str(Path(__file__).resolve()): sha256_file(__file__)}
    receipts = {}
    for symbol, source in source_review["source_reviews"].items():
        path = Path(source["path"])
        assert sha256_file(path) == source["sha256"]
        refs[str(path)] = source["sha256"]
        for receipt in json.loads(path.read_text("utf-8"))["receipts"]:
            receipts[symbol, receipt["rcept_no"]] = receipt
    tasks = [(r, receipts[r["symbol"], r["receipt"]], str(args.output)) for r in source_review["results"]]
    report = dict(status="running", expected_receipts=len(tasks), results=[], input_sha256=refs,
        production_changed=False, total_debt_approved=False, coverage_complete=False)
    export_json(args.output / "summary.json", report)
    try:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for completed in as_completed([pool.submit(review, task) for task in tasks]):
                result = completed.result()
                report["results"].append(result)
                export_json(args.output / "summary.json", report)
                print(result["symbol"], result["receipt"], len(result["components"]), "verified", len(report["results"]), flush=True)
        for path, digest in refs.items():
            assert sha256_file(path) == digest, path
        components = [r for result in report["results"] for r in result["components"]]
        pd.DataFrame(components).drop(columns="current_normalized_candidates").to_parquet(args.output / "reported_components.parquet", index=False)
        report.update(status="reported_components_verified_total_and_mapping_review_pending", components=len(components),
            numeric_components=sum(r["amount_status"] == "reported_number" for r in components),
            missing_components=sum(r["value_krw"] is None for r in components),
            mixed_components=sum(r["component_description"] == "mixed_or_other_liability_requires_notes" for r in components),
            collision_groups=sum(len(r["canonical_collisions"]) for r in report["results"]),
            components_sha256=sha256_file(args.output / "reported_components.parquet"),
            policy="Original current and noncurrent components remain distinct. Blanks and dashes are not zero. Components are not approved total debt; notes, disjointness, lease inclusion, and combined other-liability balances remain to be reviewed.")
    except BaseException as error:
        report.update(status="failed_check_receipt_evidence", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        shutil.copy2(__file__, args.output / Path(__file__).name)
        export_json(args.output / "summary.json", report)
    export_json(DATA_LAKE.gold("survivorship", "kr", "reported_debt_components", "20260911", "summary.json"),
        {k:v for k,v in report.items() if k not in {"results", "input_sha256"}} | dict(
            silver_summary=str((args.output / "summary.json").resolve()), silver_summary_sha256=sha256_file(args.output / "summary.json")))
    print({k:report[k] for k in ("status", "components", "numeric_components", "missing_components", "collision_groups")}, flush=True)


if __name__ == "__main__":
    main()
