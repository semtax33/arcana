"""Audit dated capitalization sources missing from the regular KR share input.

Source arithmetic is separate from historical issuer/listing approval. This
prepares silver observations and discrepancies; it changes no pipeline input.
"""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from validate_kr_survivorship_financial_factors import digest, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, nargs="+", default=[2013, 2014, 2015])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists(), "Preserve prior audit evidence"
    args.output.mkdir(parents=True)
    manifest_path = DATA_LAKE.meta("survivorship", "kr_reviewed.json")
    manifest_hash = digest(manifest_path)
    manifest = json.loads(manifest_path.read_text("utf-8"))
    sources = {r["source_id"]: r for r in manifest["sources"] if r.get("provider") == "MARCAP"}
    keys = ["security_id", "trade_date"]
    frames, checks = [], []
    report = dict(status="running", native_published=False, default_input_changed=False, coverage_complete=False,
        implementation_sha256=digest(__file__), reviewed_manifest_sha256=manifest_hash, years=checks,
        identity_scope="Source arithmetic and date/code uniqueness only. Alphanumeric preferred-share codes are retained as observations; this audit does not establish common-stock eligibility or approve new issuer/listing identities.")
    save(args.output / "summary.json", report)
    for year in args.years:
        source = sources[f"marcap-{year}"]
        path = ROOT / source["path"]
        assert digest(path) == source["source_sha256"]
        raw = pd.read_parquet(path, columns=["Code", "Date", "Name", "Market", "Close", "Stocks", "Marcap"])
        codes = raw.Code.astype(str).str.strip().str.zfill(6)
        assert codes.str.fullmatch(r"[0-9A-Z]{6}").all(), "Unrecognized source security code"
        dates = pd.to_datetime(raw.Date, errors="raise")
        assert dates.dt.year.eq(year).all()
        value = raw[["Close", "Stocks", "Marcap"]].apply(pd.to_numeric, errors="raise")
        positive = np.isfinite(value).all(axis=1) & value.Close.gt(0) & value.Stocks.gt(0) & value.Marcap.gt(0) & value.Stocks.mod(1).eq(0)
        identity = np.isclose(value.Close * value.Stocks, value.Marcap, rtol=1e-8, atol=1)
        valid = positive & identity
        out = pd.DataFrame(dict(security_id="SEC_KR_"+codes, trade_date=dates, shares=value.Stocks,
            market_cap=value.Marcap, raw_close=value.Close, source_name=raw.Name, source_market=raw.Market,
            source_id=source["source_id"]))
        assert not out.duplicated(keys).any(), "Source has ambiguous date/code observations"
        out.loc[~valid].to_parquet(args.output / f"invalid_{year}.parquet", index=False)
        out = out.loc[valid].copy()
        path_out = args.output / f"prepared_{year}.parquet"
        out.to_parquet(path_out, index=False)
        frames.append(out)
        checks.append(dict(year=year, source_rows=len(raw), prepared_rows=len(out), invalid_rows=int((~valid).sum()),
            non_positive_or_missing_rows=int((~positive).sum()), identity_mismatches=int((~identity).sum()),
            alphanumeric_code_rows=int((~codes.str.isdigit()).sum()),
            distinct_codes=int(codes.nunique()), trade_dates=int(dates.nunique()), source_sha256=source["source_sha256"],
            source_url=source["source_url"], prepared_sha256=digest(path_out)))
        assert digest(ROOT / source["path"]) == source["source_sha256"]
        save(args.output / "summary.json", report)
        print(checks[-1], flush=True)
    prepared = pd.concat(frames, ignore_index=True)
    current_path = DATA_LAKE.silver("krx", "shares", "kr_normalized_shares.csv")
    current_hash = digest(current_path)
    parts = []
    for part in pd.read_csv(current_path, usecols=keys+["shares", "market_cap"], chunksize=500000):
        part.trade_date = pd.to_datetime(part.trade_date, errors="raise")
        part = part.loc[part.trade_date.dt.year.isin(args.years)]
        if not part.empty:
            parts.append(part)
    current = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=keys+["shares", "market_cap"])
    assert not current.duplicated(keys).any()
    a, b = current.set_index(keys).sort_index(), prepared.set_index(keys).sort_index()
    common = a.index.intersection(b.index)
    missing, extra = b.index.difference(a.index), a.index.difference(b.index)
    shares_equal = np.isclose(a.loc[common, "shares"], b.loc[common, "shares"], rtol=0, atol=0)
    cap_equal = np.isclose(a.loc[common, "market_cap"], b.loc[common, "market_cap"], rtol=1e-8, atol=1)
    changes = a.loc[common].join(b.loc[common], lsuffix="_existing", rsuffix="_source").loc[~shares_equal | ~cap_equal].reset_index()
    changes.to_parquet(args.output / "existing_value_differences.parquet", index=False)
    a.loc[extra].reset_index().to_parquet(args.output / "existing_keys_outside_source.parquet", index=False)
    assert digest(current_path) == current_hash and digest(manifest_path) == manifest_hash
    report.update(status="source_audited_publication_pending", total_source_rows=sum(r["source_rows"] for r in checks),
        prepared_source_rows=len(prepared), invalid_source_rows=sum(r["invalid_rows"] for r in checks),
        default_share_input_rows=len(current), overlap_rows=len(common), missing_default_input_rows=len(missing),
        existing_keys_outside_source=len(extra), differing_existing_rows=len(changes),
        share_differences=int((~shares_equal).sum()), capitalization_differences=int((~cap_equal).sum()),
        default_share_input_path=str(current_path), default_share_input_sha256=current_hash)
    save(args.output / "summary.json", report)
    print({k:v for k,v in report.items() if k not in {"years", "identity_scope"}}, flush=True)


if __name__ == "__main__":
    main()
