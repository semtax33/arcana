import sys

from engine.transformers._internal import krx_market_data as _impl

sys.modules[__name__] = _impl
