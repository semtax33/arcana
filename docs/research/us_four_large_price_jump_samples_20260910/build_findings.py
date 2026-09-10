"""Package four bounded research cases. No ledger, price panel, or core mutations."""
from pathlib import Path
from decimal import Decimal
from collections import Counter
import datetime
import hashlib
import json
import re

from lxml import html

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
CASES = {"KEEL": "1812477", "SKYX": "1598981", "CAPS": "887151", "ASTI": "1350102"}


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized(path):
    raw = path.read_bytes()
    if path.suffix == ".htm":
        return " ".join(html.fromstring(raw).text_content().split())
    return " ".join(raw.decode("utf-8").split())


def source(symbol, filename, roles, markers=(), record_id=None, location=None):
    path = ROOT / symbol / filename
    meta = json.loads(path.with_suffix(path.suffix + ".metadata.json").read_text("utf-8"))
    result = {k: meta.get(k) for k in ["provider", "cik", "source_id", "document_id", "published_date", "security_id", "source_url", "source_sha256"]}
    result.update(local_path=str(path), filename=filename, roles=roles, markers=list(markers), location=location)
    if meta["provider"] == "FINRA":
        result.update(request_body=meta["request_body"], request_method="POST", request_headers=meta["request_headers"])
        if record_id is not None:
            record = next(r for r in json.loads(path.read_text("utf-8")) if r["OTCDailyListID"] == record_id)
            result.update(finra_record_id=record_id, published_date=record["dailyListDatetime"][:10], finra_record_fields={k: record.get(k) for k in ["OTCDailyListID", "dailyListDatetime", "calendarDay", "exDate", "dailyListEventCode", "dividendMasterID", "dividendTypeCode", "reverseSplitRate", "oldSymbolCode", "newSymbolCode", "oldSecurityDescription", "newSecurityDescription", "dailyListReasonDescription", "commentText"]})
    return result


def prepare_metadata():
    observed = json.loads((ROOT / "initial_local_observations.json").read_text("utf-8"))
    for symbol, cik in CASES.items():
        for kind in ["cached_TIME_SERIES_DAILY_ADJUSTED", "current_TIME_SERIES_DAILY", "cached_SPLITS"]:
            path = ROOT / symbol / f"{symbol}_{kind}.json"
            sidecar = path.with_suffix(".json.metadata.json")
            meta = json.loads(sidecar.read_text("utf-8")) if sidecar.exists() else {}
            function = "SPLITS" if kind == "cached_SPLITS" else "TIME_SERIES_DAILY_ADJUSTED" if "ADJUSTED" in kind else "TIME_SERIES_DAILY"
            payload = json.loads(path.read_text("utf-8"))
            meta.update(provider="ALPHA_VANTAGE", symbol=symbol, cik=cik, security_id="SEC_US_" + symbol,
                        source_id=f"ALPHA-VANTAGE-{symbol}-{kind.upper()}-RESEARCH-20260910",
                        document_id=f"ALPHA-VANTAGE:{symbol}:{kind}:20260910", path=str(path),
                        source_sha256=digest(path), function=function,
                        request_parameters_without_credentials={"function": function, "symbol": symbol, **({"outputsize": "full", "datatype": "json"} if function != "SPLITS" else {})})
            if function == "SPLITS":
                meta.update(source_url="https://www.alphavantage.co/query", snapshot_date="2026-07-26", published_date=None,
                            publication_note="Original archive has no publication/retrieval metadata; directory snapshot_date is not an event or publication date.",
                            source_local_path=observed[symbol]["splits_snapshot"]["path"])
            else:
                meta.update(published_date=payload["Meta Data"].get("3. Last Refreshed", "")[:10],
                            published_date_basis="Alpha Vantage Meta Data / 3. Last Refreshed; historical prices are not point-in-time corporate action evidence.")
            dump(sidecar, meta)
    for path in ROOT.glob("*/finra_*.json.metadata.json"):
        meta = json.loads(path.read_text("utf-8"))
        meta["published_date_basis"] = "Latest per-record dailyListDatetime in this query response; cited record-level publication dates control. calendarDay is a partition."
        if meta.get("records") == 0:
            meta["publication_note"] = "HTTP 204 empty query result; no publication date or event evidence. A missed partition is not proof of no action."
        if path.name == "finra_ASTI_effective_20180701_20180731.json.metadata.json":
            meta["source_id"] = meta["document_id"] = "FINRA-OTCDAILYLIST-ASTI-20180723-127113"
        dump(path, meta)
    manifest = []
    for symbol in CASES:
        for path in sorted((ROOT / symbol).glob("*.metadata.json")):
            manifest.append(json.loads(path.read_text("utf-8")))
    dump(ROOT / "manifest.json", manifest)
    return manifest


