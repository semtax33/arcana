"""Publish independently checked KR historical financial inputs and factors.

Native publication requires a complete, empty pre-publication monthly snapshot.
An interrupted insertion is reconciled from native keys before any missing-row
retry. Existing differing or unexpected values require an explicit revision.
"""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
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
from engine.workflows.financial_history import publish_reviewed_financial_history
from validate_kr_survivorship_financial_factors import FACTOR_IDS, KEYS, SILVER, canonical, digest, save
from inspect_kr_survivorship_native_factors import SQL


def compare(actual, expected):
    a, b = canonical(actual), canonical(expected)
    assert a.index.equals(b.index), "Native/prepared factor identity differs"
    np.testing.assert_allclose(a.factor_value.to_numpy(dtype=float), b.factor_value.to_numpy(dtype=float), rtol=1e-12, atol=1e-12)


def snapshot(client, months, symbols, folder):
    folder.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, month in enumerate(months):
        month = pd.Period(month, freq="M")
        parameters = {"start": month.start_time.date(), "end": (month+1).start_time.date(),
                      "securities": [f"SEC_KR_{s}" for s in symbols], "ids": FACTOR_IDS}
        frame = client.query_df(SQL, parameters=parameters)
        if frame.empty and not len(frame.columns):
            frame = pd.DataFrame(columns=KEYS + ["factor_value"])
        frame.to_parquet(folder / f"{month}.parquet", index=False)
        frames.append(frame)
        if index % 12 == 0:
            print("native", month, "rows", sum(len(f) for f in frames), flush=True)
    return pd.concat(frames, ignore_index=True)


def verify_existing_publication(record):
    manifest_path = Path(record["manifest_path"])
    assert digest(manifest_path) == record["sha256"], "Previously published index changed"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    for receipt in manifest["receipts"]:
        for kind in ("normalized", "review", "source", "publication_source"):
            if not receipt.get(kind + "_path"):
                continue
            root = (Path(receipt.get("evidence_root", manifest_path.parent))
                    if kind in {"source", "publication_source"} else manifest_path.parent)
            path = (root / receipt[kind + "_path"]).resolve()
            assert path.is_relative_to(root.resolve())
            assert digest(path) == receipt[kind + "_sha256"], "Published evidence changed"
    return dict(status="existing_verified", receipts=len(manifest["receipts"]), manifest_path=str(manifest_path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--validation", type=Path, default=SILVER / "kr_v7_daily_factor_validation_final")
    parser.add_argument("--native-before", type=Path, default=SILVER / "kr_v7_native_before")
    parser.add_argument("--output", type=Path, default=SILVER / "kr_v7_financial_publication")
    parser.add_argument("--publish-native", action="store_true")
    args = parser.parse_args()
    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
    validation = json.loads((args.validation / "summary.json").read_text("utf-8"))
    assert validation["status"] == "validated"
    assert digest(Path(__file__).with_name("validate_kr_survivorship_financial_factors.py")) == validation["validator_sha256"]
    symbols = sorted(r["symbol"] for r in validation["results"])
    frames = []
    for item in validation["results"]:
        assert item["status"] == "validated" and item["symbol"] in symbols
        for path, checksum in item["dependency_sha256"].items():
            assert digest(path) == checksum, "Validated inputs changed"
        artifact = args.validation / item["symbol"] / "annual_daily_factors.parquet"
        assert digest(artifact) == item["factor_artifact_sha256"]
        frames.append(pd.read_parquet(artifact))
    expected = pd.concat(frames, ignore_index=True)
    financial_dir = DATA_LAKE.silver("dart", "normalized")
    report_path = args.output / "publication.json"
    previous = json.loads(report_path.read_text("utf-8")) if report_path.exists() else {}
    if previous:
        assert previous["validation_sha256"] == digest(args.validation / "summary.json")
    report = dict(previous, status="preparing", validation_sha256=digest(args.validation / "summary.json"),
                  coverage_complete=False, factor_ids=FACTOR_IDS, symbols=symbols, manifests=[], publisher_sha256=digest(__file__))
    save(report_path, report)
    with SourceRefreshLock("kr"):
        prior_manifests = {r["symbol"]: r for r in previous.get("manifests", [])}
        for symbol in symbols:
            if symbol in prior_manifests:
                published = verify_existing_publication(prior_manifests[symbol])
            else:
                published = publish_reviewed_financial_history(args.review_root / symbol / "review.json", financial_dir=financial_dir)
            manifest = Path(published["manifest_path"])
            report["manifests"].append(dict(symbol=symbol, **published, sha256=digest(manifest)))
            save(report_path, report)
        actual = insert_daily_factors(stock_codes=symbols, financial_basis="annual", market="kr", start_date="2017-01-01",
            end_date="2026-09-10", factor_ids=FACTOR_IDS, financial_dir=financial_dir, dry_run=True, insert_catalog=False,
            use_edgartools=False, require_report_metadata=True, wacc_online_backfill=False)
        compare(actual, expected)
        prepared = args.output / "prepared_factors.parquet"
        actual.to_parquet(prepared, index=False)
        report.update(status="financial_inputs_published_and_validated", factor_rows=len(actual),
                      prepared_sha256=digest(prepared), native_factors_published=False)
        save(report_path, report)
        print(report["status"], "issuers", len(symbols), "factor rows", len(actual), flush=True)
        if not args.publish_native:
            return
        before_path = args.native_before / "summary.json"
        before = json.loads(before_path.read_text("utf-8"))
        assert before["status"] == "finished" and before["rows"] == 0 and before["symbols"] == symbols
        for month in before["months"]:
            assert digest(args.native_before / f"{month['month']}.parquet") == month["sha256"]
        report["native_before_sha256"] = digest(before_path)
        months = [r["month"] for r in before["months"]]
        client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=30)
        try:
            existing = snapshot(client, months, symbols, args.output / "native_preflight")
            a, e = canonical(existing), canonical(actual)
            assert not len(a.index.difference(e.index)), "Unexpected native rows require an explicit invalidation policy"
            common = a.index.intersection(e.index)
            np.testing.assert_allclose(a.loc[common, "factor_value"].to_numpy(dtype=float),
                                       e.loc[common, "factor_value"].to_numpy(dtype=float), rtol=1e-12, atol=1e-12)
            missing = e.index.difference(a.index)
            delta = e.loc[missing].reset_index()[actual.columns]
            delta.to_parquet(args.output / "insert_delta.parquet", index=False)
            report.update(status="native_publication_started", already_verified_rows=len(existing),
                          planned_insert_rows=len(delta), started_native_at=datetime.now(timezone.utc).isoformat())
            save(report_path, report)
            inserted = _insert_daily_factor_rows_by_partition(client, delta, split_by_partition=True)
            report.update(status="native_readback_pending", inserted_rows=inserted)
            save(report_path, report)
            readback = snapshot(client, months, symbols, args.output / "native_readback")
            compare(readback, actual)
            readback.to_parquet(args.output / "native_readback.parquet", index=False)
            for record in report["manifests"]:
                assert digest(record["manifest_path"]) == record["sha256"]
            gold = DATA_LAKE.gold("survivorship", "kr", "financial_factors", "20260910")
            artifact = export_frame(gold / "annual.parquet", actual)
            report.update(status="published_and_verified", native_factors_published=True,
                          verified_native_rows=len(readback), finished_at=datetime.now(timezone.utc).isoformat(), gold_artifact=artifact)
            save(report_path, report)
            export_json(gold / "summary.json", report)
            print(report["status"], "inserted", inserted, "verified", len(readback), flush=True)
        finally:
            client.close()


if __name__ == "__main__":
    main()
