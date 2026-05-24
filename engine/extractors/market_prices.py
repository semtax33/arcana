import sys

from engine.extractors._internal import krx_market_prices as _impl

sys.modules[__name__] = _impl