def price_checks():
    windows = {"KEEL": ("2019-06-01", "2019-08-31", "2019-06-14"), "SKYX": ("2021-07-01", "2022-02-28", "2022-02-10"), "CAPS": ("2019-08-01", "2019-10-10", "2019-09-19"), "ASTI": ("2018-07-01", "2018-08-31", "2018-08-17")}
    result = {}
    for symbol, (start, end, jump) in windows.items():
        cached_path = ROOT / symbol / f"{symbol}_cached_TIME_SERIES_DAILY_ADJUSTED.json"
        fresh_path = ROOT / symbol / f"{symbol}_current_TIME_SERIES_DAILY.json"
        cached = json.loads(cached_path.read_text("utf-8"))["Time Series (Daily)"]
        fresh = json.loads(fresh_path.read_text("utf-8"))["Time Series (Daily)"]
        fields = ["1. open", "2. high", "3. low", "4. close"]
        mismatch = []
        common = sorted(set(cached) & set(fresh))
        for date in common:
            diffs = {field: [cached[date][field], fresh[date][field]] for field in fields if Decimal(cached[date][field]) != Decimal(fresh[date][field])}
            if Decimal(cached[date]["6. volume"]) != Decimal(fresh[date]["5. volume"]):
                diffs["raw_volume"] = [cached[date]["6. volume"], fresh[date]["5. volume"]]
            if diffs:
                mismatch.append({"date": date, "differences": diffs})
        rows = {date: {**{field: row[field] for field in fields}, "raw_volume": row["5. volume"]} for date, row in sorted(fresh.items()) if start <= date <= end}
        prior = max(date for date in fresh if date < jump and Decimal(fresh[date]["5. volume"]) > 0)
        window_mismatches = [row for row in mismatch if start <= row["date"] <= end]
        outside_rounding = [{"date": row["date"], "field": field, "values": pair} for row in mismatch for field, pair in row["differences"].items()
                            if field == "raw_volume" or abs(Decimal(pair[0]) - Decimal(pair[1])) > Decimal("0.0000500000001")]
        result[symbol] = {"window_start": start, "window_end": end, "observed_jump_date": jump,
                          "previous_positive_volume_date": prior,
                          "previous_positive_volume_close": fresh[prior]["4. close"],
                          "jump_close": fresh[jump]["4. close"],
                          "jump_volume": fresh[jump]["5. volume"],
                          "raw_close_ratio_observation_only": str(Decimal(fresh[jump]["4. close"]) / Decimal(fresh[prior]["4. close"])),
                          "ratio_is_not_split_evidence": True,
                          "cached_raw_source_sha256": digest(cached_path), "current_daily_source_sha256": digest(fresh_path),
                          "common_history_rows_compared": len(common), "all_common_history_raw_mismatch_count": len(mismatch),
                          "all_common_history_raw_mismatches": mismatch,
                          "window_raw_mismatch_count": len(window_mismatches), "window_raw_mismatches": window_mismatches,
                          "all_common_history_mismatches_outside_4_decimal_price_rounding": outside_rounding,
                          "rounding_comparison_note": "An absolute allowance 0.00005 plus 0.0000000001 handles DAILY four-decimal formatting; raw exact differences remain above. No volume tolerance.",
                          "jump_and_prior_close_match_exactly": all(Decimal(cached[d]["4. close"]) == Decimal(fresh[d]["4. close"]) for d in [prior, jump]),
                          "window_missing_from_current": [d for d in cached if start <= d <= end and d not in fresh],
                          "window_missing_from_cached": [d for d in fresh if start <= d <= end and d not in cached],
                          "current_raw_rows": rows,
                          "cached_split_coefficients_in_window": sorted({v["8. split coefficient"] for d, v in cached.items() if start <= d <= end})}
    result["ASTI"]["no_raw_rows_interval"] = {"start": "2018-07-23", "end": "2018-08-16", "rows": [d for d in result["ASTI"]["current_raw_rows"] if "2018-07-23" <= d <= "2018-08-16"]}
    result["CAPS"]["unresolved_price_unit_interval"] = {"start": "2019-09-10", "end": "2019-09-18", "raw_rows": {d: r for d, r in result["CAPS"]["current_raw_rows"].items() if "2019-09-10" <= d <= "2019-09-18"}}
    dump(ROOT / "alpha_daily_comparison.json", result)
    return result


