import sys

from engine.workflows import score as _impl

sys.modules[__name__] = _impl
