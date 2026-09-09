"""Forward reported-earnings outcomes. Same-day releases are excluded conservatively."""
from collections import defaultdict
from datetime import timedelta
import hashlib
import json

from api.service.factor_lab_outcomes import _date, _finite, _summary


def evaluate_earnings_outcome(config, scores, events, as_of):
    as_of = _date(as_of)
    by_security, by_date = defaultdict(list), defaultdict(list)
    visible = []
    for event in events:
        if _date(event["event_date"]) <= as_of:
            if _date(event["availability_date"]) > as_of:
                event = {**event, "surprise_pct": None, "reported_eps": None, "estimated_eps": None}
            visible.append(event)
            by_security[str(event["security_id"])].append(event)
    for sid, rows in by_security.items():
        rows.sort(key=lambda e: (_date(e["event_date"]), str(e.get("fiscal_period_end") or "")))
        if len({_date(e["event_date"]) for e in rows}) != len(rows):
            raise ValueError("duplicate earnings event per security/date; select one provider vintage")
    seen = set()
    for row in scores:
        key = (_date(row["trade_date"]), str(row["security_id"]))
        if key in seen:
            raise ValueError("duplicate score security/date")
        seen.add(key)
        if key[0] > as_of:
            raise ValueError("evaluation cutoff precedes a signal")
        if _finite(row.get("value")) and row.get("is_valid", True):
            by_date[key[0]].append(row)
    buckets, k = [], config.get("bucket_count", 5)
    for day, rows in sorted(by_date.items()):
        rows.sort(key=lambda r: (r["value"] * (-1 if config["score_order"] == "lower" else 1), str(r["security_id"])))
        start = 0
        while start < len(rows):
            end = start + 1
            while end < len(rows) and rows[end]["value"] == rows[start]["value"]:
                end += 1
            b = min(k, 1 + int((start + end - 1) / 2 * k / len(rows)))
            buckets.extend((day, row, b) for row in rows[start:end])
            start = end
    observations, horizons = [], []
    for horizon in config["horizons"]:
        rows = []
        for signal, score, bucket in buckets:
            sid = str(score["security_id"])
            deadline = signal + timedelta(days=config.get("max_wait_days", 365))
            following = [e for e in by_security[sid] if signal < _date(e["event_date"]) <= deadline]
            event = following[horizon - 1] if len(following) >= horizon else None
            value = event.get(config["target_field"]) if event else None
            reason = "not_matured" if event and _date(event["availability_date"]) > as_of else "" if _finite(value) else "missing_event_target" if event else "not_matured" if as_of < deadline else "no_event_within_window"
            rows.append({"security_id": sid, "signal_date": signal.isoformat(), "score": score["value"],
                "bucket": bucket, "horizon": horizon,
                "event_date": _date(event["event_date"]).isoformat() if event else None,
                "target_date": _date(event["availability_date"]).isoformat() if event else None,
                "fiscal_period_end": str(event.get("fiscal_period_end") or "") if event else None,
                "outcome": value if not reason else None, "reason": reason})
        horizons.append({"horizon": horizon, "baseline": _summary(rows), "buckets": [
            {"bucket": b, **_summary([r for r in rows if r["bucket"] == b])} for b in range(1, k + 1)]})
        observations.extend(rows)
    payload = json.dumps({"config": config, "scores": sorted(scores, key=lambda r: (str(r['trade_date']), str(r['security_id']))),
        "events": sorted(visible, key=lambda e: (str(e['security_id']), str(e['event_date']))), "as_of": as_of}, sort_keys=True, default=str)
    return {"kind": "earnings_outcome", "config": {**config, "unit": "earnings_event"}, "as_of": as_of.isoformat(),
        "input_hash": hashlib.sha256(payload.encode()).hexdigest(), "horizons": horizons, "observations": observations,
        "warnings": ["Same-day releases are excluded because intraday signal timing is unavailable.",
            "Reported-event labels use the selected provider's stored data; historical vintage coverage may be incomplete.",
            "Pooled descriptive statistics; repeated observations are not independent events."]}
