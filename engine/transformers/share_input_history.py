"""Durable evidence of changes to the regular normalized share input."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

from engine.core.serving_storage import export_json
from engine.core.paths import market_csv_name

KEYS = ["security_id", "trade_date"]
VALUES = ["shares", "market_cap"]


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _read(path):
    if not Path(path).exists():
        return pd.DataFrame({"security_id": pd.Series(dtype=str), "trade_date": pd.Series(dtype="datetime64[ns]"),
            "shares": pd.Series(dtype=float), "market_cap": pd.Series(dtype=float)})
    rows = pd.read_csv(path, dtype={"security_id": str}, float_precision="round_trip", usecols=KEYS + VALUES)
    rows["trade_date"] = pd.to_datetime(rows.trade_date, errors="raise").astype("datetime64[ns]")
    if rows[KEYS].isna().any().any() or rows.duplicated(KEYS).any():
        raise ValueError("Normalized share input has missing or duplicate identities")
    for name in VALUES:
        rows[name] = pd.to_numeric(rows[name], errors="raise")
    return rows


def prepare_share_input_change(output, candidate, *, data_lake):
    """Write the change journal before replacing the input, retaining failure evidence."""
    output, candidate = Path(output), Path(candidate)
    before_hash = file_digest(output) if output.exists() else None
    after_hash = file_digest(candidate)
    changes, counts = {}, {}
    folder = data_lake.silver("market_input_changes", "kr", uuid4().hex)
    folder.mkdir(parents=True)
    report = dict(schema_version=1, market="kr", kind="shares", status="prepared", input_path=str(output.resolve()),
        before_sha256=before_hash, after_sha256=after_hash, changed_securities=changes,
        policy="Each changed observation invalidates that security from its date onward. Factor and snapshot completion are recorded separately.")
    if before_hash != after_hash:
        before, after = _read(output), _read(candidate)
        joined = before.merge(after, on=KEYS, how="outer", suffixes=("_before", "_after"), indicator=True, validate="one_to_one")
        equal = np.ones(len(joined), dtype=bool)
        for name in VALUES:
            left, right = joined[name + "_before"], joined[name + "_after"]
            equal &= (left.eq(right) | (left.isna() & right.isna())).to_numpy()
        changed = joined.loc[~equal | joined["_merge"].ne("both")].copy()
        changed.to_parquet(folder / "changed_observations.parquet", index=False)
        for sid, rows in changed.groupby("security_id", sort=True):
            changes[sid] = dict(from_date=rows.trade_date.min().date().isoformat(),
                last_changed_date=rows.trade_date.max().date().isoformat(), added_rows=int(rows["_merge"].eq("right_only").sum()),
                changed_rows=int(rows["_merge"].eq("both").sum()), removed_rows=int(rows["_merge"].eq("left_only").sum()))
        counts = dict(before_rows=len(before), after_rows=len(after), changed_observations=len(changed),
            changed_observations_sha256=file_digest(folder / "changed_observations.parquet"))
    registration = data_lake.silver("krx", "shares", "historical_sources.json")
    if registration.exists():
        raw = registration.read_bytes()
        (folder / "historical_sources.json").write_bytes(raw)
        report["historical_sources_sha256"] = hashlib.sha256(raw).hexdigest()
    report.update(counts)
    path = folder / "report.json"
    export_json(path, report)
    return path


def finish_share_input_change(report_path):
    path = Path(report_path)
    report = json.loads(path.read_text("utf-8"))
    if file_digest(report["input_path"]) != report["after_sha256"]:
        raise ValueError("Normalized share input changed before publication was confirmed")
    report["status"] = "published"
    export_json(path, report)
    export_json(path.parent.parent / "latest.json", dict(report_path=str(path.resolve()),
        after_sha256=report["after_sha256"], historical_sources_sha256=report.get("historical_sources_sha256")))


def historical_registration_changed(*, data_lake):
    registration = data_lake.silver("krx", "shares", "historical_sources.json")
    latest = data_lake.silver("market_input_changes", "kr", "latest.json")
    prior = json.loads(latest.read_text("utf-8")) if latest.exists() else {}
    current = file_digest(registration) if registration.exists() else None
    return prior.get("historical_sources_sha256") != current


def assert_published_share_input(*, data_lake):
    latest = data_lake.silver("market_input_changes", "kr", "latest.json")
    if not latest.exists():
        return
    prior = json.loads(latest.read_text("utf-8"))
    output = data_lake.silver("krx", "shares", market_csv_name("normalized_shares"))
    if not output.exists() or file_digest(output) != prior["after_sha256"]:
        raise ValueError("Normalized share input changed outside its normalization journal; normalize original sources before refreshing")
