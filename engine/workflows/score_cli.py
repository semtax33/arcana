import sys

from engine.workflows._internal import score_cli as _impl

sys.modules[__name__] = _impl
