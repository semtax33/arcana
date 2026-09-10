"""Bind every dated market observation to its audited original source."""
import argparse
from datetime import datetime, timezone
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
from engine.core.serving_storage import export_frame, export_json

KEYS = ["security_id", "trade_date"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--share-publication", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Prepared sources must remain in Silver")
    args.output.mkdir(parents=True, exist_ok=False)
    pinned = {}

    def check(path, digest=None):
        path = Path(path).resolve()
        actual = sha256_file(path)
        if digest is not None and actual != digest:
            raise ValueError(f"Source evidence changed: {path}")
        pinned[str(path)] = actual
        return actual

    def read(path, digest=None):
        check(path, digest)
        return json.loads(Path(path).read_text("utf-8"))

    publication = read(args.share_publication)
    if not publication["default_input_restored"]:
        raise ValueError("A verified regular share-input publication is required")
    check(publication["default_input_path"], publication["default_input_sha256"])
    registry = read(publication["registration_path"], publication["registration_sha256"])
    preparation = read(publication["preparation_path"], publication["preparation_sha256"])
    source_years = {}
    for entry in preparation["audits"]:
        path = Path(entry["path"])
        audit = read(path, entry["sha256"])
        for item in audit["years"]:
            source = path.parent / str(item["year"]) / "prepared.parquet"
            check(source, item["hashes"][source.name])
            source_years[item["year"]] = dict(year=item["year"], path=str(source.resolve()),
                sha256=item["hashes"][source.name], rows=item["prepared_rows"])
    supplemental = {}
    for source in registry["sources"]:
        path = DATA_LAKE.root / source["path"]
        check(path, source["sha256"])
        if source["format"] == "marcap_parquet":
            continue
        if source["format"] != "fdr_listing_csv":
            raise ValueError("Unsupported registered observation source")
        raw = pd.read_csv(path, dtype={"Code":str})
        codes = raw.Code.str.strip().str.upper().str.zfill(6)
        numeric = raw[["Close", "Volume", "Stocks", "Marcap"]].apply(pd.to_numeric, errors="raise")
        if (not codes.str.fullmatch(r"[0-9A-Z]{6}").all() or codes.duplicated().any()
                or not np.isfinite(numeric.to_numpy(float)).all()
                or not numeric[["Close", "Stocks", "Marcap"]].gt(0).all().all()
                or not numeric.Volume.ge(0).all() or not numeric.Stocks.mod(1).eq(0).all()
                or not np.isclose(numeric.Close * numeric.Stocks, numeric.Marcap, rtol=1e-10, atol=1).all()):
            raise ValueError("Fixed-date original has invalid or conflicting market observations")
        day = pd.Timestamp(source["observation_date"])
        if day.year != source["year"] or day > pd.Timestamp(publication["last_price_calendar_date"]):
            raise ValueError("Fixed-date source lies outside the verified publication scope")
        frame = pd.DataFrame(dict(security_id="SEC_KR_" + codes, trade_date=day, shares=numeric.Stocks,
            market_cap=numeric.Marcap, raw_close=numeric.Close, raw_volume=numeric.Volume,
            source_name=raw.Name, source_market=raw.Market, source_id=source["source_id"]))
        supplemental.setdefault(day.year, []).append(frame)
    records = []
    for year, item in sorted(source_years.items()):
        if year in supplemental:
            original = pd.read_parquet(item["path"])
            combined = pd.concat([original, *supplemental[year]], ignore_index=True)
            combined.trade_date = pd.to_datetime(combined.trade_date).astype("datetime64[ns]")
            if combined.duplicated(KEYS).any():
                raise ValueError("Supplementary source duplicates an original observation")
            before = next(record for record in publication["years"] if record["year"] == year)
            check(before["candidate_path"], before["candidate_sha256"])
            candidate = pd.read_parquet(before["candidate_path"]).set_index(KEYS).sort_index()
            wanted = combined.set_index(KEYS).sort_index()
            if not candidate.index.equals(wanted.index):
                raise ValueError("Combined originals differ from published observation identities")
            np.testing.assert_array_equal(candidate.shares.to_numpy(float), wanted.shares.to_numpy(float))
            np.testing.assert_allclose(candidate.market_cap.to_numpy(float), wanted.market_cap.to_numpy(float), rtol=1e-10, atol=1)
            target = args.output / f"prepared_{year}.parquet"
            export_frame(target, combined.sort_values(KEYS).reset_index(drop=True))
            item = dict(year=year, path=str(target.resolve()), sha256=check(target), rows=len(combined),
                original_year_rows=len(original), supplementary_rows=sum(len(f) for f in supplemental[year]))
        records.append(item)
    if sum(record["rows"] for record in records) != publication["staged_rows"]:
        raise ValueError("The original-source index does not cover the complete published share input")
    check(__file__)
    shutil.copy2(__file__, args.output / Path(__file__).name)
    for path, digest in pinned.items():
        if sha256_file(path) != digest:
            raise ValueError("An original source changed during preparation")
    result = dict(status="original_observations_verified", created_at=datetime.now(timezone.utc).isoformat(),
        share_publication_path=str(args.share_publication.resolve()), share_publication_sha256=sha256_file(args.share_publication),
        default_input_sha256=publication["default_input_sha256"], registration_sha256=publication["registration_sha256"],
        end_date=publication["last_price_calendar_date"], years=records, source_audits_sha256=pinned,
        total_rows=sum(r["rows"] for r in records), native_published=False, snapshots_published=False, coverage_complete=False,
        policy="Retain every audited original observation, including the fixed-date snapshot after the last annual file date. No new issuer or listing eligibility is approved.")
    export_json(args.output / "summary.json", result)
    print(json.dumps({key:result[key] for key in ("status", "total_rows", "end_date")}), flush=True)


if __name__ == "__main__":
    main()
