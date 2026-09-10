"""Rebuild the issuer/date scope of published normalized share changes.

The refresh command holds SourceRefreshLock while using this module. Journals,
prepared rows, preimages and completion records are derived Silver data.
"""
from datetime import date, timedelta
import json
from pathlib import Path
from uuid import uuid4

import pandas as pd

from engine.core.paths import DATA_LAKE, market_csv_name
from engine.core.serving_storage import export_frame, export_json
from engine.transformers.share_input_history import file_digest, assert_published_share_input


KEYS = ["trade_date", "security_id", "factor_id", "financial_basis"]
PAYLOAD = ["factor_value", "fiscal_year", "financial_period", "currency"]


def pending_rebuilds(market, basis, *, kind="factors", symbols=None, data_lake=DATA_LAKE):
    if basis not in {"annual", "quarterly", "ttm"} or kind not in {"factors", "snapshots"}:
        raise ValueError("Invalid share input rebuild scope")
    if market != "kr":
        return {}
    root = data_lake.silver("market_input_changes", market)
    state_path = root / "rebuild_state.json"
    state = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {}
    requested = {"SEC_KR_" + symbol for symbol in symbols} if symbols else None
    reports = [(path, json.loads(path.read_text("utf-8"))) for path in sorted(root.glob("*/report.json"))]
    published_preimages = {report["before_sha256"] for _, report in reports if report.get("status") == "published"}
    actual_hash = None
    pending = {}
    for path, report in reports:
        if report.get("schema_version") != 1 or report.get("market") != "kr" or report.get("kind") != "shares":
            raise ValueError("Invalid normalized share change journal")
        if Path(report["input_path"]).resolve() != data_lake.silver("krx", "shares", market_csv_name("normalized_shares")).resolve():
            raise ValueError("Share change journal points outside the normalized input")
        if report.get("status") == "prepared":
            # A crash between the atomic CSV swap and the receipt must not
            # discard the earlier invalidation when normalization is retried.
            if actual_hash is None:
                actual_hash = file_digest(report["input_path"]) if Path(report["input_path"]).exists() else None
            if report["after_sha256"] not in published_preimages and actual_hash != report["after_sha256"]:
                if actual_hash == report["before_sha256"] or report["before_sha256"] in published_preimages:
                    continue
                raise ValueError("Cannot determine whether the prepared share input was published")
        elif report.get("status") != "published":
            raise ValueError("Unknown share change journal status")
        if report.get("changed_observations_sha256") and file_digest(path.parent / "changed_observations.parquet") != report["changed_observations_sha256"]:
            raise ValueError("Share change observations no longer match their journal")
        for sid, changed in report["changed_securities"].items():
            if requested is not None and sid not in requested:
                continue
            completed = state.get(path.parent.name, {}).get(sid, {}).get(kind, {}).get(basis)
            if completed and completed >= changed["last_changed_date"]:
                continue
            start = changed["from_date"]
            if completed:
                start = max(start, (date.fromisoformat(completed) + timedelta(days=1)).isoformat())
            item = pending.setdefault(sid, {"symbol": sid.removeprefix("SEC_KR_"), "from_date": start, "journals": {}})
            item["from_date"] = min(item["from_date"], start)
            item["journals"][path.parent.name] = file_digest(path)
    return pending


def _input_files(data_lake):
    assert_published_share_input(data_lake=data_lake)
    files = {}
    for path in (data_lake.silver("krx", "shares", market_csv_name("normalized_shares")),
                 data_lake.silver("krx", "shares", "historical_sources.json")):
        stat = path.stat() if path.exists() else None
        files[str(path.resolve())] = dict(sha256=file_digest(path) if stat else None,
            size=stat.st_size if stat else None, mtime_ns=stat.st_mtime_ns if stat else None)
    return files


def _assert_unchanged(pending, basis, *, kind, data_lake, inputs):
    for name, expected in inputs.items():
        path = Path(name)
        stat = path.stat() if path.exists() else None
        if (stat.st_size if stat else None, stat.st_mtime_ns if stat else None) != (expected["size"], expected["mtime_ns"]):
            raise ValueError("Share inputs changed during historical rebuild")
    current = pending_rebuilds("kr", basis, kind=kind,
        symbols=[item["symbol"] for item in pending.values()], data_lake=data_lake)
    if current != pending:
        raise ValueError("Share inputs changed during historical rebuild")


