import sys

from engine.workflows._internal import pqci_inputs_workflow as _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())
else:
    sys.modules[__name__] = _impl
