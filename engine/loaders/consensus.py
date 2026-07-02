import sys

from engine.loaders._internal import clickhouse_consensus as _impl

if __name__ == "__main__":
    _impl.main()
else:
    sys.modules[__name__] = _impl
