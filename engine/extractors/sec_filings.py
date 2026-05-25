import sys

from engine.extractors._internal import sec_filings as _impl

sys.modules[__name__] = _impl
