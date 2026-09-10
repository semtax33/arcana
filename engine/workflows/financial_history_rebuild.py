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
             ("financial_history.py", "factor_metrics.py", "filing_periods.py")]
    paths += [root / "loaders/_internal/clickhouse_factors.py", root / "loaders/factor_snapshots.py"]
    return sha256(b"".join(path.read_bytes() for path in paths)).hexdigest()


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
        if not manifest["receipts"]:
            continue
        signature = sha256(raw + calculation.encode()).hexdigest()
        sid = f"SEC_{market.upper()}_{manifest['symbol']}"
        previous = state["items"].get(sid, {})
        if previous.get("signature") == signature and basis in previous.get(kind, []):
            continue
        pending[sid] = {"symbol": manifest["symbol"], "manifest_path": str(path.resolve()),
            "signature": signature, "manifest_sha256": sha256(raw).hexdigest(), "calculation_sha256": calculation,
            "from_date": (min(date.fromisoformat(r["report_date"]) for r in manifest["receipts"]) + timedelta(days=1)).isoformat()}
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
