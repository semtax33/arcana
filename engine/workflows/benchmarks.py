import sys

from engine.workflows._internal import benchmark_workflow as _impl

if __name__ == "__main__":
    main = getattr(_impl, "main", None)
    if main is None:
        raise SystemExit("engine.workflows.benchmarks has no CLI entry point")
    main()
else:
    sys.modules[__name__] = _impl
