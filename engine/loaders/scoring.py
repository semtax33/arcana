import sys

from engine.loaders._internal import clickhouse_scoring as _impl

sys.modules[__name__] = _impl