def _complete(pending, basis, through_date, *, kind, data_lake, inputs):
    _assert_unchanged(pending, basis, kind=kind, data_lake=data_lake, inputs=inputs)
    if any((file_digest(name) if Path(name).exists() else None) != expected["sha256"] for name, expected in inputs.items()):
        raise ValueError("Share input hash changed during historical rebuild")
    path = data_lake.silver("market_input_changes", "kr", "rebuild_state.json")
    state = json.loads(path.read_text("utf-8")) if path.exists() else {}
    for sid, item in pending.items():
        for journal in item["journals"]:
            record = state.setdefault(journal, {}).setdefault(sid, {}).setdefault(kind, {})
            record[basis] = max(record.get(basis, ""), through_date.isoformat())
    export_json(path, state)


def _capture(client, table, sid, basis, start, end, factor_ids):
    payload = PAYLOAD + (["source_trade_date"] if table == "fact_daily_factor_snapshot" else [])
    selected = ", ".join(f"row.{i + 1} AS {column}" for i, column in enumerate(payload + ["updated_at"]))
    query = f"""SELECT {', '.join(KEYS)}, {selected} FROM (
        SELECT {', '.join(KEYS)}, argMax(tuple({', '.join(payload + ['updated_at'])}), updated_at) AS row
        FROM {table} WHERE security_id = {{sid:String}} AND financial_basis = {{basis:String}}
        AND trade_date BETWEEN {{start:Date}} AND {{end:Date}} AND factor_id IN {{factors:Array(String)}}
        GROUP BY {', '.join(KEYS)})"""
    result = client.query(query, parameters=dict(sid=sid, basis=basis, start=start, end=end, factors=factor_ids))
    return pd.DataFrame(result.result_rows, columns=KEYS + payload + ["updated_at"])


def _canonical(frame):
    frame = frame.copy()
    for column in ("trade_date", "financial_period", "source_trade_date"):
        if column in frame:
            frame[column] = pd.to_datetime(frame[column]).astype("datetime64[ns]")
    if "updated_at" in frame:
        frame["updated_at"] = pd.to_datetime(frame.updated_at, utc=True).astype("datetime64[ns, UTC]")
    return frame.sort_values(KEYS).reset_index(drop=True)


def _publish_month(client, table, prepared, *, sid, basis, start, end, folder, assert_inputs, factor_ids=None):
    """Keep identical versions, explicitly revoke missing cells, verify readback."""
    prepared = _canonical(prepared)
    if prepared.duplicated(KEYS).any():
        raise ValueError("Duplicate prepared factor keys")
    factor_ids = sorted(prepared.factor_id.unique()) if factor_ids is None else factor_ids
    before = _canonical(_capture(client, table, sid, basis, start, end, factor_ids))
    export_frame(folder / "before.parquet", before)
    # A compressed missing run omits later dates. Old finite events on those
    # dates must be revoked too, otherwise an as-of query would revive them.
    missing = before.merge(prepared[KEYS], on=KEYS, how="left", indicator=True)
    missing = missing.loc[missing["_merge"].eq("left_only") & missing.factor_value.notna(), before.columns].copy()
    if not missing.empty:
        missing["factor_value"] = float("nan")
        missing["fiscal_year"] = None
        missing["financial_period"] = pd.NaT
        prepared = _canonical(pd.concat([prepared, missing], ignore_index=True))
    payload = PAYLOAD + (["source_trade_date"] if table == "fact_daily_factor_snapshot" else [])
    joined = prepared.merge(before, on=KEYS, how="left", suffixes=("", "_before"), indicator=True, validate="one_to_one")
    same = joined["_merge"].eq("both")
    for column in payload:
        left, right = joined[column], joined[column + "_before"]
        same &= left.eq(right) | (left.isna() & right.isna())
    delta = joined.loc[~same, prepared.columns].copy()
    now = pd.Timestamp.now(tz="UTC").floor("ms")
    version = max(now, before.updated_at.max() + pd.Timedelta(milliseconds=1)) if len(before) else now
    delta["updated_at"] = version
    export_frame(folder / "delta.parquet", delta)
    assert_inputs()
    if len(delta):
        output = delta.astype(object).where(pd.notna(delta), None)
        client.insert(table, list(output.itertuples(index=False, name=None)), column_names=list(output.columns))
    after = _canonical(_capture(client, table, sid, basis, start, end, factor_ids))
    expected = pd.concat([before.merge(delta[KEYS], on=KEYS, how="left", indicator=True)
        .query("_merge == 'left_only'")[before.columns], delta], ignore_index=True)
    expected = _canonical(expected)[after.columns]
    pd.testing.assert_frame_equal(expected, after, check_dtype=False, check_exact=True)
    export_frame(folder / "after.parquet", after)
    return dict(prepared_rows=len(prepared), inserted_rows=len(delta), verified_rows=len(after),
        published_at=now.isoformat(), revision=version.isoformat())


