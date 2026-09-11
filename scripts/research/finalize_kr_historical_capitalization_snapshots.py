"""Audit a complete native-year snapshot scope and export verified user data."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_frame, export_json
from engine.core.source_storage import sha256_file
from publish_kr_survivorship_capital_factors import latest_nullable, compare_nullable
from validate_kr_survivorship_financial_factors import canonical


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-publication", type=Path, required=True)
    parser.add_argument("--consumer-verification", type=Path, required=True)
    parser.add_argument("--day-red", type=Path, required=True)
    parser.add_argument("--day-green", type=Path, required=True)
    parser.add_argument("--input-gold-summary", type=Path, required=True)
    args = parser.parse_args()
    folder = args.snapshot_publication.resolve().parent
    output = folder / "closeout"
    if not output.is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Audit output must remain in Silver")
    if not args.input_gold_summary.resolve().is_relative_to((DATA_LAKE.root / "gold").resolve()):
        raise ValueError("Consumer input summary must remain in Gold")
    output.mkdir(exist_ok=False)
    evidence, prepared_exports = {}, []

    def check(path, digest=None):
        path = Path(path).resolve()
        actual = sha256_file(path)
        if digest is not None and digest != actual:
            raise ValueError(f"Evidence changed: {path}")
        evidence[str(path)] = actual
        return actual

    def read(path, digest=None):
        check(path, digest)
        return json.loads(Path(path).read_text("utf-8"))

    publication = read(args.snapshot_publication)
    consumer = read(args.consumer_verification)
    if (publication["status"] != "published_and_verified" or not publication["snapshots_published"]
            or publication["source_scope"] != "all" or consumer["status"] != "verified"):
        raise ValueError("Complete native-year publication and real PIT consumer validation are required")
    check(args.snapshot_publication, consumer["snapshot_publication_sha256"])
    for path, digest in publication["dependencies"].items():
        if digest is None:
            absent = (ROOT / path).resolve()
            if absent.exists():
                raise ValueError(f"A pinned absent input appeared: {absent}")
            evidence[str(absent)] = None
        else:
            check(ROOT / path, digest)
    native = read(consumer["native_verification_path"], consumer["native_verification_sha256"])
    if consumer["annual_mcap_verified_days"] != native["verified_days"]:
        raise ValueError("PIT validation does not cover every audited native trading day")
    check(DATA_LAKE.silver("survivorship", "kr", "listing_episodes.json"), consumer["listing_episodes_sha256"])
    for item in consumer["checks"]:
        for name, digest in item["hashes"].items():
            check(args.consumer_verification.parent / item["folder"] / name, digest)
    for path in (args.day_red, args.day_green):
        day = read(path)
        for name, digest in day["hashes"].items():
            check(path.parent / name, digest)
    red, green = read(args.day_red), read(args.day_green)
    if red["status"] != "failed" or green["status"] != "passed" or red["day"] != green["day"]:
        raise ValueError("The same real consumer day must fail before and pass after publication")
    if red["implementation_sha256"] != green["implementation_sha256"]:
        raise ValueError("The public day regression changed between failure and success")

    calendars = []
    for calendar_folder in ("market_calendar", "market_calendar_after"):
        summary = read(folder / calendar_folder / "summary.json")
        frames = []
        for item in summary["years"]:
            path = folder / calendar_folder / f"{item['year']}.parquet"
            check(path, item["sha256"])
            frames.append(pd.read_parquet(path))
        calendars.append(pd.DatetimeIndex(pd.to_datetime(pd.concat(frames).trade_date).unique()).sort_values())
    pd.testing.assert_index_equal(*calendars)
    market_dates = calendars[0]
    for boundary_folder in ("subsequent_events", "subsequent_events_after"):
        summary = read(folder / boundary_folder / "summary.json")
        check(folder / boundary_folder / "first_subsequent.parquet", summary["first_subsequent_sha256"])
        for item in summary["months"]:
            check(folder / boundary_folder / f"{item['month']}.parquet", item["sha256"])
        for path in (folder / boundary_folder).glob("*_market.parquet"):
            check(path)
    pd.testing.assert_frame_equal(pd.read_parquet(folder / "subsequent_events/first_subsequent.parquet"),
        pd.read_parquet(folder / "subsequent_events_after/first_subsequent.parquet"))

    for month in publication["months"]:
        if month["status"] == "no_affected_calendar_dates":
            continue
        if month["status"] != "published_and_verified":
            raise ValueError("A snapshot month is incomplete")
        month_folder = folder / month["month"]
        for name, digest in month["hashes"].items():
            check(month_folder / name, digest)
        expected = pd.read_parquet(month_folder / "expected_asof.parquet")
        latest, ambiguous = latest_nullable(pd.read_parquet(month_folder / "snapshot_after.parquet"), snapshot=True)
        if not ambiguous.empty:
            raise ValueError("Snapshot readback has ambiguous latest versions")
        actual = canonical(latest).loc[canonical(expected).index].reset_index()
        compare_nullable(actual, expected, snapshot=True)
        if len(actual) != month["rows"]:
            raise ValueError("Published monthly row count changed")
        if not pd.to_datetime(actual.source_trade_date).le(pd.to_datetime(actual.trade_date)).all():
            raise ValueError("A snapshot uses a future source observation")
        actual["is_market_trading_date"] = pd.to_datetime(actual.trade_date).isin(market_dates)
        stage_path = output / "staged_exports" / f"{month['month']}.parquet"
        export_frame(stage_path, actual)
        prepared_exports.append(dict(month=month["month"], path=stage_path, rows=len(actual),
            market_trading_date_rows=int(actual.is_market_trading_date.sum()), sha256=check(stage_path)))
    if sum(r["rows"] for r in prepared_exports) != publication["verified_rows"]:
        raise ValueError("Consumer exports do not cover the verified snapshot scope")

    years = publication["source_years"]
    scope = "_".join(map(str, years))
    if len(years) > 1 and years == list(range(years[0], years[-1] + 1)):
        scope = f"{years[0]}_{years[-1]}"
    gold = DATA_LAKE.gold("survivorship", "kr", "capitalization_factors", scope)
    summaries = {"snapshot":gold / "snapshot_summary.json", "native":gold / "summary.json",
        "input":args.input_gold_summary,
        "coverage":DATA_LAKE.gold("survivorship", "kr", "capitalization_coverage", "20260911", scope, "summary.json")}
    originals = {}
    for name, path in summaries.items():
        if path.exists():
            backup = output / f"gold_{name}_summary_before.json"
            shutil.copy2(path, backup)
            originals[name] = read(backup)
    for item in prepared_exports:
        target = gold / "snapshots" / f"{item['month']}.parquet"
        if target.exists():
            raise ValueError("Refusing to replace an existing snapshot export without a distinct run scope")
    check(__file__)
    shutil.copy2(__file__, output / Path(__file__).name)
    audit = dict(status="native_and_snapshots_and_factorlab_verified", source_years=years,
        checked_at=datetime.now(timezone.utc).isoformat(), snapshots_published=True, coverage_complete=False,
        snapshot_rows=publication["verified_rows"], new_keys=publication["new_keys"], revised_keys=publication["revised_keys"],
        market_trading_date_rows=sum(r["market_trading_date_rows"] for r in prepared_exports),
        annual_mcap_verified_days=consumer["annual_mcap_verified_days"], additional_basis_factor_checks=consumer["additional_basis_factor_checks"],
        cutoff=publication["cutoff"], last_market_calendar_date=market_dates.max().date().isoformat(), evidence_sha256=evidence,
        policy="Each exported date is an as-of snapshot, with its original source date and explicit market-trading-date flag. Extra legacy snapshot dates are not new price observations. Full financial-factor and historical issuer coverage remain incomplete.")
    export_json(output / "audit.json", audit)
    artifacts = {item["month"]:export_frame(gold / "snapshots" / f"{item['month']}.parquet", pd.read_parquet(item["path"]))
        for item in prepared_exports}
    for item in prepared_exports:
        pd.testing.assert_frame_equal(pd.read_parquet(artifacts[item["month"]]["path"]),
            pd.read_parquet(item["path"]), check_exact=True)
    serving_path = output / "serving_artifacts.json"
    export_json(serving_path, dict(status="gold_matches_verified_staging", artifacts=artifacts))
    completion = {key:value for key,value in audit.items() if key != "evidence_sha256"}
    completion.update(publication_path=str(args.snapshot_publication.resolve()), publication_sha256=sha256_file(args.snapshot_publication),
        consumer_verification_path=str(args.consumer_verification.resolve()), consumer_verification_sha256=sha256_file(args.consumer_verification),
        closeout_path=str(output / "audit.json"), closeout_sha256=sha256_file(output / "audit.json"), artifacts=artifacts,
        serving_export_path=str(serving_path), serving_export_sha256=sha256_file(serving_path))
    export_json(summaries["snapshot"], {**originals["snapshot"], **completion})
    originals["native"].update(status=audit["status"], snapshots_published=True, snapshot_completion=completion)
    export_json(summaries["native"], originals["native"])
    current = originals["input"].setdefault("market_factor_scopes", {}).setdefault(scope, {})
    current.update(status=audit["status"], snapshots_restored=True, snapshot_completion=completion)
    export_json(summaries["input"], originals["input"])
    if "coverage" in originals:
        originals["coverage"].update(status=audit["status"], snapshots_restored=True, snapshot_completion=completion)
        export_json(summaries["coverage"], originals["coverage"])
    print(json.dumps({key:audit[key] for key in ("status","snapshot_rows","new_keys","revised_keys","annual_mcap_verified_days")}), flush=True)


if __name__ == "__main__":
    main()
