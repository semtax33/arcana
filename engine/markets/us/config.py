from __future__ import annotations

from engine.core.markets import MarketConfig


def normalize_us_symbol(symbol: object) -> str:
    return str(symbol).strip().upper()


US_MARKET_CONFIG = MarketConfig(
    country="US",
    currency="USD",
    timezone="America/New_York",
    security_prefix="SEC_US",
    issuer_prefix="ISSUER_US",
    default_market_mic="US",
    symbol_normalizer=normalize_us_symbol,
)
