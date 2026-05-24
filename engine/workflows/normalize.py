import sys

from engine.workflows._internal import normalize_workflow as _impl

sys.modules[__name__] = _impl
