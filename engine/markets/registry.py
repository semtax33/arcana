from __future__ import annotations

from engine.core.markets import MarketConfig
from engine.markets.kr import KR_MARKET_CONFIG
from engine.markets.us import US_MARKET_CONFIG


MARKET_CONFIGS: dict[str, MarketConfig] = {
    "kr": KR_MARKET_CONFIG,
    "us": US_MARKET_CONFIG,
}


def market_config(market: str = "kr") -> MarketConfig:
    key = str(market or "kr").strip().lower()
    try:
        return MARKET_CONFIGS[key]
    except KeyError as exc:
        supported = ", ".join(sorted(MARKET_CONFIGS))
        raise ValueError(f"unsupported market: {market!r}; supported markets: {supported}") from exc
