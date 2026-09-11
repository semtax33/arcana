"""Audit provider listing observations without accepting issuer lifecycle facts."""
from __future__ import annotations

import csv
from collections import Counter
from datetime import date
import hashlib
import io
import json
from pathlib import Path

from engine.core.serving_storage import export_json


PROVIDER_KEY = ("symbol", "exchange", "assetType", "ipoDate")
COLUMNS = (*PROVIDER_KEY, "name", "delistingDate", "status")


def audit_alpha_vantage_listing_snapshots(*, root, end_date, output_dir):
    """Compare retained, hash-verified snapshots at or before the requested date.

    The provider key is for detecting changed observations only. It does not
    establish legal issuer, security-class continuity, or an executed event.
    """
    root = Path(root)
    end_date = date.fromisoformat(end_date).isoformat()
    sources, duplicates = [], []

    def read(day, state):
        path = root / f"snapshot_date={day}" / f"{state}.csv"
        metadata_path = path.with_suffix(".metadata.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if (metadata.get("provider") != "ALPHA_VANTAGE"
                or metadata.get("snapshot_date") != day or metadata.get("state") != state
                or metadata.get("source_sha256") != digest):
            raise ValueError(f"Listing snapshot provenance mismatch: {day}/{state}")
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        if not set(COLUMNS).issubset(reader.fieldnames or []):
            raise ValueError(f"Listing snapshot columns missing: {day}/{state}")
        rows = list(reader)
        if not rows or len(rows) != metadata.get("rows"):
            raise ValueError(f"Listing snapshot row count mismatch: {day}/{state}")
        sources.append({"snapshot_date": day, "state": state, "path": str(path.resolve()),
                        "source_sha256": digest, "rows": len(rows),
                        "metadata_path": str(metadata_path.resolve()),
                        "metadata_sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest()})
        groups = {}
        counts = Counter(tuple(row[field] for field in COLUMNS) for row in rows)
        for record, count in sorted(counts.items()):
            key = record[:len(PROVIDER_KEY)]
            groups.setdefault(key, set()).add(record)
            if count > 1:
                duplicates.append({**dict(zip(COLUMNS, record)), "snapshot_date": day,
                                   "state": state, "occurrences": count})
        return groups

    previous_dates, current, previous = {}, {}, {}
    ambiguous = []
    for state in ("active", "delisted"):
        current[state] = read(end_date, state)
        candidates = []
        for path in root.glob(f"snapshot_date=*/{state}.csv"):
            value = path.parent.name.removeprefix("snapshot_date=")
            try:
                day = date.fromisoformat(value).isoformat()
            except ValueError:
                continue
            if day < end_date:
                candidates.append(day)
        previous_dates[state] = max(candidates) if candidates else None
        previous[state] = read(previous_dates[state], state) if candidates else {}
        for label, groups in ((end_date, current[state]), (previous_dates[state], previous[state])):
            for key, variants in sorted(groups.items()):
                if len(variants) > 1 or not all(key):
                    ambiguous.append({**dict(zip(PROVIDER_KEY, key)), "snapshot_date": label,
                                      "state": state, "distinct_records": len(variants)})

    changes, names = [], []
    date_index = COLUMNS.index("delistingDate")
    name_index = COLUMNS.index("name")
    for key in sorted(previous["delisted"].keys() & current["delisted"].keys()):
        old, new = previous["delisted"][key], current["delisted"][key]
        if len(old) != 1 or len(new) != 1 or not all(key):
            continue
        old_date, new_date = next(iter(old))[date_index], next(iter(new))[date_index]
        if old_date != new_date:
            changes.append({**dict(zip(PROVIDER_KEY, key)), "previous_delisting_date": old_date,
                            "current_delisting_date": new_date})
        old_name, new_name = next(iter(old))[name_index], next(iter(new))[name_index]
        if old_name != new_name:
            names.append({**dict(zip(PROVIDER_KEY, key)), "previous_name": old_name, "current_name": new_name})

    overlaps = [dict(zip(PROVIDER_KEY, key)) for key in
                sorted(current["active"].keys() & current["delisted"].keys()) if all(key)]

    source_set = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    audit = {
        "schema_version": 1, "provider": "ALPHA_VANTAGE", "as_of": end_date,
        "status": "source_observations_require_review" if any((changes, ambiguous, names, overlaps, duplicates)) else "source_observations_audited_pending_review",
        "coverage_complete": False, "previous_snapshot_dates": previous_dates,
        "provider_key": list(PROVIDER_KEY), "sources": sources,
        "changed_delisting_dates": changes, "ambiguous_provider_keys": ambiguous,
        "changed_historical_names": names, "same_provider_key_in_active_and_delisted": overlaps,
        "duplicate_records": duplicates,
        "policy": "Provider observations do not establish issuer continuity, executed delisting dates, or settlement rights. No lifecycle facts are approved by this audit.",
    }
    target = Path(output_dir) / f"as_of={end_date}" / f"source_set={source_set}" / "audit.json"
    artifact = export_json(target, audit)
    return audit, artifact
