"""Audit all retained dated share sources against the regular KR input by year.

This prepares observations and explicit gaps/conflicts in Silver. It does not
change source registration, normalized inputs, listing identities, or databases.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.source_storage import sha256_file
from engine.core.serving_storage import export_json

KEYS = ["security_id", "trade_date"]
INPUT_COLUMNS = KEYS + ["shares", "market_cap"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--years", type=int, nargs="+")
    parser.add_argument("--additional-sources", type=Path)
    parser.add_argument("--end-date", default="2026-09-04")
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Audits and prepared data must be in Silver")
    args.output.mkdir(parents=True, exist_ok=False)
    manifest_path = DATA_LAKE.meta("survivorship", "kr_reviewed.json")
    manifest = json.loads(manifest_path.read_text("utf-8"))
    sources = {int(r["source_id"].removeprefix("marcap-")):r for r in manifest["sources"] if r["provider"] == "MARCAP"}
    if args.additional_sources:
        for source in json.loads(args.additional_sources.read_text("utf-8"))["sources"]:
            year = int(source["source_id"].removeprefix("marcap-"))
            if year in sources:
                raise ValueError("Additional source duplicates the reviewed source registry")
            sources[year] = source
    years = sorted(set(args.years or sources))
    if any(year not in sources for year in years):
        raise ValueError("A requested source year has no provenance manifest")
    current_path = DATA_LAKE.silver("krx", "shares", "kr_normalized_shares.csv")
    pinned = {str(manifest_path):sha256_file(manifest_path), str(current_path):sha256_file(current_path), str(Path(__file__)):sha256_file(__file__)}
    if args.additional_sources:
        pinned[str(args.additional_sources.resolve())] = sha256_file(args.additional_sources)
    for year in years:
        source = sources[year]
        path = (ROOT / source["path"]).resolve()
        if not path.is_relative_to(DATA_LAKE.root / "bronze") or sha256_file(path) != source["source_sha256"]:
            raise ValueError("Source must be a pinned Bronze original")
        raw = path.read_bytes()
        blob = __import__("hashlib").sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
        if source["source_url"] != f"https://api.github.com/repos/FinanceData/marcap/git/blobs/{blob}":
            raise ValueError("Source Git blob identity differs from retained bytes")
        pinned[str(path)] = source["source_sha256"]
    shutil.copy2(__file__, args.output / Path(__file__).name)
    shutil.copy2(manifest_path, args.output / "reviewed_manifest_before.json")
    current_dir = args.output / "current_by_year"
    current_dir.mkdir()
    writers = {}
    records = []
    report = dict(status="running", started_at=datetime.now(timezone.utc).isoformat(), years=records,
        requested_years=years, end_date=args.end_date, pinned_inputs=pinned, default_input_changed=False,
        native_published=False, snapshots_published=False, coverage_complete=False,
        scope="Dated market observations and source arithmetic. Historical issuer/listing eligibility is not approved by this audit.")
    export_json(args.output / "summary.json", report)
    try:
        try:
            for chunk in pd.read_csv(current_path, usecols=INPUT_COLUMNS, dtype={"security_id":str}, chunksize=300_000):
                chunk["trade_date"] = pd.to_datetime(chunk.trade_date)
                chunk = chunk.loc[chunk.trade_date.le(args.end_date)]
                for year, group in chunk.groupby(chunk.trade_date.dt.year, sort=False):
                    if year not in years:
                        continue
                    table = pa.Table.from_pandas(group.reset_index(drop=True), preserve_index=False)
                    if year not in writers:
                        writers[year] = pq.ParquetWriter(current_dir / f"{year}.parquet", table.schema)
                    writers[year].write_table(table)
        finally:
            for writer in writers.values():
                writer.close()
        for year in years:
            source = sources[year]
            path = ROOT / source["path"]
            folder = args.output / str(year)
            folder.mkdir()
            raw = pd.read_parquet(path, columns=["Code", "Date", "Name", "Market", "Close", "Volume", "Stocks", "Marcap"])
            codes = raw.Code.astype(str).str.strip().str.zfill(6)
            if not codes.str.fullmatch(r"[0-9A-Z]{6}").all():
                raise ValueError("Unknown source security code format")
            dates = pd.to_datetime(raw.Date, errors="raise")
            if not dates.dt.year.eq(year).all():
                raise ValueError("Source dates fall outside their declared year")
            values = raw[["Close", "Volume", "Stocks", "Marcap"]].apply(pd.to_numeric, errors="raise")
            positive = np.isfinite(values).all(axis=1) & values.Close.gt(0) & values.Volume.ge(0) & values.Stocks.gt(0) & values.Marcap.gt(0) & values.Stocks.mod(1).eq(0)
            arithmetic = np.isclose(values.Close * values.Stocks, values.Marcap, rtol=1e-8, atol=1)
            in_scope = dates.le(args.end_date)
            observed = pd.DataFrame(dict(security_id="SEC_KR_" + codes, trade_date=dates,
                shares=values.Stocks, market_cap=values.Marcap, raw_close=values.Close, raw_volume=values.Volume,
                source_name=raw.Name, source_market=raw.Market, source_id=source["source_id"]))
            if observed.duplicated(KEYS).any():
                raise ValueError("Ambiguous source date/code observation")
            observed.loc[in_scope & ~(positive & arithmetic)].to_parquet(folder / "invalid.parquet", index=False)
            observed = observed.loc[in_scope & positive & arithmetic].copy()
            observed.to_parquet(folder / "prepared.parquet", index=False)
            current_file = current_dir / f"{year}.parquet"
            current = pd.read_parquet(current_file) if current_file.exists() else pd.DataFrame({
                "security_id":pd.Series(dtype=str), "trade_date":pd.Series(dtype="datetime64[ns]"),
                "shares":pd.Series(dtype=float), "market_cap":pd.Series(dtype=float)})
            if current.duplicated(KEYS).any():
                raise ValueError("Regular share input contains duplicate dated keys")
            a, b = current.set_index(KEYS).sort_index(), observed.set_index(KEYS).sort_index()
            common = a.index.intersection(b.index)
            missing = b.index.difference(a.index)
            extra = a.index.difference(b.index)
            share_equal = np.isclose(a.loc[common, "shares"], b.loc[common, "shares"], rtol=0, atol=0)
            cap_equal = np.isclose(a.loc[common, "market_cap"], b.loc[common, "market_cap"], rtol=1e-8, atol=1)
            disagreements = a.loc[common].join(b.loc[common], lsuffix="_current", rsuffix="_source").loc[~share_equal | ~cap_equal].reset_index()
            disagreements.to_parquet(folder / "existing_value_differences.parquet", index=False)
            b.loc[missing].reset_index().to_parquet(folder / "missing_default_observations.parquet", index=False)
            a.loc[extra].reset_index().to_parquet(folder / "existing_without_source.parquet", index=False)
            record = dict(year=year, source_rows=int(in_scope.sum()), prepared_rows=len(observed), invalid_rows=int((in_scope & ~(positive & arithmetic)).sum()),
                default_rows=len(current), overlap_rows=len(common), missing_default_rows=len(missing),
                missing_default_securities=len(missing.get_level_values("security_id").unique()),
                differing_existing_rows=len(disagreements), share_differences=int((~share_equal).sum()),
                market_cap_differences=int((~cap_equal).sum()), existing_without_source=len(extra),
                source_path=str(path.resolve()), source_sha256=source["source_sha256"], source_url=source["source_url"],
                hashes={p.name:sha256_file(p) for p in folder.glob("*.parquet")})
            records.append(record)
            export_json(folder / "summary.json", record)
            export_json(args.output / "summary.json", report)
            print(json.dumps({k:v for k,v in record.items() if k not in {"hashes","source_path","source_sha256","source_url"}}), flush=True)
        for path, digest in pinned.items():
            if sha256_file(path) != digest:
                raise ValueError(f"An input changed during source coverage audit: {path}")
        report.update(status="audited_conflicts_require_review" if any(r["invalid_rows"] or r["differing_existing_rows"] for r in records) else "source_audited_restoration_pending",
            finished_at=datetime.now(timezone.utc).isoformat(),
            totals={k:sum(r[k] for r in records) for k in ("source_rows", "prepared_rows", "invalid_rows", "default_rows", "overlap_rows", "missing_default_rows", "differing_existing_rows", "share_differences", "market_cap_differences", "existing_without_source")})
        export_json(args.output / "summary.json", report)
        print(json.dumps(dict(status=report["status"], **report["totals"])), flush=True)
    except Exception as error:
        report.update(status="failed", error=str(error))
        export_json(args.output / "summary.json", report)
        raise


if __name__ == "__main__":
    main()
