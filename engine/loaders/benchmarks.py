import sys

from engine.loaders._internal import clickhouse_benchmarks as _impl

sys.modules[__name__] = _impl