def _export_verified(folder, report, *, kind, data_lake):
    gold = data_lake.gold("share_input_rebuilds", "kr", report["symbol"], report["financial_basis"], kind, folder.name)
    artifacts = [export_frame(gold / f"{month}.parquet", pd.read_parquet(folder / month / "after.parquet"))
        for month in sorted(report["months"])]
    return export_json(gold / "summary.json", dict(status="published_and_verified", market="kr", kind=kind,
        symbol=report["symbol"], financial_basis=report["financial_basis"], from_date=report["from_date"],
        through_date=report["through_date"], artifacts=artifacts, journals=report["journals"],
        audit_path=str((folder / "rebuild.json").resolve()), whole_market_survivorship_complete=False))


def rebuild_factors(client, pending, basis, through_date, *, data_lake=DATA_LAKE, workers=1):
    if not pending:
        return
    from engine.loaders.factors import insert_daily_factors
    from engine.transformers.factors import FactorMarketDataCache

    inputs = _input_files(data_lake)
    cache = FactorMarketDataCache(market="kr", end_date=through_date.isoformat())
    for sid, item in pending.items():
        scope = {sid: item}
        folder = data_lake.silver("share_input_rebuilds", "kr", item["symbol"], basis, uuid4().hex)
        folder.mkdir(parents=True)
        prepared = insert_daily_factors(stock_codes=[item["symbol"]], market="kr", financial_basis=basis,
            start_date=item["from_date"], end_date=through_date.isoformat(), dry_run=True,
            insert_catalog=False, client=client, parallel_workers=workers, market_data_cache=cache,
            use_edgartools=False, require_report_metadata=True, wacc_online_backfill=False, include_abstentions=True)
        if prepared.empty:
            raise RuntimeError(f"No calculable price history for changed share input {sid}; rebuild remains pending")
        prepared = _canonical(prepared)
        if set(prepared.security_id) != {sid} or set(prepared.financial_basis) != {basis} or (
            prepared.trade_date.min().date() < date.fromisoformat(item["from_date"])
            or prepared.trade_date.max().date() > through_date):
            raise ValueError("Prepared factors are outside their share change scope")
        export_frame(folder / "prepared.parquet", prepared)
        report = dict(**item, status="prepared", financial_basis=basis, through_date=through_date.isoformat(),
            input_files=inputs, factor_ids=sorted(prepared.factor_id.unique()), months={})
        export_json(folder / "rebuild.json", report)
        factor_ids = sorted(prepared.factor_id.unique())
        for period in pd.period_range(item["from_date"], through_date, freq="M"):
            rows = prepared.loc[prepared.trade_date.dt.to_period("M").eq(period)]
            report["months"][str(period)] = _publish_month(client, "fact_daily_factors", rows,
                sid=sid, basis=basis, start=max(period.start_time.date().isoformat(), item["from_date"]),
                end=min(period.end_time.date(), through_date).isoformat(), folder=folder / str(period),
                factor_ids=factor_ids,
                assert_inputs=lambda: _assert_unchanged(scope, basis, kind="factors", data_lake=data_lake, inputs=inputs))
            export_json(folder / "rebuild.json", report)
        report["gold"] = _export_verified(folder, report, kind="factors", data_lake=data_lake)
        _complete(scope, basis, through_date, kind="factors", data_lake=data_lake, inputs=inputs)
        report["status"] = "published_and_verified"
        export_json(folder / "rebuild.json", report)
        print(f"[SHARE-INPUT] rebuilt {sid} {basis} from {item['from_date']} through {through_date}", flush=True)


