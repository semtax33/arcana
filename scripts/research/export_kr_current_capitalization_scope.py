"""Verify current native lineage and export one coherent 1996-2026 market-factor scope.

This reads the database. It does not insert factors or replace raw sources.
Unaudited native events, including NULLs, end the preceding audited interval.
"""
import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import shutil
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_frame, export_json
from engine.core.source_storage import SourceRefreshLock, sha256_file
from publish_kr_historical_capitalization_snapshots import normalize, GROUPS, SNAPSHOT_COLUMNS
from publish_kr_survivorship_capital_factors import COLUMNS, KEYS, latest_nullable, compare_nullable
from validate_kr_survivorship_financial_factors import canonical

END = "2026-09-10"


def current_rows(client, month, folder, *, snapshot=False):
    """Retain all tied latest versions, so conflicting ties cannot be hidden."""
    columns = SNAPSHOT_COLUMNS if snapshot else COLUMNS
    table = "fact_daily_factor_snapshot" if snapshot else "fact_daily_factors"
    kind = "snapshot" if snapshot else "native"
    fields = ",".join(columns)
    sql = f"""SELECT {fields} FROM (
        SELECT {fields}, max(updated_at) OVER (PARTITION BY {','.join(KEYS)}) AS latest_version
        FROM {table}
        WHERE trade_date >= {{start:Date}} AND trade_date < {{end:Date}}
          AND trade_date <= {{cutoff:Date}} AND startsWith(security_id,'SEC_KR_')
          AND factor_id IN ('mcap_mil','csho') AND financial_basis IN ('annual','quarterly','ttm')
        ) WHERE updated_at = latest_version
        SETTINGS max_execution_time=40,max_threads=2"""
    (folder / f"{kind}_query.sql").write_text(sql, "utf-8")
    rows = client.query_df(sql, parameters=dict(start=month.start_time.date(),
        end=(month+1).start_time.date(), cutoff=date.fromisoformat(END)))
    rows = normalize(rows)
    rows.to_parquet(folder / f"{kind}_current_versions.parquet", index=False)
    latest, ambiguous = latest_nullable(rows, snapshot=snapshot)
    if not ambiguous.empty:
        ambiguous.to_parquet(folder / f"{kind}_ambiguous.parquet", index=False)
        raise ValueError(f"Conflicting latest {kind} versions: {month}")
    return latest


