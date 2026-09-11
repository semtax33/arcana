"""Preserve normalized SEC accessions independently of latest-period exports."""
from hashlib import sha256
import io
import json
from pathlib import Path
from uuid import uuid4

import pandas as pd


def read_accession_rows(financial_dir, symbol):
    root = Path(financial_dir) / "accessions" / symbol
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return set(), pd.DataFrame()
    manifest = json.loads(manifest_path.read_bytes())
    if (manifest.get("schema_version") != 1 or manifest.get("market") != "us"
            or manifest.get("symbol") != symbol):
        raise ValueError("SEC accession history identity/schema mismatch")
    path = (root / manifest["normalized_path"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("SEC accession history path leaves its directory")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != manifest["normalized_sha256"]:
        raise ValueError("SEC accession history digest mismatch")
    frame = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
    if not frame.empty and not frame.symbol.eq(symbol).all():
        raise ValueError("SEC accession rows contain another security")
    return set(manifest["covered_fiscal_years"]), frame


def write_accession_rows(candidates, *, output_dir, symbols, year_range,
                         canonical_names, replace_existing=False):
    from engine.transformers._internal.sec_filings import (
        add_formula_derived_candidates, candidate_to_rows, dedupe_candidates,
    )
    by_symbol = {symbol: {} for symbol in symbols}
    for candidate in candidates:
        key = (candidate.cik, candidate.accn, candidate.filed,
               candidate.fiscal_year, candidate.fiscal_month)
        by_symbol.setdefault(candidate.symbol, {}).setdefault(key, []).append(candidate)
    years = set(range(year_range[0], year_range[1] + 1))
    for symbol, groups in sorted(by_symbol.items()):
        rows = []
        for _, group in sorted(groups.items()):
            selected = dedupe_candidates(group)
            selected = add_formula_derived_candidates(selected, canonical_names=canonical_names)
            rows.extend(candidate_to_rows(candidate)[1] for candidate in selected)
        old_years, old = read_accession_rows(output_dir, symbol)
        if not old.empty:
            old = old.loc[~pd.to_numeric(old.fiscal_year).isin(years)]
        if replace_existing:
            old_years, old = set(), pd.DataFrame()
        columns = ["symbol", "canonical_account_id", "normalized_amount", "fiscal_year", "fiscal_month",
                   "period_end", "filed", "accn", "form", "statement_type", "original_account_name"]
        frame = pd.concat([old, pd.DataFrame(rows)], ignore_index=True)
        if frame.empty:
            frame = pd.DataFrame(columns=columns)
        else:
            frame = frame.sort_values(["filed", "accn", "fiscal_year", "fiscal_month", "canonical_account_id"], kind="stable")
        raw = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
        digest = sha256(raw).hexdigest()
        root = Path(output_dir) / "accessions" / symbol
        root.mkdir(parents=True, exist_ok=True)
        path = root / (digest + ".csv")
        if not path.exists():
            path.write_bytes(raw)
        manifest = dict(schema_version=1, market="us", symbol=symbol,
                        covered_fiscal_years=sorted(old_years | years),
                        normalized_path=path.name, normalized_sha256=digest)
        encoded = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        manifest_path = root / "manifest.json"
        if not manifest_path.exists() or manifest_path.read_bytes() != encoded:
            temporary = root / ("." + uuid4().hex + ".tmp")
            temporary.write_bytes(encoded)
            temporary.replace(manifest_path)


def accession_financial_versions(frame, *, symbol, basis):
    from engine.transformers._internal.filing_periods import reported_flow_amounts
    from engine.transformers._internal.factor_metrics import (
        aggregate_annual_canonical_values, extract_fallback_values, security_id_for_market,
    )
    rows = []
    if frame.empty:
        return rows
    keys = ["cik", "accn", "filed", "fiscal_year", "fiscal_month"]
    for (_, accession, published, year, month), group in frame.groupby(keys, sort=True):
        if basis == "annual" and int(month) != 12:
            continue
        # Cover-page shares and the cash-flow opening instant have their own
        # observation dates. Neither establishes the filing's period end.
        periods = group.loc[~group.canonical_account_id.isin(
            ["COMMON_SHARES_OUTSTANDING", "CF_CASH_BEGIN"]), "period_end"].unique()
        if len(periods) == 0:
            # A cover-page share observation is retained in the accession file,
            # but it does not establish a new financial statement period.
            continue
        if len(periods) != 1:
            raise ValueError("SEC accession has ambiguous financial periods")
        period_end, report_date = pd.Timestamp(periods[0]), pd.Timestamp(published)
        if pd.isna(period_end) or pd.isna(report_date) or report_date < period_end or not accession:
            raise ValueError("SEC accession lacks a valid publication/period identity")
        group = group.copy()
        reported_per_share, reported_flows = {}, {}
        per_share_ids = {"BASIC_EPS", "DILUTED_EPS", "BASIC_SHARES", "DILUTED_SHARES"}
        if "reported_durations" in group:
            for record in group.to_dict("records"):
                encoded = record.get("reported_durations")
                if isinstance(encoded, str) and encoded:
                    target = reported_per_share if record["canonical_account_id"] in per_share_ids else reported_flows
                    target[record["canonical_account_id"]] = dict(form=record["form"],
                        period_end=record["period_end"], observations=json.loads(encoded))
        if basis == "annual" and "period_semantic" in group:
            # A 10-K can disclose standalone quarters too. Its form and year
            # do not turn a QTD/YTD flow into a full-year observation.
            partial_flow = group.statement_type.ne("BS") & group.period_semantic.isin(["QTD", "YTD"])
            for index in group.index[partial_flow]:
                account = group.at[index, "canonical_account_id"]
                annual = reported_flow_amounts(reported_flows.get(account))
                if annual["annual_reported"]:
                    group.at[index, "normalized_amount"] = annual["annual"]
                    partial_flow.at[index] = False
            group = group.loc[~partial_flow].copy()
        group["normalized_amount"] = pd.to_numeric(group.normalized_amount, errors="coerce")
        values = aggregate_annual_canonical_values(group)
        values.update(extract_fallback_values(group))
        values.update(stock_code=symbol, security_id=security_id_for_market(symbol, "us"),
                      fiscal_year=int(year), fiscal_month=int(month), financial_period=period_end,
                      report_date=report_date, rcept_no=accession, source_url="",
                      financial_scope="UNKNOWN", accounting_regime="UNKNOWN",
                      _reported_per_share=reported_per_share,
                      _reported_flow_periods=reported_flows,
                      _requires_reported_per_share=bool(reported_per_share),
                      _period_semantics_by_id=dict(zip(group.canonical_account_id,
                          group.get("period_semantic", pd.Series("", index=group.index)))),
                      _fs_type_by_id=dict(zip(group.canonical_account_id, group.statement_type)))
        rows.append(values)
    return rows