def rebuild_snapshots(client, pending, basis, through_date, *, data_lake=DATA_LAKE):
    """Prepare public as-of snapshots in an isolated table before upserting."""
    from engine.loaders.factor_snapshots import build_factor_snapshot_insert_query
    from engine.transformers.factors import preferred_factor_columns

    if not pending:
        return
    inputs = _input_files(data_lake)
    factors = pending_rebuilds("kr", basis, symbols=[item["symbol"] for item in pending.values()], data_lake=data_lake)
    if any(item["from_date"] <= through_date.isoformat() for item in factors.values()):
        raise RuntimeError("share inputs changed; rebuild historical factors for this financial basis before snapshots")
    for sid, item in pending.items():
        scope = {sid: item}
        folder = data_lake.silver("share_input_snapshot_rebuilds", "kr", item["symbol"], basis, uuid4().hex)
        folder.mkdir(parents=True)
        result = client.query("""SELECT DISTINCT trade_date FROM price_daily
            WHERE startsWith(security_id, 'SEC_KR_') AND trade_date BETWEEN {start:Date} AND {end:Date}
            ORDER BY trade_date""", parameters=dict(start=item["from_date"], end=through_date))
        dates = pd.DataFrame(result.result_rows, columns=["trade_date"])
        if dates.empty:
            raise RuntimeError(f"No snapshot calendar for changed share input {sid}; rebuild remains pending")
        dates["trade_date"] = pd.to_datetime(dates.trade_date)
        report = dict(**item, status="prepared", financial_basis=basis, through_date=through_date.isoformat(), input_files=inputs, months={})
        export_json(folder / "rebuild.json", report)
        stage = "share_snapshot_stage_" + uuid4().hex
        client.command(f"CREATE TEMPORARY TABLE {stage} ENGINE = Memory AS SELECT * FROM fact_daily_factor_snapshot WHERE 0")
        try:
            for period, calendar in dates.groupby(dates.trade_date.dt.to_period("M")):
                client.command(f"TRUNCATE TABLE {stage}")
                query, params = build_factor_snapshot_insert_query(market="kr", financial_basis=basis,
                    factor_ids=preferred_factor_columns(), snapshot_table=stage,
                    security_ids=[sid], snapshot_dates=[day.date() for day in calendar.trade_date])
                client.command(query, parameters=params)
                result = client.query(f"SELECT {', '.join(KEYS + PAYLOAD + ['source_trade_date', 'updated_at'])} FROM {stage}")
                prepared = pd.DataFrame(result.result_rows, columns=KEYS + PAYLOAD + ["source_trade_date", "updated_at"])
                if prepared.empty:
                    raise RuntimeError(f"No prepared snapshots for changed share input {sid}; rebuild remains pending")
                prepared = _canonical(prepared)
                if (prepared.source_trade_date > prepared.trade_date).any():
                    raise ValueError("Prepared snapshot contains a future source date")
                export_frame(folder / str(period) / "prepared.parquet", prepared)
                report["months"][str(period)] = _publish_month(client, "fact_daily_factor_snapshot", prepared,
                    sid=sid, basis=basis, start=calendar.trade_date.min().date().isoformat(),
                    end=calendar.trade_date.max().date().isoformat(), folder=folder / str(period),
                    assert_inputs=lambda: _assert_unchanged(scope, basis, kind="snapshots", data_lake=data_lake, inputs=inputs))
                export_json(folder / "rebuild.json", report)
            report["gold"] = _export_verified(folder, report, kind="snapshots", data_lake=data_lake)
            _complete(scope, basis, through_date, kind="snapshots", data_lake=data_lake, inputs=inputs)
            report["status"] = "published_and_verified"
            export_json(folder / "rebuild.json", report)
            print(f"[SHARE-INPUT] snapshots rebuilt {sid} {basis} through {through_date}", flush=True)
        finally:
            client.command(f"DROP TABLE IF EXISTS {stage}")