def expected_snapshots(events, market_days, existing):
    entities = events.loc[events.selected_source.eq(True), GROUPS].drop_duplicates()
    events = events.merge(entities, on=GROUPS, how="inner", validate="many_to_one")
    grid = pd.DataFrame({"trade_date": market_days}).merge(entities, how="cross")
    # Preserve existing non-market dates only for their actual entity/factor keys.
    extra = existing[KEYS].merge(entities, on=GROUPS, how="inner", validate="many_to_one")
    grid = pd.concat([grid, extra], ignore_index=True).drop_duplicates(KEYS)
    right = events.rename(columns={"trade_date": "source_trade_date"})
    expected = pd.merge_asof(grid.sort_values("trade_date"), right.sort_values("source_trade_date"),
        left_on="trade_date", right_on="source_trade_date", by=GROUPS, direction="backward")
    expected = expected.loc[expected.selected_source.eq(True)].copy()
    assert expected.source_trade_date.le(expected.trade_date).all()
    carry = events.sort_values("trade_date").groupby(GROUPS, sort=False, dropna=False).tail(1)
    carry = carry.loc[carry.selected_source.eq(True), COLUMNS + ["selected_source"]]
    return expected[SNAPSHOT_COLUMNS], carry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(DATA_LAKE.silver("survivorship", "financial_research").resolve()):
        raise ValueError("Verification artifacts must remain in Silver")
    base = DATA_LAKE.silver("survivorship", "financial_research")
    publication_path = base / "kr_remaining_capitalization_snapshot_publication_20260911/publication.json"
    closeout_path = publication_path.parent / "closeout/audit.json"
    if not closeout_path.exists():
        raise ValueError("Wait for the remaining-scope Gold finalizer to complete")
    output.mkdir(exist_ok=False)
    shutil.copy2(__file__, output / Path(__file__).name)
    evidence = {}

    def check(path, expected=None):
        path = Path(path).resolve()
        actual = sha256_file(path)
        if expected is not None and actual != expected:
            raise ValueError(f"Changed evidence: {path}")
        evidence[str(path)] = actual
        return actual

    def read(path, expected=None):
        check(path, expected)
        return json.loads(Path(path).read_bytes())

    publication = read(publication_path)
    closeout = read(closeout_path)
    assert closeout["status"] == "native_and_snapshots_and_factorlab_verified"
    serving = read(publication_path.parent / "closeout/serving_artifacts.json")
    assert serving["status"] == "gold_matches_verified_staging"
    for name, digest in publication["dependencies"].items():
        path = (ROOT / name).resolve()
        if digest is None:
            assert not path.exists(), path
            evidence[str(path)] = None
        else:
            check(path, digest)
    for path in (__file__, ROOT / "scripts/research/publish_kr_survivorship_capital_factors.py",
                 ROOT / "scripts/research/validate_kr_survivorship_financial_factors.py"):
        check(path)
    prior = read(base / "kr_previous_capitalization_scopes_recheck_20260911/summary.json")
    remaining = read(base / "kr_remaining_full_snapshot_factorlab_20260911/summary.json")
    assert prior["status"] == remaining["status"] == "verified"
    assert prior["latest_snapshot_publication_sha256"] == check(publication_path)
    assert remaining["snapshot_publication_sha256"] == check(publication_path)
    assert prior["combined_annual_mcap_trading_days"] == 7701
    check(DATA_LAKE.silver("survivorship", "kr", "listing_episodes.json"), remaining["listing_episodes_sha256"])
    sources, source_counts = {}, []
    for name in ("kr_historical_capitalization_native_publication_20260911_v2",
                 "kr_full_share_native_publication_20260911_2024",
                 "kr_remaining_capitalization_native_publication_20260911"):
        path = base / name / "publication.json"
        native = read(path)
        assert native["native_published"]
        source_counts.append(native["verified_rows"])
        for record in native["months"]:
            assert record["status"] == "native_published_and_verified"
            month = record["month"]
            if month in sources:
                raise ValueError(f"Overlapping native source month: {month}")
            source = path.parent / month / "expected.parquet"
            sources[month] = (source, record["hashes"][source.name], record["verified_rows"])
    months = list(pd.period_range("1996-01", END, freq="M"))
    assert sorted(sources) == [str(month) for month in months]
    calendar_folder = publication_path.parent / "market_calendar_after"
    calendar = read(calendar_folder / "summary.json")
    calendar_frames = []
    for record in calendar["years"]:
        path = calendar_folder / f"{record['year']}.parquet"
        check(path, record["sha256"])
        calendar_frames.append(pd.read_parquet(path))
    market_dates = pd.DatetimeIndex(pd.to_datetime(pd.concat(calendar_frames).trade_date).unique()).sort_values()
    assert market_dates.max().date().isoformat() == "2026-09-04"
    report = dict(status="verifying_current_database", started_at=datetime.now(timezone.utc).isoformat(),
        source_years=list(range(1996, 2027)), cutoff=END, last_price_calendar_date="2026-09-04",
        factors=["mcap_mil", "csho"], financial_bases=["annual", "quarterly", "ttm"],
        database_written=False, coverage_complete=False, all_share_dependent_factors_rebuilt=False,
        annual_mcap_factorlab_verified_days=7701, months=[], native_rows=0, snapshot_rows=0,
        expected_native_rows=sum(source_counts), evidence_pins=evidence)
    target = output / "summary.json"
    export_json(target, report)
    client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=50)
    try:
        with SourceRefreshLock("kr"):
            carry = None
            for month in months:
                folder = output / str(month)
                folder.mkdir()
                source, source_hash, expected_count = sources[str(month)]
                check(source, source_hash)
                expected_native = normalize(pd.read_parquet(source))
                assert len(expected_native) == expected_count
                native = current_rows(client, month, folder)
                expected_index = canonical(expected_native).index
                native_indexed = canonical(native)
                if not expected_index.isin(native_indexed.index).all():
                    raise ValueError(f"Native source keys disappeared: {month}")
                verified_native = native_indexed.loc[expected_index].reset_index()
                compare_nullable(verified_native, expected_native)
                # The explicit merge avoids relying on query row order for scope membership.
                native = native.merge(
                    expected_native[KEYS].assign(selected_source=True), on=KEYS, how="left", validate="one_to_one")
                native["selected_source"] = native.selected_source.eq(True)
                events = native if carry is None else pd.concat([carry, native], ignore_index=True)
                current = current_rows(client, month, folder, snapshot=True)
                days = market_dates[(market_dates >= month.start_time) & (market_dates < (month+1).start_time)]
                expected, carry = expected_snapshots(normalize(events), days, current)
                expected.to_parquet(folder / "expected_current_snapshots.parquet", index=False)
                expected_snapshot_index = canonical(expected).index
                current_indexed = canonical(current)
                missing = expected_snapshot_index.difference(current_indexed.index)
                if len(missing):
                    missing.to_frame(index=False).to_parquet(folder / "missing_snapshot_keys.parquet", index=False)
                    raise ValueError(f"Current native scope lacks {len(missing)} snapshot keys: {month}")
                verified_snapshot = current_indexed.loc[expected_snapshot_index].reset_index()
                compare_nullable(verified_snapshot, expected, snapshot=True)
                verified_snapshot["is_market_trading_date"] = pd.to_datetime(verified_snapshot.trade_date).isin(market_dates)
                export_frame(folder / "native_export.parquet", verified_native)
                export_frame(folder / "snapshot_export.parquet", verified_snapshot)
                export_frame(folder / "next_month_carry.parquet", carry)
                report["months"].append(dict(month=str(month), status="current_native_and_lineage_verified",
                    native_rows=len(verified_native), snapshot_rows=len(verified_snapshot),
                    market_trading_date_rows=int(verified_snapshot.is_market_trading_date.sum()),
                    snapshot_rows_outside_audited_lineage=len(current)-len(verified_snapshot),
                    hashes={p.name: sha256_file(p) for p in folder.iterdir() if p.is_file()}))
                report["native_rows"] += len(verified_native)
                report["snapshot_rows"] += len(verified_snapshot)
                export_json(target, report)
                print(month, len(verified_native), len(verified_snapshot), "current lineage verified", flush=True)
            assert report["native_rows"] == report["expected_native_rows"]
            for path, digest in evidence.items():
                if digest is None:
                    assert not Path(path).exists(), path
                else:
                    assert sha256_file(path) == digest, path
            report.update(status="current_database_verified_gold_exporting")
            export_json(target, report)
            gold = DATA_LAKE.gold("survivorship", "kr", "capitalization_factors", "1996_2026", output.name)
            if gold.exists():
                raise ValueError("Use a distinct Gold run; existing exports are immutable")
            artifacts = {}
            for record in report["months"]:
                month = record["month"]
                artifacts[month] = {}
                for kind in ("native", "snapshot"):
                    path = output / month / f"{kind}_export.parquet"
                    check(path, record["hashes"][path.name])
                    artifact = export_frame(gold / kind / f"{month}.parquet", pd.read_parquet(path))
                    pd.testing.assert_frame_equal(pd.read_parquet(artifact["path"]), pd.read_parquet(path), check_exact=True)
                    artifacts[month][kind] = artifact
            report.update(status="current_native_and_snapshots_exported_and_verified", artifacts=artifacts,
                completed_at=datetime.now(timezone.utc).isoformat(),
                policy="Original dated native observations and latest native boundaries determine every snapshot. All three bases and both market factors are exported. Full issuer eligibility and remaining share-dependent financial factors are still incomplete.")
            export_json(target, report)
            completion = {key: value for key, value in report.items() if key not in {"evidence_pins", "months"}}
            completion.update(verification_path=str(target), verification_sha256=sha256_file(target))
            summary = export_json(gold / "summary.json", completion)
            pointer = DATA_LAKE.gold("survivorship", "kr", "capitalization_factors", "current.json")
            if pointer.exists():
                shutil.copy2(pointer, output / "gold_current_before.json")
            export_json(pointer, dict(status=report["status"], current=summary,
                source_years=report["source_years"], factors=report["factors"], coverage_complete=False,
                supersedes_prior_exports_for_current_use=True))
            share_input = DATA_LAKE.gold("survivorship", "kr", "share_inputs", "1996_2026", "summary.json")
            shutil.copy2(share_input, output / "gold_share_input_before.json")
            input_summary = json.loads(share_input.read_bytes())
            input_summary["current_capitalization"] = dict(status=report["status"], summary=summary,
                native_rows=report["native_rows"], snapshot_rows=report["snapshot_rows"],
                annual_mcap_factorlab_verified_days=7701, all_share_dependent_factors_rebuilt=False)
            export_json(share_input, input_summary)
    except BaseException as error:
        report.update(status="failed", error_type=type(error).__name__, error=str(error))
        export_json(target, report)
        raise
    finally:
        client.close()
    print(report["status"], report["native_rows"], report["snapshot_rows"], flush=True)


if __name__ == "__main__":
    main()
