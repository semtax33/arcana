from engine.extractors._internal.yfinance_market_prices import (
    download_us_equity_universe,
    download_us_price_histories,
    fetch_yfinance_price,
    filter_us_equity_universe,
    normalize_yfinance_ticker,
    parse_nasdaq_symbol_directory_text,
)


def fetch_price(*args, **kwargs):
    from engine.extractors._internal.krx_market_prices import fetch_price as _fetch_price

    return _fetch_price(*args, **kwargs)


def fetch_share(*args, **kwargs):
    from engine.extractors._internal.krx_market_prices import fetch_share as _fetch_share

    return _fetch_share(*args, **kwargs)


def fetch_all_prices(*args, **kwargs):
    from engine.extractors._internal.krx_market_prices import fetch_all_prices as _fetch_all_prices

    return _fetch_all_prices(*args, **kwargs)


def fetch_all_shares(*args, **kwargs):
    from engine.extractors._internal.krx_market_prices import fetch_all_shares as _fetch_all_shares

    return _fetch_all_shares(*args, **kwargs)


__all__ = [
    "download_us_equity_universe",
    "download_us_price_histories",
    "fetch_all_prices",
    "fetch_all_shares",
    "fetch_price",
    "fetch_share",
    "fetch_yfinance_price",
    "filter_us_equity_universe",
    "normalize_yfinance_ticker",
    "parse_nasdaq_symbol_directory_text",
]
