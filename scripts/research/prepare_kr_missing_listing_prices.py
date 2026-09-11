"""Prepare full price-history previews for the three DART-reviewed identities.

Retain the complete prior registry. All output is isolated; this does not change
production membership, price panels, factors, or any rebuild completion marker.
"""
import argparse
from datetime import date
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.source_storage import sha256_file
from engine.core.serving_storage import export_json
from engine.workflows.survivorship import run_survivorship_refresh

SYMBOLS = ("204210", "464440", "464680")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal-summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Preparation must stay in silver")
    if not args.gold.resolve().is_relative_to((DATA_LAKE.root / "gold").resolve()):
        raise ValueError("User previews must stay in gold")
    if args.output.exists() or args.gold.exists():
        raise ValueError("Preserve previous preparation and preview generations")
    prior = json.loads(args.proposal_summary.read_text("utf-8"))
    proposal = Path(prior["proposal"])
    assert sha256_file(proposal) == prior["proposal_sha256"]
    assert sha256_file(prior["base_registry"]) == prior["base_registry_sha256"]
    manifest = json.loads(proposal.read_text("utf-8"))
    assert len(manifest["listing_episodes"]) == 15
    registration = DATA_LAKE.silver("krx", "shares", "historical_sources.json")
    registered = json.loads(registration.read_text("utf-8"))
    pins = {str(p.resolve()): sha256_file(p) for p in (Path(__file__), args.proposal_summary, proposal,
        Path(prior["base_registry"]), registration,
        ROOT / "engine/transformers/survivorship_marcap.py", ROOT / "engine/transformers/stock_splits.py",
        ROOT / "engine/workflows/survivorship.py")}
    for path, digest in prior["pinned_inputs"].items():
        assert sha256_file(path) == digest, path
        pins[path] = digest
    by_year = {r["year"]: r for r in registered["sources"] if r.get("format") == "marcap_parquet"}
    source_ids = {r["source_id"] for r in manifest["sources"]}
    for year in (2025, 2026):
        source = by_year[year]
        path = DATA_LAKE.root / source["path"]
        assert sha256_file(path) == source["sha256"]
        pins[str(path.resolve())] = source["sha256"]
        assert source["source_id"] not in source_ids
        manifest["sources"].append(dict(source_id=source["source_id"], provider="MARCAP",
            path=path.resolve().relative_to(ROOT).as_posix(), published_date="2026-09-10",
            published_date_basis="Verification snapshot; economic observations use their own Date field.",
            source_url=source["source_url"], source_sha256=source["sha256"]))
    requests = []
    for symbol in SYMBOLS:
        episodes = [r for r in manifest["listing_episodes"] if r["security_id"] == "SEC_KR_" + symbol]
        assert len(episodes) == 1 and episodes[0]["valid_until"]
        episode = episodes[0]
        requests.append(dict(security_id=episode["security_id"], symbol=symbol, identity_status="verified",
            episode_ids=[episode["episode_id"]], source_ids=[f"marcap-{y}" for y in range(
                date.fromisoformat(episode["valid_from"]).year, date.fromisoformat(episode["valid_until"]).year + 1)]))
    assert not {r["security_id"] for r in requests} & {r["security_id"] for r in manifest["market_data_sources"]}
    manifest["market_data_sources"].extend(requests)
    manifest["source_root"] = str(ROOT)
    args.output.mkdir(parents=True)
    manifest_path = args.output / "merged_price_review_proposal.json"
    export_json(manifest_path, manifest)
    report = dict(status="preparing", production_changed=False, coverage_complete=False,
        price_history_preview_verified=False, financial_histories_approved=False,
        terminal_rights_complete=False, full_split_source_coverage_verified=False,
        proposal_path=str(manifest_path.resolve()), proposal_sha256=sha256_file(manifest_path),
        pinned_inputs=pins, checks=[])
    export_json(args.output / "summary.json", report)
    try:
        # No panel_dir is supplied: load_clickhouse=False leaves regular panels
        # and their change journals untouched, as well as leaving the DB alone.
        result = run_survivorship_refresh(market="kr", end_date="2026-09-10", manifest_path=manifest_path,
            output_dir=args.output / "canonical_preview", gold_dir=args.gold / "preview",
            download=False, load_clickhouse=False)
        assert result["database_publication"] is None and result["listing_episodes"] == 15
        sources = {r["source_id"]: r for r in manifest["sources"]}
        for request in requests:
            sid, symbol = request["security_id"], request["symbol"]
            episode = next(r for r in manifest["listing_episodes"] if r["security_id"] == sid)
            frames = []
            for source_id in request["source_ids"]:
                source = sources[source_id]
                path = ROOT / source["path"]
                assert sha256_file(path) == source["source_sha256"]
                pins[str(path.resolve())] = source["source_sha256"]
                frame = pd.read_parquet(path, columns=["Code", "Date", "Open", "High", "Low", "Close", "Volume", "Stocks", "Marcap", "Market"],
                    filters=[("Code", "==", symbol)])
                frame["source_id"] = source_id
                frames.append(frame)
            raw = pd.concat(frames, ignore_index=True).sort_values("Date")
            raw.Date = pd.to_datetime(raw.Date)
            original = raw.loc[raw.Date.ge(episode["valid_from"]) & raw.Date.lt(episode["valid_until"])].copy()
            assert not original.Date.duplicated().any() and len(original)
            folder = args.output / symbol
            folder.mkdir()
            raw.to_parquet(folder / "all_original_observations.parquet", index=False)
            original.to_parquet(folder / "original_listed_observations.parquet", index=False)
            path = args.output / "canonical_preview" / "prices" / f"kr_{symbol}.parquet"
            actual = pd.read_parquet(path).sort_values("trade_date")
            pd.testing.assert_index_equal(pd.DatetimeIndex(actual.trade_date), pd.DatetimeIndex(original.Date), check_names=False)
            for name, source_name in [("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"),
                ("volume", "Volume"), ("shares", "Stocks"), ("market_cap", "Marcap")]:
                np.testing.assert_allclose(actual[name].to_numpy(float), original[source_name].to_numpy(float), rtol=1e-12, atol=1e-6)
            halts = [r for r in manifest["trading_halts"] if r["security_id"] == sid]
            blocked = pd.Series(False, index=actual.index)
            for halt in halts:
                blocked |= actual.trade_date.ge(halt["start_date"]) & actual.trade_date.lt(halt["end_date"])
            metadata = json.loads(path.with_suffix(".metadata.json").read_text("utf-8"))
            check = dict(symbol=symbol, source_rows=len(raw), listed_rows=len(actual),
                excluded_outside_listing=len(raw)-len(actual), first_date=actual.trade_date.min().date().isoformat(),
                last_date=actual.trade_date.max().date().isoformat(), halted_quote_rows=int(blocked.sum()),
                nonpositive_volume_rows=int(actual.volume.le(0).sum()), original_fields_match=True,
                split_ledger_sha256=metadata["split_ledger_sha256"],
                preview_sha256=sha256_file(path), original_sha256=sha256_file(folder / "original_listed_observations.parquet"))
            report["checks"].append(check)
            print(json.dumps(check), flush=True)
        for path, digest in pins.items():
            assert sha256_file(path) == digest, path
        report.update(status="original_price_fields_verified_pending_source_and_consumer_review",
            price_history_preview_verified=True, canonical_refresh=result,
            limitation="Full retained Marcap history matches for the three proposed listings. Known split ledger is applied, but complete corporate-action source coverage, terminal payouts, and financial histories are not approved. Halted vendor quotes are retained as observations and must not be executed.")
    except BaseException as error:
        report.update(status="failed_check_evidence", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        shutil.copy2(__file__, args.output / Path(__file__).name)
        export_json(args.output / "summary.json", report)
        export_json(args.gold / "summary.json", report)
    print(report["status"], flush=True)


if __name__ == "__main__":
    main()
