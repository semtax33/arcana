"""Review retrospective RTN class boundaries without changing price identities."""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys

from bs4 import BeautifulSoup
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_frame, export_json


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def main():
    base = DATA_LAKE.silver("survivorship", "financial_research")
    output = base / "us_rtn_trading_boundary_review_20260911"
    output.mkdir(exist_ok=False)
    collection_path = base / "us_rtn_trading_boundary_20260911/collection.json"
    collection = json.loads(collection_path.read_bytes())
    assert set(collection["sources"]) == {"proxy_2002", "proxy_2002_index"}
    prior_path = base / "us_rtn_share_identity_review_20260911/summary.json"
    prior = json.loads(prior_path.read_bytes())
    pins = {item["path"]: item["sha256"] for item in prior["source_pins"]}
    pins[str(prior_path)] = digest(prior_path)
    for item in collection["sources"].values():
        assert item["provider"] == "SEC" and item["http_status"] == 200
        path = Path(item["source_path"])
        assert digest(path) == item["source_sha256"]
        pins[str(path)] = digest(path)
        metadata = path.with_name(path.stem + ".metadata.json")
        pins[str(metadata)] = digest(metadata)
    pins[str(collection_path)] = digest(collection_path)
    # Exercise the public collector's scoped cached rerun; earlier collections stay immutable.
    command = [sys.executable, "-X", "utf8", str(ROOT / "scripts/research/collect_rtn_share_identity_sources.py"),
               "--run-scope", "trading_boundary_20260911"]
    for name, item in collection["sources"].items():
        command += ["--source", f"{name}={item['source_url']}"]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=90)
    export_json(output / "cached_rerun.json", dict(exit_code=completed.returncode, stdout=completed.stdout,
                                                  stderr=completed.stderr))
    if completed.returncode:
        raise RuntimeError("Scoped source collection rerun failed")
    assert all(digest(path) == expected for path, expected in pins.items())

    source = collection["sources"]["proxy_2002"]
    text = BeautifulSoup(Path(source["source_path"]).read_bytes(), "html.parser").get_text("\n", strip=True)
    flat = re.sub(r"\s+", " ", text)
    index_source = collection["sources"]["proxy_2002_index"]
    index_text = BeautifulSoup(Path(index_source["source_path"]).read_bytes(), "html.parser").get_text(" ", strip=True)
    assert re.search(r"Filing Date\s+2002-03-28", index_text)
    paragraph = re.search(r"The first graph covers the period from December 18, 1997,.*?single class of common stock\.", flat)
    assert paragraph is not None
    assert "Class A and Class B shares first began trading" in paragraph[0]
    assert "May 14, 2001, the last date" in paragraph[0]
    assert "May 15, 2001 through December 31, 2001" in paragraph[0]
    assert "Assumes $100 invested on May 15, 2001 in Raytheon common stock" in flat
    table_rows = {}
    for day, width in [("12/31/2000", 4), ("05/14/2001", 4), ("05/15/2001", 3), ("12/31/2001", 3)]:
        match = re.search(r"(?m)^" + re.escape(day) + r"\s+" + r"\s+".join([r"([0-9]+\.[0-9]+)"] * width), text)
        assert match is not None, day
        table_rows[day] = [Decimal(value) for value in match.groups()]
    assert table_rows["05/15/2001"][0] == Decimal("100")
    assert table_rows["12/31/2001"][0] == Decimal("112.98")
    terminal_review = json.loads((base / "us_rtn_source_review_20260911/review.json").read_bytes())
    price_path = Path(terminal_review["price_source_path"])
    assert digest(price_path) == terminal_review["price_source_sha256"]
    payload = json.loads(price_path.read_bytes())
    assert payload["Meta Data"]["2. Symbol"] == "RTN"
    prices = payload["Time Series (Daily)"]
    comparisons = []
    for name, first, last, published_first, published_last, first_exact in [
        ("new_common", "2001-05-15", "2001-12-31", table_rows["05/15/2001"][0], table_rows["12/31/2001"][0], True),
        ("old_class_a_candidate", "2000-12-29", "2001-05-14", table_rows["12/31/2000"][0], table_rows["05/14/2001"][0], False),
        ("old_class_b_candidate", "2000-12-29", "2001-05-14", table_rows["12/31/2000"][1], table_rows["05/14/2001"][1], False),
    ]:
        first_value = Decimal(prices[first]["5. adjusted close"])
        last_value = Decimal(prices[last]["5. adjusted close"])
        actual = last_value / first_value
        error = Decimal("0.005")
        first_error = Decimal("0") if first_exact else error
        low = (published_last-error) / (published_first+first_error)
        high = (published_last+error) / (published_first-first_error)
        comparisons.append(dict(candidate=name, start_price_date=first, end_price_date=last,
            published_start_index=str(published_first), published_end_index=str(published_last),
            vendor_start_adjusted_close=str(first_value), vendor_end_adjusted_close=str(last_value),
            vendor_growth=str(actual), published_growth=str(published_last/published_first),
            published_rounding_lower=str(low), published_rounding_upper=str(high),
            within_published_rounding=low <= actual <= high))
    assert comparisons[0]["within_published_rounding"]
    assert not any(row["within_published_rounding"] for row in comparisons[1:])
    artifact = export_frame(output / "performance_comparison.parquet", pd.DataFrame(comparisons))
    (output / Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    report = dict(status="retrospective_daily_boundaries_supported_pre_common_prices_unresolved",
        reviewed_at=datetime.now(timezone.utc).isoformat(), symbol="RTN", issuer_cik="1047122",
        official_source_url=source["source_url"], official_source_sha256=source["source_sha256"], filing_date="2002-03-28",
        confirmed_class_a_first_exchange_session="1997-12-18", confirmed_class_b_first_exchange_session="1997-12-18",
        confirmed_old_classes_last_exchange_session="2001-05-14", retrospective_common_performance_start="2001-05-15",
        supported_new_common_daily_start="2001-05-15",
        boundary_reason="Contemporaneous completed reclassification and expected May 15 trading are corroborated by the later explicit old-class final day, May 15 common investment/performance period, and retained daily quotes. This is a daily-series boundary, not an intraday execution timestamp.",
        original_paragraph=paragraph[0], performance_comparisons=comparisons, performance_artifact=artifact,
        evidence_pins=pins, cached_rerun_preserved_all_pins=True, prior_review_inputs_preserved=30,
        production_registry_changed=False, production_prices_changed=False, coverage_complete=False,
        remaining_review=[
            "The 387 pre-common Alpha rows still lack an approved class-specific provider identity. Neither candidate matches the published 2000-year-end to May 14 index change within its printed rounding.",
            "A discrepancy between a vendor-adjusted series and published total-return indices is not by itself proof of a wrong quote or class. Do not repair it with an invented coefficient or infer the cash-out proceeds.",
            "Vendor-adjusted-close growth is used only for source corroboration; the strategy price input remains split-only adjusted.",
            "Account-dependent cash-out, 2020 fractional rights, actual delivery/payment and financial-history review remain open."])
    assert all(digest(path) == expected for path, expected in pins.items())
    report["implementation_sha256"] = digest(__file__)
    export_json(output / "summary.json", report)
    export_json(DATA_LAKE.gold("survivorship", "us", "rtn_share_identity", "20260911", "trading_boundary.json"),
        {**report, "verification_path": str(output / "summary.json"), "verification_sha256": digest(output / "summary.json")})
    print(json.dumps({"status": report["status"], "common_daily_start": report["supported_new_common_daily_start"],
                      "common_index_matches": True, "pre_common_alias_approved": False}), flush=True)


if __name__ == "__main__":
    main()
