"""Track historical factor/snapshot rebuilds by receipt and calculation version."""
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from engine.core.paths import DATA_LAKE


def _root(market, data_lake):
    return data_lake.silver("dart" if market == "kr" else "sec", "normalized", "history")


def _calculation_signature():
    root = Path(__file__).resolve().parents[1]
    paths = [root / "transformers/_internal" / name for name in
             ("financial_history.py", "sec_accession_history.py", "factor_metrics.py", "filing_periods.py")]
    paths += [root / "loaders/_internal/clickhouse_factors.py", root / "loaders/factor_snapshots.py"]
    return sha256(b"".join(path.read_bytes() for path in paths)).hexdigest()


def _ordinary_rebuild_inputs(market, state, calculation, requested, data_lake):
    """Track the local, dated files used for reviewed historical securities.

    This uses the loader's historical-identity boundary. Current issuers whose
    provider fetches financials on demand are not switched to local CSVs here.
    """
    import pandas as pd
    from engine.loaders._internal.clickhouse_factors import _reviewed_historical_symbols
    from engine.transformers._internal.statement_files import (
        consolidated_statement_path, legacy_statement_snapshot_files,
    )

    financial_dir = _root(market, data_lake).parent
    symbols = _reviewed_historical_symbols(market, data_lake=data_lake)
    if requested is not None:
        symbols &= requested
    symbols = sorted(s for s in symbols if not (financial_dir / "history" / s / "manifest.json").exists())
    if not symbols:
        return {}
    provider = "sec" if market == "us" else "dart"
    metadata_path = data_lake.silver(provider, f"{market}_report_metadata.csv")
    if market == "kr" and not metadata_path.exists():
        metadata_path = data_lake.silver(provider, "report_metadata.csv")
    metadata = pd.read_csv(metadata_path, dtype=str, keep_default_na=False) if metadata_path.exists() else pd.DataFrame()
    if not metadata.empty:
        if "stock_code" not in metadata:
            raise ValueError("Financial report metadata has no security identity")
        if "source_type" in metadata:
            metadata = metadata.loc[metadata.source_type.eq("statement")]
    inputs = {}
    for symbol in symbols:
        sid = f"SEC_{market.upper()}_{symbol}"
        previous = state["items"].get(sid, {})
        consolidated = consolidated_statement_path(financial_dir, symbol, market=market)
        paths = [consolidated] if consolidated.exists() else legacy_statement_snapshot_files(symbol, financial_dir, market=market)
        accession_manifest = financial_dir / "accessions" / symbol / "manifest.json"
        if market == "us" and accession_manifest.exists():
            from engine.transformers._internal.sec_accession_history import read_accession_rows
            read_accession_rows(financial_dir, symbol)
            manifest = json.loads(accession_manifest.read_bytes())
            paths += [accession_manifest, accession_manifest.parent / manifest["normalized_path"]]
        source_files = {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in paths}
        records = metadata.loc[metadata.stock_code.str.strip().str.upper().eq(symbol)].to_dict("records") if not metadata.empty else []
        # Other issuers' metadata rows do not invalidate this security. Keep
        # every retained row (including invalid dates), so withdrawal or a date
        # correction cannot reuse a completed calculation.
        metadata_rows = sorted(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in records)
        metadata_sha = sha256(json.dumps(metadata_rows, ensure_ascii=False).encode()).hexdigest()
        signature = sha256(json.dumps(dict(kind="ordinary_dated_financials_v1", files=source_files,
            metadata_path=str(metadata_path.resolve()), metadata_rows_sha256=metadata_sha,
            calculation=calculation), sort_keys=True).encode()).hexdigest()
        # Include the complete retained price interval on initial enrollment;
        # financial values before their first evidenced publication must clear.
        panel = data_lake.silver("corporate_actions", "prices", market, f"{market}_{symbol}.parquet")
        starts = [previous["from_date"]] if previous.get("from_date") else []
        if panel.exists():
            dates = pd.to_datetime(pd.read_parquet(panel, columns=["trade_date"]).trade_date, errors="raise")
            if not dates.empty:
                starts.append(dates.min().date().isoformat())
        if not starts:
            dates = pd.to_datetime([r.get("report_date") for r in records], errors="coerce").dropna()
            if len(dates):
                starts.append((dates.min().date() + timedelta(days=1)).isoformat())
        if not starts:
            continue
        inputs[sid] = dict(symbol=symbol, source_kind="ordinary_dated_financials", signature=signature,
            source_files=source_files, report_metadata_path=str(metadata_path.resolve()),
            report_metadata_rows_sha256=metadata_sha, calculation_sha256=calculation, from_date=min(starts))
    return inputs


def pending_rebuilds(market, basis, *, kind="factors", symbols=None, data_lake=DATA_LAKE):
    if kind not in {"factors", "snapshots"} or basis not in {"annual", "quarterly", "ttm"}:
        raise ValueError("Invalid historical financial rebuild scope")
    root = _root(market, data_lake)
    state_path = root / "rebuild_state.json"
    state = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {"items": {}}
    calculation = _calculation_signature()
    requested = set(symbols) if symbols else None
    pending = {}
    for path in sorted(root.glob("*/manifest.json")):
        if requested is not None and path.parent.name not in requested:
            continue
        raw = path.read_bytes()
        manifest = json.loads(raw)
        if manifest.get("market") != market or manifest.get("symbol") != path.parent.name or manifest.get("schema_version") != 1:
            raise ValueError("Invalid historical financial manifest identity")
        sid = f"SEC_{market.upper()}_{manifest['symbol']}"
        previous = state["items"].get(sid, {})
        starts = [(date.fromisoformat(r["report_date"]) + timedelta(days=1)).isoformat()
                  for r in manifest["receipts"]]
        if previous.get("from_date"):
            starts.append(previous["from_date"])
        if not starts:
            continue
        signature = sha256(raw + calculation.encode()).hexdigest()
        if previous.get("signature") == signature and basis in previous.get(kind, []):
            continue
        pending[sid] = {"symbol": manifest["symbol"], "manifest_path": str(path.resolve()),
            "signature": signature, "manifest_sha256": sha256(raw).hexdigest(), "calculation_sha256": calculation,
            "from_date": min(starts)}
    for sid, item in _ordinary_rebuild_inputs(market, state, calculation, requested, data_lake).items():
        previous = state["items"].get(sid, {})
        if previous.get("signature") != item["signature"] or basis not in previous.get(kind, []):
            pending[sid] = item
    return pending


def complete_rebuilds(market, basis, pending, *, kind="factors", data_lake=DATA_LAKE):
    if not pending:
        return
    current = pending_rebuilds(market, basis, kind=kind, data_lake=data_lake)
    for sid, item in pending.items():
        if sid not in current or current[sid]["signature"] != item["signature"]:
            raise ValueError("Financial inputs changed during rebuild")
    path = _root(market, data_lake) / "rebuild_state.json"
    state = json.loads(path.read_text("utf-8")) if path.exists() else {"schema_version": 1, "items": {}}
    for sid, item in pending.items():
        previous = state["items"].get(sid, {})
        record = previous if previous.get("signature") == item["signature"] else {**item, "factors": [], "snapshots": []}
        record[kind] = sorted(set(record.get(kind, [])) | {basis})
        state["items"][sid] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
