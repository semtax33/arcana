from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


SymbolNormalizer = Callable[[object], str]


@dataclass(frozen=True)
class MarketConfig:
    country: str
    currency: str
    timezone: str
    security_prefix: str
    issuer_prefix: str
    default_market_mic: str = ""
    symbol_normalizer: SymbolNormalizer | None = None
    issuer_symbol_normalizer: Callable[[str], str] | None = None

    def normalize_symbol(self, symbol: object) -> str:
        if self.symbol_normalizer is not None:
            return self.symbol_normalizer(symbol)
        return str(symbol).strip().upper()


@dataclass(frozen=True)
class SecurityRef:
    security_id: str
    stock_code: str
    country: str
    market_mic: str = ""
    currency: str = ""


@dataclass(frozen=True)
class IssuerRef:
    issuer_id: str
    country: str

