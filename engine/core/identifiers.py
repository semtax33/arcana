from __future__ import annotations

from engine.core.markets import MarketConfig


def normalize_market_symbol(symbol: object, config: MarketConfig) -> str:
    return config.normalize_symbol(symbol)


def security_id_of(symbol: object, config: MarketConfig) -> str:
    normalized = normalize_market_symbol(symbol, config)
    return f"{config.security_prefix}_{normalized}"


def issuer_id_of(symbol: object, config: MarketConfig) -> str:
    normalized = normalize_market_symbol(symbol, config)
    if config.issuer_symbol_normalizer is not None:
        normalized = config.issuer_symbol_normalizer(normalized)
    return f"{config.issuer_prefix}_{normalized}"

