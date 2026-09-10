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


def capture(client, month, path):
    rows = client.query_df(SQL, parameters=dict(start=month.start_time.date(), end=(month+1).start_time.date()))
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
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Publication backups and audits must be in silver")
    args.output.mkdir(parents=True, exist_ok=False)
    summary_path = args.preparation / "summary.json"
    preparation = json.loads(summary_path.read_text("utf-8"))
    source_years = sorted(preparation.get("selected_years", [2013, 2014, 2015]))
    expected_months = [f"{year}-{month:02d}" for year in source_years for month in range(1, 13)]
    if (preparation["status"] != "prepared_native_publication_pending" or not source_years
            or [row["month"] for row in preparation["months"]] != expected_months):
        raise ValueError("Complete discrepancy-free preparation is required")
    checks = ["source_without_price", "price_without_source", "price_discrepancies", "price_ambiguities",
        "native_ambiguities", "differing_native_values", "native_outside_prepared"]
    if any(preparation["totals"][key] for key in checks):
        raise ValueError("Unresolved source/native observations")
    dependency_paths = {summary_path:sha256_file(summary_path), **{Path(p):h for p,h in preparation["implementation_sha256"].items()}}
    dependency_paths.update({Path(p):h for p,h in preparation.get("source_audits_sha256", {}).items()})
    dependency_paths[Path(preparation["share_publication_path"])] = preparation["share_publication_sha256"]
    default_input = DATA_LAKE.silver("krx", "shares", "kr_normalized_shares.csv")
    registration = DATA_LAKE.silver("krx", "shares", "historical_sources.json")
    dependency_paths[default_input] = preparation["default_input_sha256"]
    dependency_paths[registration] = preparation["registration_sha256"]
    for path in [Path(__file__), ROOT / "scripts/research/validate_kr_survivorship_financial_factors.py"]:
        dependency_paths[path] = sha256_file(path)
    def verify_dependencies():
        for path, expected in dependency_paths.items():
            if sha256_file(path) != expected:
                raise ValueError(f"Publication dependency changed: {path.name}")
        for source in json.loads(registration.read_text("utf-8"))["sources"]:
            if sha256_file(DATA_LAKE.root / source["path"]) != source["sha256"]:
                raise ValueError("Registered original changed")
    records = []
    report = dict(status="starting", native_published=False, snapshots_published=False, coverage_complete=False,
        preparation=str(args.preparation.resolve()), preparation_sha256=sha256_file(summary_path),
        source_years=source_years, factor_ids=["mcap_mil","csho"], financial_bases=["annual","quarterly","ttm"],
        dependencies={str(path):h for path,h in dependency_paths.items()}, months=records, inserted_rows=0,
        policy="Add absent keys only. Existing value, fiscal metadata and revision timestamps are preserved. Quote observations do not grant new common-stock listing eligibility.")
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
                before = capture(client, month, folder / "native_before.parquet")
                a, e = canonical(before), canonical(prepared)
                if len(a.index.difference(e.index)):
                    raise ValueError("Native keys outside prepared scope require review")
                if not np.isclose(a.factor_value.to_numpy(float), e.loc[a.index,"factor_value"].to_numpy(float),
                    rtol=1e-10, atol=1e-8).all():
                    raise ValueError("Existing native values differ from audited observations")
                new_keys = e.index.difference(a.index)
                delta = e.loc[new_keys].reset_index()[COLUMNS]
                wall_clock = pd.Timestamp.now(tz="Asia/Seoul")
                delta["updated_at"] = wall_clock.ceil("ms")
                delta = clickhouse_rows(delta)
                # All earlier native metadata remains authoritative for matching keys.
                expected = pd.concat([clickhouse_rows(before), delta], ignore_index=True)[COLUMNS]
                delta.to_parquet(folder / "insert_delta.parquet", index=False)
                expected.to_parquet(folder / "expected.parquet", index=False)
                client.command(f"TRUNCATE TABLE {database}.source")
                client.insert_df(f"{database}.source", expected, column_names=COLUMNS)
                staged = client.query_df(f"SELECT * FROM {database}.source")
                compare_complete(staged, expected)
                record = dict(month=str(month), status="staged_verified", existing_rows=len(before), new_rows=len(delta),
                    verified_rows=len(expected), publication_wall_clock=wall_clock.isoformat())
                records.append(record)
                export_json(report_path, report)
                if len(delta):
                    inserted = _insert_daily_factor_rows_by_partition(client, delta)
                    if inserted != len(delta):
                        raise RuntimeError("Native insert count mismatch")
                    report["inserted_rows"] += inserted
                record["status"] = "readback_pending"
                export_json(report_path, report)
                after = capture(client, month, folder / "native_after.parquet")
                compare_complete(after, expected)
                compare_complete(canonical(after).loc[a.index].reset_index(), before)
                record.update(status="native_published_and_verified", hashes={path.name:sha256_file(path) for path in folder.glob("*.parquet")})
                export_json(folder / "summary.json", record)
                export_json(report_path, report)
                print({k:v for k,v in record.items() if k!='hashes'}, flush=True)
            verify_dependencies()
            report.update(status="native_published_and_verified_snapshots_pending", native_published=True,
                verified_rows=sum(row["verified_rows"] for row in records), unchanged_existing_rows=sum(row["existing_rows"] for row in records),
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
    scope_name = "_".join(map(str, source_years))
    if source_years == list(range(source_years[0], source_years[-1] + 1)) and len(source_years) > 1:
        scope_name = f"{source_years[0]}_{source_years[-1]}"
    gold = DATA_LAKE.gold("survivorship","kr","capitalization_factors",scope_name)
    artifacts = {row["month"]:export_frame(gold / f"{row['month']}.parquet",
        pd.read_parquet(args.output / row["month"] / "expected.parquet")) for row in records}
    export_json(gold / "summary.json", dict(status=report["status"], native_published=True, snapshots_published=False,
        publication_path=str(report_path.resolve()), publication_sha256=sha256_file(report_path),
        inserted_rows=report["inserted_rows"], verified_rows=report["verified_rows"], artifacts=artifacts,
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
        native_added_rows=report["inserted_rows"],native_verified_rows=report["verified_rows"])
    export_json(coverage,current)
    print(report["status"], report["inserted_rows"], report["verified_rows"], flush=True)


if __name__ == "__main__":
    main()
