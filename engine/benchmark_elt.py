import sys

from engine.loaders import benchmarks as _impl

sys.modules[__name__] = _impl
