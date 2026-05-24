import sys

from engine.loaders._internal import clickhouse_market_data as _impl

sys.modules[__name__] = _impl
