import sys

from engine.loaders._internal import clickhouse_dividends as _impl

if __name__ == "__main__":
    _impl.main()
else:
    sys.modules[__name__] = _impl