def findings():
    asti = {
        "symbol": "ASTI", "cik": "1350102", "security_id": "SEC_US_ASTI", "share_class": "common",
        "classification": "missing_official_reverse_split_and_temporary_symbol_price_gap",
        "new_shares": "1", "old_shares": "1000", "new_to_old_exact": "1/1000",
        "legal_effective_date": "2018-07-20", "legal_effective_local_time": "17:00:00 Eastern Time",
        "first_split_adjusted_trading_date": "2018-07-23", "venue_at_action": "OTC Marketplace",
        "historical_symbol": "ASTI", "temporary_symbol": "ASTID", "post_split_cusip": "043635507",
        "original_symbol_restored_date": "2018-08-17", "observed_jump_date": "2018-08-17",
        "official_market_action_confirmed": True, "price_inference_used": False,
        "sources": [
            source("ASTI", "0001350102-18-000024_asti-form8xkxreversestocks.htm", ["actual_execution", "exact_ratio", "legal_effective_time"], ["at a ratio of one-for-one thousand", "became effective as of 5:00 p.m. Eastern Time on July 20, 2018"], location="Item 5.03 operative reverse split"),
            source("ASTI", "0001350102-18-000024_finalasticharteramendment-.htm", ["exact_ratio", "operative_charter"], ["each one thousand (1,000) shares of Common Stock issued and outstanding at such time shall be combined into one (1) share"], location="Amendment ARTICLE 4"),
            source("ASTI", "0001350102-19-000030_asti-20181231x10k.htm", ["actual_execution", "trading_date", "exact_ratio"], ["at a ratio of one-for-one thousand", "continued on the OTC Marketplace on a split-adjusted basis on July 23, 2018"], location="Note 15 STOCKHOLDERS EQUITY / Reverse Stock Split; later annual actual execution"),
            source("ASTI", "finra_ASTI_effective_20180701_20180731.json", ["exact_ratio", "trading_date", "market_effective_date_confirmation", "historical_security_identity"], ["1:1000", "2018-07-23 00:00:00.0", "ASTID"], 127113),
            source("ASTI", "finra_ASTID_effective_20180801_20180831.json", ["original_symbol_restoration", "no_additional_ratio_at_jump"], ["2018-08-17 00:00:00.0", "Symbol Change"], 129351),
        ],
        "limitations": [
            "The Alpha Vantage cached SPLITS snapshot and both cached/current daily series omit the July 2018 action; daily split coefficients in this window are all 1.",
            "Alpha Vantage has no rows from July 23 through August 16, 2018, the official temporary ASTID interval. The first returning ASTI row on August 17 is not a new split date.",
            "The 1/1000 unit adjustment is independently confirmed. Missing temporary-symbol prices and exact performance across that gap are not repaired or fully validated here.",
            "FINRA calendarDay 2018-10-25 is a migrated partition; using it as an action date or querying July partitions alone loses the action.",
            "The existing narrow number-word contract parser may not recognize 'one thousand'; these evidence files do not modify it."
        ]
    }
    caps = {
        "symbol": "CAPS", "cik": "887151", "security_id": "SEC_US_CAPS", "share_class": "common",
        "classification": "missing_official_reverse_split_with_unresolved_post_effective_vendor_units_and_CVR",
        "new_shares": "1", "old_shares": "1000", "new_to_old_exact": "1/1000",
        "legal_effective_date": "2019-08-31", "legal_effective_local_time": "00:01:00 Eastern Time",
        "first_split_adjusted_trading_date": "2019-09-10", "venue_at_action": "OTC",
        "historical_symbol": "CAPS", "temporary_symbol": "CAPSD", "original_symbol_restored_date": "2019-10-08",
        "observed_jump_date": "2019-09-19", "official_market_action_confirmed": True, "price_inference_used": False,
        "sources": [
            source("CAPS", "0001171843-19-005659_f8k_082619.htm", ["legal_effective_time", "same_common_share_action"], ["Effective Date (12:01AM Eastern August 31, 2019)", "to be combined into one share"], location="Item 5.03 legal scheduled effectiveness, not market ex-date"),
            source("CAPS", "0001171843-19-005659_exh_31.htm", ["exact_ratio", "operative_charter"], ["each 1,000 shares of Common Stock", "reclassified and combined into one validly issued, fully paid and non-assessable share"], location="ARTICLE FOURTH actual issued/outstanding common shares; fractional shares aggregated and sold for cash"),
            source("CAPS", "0001213900-25-026436_ea0235933-10k_capstone.htm", ["actual_execution", "exact_ratio", "historical_security_identity"], ["each 1,000 shares of common stock of the Company became 1 share of common stock"], location="Corporate History August 22, 2019 amendment; later annual execution confirmation"),
            source("CAPS", "finra_oldSymbolCode_CAPS_20190801_20191031.json", ["exact_ratio", "trading_date", "market_effective_date_confirmation", "historical_security_identity"], ["1:1000", "2019-09-10 00:00:00.0", "CAPSD"], 162116),
            source("CAPS", "finra_oldSymbolCode_CAPSD_20190901_20191031.json", ["original_symbol_restoration"], ["2019-10-08 00:00:00.0", "Symbol Change"], 164774),
            source("CAPS", "0001171843-19-005659_exh_101.htm", ["non_price_wealth_distribution", "CVR_record_date"], ["Shareholders of record on July 10, 2019", "DATED AS OF August 23, 2019"], location="LDI contingent value rights agreement; economic distribution separate from split"),
            source("CAPS", "0001213900-25-053782_ea0245522-424b4_capstone.htm", ["conflicting_historical_description"], ["1-for-750 reverse stock split", "each 1,000 shares of Common Stock of the Company became 1 share"], location="The same prospectus has both descriptions; the 2019 operative charter and independent FINRA action identify 1:1000")
        ],
        "conflicting_descriptions": [{"description": "1-for-750", "source_id": "0001213900-25-053782", "status": "not_selected_for_issued_share_unit_ratio", "reason": "Same prospectus also says each 1000 became 1; operative 2019 charter, later 10-K, and FINRA agree 1/1000. No issuer correction located."}],
        "separate_distribution": {"kind": "LipimetiX Development contingent value rights", "record_date": "2019-07-10", "agreement_effective_date": "2019-08-23", "cash_or_right_value": None, "included_in_split_ratio": False},
        "limitations": [
            "August 31 is the charter legal effective date. FINRA exDate September 10 is the independently confirmed market application date; September 19 is only the later observed Alpha price jump.",
            "Both cached and fresh Alpha daily retain close 0.013 and positive but tiny volume on September 10-18 despite the official September 10 action. The correct daily price unit in that interval is unresolved, so an exact split ratio alone does not fully repair these prices.",
            "The Alpha cached SPLITS response is empty; all daily split coefficients in the research window are 1.",
            "CVR value and cash from fractional-share liquidation are unmodeled. A split-only close cannot be certified as complete shareholder total wealth across this transaction.",
            "No share ratio, cash value, replacement price, or event date is inferred from the raw jump. No automatic price repair is proposed by this evidence package."
        ]
    }
    skyx = {
        "symbol": "SKYX", "cik": "1598981", "security_id": "SEC_US_SKYX", "share_class": "common",
        "classification": "same_issuer_common_stock_OTC_to_Nasdaq_IPO_with_unverified_tiny_pre_IPO_quotes",
        "new_to_old_exact": None, "split_event_to_add": None, "observed_jump_date": "2022-02-10",
        "market_transition_date": "2022-02-10", "historical_symbol": "SQFL", "new_market_symbol": "SKYX",
        "ipo_offer_price_USD": "14.00", "ipo_shares_offered": "1650000", "price_inference_used": False,
        "sources": [
            source("SKYX", "0000721748-17-000616_sqlposam083117.htm", ["historical_security_identity", "OTC_common_stock_market", "thin_market_context"], ["quoted on the OTC Pink marketplace under the symbol “SQFL”", "a liquid public market has not yet developed"], location="Market for Common Stock and Determination of Offering Price"),
            source("SKYX", "0001493152-22-003810_form-424b4.htm", ["IPO", "no_established_prior_public_market", "offer_price"], ["no established public market for our common stock", "initial public offering price is $14.00 per share"], location="Prospectus cover; generic equity-plan adjustments are not executed split evidence"),
            source("SKYX", "0001493152-22-004370_ex99-1.htm", ["actual_trading_commencement", "market_transition_date", "historical_security_identity"], ["common stock began trading on the Nasdaq stock market on February 10, 2022", "ticker symbol “SKYX”"], location="February 14 closing press release"),
            source("SKYX", "finra_oldSymbolCode_SQFL_20220101_20220331.json", ["market_transition_date", "OTC_deletion_for_Nasdaq_move"], ["2022-02-10 04:47:15.0", "Market Center Change Listed on NASDAQ"], 227373),
            source("SKYX", "finra_oldSymbolCode_SQFL_20220101_20220331.json", ["prior_market_move_postponed"], ["Market Move postponed."], 227222)
        ],
        "limitations": [
            "The predecessor OTC SQFL record and issuer filings establish corporate/common-class continuity. They do not prove every Alpha backfilled historical quote was an executable price or establish an unrelated-issuer ticker collision.",
            "The reviewed IPO prospectus, closing release, earlier registration, and FINRA market move contain no executed pre-IPO common-stock split. This bounded negative finding is not a proof that no historical unit change ever occurred.",
            "Alpha shows 0.001 with 30 shares on January 25, 2022, then February 10 Nasdaq IPO day close 11.85 with 1,099,242 shares. The raw jump must not be translated into an inferred split or assumed risk-free tradable gain.",
            "The February 8 FINRA move was postponed and reinstated; actual official move and issuer trading commencement agree on February 10.",
            "No episode boundary, return clipping, or production eligibility override was invented or applied."
        ]
    }
    keel = {
        "symbol": "KEEL", "cik": "1812477", "security_id": "SEC_US_KEEL", "share_class": "current common; predecessor Israel ordinary shares",
        "classification": "one_for_one_issuer_succession_with_unverified_minimum_OTC_print",
        "split_event_to_add": None, "new_to_old_exact": None, "observed_jump_date": "2019-06-14", "price_inference_used": False,
        "identified_non_split_actions": [{"kind": "Israeli issuer shares exchanged for Canadian issuer shares", "actual_arrangement_date": "2019-06-12", "new_shares": "1", "old_shares": "1", "old_historical_symbol": "BLLCF", "new_historical_symbol": "BFARF", "old_OTC_deletion_exDate": "2019-06-18", "new_OTC_addition_exDate": "2019-08-15"}, {"kind": "Canada to US redomiciliation into Keel successor", "actual_date": "2026-04-01", "new_shares": "1", "old_shares": "1"}],
        "sources": [
            source("KEEL", "0001213900-21-023256_ea139842ex99-123_bitfarms.htm", ["actual_arrangement", "exact_exchange_ratio", "historical_security_identity"], ["Arrangement with Bitfarms Israel On June 12, 2019", "one Bitfarms Israel Share for one Bitfarms Canada Share basis"], location="General Development of Business / Arrangement with Bitfarms Israel"),
            source("KEEL", "0001213900-21-023256_ea139842ex99-101_bitfarms.htm", ["OTC_price_table_scope", "historical_security_identity"], ["OTCBB under the symbol “BFARF.”", "from August 16, 2019 to March 5, 2021"], location="Trading Price and Volume table scope; not proof of no predecessor trading"),
            source("KEEL", "finra_oldSymbolCode_BLLCF_20190101_20191231.json", ["official_predecessor_security_deletion", "actual_arrangement", "exact_exchange_ratio"], ["one Bitfarms Israel Share for one Bitfarms Canada Share", "6/12/19"], 155938),
            source("KEEL", "finra_oldSymbolCode_BFARF_20190101_20191231.json", ["official_successor_OTC_addition"], ["2019-08-15 11:33:36.0", "Bitfarms Ltd Common Shares (Canada)"], 160225),
            source("KEEL", "0001812477-26-000023_keel-20260630.htm", ["current_security_identity", "later_one_for_one_successor_exchange"], ["April 1, 2026", "one share of common stock of Keel per common share of Bitfarms"], location="Note 1 organization and U.S. redomiciliation")
        ],
        "limitations": [
            "The 2019 official action is an issuer/security exchange at 1:1, not a 20,000-for-1 share split. The current KEEL series demonstrably extends through predecessors; calling it unrelated ticker reuse is not supported.",
            "The Alpha June 13, 2019 row has all OHLC 0.0001 and volume 441 between June 12 close 3 and June 14 close 2. A contemporaneous consolidated trade audit or issuer-specific official price correction was not located, so actual tiny OTC execution versus vendor bad print remains unknown.",
            "FINRA old-security deletion June 18, arrangement effectiveness June 12, and new OTC addition August 15 are different dates. The precise venue/security identity underlying each intervening Alpha row is not certified.",
            "The issuer BFARF table beginning August 16 does not establish that predecessor BLLCF could not trade earlier.",
            "No ratio or price episode was inferred from the 20,000x raw observation, and no Alpha row was replaced."
        ]
    }
    result = {"scope": "Exactly KEEL 2019-06-14, SKYX 2022-02-10, CAPS 2019-09-19, ASTI 2018-08-17. Read-only research; no core, ledger, production, or price-panel edits.",
              "researched_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "events": [keel, skyx, caps, asti], "price_provider": "Alpha Vantage only", "production_installation_performed": False}
    dump(ROOT / "expected_parser_fields.json", result)
    return result


def validate(manifest, expected, prices):
    errors = []
    seen = set()
    for entry in manifest:
        path = Path(entry["path"])
        if digest(path) != entry["source_sha256"]:
            errors.append("hash:" + str(path))
        if entry["document_id"] in seen:
            errors.append("duplicate_document_id:" + entry["document_id"])
        seen.add(entry["document_id"])
        for key in ["source_url", "source_id", "security_id", "cik"]:
            if not entry.get(key):
                errors.append("missing_" + key + ":" + str(path))
        if entry["provider"] == "EDGAR":
            index = entry["filing_date_evidence"]
            if digest(Path(index["path"])) != index["sha256"]:
                errors.append("index_hash:" + str(path))
        if entry["provider"] == "FINRA" and not entry.get("request_body"):
            errors.append("finra_query_missing:" + str(path))
    marker_count = 0
    for event in expected["events"]:
        for evidence in event["sources"]:
            path = Path(evidence["local_path"])
            text = normalized(path)
            for marker in evidence["markers"]:
                marker_count += 1
                if marker not in text:
                    errors.append("marker:" + path.name + ":" + marker)
            if evidence["provider"] == "FINRA":
                record = evidence["finra_record_fields"]
                if record["dailyListDatetime"][:10] != evidence["published_date"]:
                    errors.append("finra_publication:" + str(path))
                if "exact_ratio" in evidence["roles"]:
                    a, b = record["reverseSplitRate"].split(":")
                    if Decimal(a) / Decimal(b) != Decimal(event["new_shares"]) / Decimal(event["old_shares"]):
                        errors.append("finra_ratio:" + str(path))
                    if record["exDate"][:10] != event["first_split_adjusted_trading_date"]:
                        errors.append("finra_exDate:" + str(path))
                    if "cancel" in (record["commentText"] or "").lower():
                        errors.append("cancelled_finra_action:" + str(path))
    validation = {"source_count": len(manifest), "provider_counts": dict(Counter(e["provider"] for e in manifest)),
                  "unique_SEC_filing_indexes": len({e["source_id"] for e in manifest if e["provider"] == "EDGAR"}),
                  "literal_marker_count": marker_count, "sha256_and_marker_validation_passed": not errors,
                  "exact_reverse_split_FINRA_ratios_validated": 2,
                  "current_vs_cached_daily_raw_mismatches": {s: p["all_common_history_raw_mismatch_count"] for s, p in prices.items()},
                  "current_vs_cached_outside_daily_4_decimal_rounding": {s: len(p["all_common_history_mismatches_outside_4_decimal_price_rounding"]) for s, p in prices.items()},
                  "observed_jump_and_prior_close_exactly_agree": all(p["jump_and_prior_close_match_exactly"] for p in prices.values()),
                  "errors": errors, "no_price_or_ledger_mutations": True,
                  "limitations": "Evidence integrity and current-vs-cached Alpha agreement do not certify every quote as an executable market price or validate total-wealth returns."}
    dump(ROOT / "validation.json", validation)
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    validate(prepare_metadata(), findings(), price_checks())
