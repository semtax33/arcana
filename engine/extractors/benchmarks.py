import sys

from engine.extractors._internal import krx_benchmarks as _impl

sys.modules[__name__] = _impl
