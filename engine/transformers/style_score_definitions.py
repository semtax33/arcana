import sys

from engine.transformers._internal import style_score_definitions as _impl

sys.modules[__name__] = _impl
