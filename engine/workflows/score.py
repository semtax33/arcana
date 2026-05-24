import sys

from engine.workflows._internal import score_workflow as _impl

sys.modules[__name__] = _impl
