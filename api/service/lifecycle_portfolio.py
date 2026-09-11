"""Portfolio holdings, cash and cash receivables across rebalance boundaries."""
from __future__ import annotations

from datetime import date
import math
import re


def _day(value):
    return date.fromisoformat(str(value)[:10])


def _optional_day(value):
    return None if value is None or str(value) in {"NaT", "nan"} else _day(value)


def simulate_lifecycle_portfolio(*, segments, trading_days, prices, events, entitlements=(), trading_halts=(),
                                 listing_episodes=()):
    """Return daily observed wealth; only settled cash can fund a rebalance.

    Holdings use the supplied split-adjusted price unit. A cash entitlement is
    converted from disclosed shares using the last executable raw/adjusted pair.
    Unsettled cash is a separate asset, never a new purchase's funding source.
    """
    components = {}
    event_ids = {event["event_id"] for event in events}
    component_ids = set()
    for source in entitlements:
        component = dict(source)
        key = (component["event_id"], component["component_id"])
        units = component.get("units_per_share")
        if (key in component_ids or component["event_id"] not in event_ids
                or component.get("component_type") != "security"
                or not component.get("recipient_security_id") or not component.get("currency")
                or units is None or not math.isfinite(float(units)) or float(units) <= 0):
            raise ValueError("Unresolved or conflicting lifecycle security entitlement")
        component_ids.add(key)
        component["delivery_date"] = _optional_day(component.get("delivery_date"))
        component["tradable_date"] = _optional_day(component.get("tradable_date"))
        components.setdefault(component["event_id"], []).append(component)
    by_security = {}
    for source in events:
        event = dict(source)
        security = event["security_id"]
        if event.get("status") != "confirmed":
            raise ValueError("Lifecycle event must be confirmed")
        if security in by_security:
            raise ValueError("Conflicting lifecycle events for the same security require review")
        if (not event.get("source_url") or not event.get("currency")
                or not re.fullmatch(r"[0-9a-f]{64}", str(event.get("source_sha256", "")))):
            raise ValueError("Lifecycle entitlement requires source provenance and currency")
        event["effective_date"] = _day(event["effective_date"])
        event["cash_payment_date"] = _optional_day(event.get("cash_payment_date"))
        if event["cash_payment_date"] and event["cash_payment_date"] < event["effective_date"]:
            raise ValueError("Cash payment before the entitlement date requires separate evidence")
        for component in components.get(event["event_id"], []):
            if component["delivery_date"] and component["delivery_date"] < event["effective_date"]:
                raise ValueError("Security delivery precedes the entitlement date")
            if component["tradable_date"] and component["tradable_date"] < event["effective_date"]:
                raise ValueError("Received security tradability precedes the entitlement date")
            if component["currency"] != event["currency"]:
                raise ValueError("Lifecycle security currency requires explicit FX conversion")
        by_security[security] = event

    halts_by_security, halt_ids = {}, set()
    for source in trading_halts:
        halt = dict(source)
        if (halt.get("status") != "confirmed" or not halt.get("halt_id")
                or halt["halt_id"] in halt_ids or not halt.get("security_id")
                or not halt.get("source_url")
                or not re.fullmatch(r"[0-9a-f]{64}", str(halt.get("source_sha256", "")))):
            raise ValueError("Trading halt requires confirmed, unique source evidence")
        halt_ids.add(halt["halt_id"])
        start, end = _day(halt["start_date"]), _optional_day(halt.get("end_date"))
        if end is not None and end <= start:
            raise ValueError("Trading halt must end after its start")
        halts_by_security.setdefault(halt["security_id"], []).append((start, end))

    def is_halted(security, day):
        return any(start <= day and (end is None or day < end)
                   for start, end in halts_by_security.get(security, ()))

    listings_by_security = {}
    for source in listing_episodes:
        start, end = _day(source["valid_from"]), _optional_day(source.get("valid_until"))
        if source.get("status") != "confirmed" or (end is not None and end <= start):
            raise ValueError("Listing interval requires a confirmed positive lifetime")
        listings_by_security.setdefault(source["security_id"], []).append((start, end))

    def listing_has_ended(security, day):
        intervals = listings_by_security.get(security, ())
        return (any(end is not None and end <= day for _, end in intervals)
                and not any(start <= day and (end is None or day < end) for start, end in intervals))

    quotes = {}
    for row in prices:
        quote = dict(row)
        values = [quote.get(name) for name in ("raw_close", "close", "volume")]
        if any(value is None or not math.isfinite(float(value)) or float(value) <= 0 for value in values):
            continue
        day = _day(quote["trade_date"])
        if is_halted(quote["security_id"], day) or listing_has_ended(quote["security_id"], day):
            continue
        quote.update(raw_close=float(quote["raw_close"]), close=float(quote["close"]), trade_date=day)
        quotes[(day, quote["security_id"])] = quote

    schedule = {_day(segment["start_date"]): segment for segment in segments}
    days = sorted({_day(day) for day in trading_days})
    holdings, marks, receivables, security_receivables = {}, {}, [], []
    recipient_ids = {component["recipient_security_id"] for rows in components.values() for component in rows}
    processed, history = set(), []
    cash, portfolio_currency = 1.0, None

    for day in days:
        # Rights arise at the actual event, before that day's vendor quotes can
        # be mistaken for continued exchange trading in the extinguished share.
        for security, event in by_security.items():
            if security in processed or event["effective_date"] > day:
                continue
            processed.add(security)
            units = holdings.pop(security, 0.0)
            incoming = [claim for claim in security_receivables if claim["security_id"] == security]
            if units or incoming:
                if event.get("entitlements_complete") is not True:
                    raise ValueError(f"Lifecycle entitlements are not complete: {event['event_id']}")
                amount = event.get("cash_per_share")
                if (event.get("event_type") not in {"cash_merger", "cash_exchange", "share_exchange", "cancellation"}
                        or amount is None or not math.isfinite(float(amount)) or float(amount) < 0
                        or (float(amount) == 0 and not components.get(event["event_id"])
                            and event.get("event_type") != "cancellation")):
                    raise ValueError("Unresolved lifecycle entitlement; no verified return can be calculated")
                basis = marks.get(security)
                if basis is None or any("adjusted_units" not in claim for claim in incoming):
                    raise ValueError("Unpriced or same-day chained lifecycle entitlement requires review")
                if basis["currency"] != event["currency"]:
                    raise ValueError("Lifecycle cash currency does not match the held security")
                units += sum(claim["adjusted_units"] for claim in incoming)
                security_receivables = [claim for claim in security_receivables if claim["security_id"] != security]
                raw_shares = units * basis["close"] / basis["raw_close"]
                if float(event["cash_per_share"]) > 0:
                    receivables.append({
                        "event_id": event["event_id"], "security_id": security,
                        "amount": raw_shares * float(event["cash_per_share"]),
                        "payment_date": event["cash_payment_date"], "currency": event["currency"],
                    })
                for component in components.get(event["event_id"], []):
                    recipient_event = by_security.get(component["recipient_security_id"])
                    if recipient_event and recipient_event["effective_date"] <= day:
                        raise ValueError("Same-day or already extinguished received security requires explicit event ordering")
                    security_receivables.append({
                        "event_id": event["event_id"], "component_id": component["component_id"],
                        "security_id": component["recipient_security_id"],
                        "units": raw_shares * float(component["units_per_share"]),
                        "delivery_date": component["delivery_date"], "currency": component["currency"],
                        "tradable_date": component["tradable_date"],
                    })
        # Exchange removal is evidence of the end of trading, even when no
        # cash, private-equity or cancellation outcome has been registered.
        # Process known terminal rights first; unresolved residual holdings
        # cannot be sold or valued using a surviving vendor quotation.
        for security in set(holdings) | {claim["security_id"] for claim in security_receivables}:
            if listing_has_ended(security, day):
                raise ValueError(f"Unresolved terminal outcome for listing: {security} on {day}")

        outstanding = []
        for claim in receivables:
            if claim["payment_date"] is not None and claim["payment_date"] <= day:
                cash += claim["amount"]
            else:
                outstanding.append(claim)
        receivables = outstanding

        for security in set(holdings) | recipient_ids:
            quote = quotes.get((day, security))
            if quote is not None:
                marks[security] = quote

        outstanding_securities = []
        for claim in security_receivables:
            security = claim["security_id"]
            basis = marks.get(security)
            if basis is None:
                raise ValueError(f"Unpriced lifecycle security entitlement: {security}; valuation requires review")
            if basis["currency"] != claim["currency"]:
                raise ValueError("Received security price currency does not match its entitlement")
            if "adjusted_units" not in claim:
                # Fix the claim's price unit on recognition, before later
                # recipient splits change its raw share count and raw price.
                claim["adjusted_units"] = claim["units"] * basis["raw_close"] / basis["close"]
            if (claim["delivery_date"] is not None and claim["delivery_date"] <= day
                    and (claim["tradable_date"] is None or claim["tradable_date"] <= day)):
                holdings[security] = holdings.get(security, 0.0) + claim["adjusted_units"]
            else:
                outstanding_securities.append(claim)
        security_receivables = outstanding_securities

        segment = schedule.get(day)
        if segment is not None:
            # Unsellable holdings stay owned. Their carried marks and unpaid
            # receivables cannot finance purchases in the next segment.
            for security in list(holdings):
                quote = quotes.get((day, security))
                if quote is not None:
                    cash += holdings.pop(security) * quote["close"]
            targets = sorted(set(segment["security_ids"]))
            cost = float(segment.get("transaction_cost_bps") or 0) / 10000.0
            if not 0 <= cost < 1:
                raise ValueError("Transaction costs must be less than available capital")
            if targets:
                cash *= 1 - cost
                allocation = cash / len(targets)
                for security in targets:
                    quote = quotes.get((day, security))
                    if security in processed or quote is None:
                        continue
                    if portfolio_currency is None:
                        portfolio_currency = quote["currency"]
                    if quote["currency"] != portfolio_currency:
                        raise ValueError("Mixed currency lifecycle portfolios require explicit FX conversion")
                    if allocation > 0:
                        holdings[security] = holdings.get(security, 0.0) + allocation / quote["close"]
                        marks[security] = quote
                        cash -= allocation
            if abs(cash) < 1e-14:
                cash = 0.0

        positions = [{"security_id": security, "adjusted_units": units,
                      "market_value": units * marks[security]["close"],
                      "mark_date": marks[security]["trade_date"],
                      "trading_status": "halted" if is_halted(security, day) else
                          ("trading" if (day, security) in quotes else "no_executable_quote")}
                     for security, units in sorted(holdings.items())]
        security_claims = [dict(claim, market_value=claim["adjusted_units"] * marks[claim["security_id"]]["close"])
                           for claim in security_receivables]
        receivable_value = (sum(claim["amount"] for claim in receivables)
                            + sum(claim["market_value"] for claim in security_claims))
        nav = cash + receivable_value + sum(position["market_value"] for position in positions)
        history.append({"trade_date": day, "nav": nav, "cash": cash,
                        "receivable_value": receivable_value, "positions": positions,
                        "cash_receivables": [dict(claim) for claim in receivables],
                        "security_receivables": security_claims})
    return history
