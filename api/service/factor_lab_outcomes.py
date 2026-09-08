"""Descriptive forward outcomes. Future observations never become graph values."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import hashlib
import json
import math
from statistics import mean
from typing import Any


def _date(value: Any) -> date:
    return value.date() if isinstance(value, datetime) else value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    x = sorted(values)
    p = (len(x) - 1) * q
    lo, hi = math.floor(p), math.ceil(p)
    return x[lo] + (x[hi] - x[lo]) * (p - lo)


def _summary(rows: list[dict]) -> dict:
    values = [r["outcome"] for r in rows if r["reason"] == ""]
    reasons = Counter(r["reason"] for r in rows if r["reason"])
    pending = reasons.get("not_matured", 0)
    return {
        "opportunity_count": len(rows), "valid_count": len(values),
        "pending_count": pending, "missing_count": len(rows) - len(values) - pending,
        "missing_rate": (len(rows) - len(values) - pending) / (len(rows) - pending) if len(rows) > pending else None,
        "mean": mean(values) if values else None, "median": _quantile(values, 0.5),
        "p10": _quantile(values, 0.1), "p90": _quantile(values, 0.9),
        "positive_rate": sum(v > 0 for v in values) / len(values) if values else None,
        "negative_rate": sum(v < 0 for v in values) / len(values) if values else None,
        "zero_rate": sum(v == 0 for v in values) / len(values) if values else None,
        "security_count": len({r["security_id"] for r in rows if not r["reason"]}),
        "signal_date_count": len({r["signal_date"] for r in rows if not r["reason"]}),
        "invalid_reason_counts": dict(sorted(reasons.items())),
    }


def evaluate_forward_outcome(config: dict, scores: list[dict], targets: list[dict], trading_days: list, as_of: date) -> dict:
    """Evaluate frozen score rows against exact-date snapshots through as_of.

    Q1 is the lowest priority bucket, QK the highest. Ties receive the same
    midpoint bucket; target missingness never changes bucket assignment.
    """
    as_of = _date(as_of)
    targets = [{**r, "value": r.get("value") if _finite(r.get("value")) else None} for r in targets]
    days = sorted({_date(d) for d in trading_days if _date(d) <= as_of})
    day_positions = {d: p for p, d in enumerate(days)}
    by_date = defaultdict(list)
    seen_scores = set()
    for row in scores:
        key = (_date(row["trade_date"]), str(row["security_id"]))
        if key in seen_scores:
            raise ValueError("duplicate score security/date")
        seen_scores.add(key)
        if _finite(row.get("value")) and row.get("is_valid", True):
            by_date[key[0]].append({**row, "security_id": key[1]})
    target_map = {}
    for row in targets:
        d = _date(row["trade_date"])
        if d > as_of:
            continue
        key = (d, str(row["security_id"]))
        if key in target_map:
            raise ValueError("duplicate target security/date")
        target_map[key] = row
    k = config.get("bucket_count", 5)
    bucketed = []
    for d, rows in sorted(by_date.items()):
        rows.sort(key=lambda r: (r["value"] * (-1 if config["score_order"] == "lower" else 1), r["security_id"]))
        start = 0
        while start < len(rows):
            end = start + 1
            while end < len(rows) and rows[end]["value"] == rows[start]["value"]:
                end += 1
            bucket = min(k, 1 + int(((start + end - 1) / 2) * k / len(rows)))
            bucketed.extend((d, r, bucket) for r in rows[start:end])
            start = end
    observations, horizons = [], []
    for horizon in config["horizons"]:
        rows = []
        for signal_date, score, bucket in bucketed:
            sid = score["security_id"]
            if config["unit"] == "calendar_day":
                target_date = signal_date + timedelta(days=horizon)
            else:
                pos = day_positions.get(signal_date)
                target_date = days[pos + horizon] if pos is not None and pos + horizon < len(days) else None
            reason, value = "", None
            baseline = target_map.get((signal_date, sid))
            future = target_map.get((target_date, sid))
            if signal_date > as_of or target_date is None or target_date > as_of:
                reason = "not_matured"
                if signal_date <= as_of and signal_date not in day_positions and config["unit"] == "trading_day":
                    reason = "missing_signal_calendar"
            elif future is None or not _finite(future.get("value")):
                reason = "missing_target"
            elif config["measure"] != "level" and (baseline is None or not _finite(baseline.get("value"))):
                reason = "missing_baseline"
            elif (future.get("source_trade_date") and _date(future["source_trade_date"]) > target_date
                  or config["measure"] != "level" and baseline.get("source_trade_date") and _date(baseline["source_trade_date"]) > signal_date):
                reason = "source_after_snapshot"
            else:
                current = baseline.get("value") if baseline else None
                next_value = future["value"]
                if config["measure"] == "level":
                    value = next_value
                elif config.get("period_policy", "same_period") == "same_period" and str(baseline.get("financial_period") or "") != str(future.get("financial_period") or ""):
                    reason = "financial_period_changed"
                elif config["measure"] == "pct_change" and current <= 0:
                    reason = "nonpositive_baseline"
                else:
                    delta = next_value - current
                    value = delta if config["measure"] == "change" else 100 * delta / current if config["measure"] == "pct_change" else float((delta > 0) - (delta < 0))
                if value is not None and not _finite(value):
                    reason, value = "non_finite_result", None
            observation = {
                "security_id": sid, "signal_date": signal_date.isoformat(), "score": score["value"],
                "bucket": bucket, "horizon": horizon,
                "target_date": target_date.isoformat() if target_date else None,
                "baseline_value": baseline.get("value") if baseline else None,
                "target_value": future.get("value") if future else None,
                "outcome": value, "reason": reason,
            }
            rows.append(observation)
        horizons.append({"horizon": horizon, "baseline": _summary(rows), "buckets": [
            {"bucket": b, **_summary([r for r in rows if r["bucket"] == b])} for b in range(1, k + 1)
        ]})
        observations.extend(rows)
    payload = json.dumps({"config": config, "scores": sorted(scores, key=lambda r: (str(r['trade_date']), str(r['security_id']))),
                          "targets": sorted(target_map.values(), key=lambda r: (str(r['trade_date']), str(r['security_id']))),
                          "calendar": days, "as_of": as_of}, sort_keys=True, default=str)
    return {"config": config, "as_of": as_of.isoformat(), "input_hash": hashlib.sha256(payload.encode()).hexdigest(),
            "horizons": horizons, "observations": observations,
            "warnings": ["Pooled descriptive statistics; repeated security/date observations are not independent events.",
                         "Baseline is the evaluated score cohort, not the whole market. Q1=lowest priority; ties stay together."]}
