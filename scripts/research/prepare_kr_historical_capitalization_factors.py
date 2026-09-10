"""Reconcile restored share observations, actual prices and native market factors.

Read-only with respect to ClickHouse. Every monthly source/readback, discrepancy,
and public factor preparation is saved in silver. Quote coverage is distinct
from historical common-stock listing approval.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import sha256_file
from engine.loaders.factors import prepare_daily_factor_rows
from engine.transformers.factors import add_daily_market_valuation_factors
from publish_kr_survivorship_capital_factors import latest_nullable, COLUMNS

KEYS = ["security_id", "trade_date"]
FACTORS = ["mcap_mil", "csho"]
BASES = ["annual", "quarterly", "ttm"]


def latest_prices(frame):
    if frame.empty:
        return frame, frame
    selected = frame.loc[frame.updated_at.eq(frame.groupby(KEYS).updated_at.transform("max"))]
    ambiguous = selected.groupby(KEYS)[["close", "volume", "currency"]].nunique(dropna=False).gt(1).any(axis=1)
    return selected.drop_duplicates(KEYS), ambiguous.loc[ambiguous].reset_index()[KEYS]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--share-publication", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--years", type=int, nargs="+", help="Restrict a read-only preparation to these audited years")
    parser.add_argument("--observation-sources", type=Path, help="Verified index including fixed-date originals after annual files")
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Preparation and readback files must be in silver")
    args.output.mkdir(parents=True, exist_ok=False)
    publication = json.loads(args.share_publication.read_text("utf-8"))
    if not publication["default_input_restored"]:
        raise ValueError("Restore regular share input before preparing factors")
    audit_refs, source_years = {}, []
    if "preparation_path" in publication:
        preparation_path = Path(publication["preparation_path"])
        if sha256_file(preparation_path) != publication["preparation_sha256"]:
            raise ValueError("Full-source preparation changed")
        preparation = json.loads(preparation_path.read_text("utf-8"))
        audit_refs[str(preparation_path)] = publication["preparation_sha256"]
        for entry in preparation["audits"]:
            source_audit = Path(entry["path"])
            if sha256_file(source_audit) != entry["sha256"]:
                raise ValueError("Original observation audit changed")
            audit_refs[str(source_audit)] = entry["sha256"]
            audit = json.loads(source_audit.read_text("utf-8"))
            source_years.extend(dict(year=year["year"], path=source_audit.parent / str(year["year"]) / "prepared.parquet",
                sha256=year["hashes"]["prepared.parquet"]) for year in audit["years"])
    else:
        source_audit = Path(publication["source_audit_path"])
        if sha256_file(source_audit) != publication["source_audit_sha256"]:
            raise ValueError("Original observation audit changed")
        audit_refs[str(source_audit)] = publication["source_audit_sha256"]
        audit = json.loads(source_audit.read_text("utf-8"))
        source_years = [dict(year=year["year"], path=source_audit.parent / f"prepared_{year['year']}.parquet",
            sha256=year["prepared_sha256"]) for year in audit["years"]]
    if args.observation_sources:
        index = json.loads(args.observation_sources.read_text("utf-8"))
        if (index["status"] != "original_observations_verified"
                or index["share_publication_sha256"] != sha256_file(args.share_publication)
                or index["default_input_sha256"] != publication["default_input_sha256"]
                or index["registration_sha256"] != publication["registration_sha256"]):
            raise ValueError("Observation index differs from the published share-input scope")
        for path, digest in index["source_audits_sha256"].items():
            if sha256_file(path) != digest:
                raise ValueError("An indexed original observation changed")
        audit_refs.update(index["source_audits_sha256"])
        audit_refs[str(args.observation_sources.resolve())] = sha256_file(args.observation_sources)
        source_years = [dict(year=year["year"], path=Path(year["path"]), sha256=year["sha256"]) for year in index["years"]]
    if args.years:
        if not set(args.years).issubset({year["year"] for year in source_years}):
            raise ValueError("Requested year is outside the audited original scope")
        source_years = [year for year in source_years if year["year"] in args.years]
    cutoff = pd.Timestamp(publication.get("last_price_calendar_date", f"{max(year['year'] for year in source_years)}-12-31"))
    if args.observation_sources and pd.Timestamp(index["end_date"]) != cutoff:
        raise ValueError("Observation source cutoff differs from the published input")
    default_input = Path(publication["default_input_path"])
    if sha256_file(default_input) != publication["default_input_sha256"]:
        raise ValueError("Restored default input changed")
    registration_path = DATA_LAKE.silver("krx", "shares", "historical_sources.json")
    registration_hash = sha256_file(registration_path)
    registration = json.loads(registration_path.read_text("utf-8"))
    for source in registration["sources"]:
        if sha256_file(DATA_LAKE.root / source["path"]) != source["sha256"]:
            raise ValueError("Registered original changed")
        if (not args.observation_sources and source["format"] != "marcap_parquet"
                and source["year"] in {year["year"] for year in source_years}
                and "preparation_path" in publication):
            raise ValueError("Use the verified observation index to retain the fixed-date original source")
    implementation = args.output / "implementation"
    implementation.mkdir()
    code_paths = [Path(__file__), ROOT / "engine/transformers/_internal/factor_metrics.py",
        ROOT / "engine/loaders/_internal/clickhouse_factors.py",
        ROOT / "scripts/research/publish_kr_survivorship_capital_factors.py"]
    for path in code_paths:
        shutil.copy2(path, implementation / path.name)
    months = []
    report = dict(status="running", native_published=False, snapshots_published=False, coverage_complete=False,
        share_publication_path=str(args.share_publication.resolve()), share_publication_sha256=sha256_file(args.share_publication),
        default_input_sha256=publication["default_input_sha256"], registration_sha256=registration_hash,
        implementation_sha256={str(path):sha256_file(path) for path in code_paths}, months=months,
        source_audits_sha256=audit_refs, selected_years=[year["year"] for year in source_years],
        end_date=cutoff.date().isoformat(), requested_months=[str(month) for year in source_years
            for month in pd.period_range(f"{year['year']}-01", min(pd.Timestamp(f"{year['year']}-12-31"), cutoff), freq="M")],
        policy="Only same-date positive raw price observations can support prepared market factors. No listing episodes are approved. Existing native discrepancies remain review items; no writes or deletions.")
    export_json(args.output / "summary.json", report)
    query_prices = """SELECT security_id,trade_date,close,volume,currency,updated_at FROM price_daily
        WHERE trade_date >= {start:Date} AND trade_date < {end:Date} AND trade_date <= {cutoff:Date}
        AND startsWith(security_id,'SEC_KR_')
        SETTINGS max_execution_time=25,max_threads=2"""
    query_native = "SELECT " + ",".join(COLUMNS) + """ FROM fact_daily_factors
        WHERE trade_date >= {start:Date} AND trade_date < {end:Date} AND trade_date <= {cutoff:Date}
        AND factor_id IN ('mcap_mil','csho') AND financial_basis IN ('annual','quarterly','ttm')
        AND startsWith(security_id,'SEC_KR_') SETTINGS max_execution_time=25,max_threads=2"""
    (args.output / "prices.sql").write_text(query_prices, "utf-8")
    (args.output / "native.sql").write_text(query_native, "utf-8")
    client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=35)
    try:
        for year in source_years:
            source_path = year["path"]
            if sha256_file(source_path) != year["sha256"]:
                raise ValueError("Audited observations changed")
            source = pd.read_parquet(source_path)
            source.trade_date = pd.to_datetime(source.trade_date)
            source = source.loc[source.trade_date.le(cutoff)].copy()
            for month in pd.period_range(f"{year['year']}-01", min(pd.Timestamp(f"{year['year']}-12-31"), cutoff), freq="M"):
                folder = args.output / str(month)
                folder.mkdir()
                params = dict(start=month.start_time.date(), end=(month+1).start_time.date(), cutoff=cutoff.date())
                prices = client.query_df(query_prices, parameters=params)
                if prices.empty and not len(prices.columns):
                    prices = pd.DataFrame(columns=KEYS + ["close", "volume", "currency", "updated_at"])
                prices.to_parquet(folder / "price_before.parquet", index=False)
                prices.trade_date = pd.to_datetime(prices.trade_date)
                prices, price_ambiguity = latest_prices(prices)
                price_ambiguity.to_parquet(folder / "price_ambiguity.parquet", index=False)
                native = client.query_df(query_native, parameters=params)
                if native.empty and not len(native.columns):
                    native = pd.DataFrame(columns=COLUMNS)
                native.to_parquet(folder / "native_before.parquet", index=False)
                native.trade_date = pd.to_datetime(native.trade_date)
                physical_rows = len(native)
                native, native_ambiguity = latest_nullable(native)
                native_ambiguity.to_parquet(folder / "native_ambiguity.parquet", index=False)
                observed = source.loc[source.trade_date.between(month.start_time, month.end_time)].set_index(KEYS).sort_index()
                quoted = prices.set_index(KEYS).sort_index()
                common = observed.index.intersection(quoted.index)
                missing_price = observed.index.difference(quoted.index)
                missing_source = quoted.index.difference(observed.index)
                observed.loc[missing_price].reset_index().to_parquet(folder / "source_without_price.parquet", index=False)
                quoted.loc[missing_source].reset_index().to_parquet(folder / "price_without_source.parquet", index=False)
                wanted = observed.loc[common].join(quoted.loc[common], rsuffix="_native")
                close = pd.to_numeric(wanted.close, errors="coerce").to_numpy(float)
                valid_price = np.isfinite(close) & (close > 0) & wanted.currency.eq("KRW").to_numpy()
                same_price = np.isclose(close, wanted.raw_close.to_numpy(float), rtol=1e-10, atol=1e-6)
                matched = valid_price & same_price
                wanted.loc[~matched].reset_index().to_parquet(folder / "price_discrepancies.parquet", index=False)
                usable = wanted.loc[matched].reset_index()
                calculation = add_daily_market_valuation_factors(usable[KEYS + ["shares", "market_cap", "close", "volume", "currency"]])
                # Independent source oracle: known currency units, without reuse of the production formula.
                np.testing.assert_allclose(calculation.mcap_mil.to_numpy(float) * 1000000,
                    usable.market_cap.to_numpy(float), rtol=1e-12, atol=1)
                np.testing.assert_array_equal(calculation.csho.to_numpy(float), usable.shares.to_numpy(float))
                calculation["updated_at"] = datetime.now(ZoneInfo("Asia/Seoul"))
                prepared = pd.concat([prepare_daily_factor_rows(calculation, financial_basis=basis, factor_ids=FACTORS)
                    for basis in BASES], ignore_index=True)
                prepared.trade_date = pd.to_datetime(prepared.trade_date)
                prepared.to_parquet(folder / "prepared.parquet", index=False)
                full_keys = KEYS + ["factor_id", "financial_basis"]
                before, expected = native.set_index(full_keys), prepared.set_index(full_keys)
                overlap = before.index.intersection(expected.index)
                native_extra = before.index.difference(expected.index)
                additions = expected.index.difference(before.index)
                same = np.isclose(before.loc[overlap, "factor_value"].to_numpy(float),
                    expected.loc[overlap, "factor_value"].to_numpy(float), rtol=1e-10, atol=1e-8, equal_nan=True)
                differences = before.loc[overlap].join(expected.loc[overlap, ["factor_value"]], rsuffix="_expected").loc[~same]
                differences.reset_index().to_parquet(folder / "native_value_discrepancies.parquet", index=False)
                before.loc[native_extra].reset_index().to_parquet(folder / "native_outside_prepared.parquet", index=False)
                expected.loc[additions].reset_index().to_parquet(folder / "new_keys.parquet", index=False)
                record = dict(month=str(month), source_rows=len(observed), price_rows=len(prices), matched_source_price_rows=len(usable),
                    source_without_price=len(missing_price), price_without_source=len(missing_source), price_discrepancies=int((~matched).sum()),
                    price_ambiguities=len(price_ambiguity), native_ambiguities=len(native_ambiguity), native_physical_rows=physical_rows,
                    native_latest_rows=len(native), prepared_rows=len(prepared), matching_existing_native_rows=int(same.sum()),
                    new_native_keys=len(additions), differing_native_values=len(differences), native_outside_prepared=len(native_extra),
                    hashes={path.name:sha256_file(path) for path in folder.glob("*.parquet")})
                months.append(record)
                export_json(folder / "summary.json", record)
                export_json(args.output / "summary.json", report)
                print({k:v for k,v in record.items() if k!='hashes'}, flush=True)
        if sha256_file(default_input) != report["default_input_sha256"] or sha256_file(registration_path) != registration_hash:
            raise ValueError("Input changed during preparation")
        for path, digest in audit_refs.items():
            if sha256_file(path) != digest:
                raise ValueError("Original source audit changed during preparation")
        for year in source_years:
            if sha256_file(year["path"]) != year["sha256"]:
                raise ValueError("Prepared original observations changed during calculation")
        report["totals"] = {key:sum(row[key] for row in months) for key in months[0] if key not in {"month", "hashes"}}
        report["status"] = "prepared_discrepancies_require_review" if any(report["totals"][key] for key in
            ["source_without_price", "price_without_source", "price_discrepancies", "price_ambiguities", "native_ambiguities", "differing_native_values", "native_outside_prepared"]) else "prepared_native_publication_pending"
        export_json(args.output / "summary.json", report)
        print(report["status"], report["totals"], flush=True)
    except BaseException as error:
        report.update(status="failed_check_month_checkpoints", error_type=type(error).__name__, error=str(error))
        export_json(args.output / "summary.json", report)
        raise
    finally:
        client.close()


if __name__ == "__main__":
    main()
