import sys

from engine.transformers._internal import style_scoring as _impl

sys.modules[__name__] = _impl
