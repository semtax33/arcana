import sys

from engine.workflows._internal import load_workflow as _impl

sys.modules[__name__] = _impl
