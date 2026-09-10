"""Publish reviewed period factors and the audited 008560 annual ROE correction.

Every attempt retains a fresh native before-image. Corrections are limited to
the known annual ownership-basis defect and must match the previous publication.
No rows are deleted and no general financial rebuild marker is completed here.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4
import warnings

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_frame, export_json
from engine.core.source_storage import SourceRefreshLock
from engine.loaders.factors import insert_daily_factors, _insert_daily_factor_rows_by_partition
from validate_kr_survivorship_financial_factors import FACTOR_IDS, KEYS, SILVER, canonical, digest, save
from publish_kr_survivorship_financial_factors import compare, verify_existing_publication

SQL = """
SELECT trade_date, factor_id, financial_basis, security_id,
       argMax(factor_value, updated_at) AS factor_value
FROM fact_daily_factors
WHERE security_id IN {securities:Array(String)}
  AND financial_basis IN {bases:Array(String)}
  AND trade_date >= {start:Date} AND trade_date < {end:Date}
  AND factor_id IN {ids:Array(String)}
GROUP BY trade_date, factor_id, financial_basis, security_id
SETTINGS max_execution_time=20, max_threads=2
"""


def plan_delta(existing, prepared, previous):
    a, e, old = canonical(existing), canonical(prepared), canonical(previous)
    assert not len(a.index.difference(e.index)), "Unexpected native rows need an explicit missing-value revision"
    common = a.index.intersection(e.index)
    changed = common[~np.isclose(a.loc[common, "factor_value"].to_numpy(dtype=float),
        e.loc[common, "factor_value"].to_numpy(dtype=float), rtol=1e-12, atol=1e-12)]
    for key in changed:
        assert key[0] == "SEC_KR_008560" and key[2:] == ("annual", "roe"), "Correction outside reviewed scope"
        assert key in old.index and np.isclose(a.loc[key, "factor_value"], old.loc[key, "factor_value"],
            rtol=1e-12, atol=1e-12), "Native value differs from reviewed previous publication"
    keys = e.index.difference(a.index).union(changed)
    delta = e.loc[keys].reset_index()[prepared.columns]
    corrections = a.loc[changed, ["factor_value"]].rename(columns={"factor_value": "previous_value"})
    corrections["corrected_value"] = e.loc[changed, "factor_value"]
    return delta, corrections.reset_index()


def snapshot(client, months, symbols, bases, folder):
    folder.mkdir(parents=True, exist_ok=False)
    frames, records = [], []
    (folder / "query.sql").write_text(SQL, encoding="utf-8")
    for index, month in enumerate(months):
        frame = client.query_df(SQL, parameters={"start": month.start_time.date(),
            "end": (month + 1).start_time.date(), "securities": [f"SEC_KR_{s}" for s in symbols],
            "bases": bases, "ids": FACTOR_IDS})
        if frame.empty and not len(frame.columns):
            frame = pd.DataFrame(columns=KEYS + ["factor_value"])
        path = folder / f"{month}.parquet"
        frame.to_parquet(path, index=False)
        frames.append(frame)
        records.append(dict(month=str(month), rows=len(frame), sha256=digest(path)))
        save(folder / "summary.json", dict(status="running", months=records))
        if index % 12 == 0:
            print("native", folder.name, month, "rows", sum(len(f) for f in frames), flush=True)
    result = pd.concat(frames, ignore_index=True)
    save(folder / "summary.json", dict(status="finished", months=records, rows=len(result)))
    return result


def checked_inputs(validations):
    expected, dependencies, cases = [], {}, set()
    for folder in validations:
        summary_path = folder / "summary.json"
        summary = json.loads(summary_path.read_text("utf-8"))
        assert summary["status"] == "validated" and summary["results"]
        dependencies[str(summary_path)] = digest(summary_path)
        validator = folder / "implementation/scripts/research/validate_kr_survivorship_period_factors.py"
        assert digest(validator) == summary["validator_sha256"], "Preserve the validated implementation"
        for item in summary["results"]:
            assert item["status"] == "validated"
            case = (item["symbol"], item["basis"])
            assert case not in cases
            cases.add(case)
            for path, checksum in item["source_hashes"].items():
                assert digest(path) == checksum, "Validated source changed"
                dependencies[path] = checksum
            path = folder / item["symbol"] / item["basis"] / "actual.parquet"
            assert digest(path) == item["actual_sha256"]
            dependencies[str(path)] = digest(path)
            target = folder / item["symbol"] / item["basis"] / "expected.parquet"
            actual, oracle = pd.read_parquet(path), pd.read_parquet(target)
            compare(actual, oracle)
            dependencies[str(target)] = digest(target)
            expected.append(oracle)
    symbols = sorted({s for s, _ in cases})
    bases = sorted({b for _, b in cases})
    assert cases == {(s, b) for s in symbols for b in bases}, "Incomplete validation scope"
    return pd.concat(expected, ignore_index=True), dependencies, symbols, bases


def verify_dependencies(dependencies):
    for path, checksum in dependencies.items():
        assert digest(path) == checksum, f"Publication input changed: {path}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, default=SILVER / "kr_v7_period_publication")
    parser.add_argument("--publish-native", action="store_true")
    args = parser.parse_args()
    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
    attempt = args.output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8])
    expected, dependencies, symbols, bases = checked_inputs(args.validation)
    previous_report = SILVER / "kr_v7_financial_publication/publication.json"
    published = json.loads(previous_report.read_text("utf-8"))
    previous_path = previous_report.parent / "prepared_factors.parquet"
    assert published["native_factors_published"] and digest(previous_path) == published["prepared_sha256"]
    previous = pd.read_parquet(previous_path)
    for path in (previous_report, previous_path, Path(__file__)):
        dependencies[str(path)] = digest(path)
    for name in ("financial_history.py", "factor_metrics.py", "filing_periods.py"):
        path = ROOT / "engine/transformers/_internal" / name
        dependencies[str(path)] = digest(path)
    report = dict(status="preparing", symbols=symbols, bases=bases, factor_ids=FACTOR_IDS,
        coverage_complete=False, dependencies=dependencies, revision_reason="coherent parent/group ROE ownership basis",
        native_factors_published=False, attempt=str(attempt))
    save(attempt / "publication.json", report)
    with SourceRefreshLock("kr"):
        financial_dir = DATA_LAKE.silver("dart", "normalized")
        for symbol in symbols:
            path = financial_dir / "history" / symbol / "manifest.json"
            verify_existing_publication(dict(manifest_path=str(path), sha256=digest(path)))
        prepared = pd.concat([insert_daily_factors(stock_codes=symbols, financial_basis=basis, market="kr",
            start_date="2017-01-01", end_date="2026-09-10", factor_ids=FACTOR_IDS,
            financial_dir=financial_dir, dry_run=True, insert_catalog=False, use_edgartools=False,
            require_report_metadata=True, wacc_online_backfill=False) for basis in bases], ignore_index=True)
        compare(prepared, expected)
        prepared.to_parquet(attempt / "prepared.parquet", index=False)
        verify_dependencies(dependencies)
        report.update(status="prepared_and_validated", rows=len(prepared),
            by_basis=prepared.groupby("financial_basis").size().to_dict(), prepared_sha256=digest(attempt / "prepared.parquet"))
        save(attempt / "publication.json", report)
        print(report["status"], report["by_basis"], flush=True)
        if not args.publish_native:
            return
        months = pd.period_range("2017-01", "2024-06", freq="M")
        dates = pd.to_datetime(prepared.trade_date)
        assert dates.min() >= months[0].start_time and dates.max() <= months[-1].end_time
        client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=30)
        try:
            existing = snapshot(client, months, symbols, bases, attempt / "native_before")
            delta, corrections = plan_delta(existing, prepared, previous)
            delta.to_parquet(attempt / "insert_delta.parquet", index=False)
            corrections.to_parquet(attempt / "corrections.parquet", index=False)
            verify_dependencies(dependencies)
            report.update(status="native_publication_started", previous_rows=len(existing),
                planned_insert_rows=len(delta), corrected_rows=len(corrections))
            save(attempt / "publication.json", report)
            inserted = _insert_daily_factor_rows_by_partition(client, delta, split_by_partition=True)
            report.update(status="readback_pending", inserted_rows=inserted)
            save(attempt / "publication.json", report)
            readback = snapshot(client, months, symbols, bases, attempt / "native_after")
            compare(readback, prepared)
            readback.to_parquet(attempt / "native_readback.parquet", index=False)
            verify_dependencies(dependencies)
            gold = DATA_LAKE.gold("survivorship", "kr", "financial_factors", "20260910")
            # Preserve the previous consumer bundle before replacing any member.
            old_gold = attempt / "gold_before"
            old_gold.mkdir()
            for path in gold.iterdir() if gold.exists() else []:
                if path.is_file():
                    shutil.copy2(path, old_gold / path.name)
            artifacts = {basis: export_frame(gold / f"{basis}.parquet",
                prepared.loc[prepared.financial_basis.eq(basis)].reset_index(drop=True)) for basis in bases}
            report.update(status="published_and_verified", native_factors_published=True,
                verified_native_rows=len(readback), gold_artifacts=artifacts, finished_at=datetime.now(timezone.utc).isoformat())
            save(attempt / "publication.json", report)
            save(args.output / "latest.json", dict(publication_path=str(attempt / "publication.json"), sha256=digest(attempt / "publication.json")))
            export_json(gold / "period_summary.json", report)
            export_json(gold / "summary.json", report)
            export_json(gold / "restoration_summary.json", dict(status="verified", market="kr",
                reviewed_issuers=len(symbols), factor_ids=FACTOR_IDS, factor_rows_by_basis=report["by_basis"],
                verified_native_rows=len(readback), corrected_annual_roe_rows=len(corrections),
                coverage_complete=False, publication_path=str(attempt / "publication.json"),
                remaining_scope="Other financial factors, whole-market listing/source coverage and unresolved settlement rights remain incomplete."))
            print(report["status"], "inserted", inserted, "corrected", len(corrections), "verified", len(readback), flush=True)
        finally:
            client.close()


if __name__ == "__main__":
    main()
