import sys

from engine.transformers._internal import benchmark_prices as _impl

sys.modules[__name__] = _impl
