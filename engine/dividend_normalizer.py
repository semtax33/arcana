import sys

from engine.transformers import dividends as _impl

sys.modules[__name__] = _impl
