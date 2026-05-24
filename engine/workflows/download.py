import sys

from engine.workflows._internal import download_workflow as _impl

if __name__ == "__main__":
    _impl.main()
else:
    sys.modules[__name__] = _impl
