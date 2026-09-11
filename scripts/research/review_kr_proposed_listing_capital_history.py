"""Cross-check full retained price intervals against original DART capital histories."""
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
from engine.core.serving_storage import export_frame, export_json
from engine.transformers._internal.dart_document import _decode


def compact(value):
    return re.sub(r"\s+", "", value)


def number(value):
    if not re.fullmatch(r"[0-9,]+", value):
        raise ValueError("A disclosed numeric value is required; blanks are not zero")
    return int(value.replace(",", ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = DATA_LAKE.silver("survivorship", "financial_research")
    output = args.output.resolve()
    if not output.is_relative_to(base.resolve()) or output.exists():
        raise ValueError("Use a new immutable Silver review output")
    pins = {}

    def pin(path, expected=None):
        path = Path(path).resolve()
        digest = sha256(path.read_bytes()).hexdigest()
        if expected and expected != digest:
            raise ValueError(f"Pinned input changed: {path}")
        pins[str(path)] = digest
        return path

    pin(__file__)
    collection_path = pin(base / "kr_proposed_listing_capital_documents_20260911" / "summary.json")
    collection = json.loads(collection_path.read_bytes())
    index_path = pin(collection["index_summary_path"], collection["index_summary_sha256"])
    indexes = json.loads(index_path.read_bytes())
    for source in indexes["sources"]:
        pin(source["source_path"], source["source_sha256"])
    filings = pd.read_parquet(pin(indexes["artifacts"]["all_filings"]["path"], indexes["artifacts"]["all_filings"]["sha256"]))
    proposal_path = pin(indexes["proposal_path"], indexes["proposal_sha256"])
    proposal = json.loads(proposal_path.read_bytes())
    price_summary_path = pin(base / "kr_missing_listing_price_preparation_20260911" / "summary.json")
    price_summary = json.loads(price_summary_path.read_bytes())
    price_inputs = {item["symbol"]: item for item in price_summary["checks"]}
    episodes = {item["symbol"]: item for item in proposal["listing_episodes"] if item["symbol"] in price_inputs}
    expected_shares = {"204210": 7826815, "464440": 4320000, "464680": 12905000}
    expected_par = {"204210": 5000, "464440": 100, "464680": 100}
    report = {"status": "reviewing", "as_of": "2026-09-10", "production_changed": False,
        "coverage_complete": False, "documents": [], "securities": [], "pinned_inputs": pins}
    output.mkdir(parents=True)
    all_cells, points, issuances = [], [], []
    action_texts = {}
    try:
        followup_path = pin(base / "kr_capital_followup_sections_20260911" / "summary.json")
        followup = json.loads(followup_path.read_bytes())
        if followup["status"] != "original_sections_retained_and_titles_verified":
            raise ValueError("Issuance-year source conflict has no reviewed follow-up")
        followup_soups = {}
        for source in followup["sources"]:
            path = pin(source["source_path"], source["source_sha256"])
            pin(source["main_path"], source["main_sha256"])
            followup_soups[(source["receipt"], source["title"])] = BeautifulSoup(_decode(path.read_bytes())[0], "lxml")
        pre_ipo = followup_soups[("20160819000146", "2. 집합투자기구의 연혁")]
        if "2014년3월15일사모증자46.5억원" not in compact(pre_ipo.get_text(" ", strip=True)):
            raise ValueError("Pre-IPO prospectus does not confirm the historical capital increase")
        corrected_quarter = followup_soups[("20170518000045", "3. 자본금 변동사항")]
        corrected_rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"], recursive=False)]
            for tr in corrected_quarter.find_all("tr")]
        historical_issue = ["2014.03.15", "유상증자(제3자배정)", "보통주", "930,000", "5,000", "5,000", "-"]
        if corrected_rows.count(historical_issue) != 1 or 930000 * 5000 != 4650000000:
            raise ValueError("Contemporaneous corrected quarter does not corroborate the same issuance")
        report["source_conflicts"] = []
        for document in collection["documents"]:
            symbol, receipt = document["observed_symbol"], document["rcept_no"]
            source = pin(document["source_path"], document["source_sha256"])
            archive_path = pin(document["response_path"], document["response_sha256"])
            index_original = pin(document["index_source"], document["index_sha256"])
            original_index = json.loads(index_original.read_bytes())
            rows = [r for r in original_index["list"] if r["rcept_no"] == receipt]
            if len(rows) != 1 or rows[0]["corp_code"] != document["corp_code"]:
                raise ValueError("Original index does not identify this issuer")
            with ZipFile(io.BytesIO(archive_path.read_bytes())) as archive:
                if archive.read(document["archive_member"]) != source.read_bytes():
                    raise ValueError("Extracted original differs from exact archive member")
            soup = BeautifulSoup(_decode(source.read_bytes())[0], "lxml-xml")
            for node in soup.find_all(lambda node: node.name.lower() in {"style", "script"}):
                node.decompose()
            if not re.match(r"^(?:\[[^\]]+\])?(?:사업|분기|반기)보고서\s*\(", document["report_nm"]):
                action_texts[receipt] = {"symbol": symbol, "title": document["report_nm"],
                    "published_date": document["rcept_dt"], "text": soup.get_text("\n", strip=True),
                    "source_path": str(source), "source_sha256": document["source_sha256"]}
                continue
            company = soup.find("COMPANY-NAME")
            if company is None or company.get("AREGCIK") != document["corp_code"]:
                raise ValueError("Report company identity is not verified")
            sections = {}
            for title in soup.find_all("TITLE"):
                name = compact(title.get_text())
                if name in {"3.자본금변동사항", "4.주식의총수등"}:
                    if name in sections or title.parent.name != "SECTION-2":
                        raise ValueError("Ambiguous main capital section")
                    sections[name] = title.parent
            if set(sections) != {"3.자본금변동사항", "4.주식의총수등"}:
                raise ValueError("Required original capital sections missing")
            section_rows = {}
            for name, section in sections.items():
                table_rows = []
                for ti, table in enumerate(section.find_all("TABLE")):
                    for ri, tr in enumerate(table.find_all("TR")):
                        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["TD", "TH", "TE", "TU"], recursive=False)]
                        if not cells:
                            continue
                        table_rows.append(cells)
                        all_cells.append({"symbol": symbol, "receipt": receipt, "section": name,
                            "table_index": ti, "row_index": ri, "cells": cells,
                            "source_path": str(source), "source_sha256": document["source_sha256"]})
                section_rows[name] = table_rows
                destination = output / symbol / receipt / ("capital.txt" if name.startswith("3.") else "shares.txt")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(section.get_text("\n", strip=True), "utf-8")
            total_rows = [row for row in section_rows["4.주식의총수등"] if compact(row[0]).startswith("Ⅳ.발행주식의총수")]
            if len(total_rows) != 1 or len(total_rows[0]) != 5:
                raise ValueError("Ambiguous issued share class columns")
            shares_rows = section_rows["4.주식의총수등"]
            if not any([compact(cell) for cell in row] == ["보통주", "우선주", "합계"] for row in shares_rows):
                raise ValueError("Share class header is not verified")
            common, total = number(total_rows[0][1]), number(total_rows[0][3])
            if common != expected_shares[symbol] or total != common:
                raise ValueError("Disclosed common shares conflict with the dated price observations")
            dates = re.findall(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", sections["4.주식의총수등"].get_text(" ", strip=True))
            if not dates:
                raise ValueError("Original share total has no report date")
            as_of = "-".join([dates[0][0], dates[0][1].zfill(2), dates[0][2].zfill(2)])
            points.append({"symbol": symbol, "receipt": receipt, "report_as_of": as_of,
                "published_date": document["rcept_dt"], "common_shares": common,
                "source_path": str(source), "source_sha256": document["source_sha256"]})
            capital_rows = section_rows["3.자본금변동사항"]
            capital_text = sections["3.자본금변동사항"].get_text(" ", strip=True)
            dated_issues = []
            unit_basis = "no_numeric_capital_table"
            if capital_rows:
                if "(단위:원,주)" in compact(capital_text):
                    unit_basis = "explicit_table_unit_won_and_shares"
                elif symbol == "464680" and all(token in compact(capital_text) for token in
                        ["405,000주", "40,500,000원", "12,500,000주", "1,250,000,000원"]):
                    # The table unit is blank; the same section explicitly
                    # states both issued-share quantities and capital in won.
                    if 405000 + 12500000 != common or (40500000 + 1250000000) != common * expected_par[symbol]:
                        raise ValueError("Narrative quantities do not reconcile the omitted table unit")
                    unit_basis = "blank_table_unit_resolved_by_same_section_explicit_won_and_share_quantities"
                else:
                    raise ValueError("Original capital table has no supported unit")
            common_trends = [i for i, row in enumerate(capital_rows) if len(row) >= 3 and row[:2] == ["보통주", "발행주식총수"]]
            if common_trends:
                if len(common_trends) != 1:
                    raise ValueError("Ambiguous capital trend")
                i = common_trends[0]
                shares = [number(value) for value in capital_rows[i][2:]]
                par_row, cap_row = capital_rows[i+1], capital_rows[i+2]
                if par_row[0] != "액면금액" or cap_row[0] != "자본금":
                    raise ValueError("Capital trend row hierarchy changed")
                pars, caps = [number(v) for v in par_row[1:]], [number(v) for v in cap_row[1:]]
                if not len(shares) == len(pars) == len(caps):
                    raise ValueError("Capital period columns do not align")
                if any(s != common or p != expected_par[symbol] or c != s*p for s,p,c in zip(shares,pars,caps)):
                    raise ValueError("Capital, par and share-count history do not reconcile")
                capital_status = "all_reported_common_capital_periods_reconciled"
            elif capital_rows:
                if not any("주당액면가액" in row for row in capital_rows):
                    raise ValueError("Unrecognized original capital table")
                for row in capital_rows:
                    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", row[0]):
                        continue
                    if len(row) != 7 or row[2] != "보통주":
                        raise ValueError("Ambiguous dated issuance row")
                    day, kind, _, quantity, par, issue_price, note = row
                    if number(par) != expected_par[symbol]:
                        raise ValueError("Dated historical par changed")
                    item = {"symbol": symbol, "receipt": receipt, "date": day.replace(".", "-"),
                        "type": kind, "shares": number(quantity), "par": number(par),
                        "issue_price": number(issue_price), "note": note}
                    item["reviewed_date"] = item["date"]
                    if receipt == "20190401003279" and row == ["2017.03.15", *historical_issue[1:]]:
                        # Preserve the original date. The pre-2017 prospectus and
                        # contemporaneous corrected quarter establish that this
                        # is the same 2014 issuance, not a new 2017 share increase.
                        item["reviewed_date"] = "2014-03-15"
                        report["source_conflicts"].append({"symbol": symbol, "receipt": receipt,
                            "original_row": row, "reported_date": item["date"],
                            "reviewed_historical_date": item["reviewed_date"],
                            "corroborating_receipts": ["20160819000146", "20170518000045"],
                            "followup_summary_path": str(followup_path),
                            "disposition": "Do not create a new 2017 capital event from this isolated later-report year discrepancy. Original bytes and reported date remain unchanged."})
                    dated_issues.append(item)
                    issuances.append(item)
                if not dated_issues or any(item["reviewed_date"] >= episodes[symbol]["valid_from"] for item in dated_issues):
                    raise ValueError("A post-listing capital event requires separate review")
                capital_status = ("historical_issuance_date_conflict_reviewed_original_preserved" if receipt == "20190401003279"
                    else "dated_issuances_all_precede_listing")
            else:
                if "기재하지않습니다" not in compact(capital_text):
                    raise ValueError("Missing capital table has no explicit omission statement")
                capital_status = "capital_table_explicitly_omitted_not_assumed_unchanged"
            report["documents"].append({"symbol": symbol, "receipt": receipt, "report_as_of": as_of,
                "common_shares": common, "capital_review": capital_status,
                "capital_unit_basis": unit_basis,
                "dated_issuances": len(dated_issues), "source_sha256": document["source_sha256"]})
        if len(report["documents"]) != 21 or len(action_texts) != 4:
            raise ValueError("Every selected report and action document must be reviewed")
        if len(report["source_conflicts"]) != 1:
            raise ValueError("Expected original issuance-year discrepancy was not accounted for")
        for receipt in ["20260608900348", "20260527900424"]:
            text = compact(action_texts[receipt]["text"])
            if not all(anchor in text for anchor in ["공모전주주를제외한주주", "구체적인예치금분배방법및금액", "향후최종청산종결"]):
                raise ValueError("Original does not support the reviewed unresolved liquidation policy")
            action_texts[receipt]["review"] = "Liquidation rights are planned; pre-IPO shares are excluded from escrow distribution. No actual payment amount/date is established. Generic statutory merger/split wording is not an executed split."
        prior_path = pin(base / "kr_remaining_missing_membership_20260911" / "source_review.json")
        prior = json.loads(prior_path.read_bytes())
        for receipt in ["20260618000252", "20260609000084"]:
            matches = [record for record in prior["documents"] if record["receipt"] == receipt]
            if len(matches) != 1 or action_texts[receipt]["source_sha256"] != matches[0]["source_sha256"]:
                raise ValueError("Previously reviewed dissolution source changed")
            action_texts[receipt]["review"] = "Previously reviewed effective dissolution/delisting notice; unpaid liquidation rights remain open."
        for symbol in expected_shares:
            original = base / "kr_missing_listing_price_preparation_20260911" / symbol / "original_listed_observations.parquet"
            prices = pd.read_parquet(pin(original, price_inputs[symbol]["original_sha256"])).sort_values("Date")
            if prices.Stocks.nunique() != 1 or prices.Stocks.iloc[0] != expected_shares[symbol]:
                raise ValueError("Original daily share counts contradict the capital-history review")
            original_report_years = {int(re.search(r"\((\d{4})\.", row["report_nm"]).group(1))
                for row in collection["documents"] if row["observed_symbol"] == symbol
                and re.match(r"^(?:\[[^\]]+\])?사업보고서\s*\(", row["report_nm"])}
            expected_years = set(range(int(episodes[symbol]["valid_from"][:4]), 2026))
            if original_report_years != expected_years:
                raise ValueError("A listed-year annual capital report is missing")
            candidate_titles = filings.loc[filings.observed_symbol.eq(symbol) & filings.report_nm.str.contains("분할|병합|합병|감자|자본감소|액면")]
            if len(candidate_titles):
                raise ValueError("Unreviewed corporate-action titles remain")
            report["securities"].append({"symbol": symbol, "first_price_date": str(prices.Date.min().date()),
                "last_price_date": str(prices.Date.max().date()), "original_price_rows": len(prices),
                "common_shares": expected_shares[symbol], "documented_par": expected_par[symbol],
                "annual_years": sorted(original_report_years), "new_split_adjustments_identified": 0,
                "full_retained_price_interval_reviewed": True,
                "decision": "No additional split/reverse-split coefficient identified from the complete dated issuer indexes, reviewed capital histories and all retained daily share observations.",
                "financial_histories_approved": False, "terminal_rights_complete": False})
        report["artifacts"] = {
            "original_dom_rows": export_frame(output / "original_capital_table_rows.parquet", pd.DataFrame(all_cells)),
            "dated_common_shares": export_frame(output / "dated_common_shares.parquet", pd.DataFrame(points)),
            "original_issuance_history": export_frame(output / "original_issuance_history.parquet", pd.DataFrame(issuances)),
            "reviewed_action_notices": export_json(output / "reviewed_action_notices.json", action_texts),
        }
        for path, digest in pins.items():
            if sha256(Path(path).read_bytes()).hexdigest() != digest:
                raise ValueError("An original or implementation changed during review")
        report.update(status="three_full_price_intervals_reviewed_no_additional_split_adjustments",
            listed_price_rows=sum(item["original_price_rows"] for item in report["securities"]),
            capital_reports_reviewed=len(report["documents"]), action_notices_reviewed=len(action_texts),
            limitation="This is a source-backed review for these three retained listing-price intervals. It does not approve financial normalization, terminal liquidation values, or the entire market. Omitted capital tables and blank reported cells are not evidence of zero balances.")
    except BaseException as error:
        report.update(status="failed_check_evidence", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        Path(output / Path(__file__).name).write_bytes(Path(__file__).read_bytes())
        export_json(output / "summary.json", report)
    gold = DATA_LAKE.gold("survivorship", "kr", "proposed_listing_capital_history", "20260911", "summary.json")
    if gold.exists():
        raise ValueError("Preserve existing Gold generation")
    export_json(gold, {**report, "silver_review_path": str(output / "summary.json"),
        "silver_review_sha256": sha256((output / "summary.json").read_bytes()).hexdigest()})
    print(json.dumps({key: report[key] for key in ("status", "listed_price_rows", "capital_reports_reviewed", "action_notices_reviewed")}), flush=True)


if __name__ == "__main__":
    main()
