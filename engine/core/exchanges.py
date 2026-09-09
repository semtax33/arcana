"""Current listing classifications; unknown listings remain unclassified."""
from __future__ import annotations

import csv
from engine.core.paths import DATA_LAKE


def normalize_exchange(value, country: str) -> str:
    code = str(value or "").strip().upper()
    if country.upper() == "KR":
        return {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ", "유가증권": "KOSPI", "코스닥": "KOSDAQ"}.get(code, "")
    return {"XNAS": "NASDAQ", "NASDAQ": "NASDAQ", "N": "NYSE", "NYSE": "NYSE",
            "A": "NYSE_AMERICAN", "NYSE_AMERICAN": "NYSE_AMERICAN",
            "P": "OTHER", "Z": "OTHER", "V": "OTHER", "OTHER": "OTHER"}.get(code, "")


def us_exchange_mapping(path=None) -> dict[str, str]:
    path = path or DATA_LAKE.bronze("yfinance", "universe", "us_equity_universe.csv")
    if not path.exists():
        return {}
    from engine.markets.registry import market_config
    config = market_config("us")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return {config.normalize_symbol(row["ticker"]): normalize_exchange(row.get("exchange"), "US")
                for row in csv.DictReader(stream) if row.get("ticker")}


def refresh_exchange_codes(client, *, dry_run=False) -> dict[str, int]:
    """Update only classification, preserving every other security attribute."""
    from datetime import datetime
    us = us_exchange_mapping()
    result = client.query("SELECT security_id, argMax(market_mic, updated_at) FROM identifiers WHERE id_type = 'TICKER' AND is_primary GROUP BY security_id")
    kr = {sid: normalize_exchange(code, "KR") for sid, code in result.result_rows}
    master = client.query("SELECT * FROM security_master FINAL")
    columns = list(master.column_names)
    changed = []
    counts = {"classified": 0, "unclassified": 0, "changed": 0}
    for raw in master.result_rows:
        row = dict(zip(columns, raw))
        sid = row["security_id"]
        if row["country"] == "KR":
            code = kr.get(sid, row.get("exchange_code", ""))
        else:
            code = us.get(sid.removeprefix("SEC_US_"), "") if us else row.get("exchange_code", "")
        counts["classified" if code else "unclassified"] += 1
        if row.get("exchange_code", "") == code:
            continue
        row.update(exchange_code=code, updated_at=datetime.now())
        changed.append([row[c] for c in columns])
    counts["changed"] = len(changed)
    if changed and not dry_run:
        client.insert("security_master", changed, column_names=columns)
    return counts
