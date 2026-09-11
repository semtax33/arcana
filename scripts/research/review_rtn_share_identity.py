"""Audit retained RTN share-class evidence and every Alpha price row, without publishing."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

from bs4 import BeautifulSoup
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_frame, export_json


def fingerprint(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    silver_root = DATA_LAKE.silver("survivorship", "financial_research").resolve()
    if not output.is_relative_to(silver_root) or output.exists():
        raise ValueError("Use a new immutable Silver review folder")
    collection_path = silver_root / "us_rtn_share_identity_20260911" / "collection.json"
    collection = json.loads(collection_path.read_bytes())
    prior_path = silver_root / "us_rtn_source_review_20260911" / "review.json"
    prior = json.loads(prior_path.read_bytes())
    source_pins = [fingerprint(collection_path), fingerprint(prior_path), fingerprint(__file__)]
    texts = {}
    for name, source in collection["sources"].items():
        pin = fingerprint(source["source_path"])
        if pin["sha256"] != source["source_sha256"]:
            raise ValueError(f"Original changed: {name}")
        source_pins.append(pin)
        if source["http_status"] == 200 and not pin["path"].endswith(".json"):
            soup = BeautifulSoup(Path(pin["path"]).read_bytes(), "html.parser")
            for node in soup.find_all(["script", "style"]):
                node.decompose()
            texts[name] = soup.get_text("\n", strip=True)
    for source in prior["sources"].values():
        pin = fingerprint(source["source_path"])
        if pin["sha256"] != source["source_sha256"]:
            raise ValueError("Previously reviewed terminal original changed")
        source_pins.append(pin)
    price_path = Path(prior["price_source_path"])
    price_pin = fingerprint(price_path)
    if price_pin["sha256"] != prior["price_source_sha256"]:
        raise ValueError("Retained Alpha prices changed")
    source_pins.append(price_pin)
    output.mkdir(parents=True)
    export_json(output / "collection_snapshot.json", collection)
    Path(output / Path(__file__).name).write_bytes(Path(__file__).read_bytes())

    fragments = []

    def evidence(name, anchor, length=750):
        text = re.sub(r"\s+", " ", texts[name])
        start = text.find(anchor)
        if start < 0:
            raise ValueError(f"Reviewed original passage absent: {name}, {anchor}")
        source = collection["sources"][name]
        fragments.append({"source_name": name, "source_url": source["source_url"],
            "source_sha256": source["source_sha256"], "normalized_text_offset": start,
            "text": text[start:start + length]})
        return len(fragments) - 1

    claims = [
        {"claim": "Old Raytheon merged into HE Holdings on 1997-12-17; HE Holdings was renamed Raytheon. Old common became new issuer Class B, one for one.",
         "evidence": evidence("merger_1997_full", "On December 17, 1997, HE Holdings")},
        {"claim": "New issuer Class A started trading 1997-12-18. This statement does not establish a 1981 IPO for the 2001 common class.",
         "evidence": evidence("annual_1997_full", "Class A common stock began trading on December 18, 1997.", 210)},
        {"claim": "The issuer reported the Class A/B reclassification complete on 2001-05-14. RTN trading on 2001-05-15 was stated as expected.",
         "evidence": evidence("reclassification_release", "announced today that it completed the reclassification", 410)},
        {"claim": "The 2001 proxy defines one new common share for each retained Class A or Class B share; later completion reports confirm the reclassification occurred.",
         "evidence": evidence("proxy_2001_document", "Each share of Class A Common Stock, par value $.01", 1150)},
        {"claim": "The reverse/forward split is account-size dependent. Fewer than 20 shares were cashed out; holdings of at least 20 retained fractional intermediate units and returned to the same share count.",
         "evidence": evidence("proxy_2001_document", "If a registered holder has 20 or more Class A shares", 1580)},
        {"claim": "The issuer announced completed cash-out treatment, with proceeds distributed after independent-agent market sales. No fixed actual amount or payment date is established here.",
         "evidence": evidence("reclassification_release", "The company has arranged for the sale of the shares", 1120)},
        {"claim": "The 2001 second-quarter report confirms cash-out shares were aggregated and sold, leaving total outstanding shares unchanged.",
         "evidence": evidence("quarterly_2001_jul", "During the second quarter of 2001, the Company eliminated", 560)},
        {"claim": "The May 16 Form 15 deregisters Class A/B with zero record holders; it explicitly retains reporting for common stock. The company was not wholly delisted by this filing.",
         "evidence": evidence("old_classes_deregistration", "Class A Common Stock, $0.01 par value per share", 1500)},
        {"claim": "The May 16 8-K/A amends the May 9 filing and attaches an equity-unit pledge agreement. It is not a correction of the May 14 reclassification notice.",
         "evidence": evidence("reclassification_amendment", "The registrant hereby amends its Form 8-K, dated May 9, 2001.", 750)},
    ]

    payload = json.loads(price_path.read_bytes())
    if payload["Meta Data"]["2. Symbol"] != "RTN":
        raise ValueError("Wrong provider symbol")
    rows = []
    for day, row in sorted(payload["Time Series (Daily)"].items()):
        if day < "2001-05-15":
            interval, candidate = "pre_common_reclassification", "Class B (corroboration only)"
        elif day < "2020-04-03":
            interval, candidate = "new_common_before_merger", "Common Stock"
        else:
            interval, candidate = "after_official_trading_end", "Nonexecutable residual provider quote"
        numeric = [Decimal(row[key]) for key in ("1. open", "2. high", "3. low", "4. close")]
        if not all(value.is_finite() and value > 0 for value in numeric):
            raise ValueError("Invalid retained quote")
        open_, high, low, close = numeric
        if not low <= min(open_, close) <= max(open_, close) <= high:
            raise ValueError(f"Invalid OHLC bounds on {day}")
        rows.append({"trade_date": day, "provider_symbol": "RTN", "interval": interval,
            "candidate_share_class": candidate, "issuer_cik": "1047122",
            "open": float(open_), "high": float(high), "low": float(low), "close": float(close),
            "provider_adjusted_close": float(Decimal(row["5. adjusted close"])),
            "volume": int(row["6. volume"]), "provider_split_coefficient": row["8. split coefficient"],
            "identity_approved_for_production": False,
            "price_source_sha256": price_pin["sha256"]})
    prices = pd.DataFrame(rows)
    if len(prices) != prior["source_price_rows"] or prices.trade_date.duplicated().any():
        raise ValueError("Price history no longer matches reviewed original")
    if prior["official_trading_end_exclusive"] != "2020-04-03":
        raise ValueError("Reviewed termination date changed")
    partition = prices.groupby("interval", sort=True).agg(
        rows=("trade_date", "size"), first_date=("trade_date", "min"), last_date=("trade_date", "max")
    ).reset_index()
    if partition.rows.sum() != 5139:
        raise ValueError("Every original price row must remain accounted for")

    # Read both classes and both extrema, without selecting only matching cells.
    # The annual report labels these as stock prices, but does not define a
    # common intraday/closing convention for both years. Preserve both comparisons.
    table_rows = []
    year = None
    lines = texts["annual_2001_exhibit13"].splitlines()
    for line_index, line in enumerate(lines):
        header = re.fullmatch(r"(200[01])\s+First\s+Second\s+Third\s+Fourth", line.strip())
        if header:
            year = int(header.group(1))
        match = re.fullmatch(r"(Common Stock|Class A|Class B)--(High|Low)\s+(.+)", line.strip())
        if not match or year is None:
            continue
        share_class, extreme, values = match.groups()
        values = values.split()
        if len(values) != 4:
            raise ValueError("Original quarterly price columns are ambiguous")
        for quarter, original in enumerate(values, 1):
            if original == "--":
                continue
            first = f"{year}-{quarter * 3 - 2:02d}-01"
            last = str((pd.Period(first, freq="Q").end_time).date())
            if share_class == "Common Stock":
                first = max(first, "2001-05-15")
            else:
                last = min(last, "2001-05-14")
            sample = prices.loc[prices.trade_date.between(first, last)]
            if sample.empty:
                raise ValueError("No Alpha rows for original quarterly comparison")
            function = "max" if extreme == "High" else "min"
            intraday = getattr(sample[extreme.lower()], function)()
            closing = getattr(sample.close, function)()
            value = Decimal(original)
            table_rows.append({"year": year, "quarter": quarter, "share_class": share_class,
                "extreme": extreme, "reported_value": original, "reported_text_line": line_index + 1,
                "source_url": collection["sources"]["annual_2001_exhibit13"]["source_url"],
                "source_sha256": collection["sources"]["annual_2001_exhibit13"]["source_sha256"],
                "candidate_first_date": first, "candidate_last_date": last,
                "alpha_rows": len(sample), "alpha_intraday_extreme": intraday,
                "alpha_close_extreme": closing,
                "intraday_difference": str(Decimal(str(intraday)) - value),
                "close_difference": str(Decimal(str(closing)) - value),
                "quote_convention_verified": False, "individual_daily_identity_verified": False})
    if len(table_rows) != 30:
        raise ValueError("Expected all 30 reported class/quarter/extreme cells")
    comparisons = pd.DataFrame(table_rows)
    artifacts = {
        "all_original_price_rows": export_frame(output / "price_intervals.parquet", prices),
        "interval_summary": export_frame(output / "price_interval_summary.parquet", partition),
        "reported_price_comparisons": export_frame(output / "reported_price_comparisons.parquet", comparisons),
        "source_fragments": export_json(output / "source_fragments.json", fragments),
    }
    nonunit = prices.loc[prices.provider_split_coefficient.map(Decimal) != Decimal(1)]
    summary = {"status": "share_class_history_reviewed_identity_and_settlement_publication_pending",
        "reviewed_at": datetime.now(timezone.utc).isoformat(), "symbol": "RTN",
        "issuer_cik": "1047122", "claims": claims, "price_intervals": partition.to_dict("records"),
        "original_price_rows": len(prices), "reported_price_comparison_cells": len(comparisons),
        "nonunit_provider_split_rows": len(nonunit),
        "expected_common_trading_start": "2001-05-15", "official_actual_first_trade_confirmed": False,
        "reclassification_completion_date": "2001-05-14", "new_common_ratio": "1",
        "small_account_cashout_threshold_shares": 20, "small_account_cash_amount": None,
        "small_account_cash_payment_date": None,
        "terminal_exchange_ratio": prior["exchange_ratio"],
        "terminal_fractional_cash_amount": prior["fractional_cash_amount"],
        "terminal_verified_delivery_date": prior["verified_delivery_date"],
        "source_count": len(collection["sources"]),
        "unavailable_source_responses": [name for name, source in collection["sources"].items()
            if source["http_status"] != 200],
        "unavailable_response_resolution": {"proxy_2001": "The submissions primaryDocument basename returned 404. The retained SEC filing index resolved the prefixed filename, retained as proxy_2001_document."},
        "production_changed": False, "coverage_complete": False, "new_listing_episodes_registered": 0,
        "artifacts": artifacts,
        "remaining_review": [
            "Do not assign Alpha's 1981-12-31 IPO date to CIK1047122 or its 2001 common class.",
            "The 387 pre-2001-05-15 rows remain preserved. Class B is a candidate supported by class-specific quarterly extrema, not a confirmed provider alias for each daily row.",
            "The 1997-12-18 first-trade statement concerns Class A; do not silently apply it to Class B.",
            "The 2001 common first-trade date is announced as expected and corroborated by Alpha quotes. Additional retrospective first-trade confirmation remains open.",
            "The 2000 low comparison differs by one cent in Q4; keep both raw values without an assumed rounding correction. Closing and intraday bases must remain distinct.",
            "No ordinary scalar split coefficient can establish account-size cash-out proceeds. All Alpha split coefficients being one is not proof that no corporate action occurred.",
            "Review original fractional rights, share delivery/payment, financial histories and price-series identity before actual pipeline registration.",
        ]}
    for pin in source_pins:
        if fingerprint(pin["path"])["sha256"] != pin["sha256"]:
            raise ValueError("A source or implementation changed during review")
    summary["source_pins"] = source_pins
    summary_artifact = export_json(output / "summary.json", summary)
    gold = DATA_LAKE.gold("survivorship", "us", "rtn_share_identity", "20260911", "summary.json")
    if gold.exists():
        raise ValueError("Preserve the existing Gold generation before superseding it")
    export_json(gold, {**summary, "silver_review": summary_artifact})
    print(json.dumps({"status": summary["status"], "price_intervals": summary["price_intervals"],
        "comparison_cells": len(comparisons), "sources": summary["source_count"],
        "production_changed": False}), flush=True)


if __name__ == "__main__":
    main()
