from __future__ import annotations

from engine.core.markets import MarketConfig


def normalize_kr_stock_code(symbol: object) -> str:
    text = str(symbol).strip().upper()
    return text.zfill(6) if text.isdigit() else text


def normalize_kr_issuer_symbol(symbol: str) -> str:
    return f"{symbol[:-1]}0" if symbol and symbol[-1] != "0" else symbol


KR_MARKET_CONFIG = MarketConfig(
    country="KR",
    currency="KRW",
    timezone="Asia/Seoul",
    security_prefix="SEC_KR",
    issuer_prefix="ISSUER_ID",
    default_market_mic="KRX",
    symbol_normalizer=normalize_kr_stock_code,
    issuer_symbol_normalizer=normalize_kr_issuer_symbol,
)

