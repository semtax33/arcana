"""Add verified historical market-cap/share factor keys, preserving existing rows.

Each month is backed up, tested in actual ClickHouse, inserted and fully read
back. No financial metadata is invented for pure market observations; existing
metadata and revision timestamps are preserved. Full factor rebuilds stay pending.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_frame, export_json
from engine.core.source_storage import SourceRefreshLock, sha256_file
from engine.loaders.factors import _insert_daily_factor_rows_by_partition
from publish_kr_survivorship_capital_factors import latest_nullable, compare_nullable, clickhouse_rows, COLUMNS
from validate_kr_survivorship_financial_factors import canonical

SQL = "SELECT " + ",".join(COLUMNS) + """ FROM fact_daily_factors
    WHERE trade_date >= {start:Date} AND trade_date < {end:Date}
    AND factor_id IN ('mcap_mil','csho') AND financial_basis IN ('annual','quarterly','ttm')
    AND startsWith(security_id,'SEC_KR_') SETTINGS max_execution_time=25,max_threads=2"""


def capture(client, month, path, *, end_date=None):
    stop = (month+1).start_time.date()
    if end_date is not None:
        stop = min(stop, (pd.Timestamp(end_date) + pd.Timedelta(days=1)).date())
    rows = client.query_df(SQL, parameters=dict(start=month.start_time.date(), end=stop))
    if rows.empty and not len(rows.columns):
        rows = pd.DataFrame(columns=COLUMNS)
    rows.to_parquet(path, index=False)
    latest, ambiguous = latest_nullable(rows)
    if not ambiguous.empty:
        raise ValueError("Native latest version is ambiguous")
    return latest


def compare_complete(actual, expected):
    compare_nullable(actual, expected)
    a, e = canonical(actual), canonical(expected)
    if not pd.to_datetime(a.updated_at, utc=True).eq(pd.to_datetime(e.updated_at, utc=True)).all():
        raise ValueError("Native revision timestamps differ from expected")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--review", type=Path, help="Exact verified dispositions for differing values and preserved unpriced observations")
    parser.add_argument("--stage-only", action="store_true", help="Verify every month in isolated ClickHouse staging without production inserts")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Publication backups and audits must be in silver")
    args.output.mkdir(parents=True, exist_ok=False)
    summary_path = args.preparation / "summary.json"
    preparation = json.loads(summary_path.read_text("utf-8"))
    source_years = sorted(preparation.get("selected_years", [2013, 2014, 2015]))
    cutoff = preparation.get("end_date") if args.review else None
    expected_months = [f"{year}-{month:02d}" for year in source_years for month in range(1, 13)
        if cutoff is None or f"{year}-{month:02d}" <= cutoff[:7]]
    review, dispositions = None, {}
    if args.review:
        review = json.loads(args.review.read_text("utf-8"))
        if (review["status"] != "native_dispositions_verified"
                or review["preparation_sha256"] != sha256_file(summary_path)
                or not cutoff or preparation.get("requested_months") != expected_months):
            raise ValueError("Review does not cover this exact source preparation")
        for name, artifact in review["artifacts"].items():
            path = Path(artifact["path"])
            if sha256_file(path) != artifact["sha256"]:
                raise ValueError("Review artifact changed")
            dispositions[name] = pd.read_parquet(path)
            if len(dispositions[name]) != artifact["rows"]:
                raise ValueError("Review row count differs")
        for name, key in (("corrections", "differing_native_values"),
                          ("preserved_unpriced_native", "native_outside_prepared"),
                          ("excluded_price_observations", "price_without_source")):
            if len(dispositions[name]) != preparation["totals"][key]:
                raise ValueError("Review does not account for every discrepancy")
    allowed_status = {"prepared_native_publication_pending"}
    if review:
        allowed_status.add("prepared_discrepancies_require_review")
    if (preparation["status"] not in allowed_status or not source_years
            or [row["month"] for row in preparation["months"]] != expected_months):
        raise ValueError("Complete discrepancy-free preparation is required")
    checks = ["source_without_price", "price_discrepancies", "price_ambiguities", "native_ambiguities"]
    if not review:
        checks += ["price_without_source", "differing_native_values", "native_outside_prepared"]
    if any(preparation["totals"][key] for key in checks):
        raise ValueError("Unresolved source/native observations")
    dependency_paths = {summary_path:sha256_file(summary_path), **{Path(p):h for p,h in preparation["implementation_sha256"].items()}}
    dependency_paths.update({Path(p):h for p,h in preparation.get("source_audits_sha256", {}).items()})
    dependency_paths[Path(preparation["share_publication_path"])] = preparation["share_publication_sha256"]
    default_input = DATA_LAKE.silver("krx", "shares", "kr_normalized_shares.csv")
    registration = DATA_LAKE.silver("krx", "shares", "historical_sources.json")
    dependency_paths[default_input] = preparation["default_input_sha256"]
    dependency_paths[registration] = preparation["registration_sha256"]
    if review:
        dependency_paths[args.review.resolve()] = sha256_file(args.review)
        dependency_paths.update({Path(p): h for p, h in review["input_hashes"].items()})
        dependency_paths.update({Path(item["path"]): item["sha256"] for item in review["artifacts"].values()})
    for path in [Path(__file__), ROOT / "scripts/research/validate_kr_survivorship_financial_factors.py"]:
        dependency_paths[path] = sha256_file(path)
    def verify_dependencies():
        for path, expected in dependency_paths.items():
            if (sha256_file(path) if path.exists() else None) != expected:
                raise ValueError(f"Publication dependency changed: {path.name}")
        for source in json.loads(registration.read_text("utf-8"))["sources"]:
            if sha256_file(DATA_LAKE.root / source["path"]) != source["sha256"]:
                raise ValueError("Registered original changed")
    records = []
    report = dict(status="starting", native_published=False, snapshots_published=False, coverage_complete=False,
        preparation=str(args.preparation.resolve()), preparation_sha256=sha256_file(summary_path),
        source_years=source_years, factor_ids=["mcap_mil","csho"], financial_bases=["annual","quarterly","ttm"],
        dependencies={str(path):h for path,h in dependency_paths.items()}, months=records, inserted_rows=0,
        added_rows=0, revised_rows=0, end_date=cutoff, stage_only=args.stage_only,
        review_path=str(args.review.resolve()) if review else None,
        policy=(review["policy"] if review else "Add absent keys only. Existing value, fiscal metadata and revision timestamps are preserved. Quote observations do not grant new common-stock listing eligibility."))
    report_path = args.output / "publication.json"
    export_json(report_path, report)
    (args.output / "native.sql").write_text(SQL,"utf-8")
    implementation = args.output / "implementation"
    implementation.mkdir()
    for path in dependency_paths:
        if path.suffix == ".py":
            shutil.copy2(path, implementation / path.name)
    database = "arcana_historical_cap_stage_" + uuid4().hex
    created = False
    with SourceRefreshLock("kr"):
        client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=35)
        try:
            verify_dependencies()
            client.command(f"CREATE DATABASE {database}")
            created = True
            client.command(f"""CREATE TABLE {database}.source (
                security_id String, trade_date Date, financial_basis String, factor_id String,
                factor_value Nullable(Float64), fiscal_year Nullable(UInt16), financial_period Nullable(Date),
                currency String, updated_at DateTime64(3,'Asia/Seoul')) ENGINE=Memory""")
            for prepared_month in preparation["months"]:
                month = pd.Period(prepared_month["month"], freq="M")
                source_folder = args.preparation / str(month)
                folder = args.output / str(month)
                folder.mkdir()
                for name, expected_hash in prepared_month["hashes"].items():
                    if sha256_file(source_folder / name) != expected_hash:
                        raise ValueError("Monthly preparation evidence changed")
                prepared = pd.read_parquet(source_folder / "prepared.parquet")
                before = capture(client, month, folder / "native_before.parquet", end_date=cutoff)
                a, e = canonical(before), canonical(prepared)
                outside = a.index.difference(e.index)
                if len(outside) and not review:
                    raise ValueError("Native keys outside prepared scope require review")
                overlap = a.index.intersection(e.index)
                same = np.isclose(a.loc[overlap, "factor_value"].to_numpy(float), e.loc[overlap,"factor_value"].to_numpy(float),
                    rtol=1e-10, atol=1e-8, equal_nan=True)
                revised = overlap[~same]
                if len(revised) and not review:
                    raise ValueError("Existing native values differ from audited observations")
                if review:
                    audited_before, ambiguous = latest_nullable(pd.read_parquet(source_folder / "native_before.parquet"))
                    if not ambiguous.empty:
                        raise ValueError("Reviewed native preimage is ambiguous")
                    compare_complete(before, audited_before)
                    outside_proof = dispositions["preserved_unpriced_native"]
                    outside_proof = outside_proof.loc[pd.to_datetime(outside_proof.trade_date).dt.to_period("M").eq(month)]
                    compare_complete(a.loc[outside].reset_index(), outside_proof)
                    proof = dispositions["corrections"]
                    proof = proof.loc[pd.to_datetime(proof.trade_date).dt.to_period("M").eq(month)]
                    compare_complete(a.loc[revised].reset_index(), proof[COLUMNS])
                    if len(proof):
                        values = canonical(proof.rename(columns={"factor_value": "old_factor_value", "factor_value_expected": "factor_value"}))
                        if not np.isclose(e.loc[values.index, "factor_value"].to_numpy(float), values.factor_value.to_numpy(float), rtol=1e-12, atol=1e-8).all():
                            raise ValueError("Reviewed corrections differ from prepared values")
                new_keys = e.index.difference(a.index)
                delta = e.loc[new_keys].reset_index()[COLUMNS]
                if len(revised):
                    correction = a.loc[revised].copy()
                    correction["factor_value"] = e.loc[revised, "factor_value"]
                    delta = pd.concat([delta, correction.reset_index()[COLUMNS]], ignore_index=True)
                wall_clock = pd.Timestamp.now(tz="Asia/Seoul")
                version = wall_clock.ceil("ms")
                if len(before):
                    version = max(version, pd.to_datetime(before.updated_at, utc=True).max().tz_convert("Asia/Seoul") + pd.Timedelta(milliseconds=1))
                delta["updated_at"] = version
                delta = clickhouse_rows(delta)
                # All earlier native metadata remains authoritative for matching keys.
                unchanged = a.index.difference(revised)
                expected = pd.concat([clickhouse_rows(a.loc[unchanged].reset_index()), delta], ignore_index=True)[COLUMNS]
                delta.to_parquet(folder / "insert_delta.parquet", index=False)
                expected.to_parquet(folder / "expected.parquet", index=False)
                client.command(f"TRUNCATE TABLE {database}.source")
                client.insert_df(f"{database}.source", expected, column_names=COLUMNS)
                staged = client.query_df(f"SELECT * FROM {database}.source")
                compare_complete(staged, expected)
                record = dict(month=str(month), status="staged_verified", existing_rows=len(before), new_rows=len(delta),
                    added_rows=len(new_keys), revised_rows=len(revised), preserved_unpriced_rows=len(outside),
                    verified_rows=len(expected), publication_wall_clock=wall_clock.isoformat())
                records.append(record)
                export_json(report_path, report)
                if args.stage_only:
                    record.update(status="native_stage_verified", hashes={path.name:sha256_file(path) for path in folder.glob("*.parquet")})
                    export_json(folder / "summary.json", record)
                    export_json(report_path, report)
                    print({k:v for k,v in record.items() if k!='hashes'}, flush=True)
                    continue
                if len(delta):
                    inserted = _insert_daily_factor_rows_by_partition(client, delta)
                    if inserted != len(delta):
                        raise RuntimeError("Native insert count mismatch")
                    report["inserted_rows"] += inserted
                    report["added_rows"] += len(new_keys)
                    report["revised_rows"] += len(revised)
                record["status"] = "readback_pending"
                export_json(report_path, report)
                after = capture(client, month, folder / "native_after.parquet", end_date=cutoff)
                compare_complete(after, expected)
                compare_complete(canonical(after).loc[unchanged].reset_index(), a.loc[unchanged].reset_index())
                record.update(status="native_published_and_verified", hashes={path.name:sha256_file(path) for path in folder.glob("*.parquet")})
                export_json(folder / "summary.json", record)
                export_json(report_path, report)
                print({k:v for k,v in record.items() if k!='hashes'}, flush=True)
            verify_dependencies()
            report.update(status="native_staged_verified_not_published" if args.stage_only else "native_published_and_verified_snapshots_pending", native_published=not args.stage_only,
                verified_rows=sum(row["verified_rows"] for row in records), unchanged_existing_rows=sum(row["existing_rows"]-row["revised_rows"] for row in records),
                candidate_added_rows=sum(row["added_rows"] for row in records), candidate_revised_rows=sum(row["revised_rows"] for row in records),
                finished_at=datetime.now(timezone.utc).isoformat())
            export_json(report_path, report)
        except Exception as error:
            report.update(status="failed_check_month_checkpoints", error=str(error))
            export_json(report_path, report)
            raise
        finally:
            if created:
                client.command(f"DROP DATABASE {database}")
            client.close()
    if args.stage_only:
        print(report["status"], report["verified_rows"], flush=True)
        return
    scope_name = "_".join(map(str, source_years))
    if source_years == list(range(source_years[0], source_years[-1] + 1)) and len(source_years) > 1:
        scope_name = f"{source_years[0]}_{source_years[-1]}"
    gold = DATA_LAKE.gold("survivorship","kr","capitalization_factors",scope_name)
    artifacts = {row["month"]:export_frame(gold / f"{row['month']}.parquet",
        pd.read_parquet(args.output / row["month"] / "expected.parquet")) for row in records}
    export_json(gold / "summary.json", dict(status=report["status"], native_published=True, snapshots_published=False,
        publication_path=str(report_path.resolve()), publication_sha256=sha256_file(report_path),
        inserted_rows=report["inserted_rows"], added_rows=report["added_rows"], revised_rows=report["revised_rows"],
        review_path=report["review_path"], end_date=cutoff, verified_rows=report["verified_rows"], artifacts=artifacts,
        source_years=source_years, unchanged_existing_rows=report["unchanged_existing_rows"], coverage_complete=False))
    coverage = DATA_LAKE.gold("survivorship","kr","capitalization_coverage","20260911","summary.json")
    if source_years != [2013, 2014, 2015]:
        coverage = coverage.parent / scope_name / "summary.json"
    if coverage.exists():
        shutil.copy2(coverage, args.output / "gold_coverage_before.json")
        current = json.loads(coverage.read_text("utf-8"))
    else:
        current = dict(source_years=source_years, coverage_complete=False)
    current.update(status=report["status"],native_restored=True,snapshots_restored=False,
        native_publication_path=str(report_path.resolve()),native_publication_sha256=sha256_file(report_path),
        native_added_rows=report["added_rows"],native_revised_rows=report["revised_rows"],native_verified_rows=report["verified_rows"])
    export_json(coverage,current)
    print(report["status"], report["inserted_rows"], report["verified_rows"], flush=True)


if __name__ == "__main__":
    main()
