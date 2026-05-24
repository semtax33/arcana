import sys

from engine.extractors._internal import krx_market_universe as _impl

sys.modules[__name__] = _impl
