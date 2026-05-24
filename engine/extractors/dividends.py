import sys

from engine.extractors._internal import dart_dividends as _impl

sys.modules[__name__] = _impl
