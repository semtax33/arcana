"""Atomic file exports of consumer data; raw evidence stays in bronze."""
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

PRICE_COLUMNS = ["security_id", "trade_date", "open", "high", "low", "close", "volume", "adj_close", "currency"]


def _write(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != raw:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_bytes(raw)
        temporary.replace(path)
    return {"path": str(path.resolve()), "sha256": sha256(raw).hexdigest()}


def export_json(path, data):
    return _write(path, (json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8"))


def export_frame(path, frame):
    return {**_write(path, frame.to_parquet(index=False)), "rows": len(frame)}


def export_csv(path, frame):
    return {**_write(path, frame.to_csv(index=False).encode('utf-8-sig')), "rows": len(frame)}


def export_prices(path, frame):
    consumer = frame.assign(adj_close=frame["split_adj_close"])[PRICE_COLUMNS]
    return {**export_frame(path, consumer), "adjustment_basis": "split_only"}
