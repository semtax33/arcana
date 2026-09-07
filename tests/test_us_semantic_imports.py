from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pandas as pd


def test_us_sec_normalizer_import_does_not_load_heavy_nlp_stack() -> None:
    script = """
import importlib
import sys

importlib.import_module("engine.semantic.us_dsl")
importlib.import_module("engine.transformers._internal.sec_filings")
heavy = {"spacy", "thinc", "torch"}.intersection(sys.modules)
if heavy:
    raise SystemExit(f"unexpected heavy imports: {sorted(heavy)}")
"""
    subprocess.run([sys.executable, "-c", script], check=True)


def test_explicit_symbol_resolves_companyfacts_without_directory_scan(
    tmp_path: Path, monkeypatch
) -> None:
    from engine.transformers._internal.sec_filings import resolve_companyfacts_files

    expected = tmp_path / "CIK0000320193.json"
    expected.write_text("{}", encoding="utf-8")
    ticker_map = pd.DataFrame(
        [{"cik": "320193", "ticker": "AAPL", "title": "Apple Inc."}]
    )

    def fail_glob(*args, **kwargs):
        raise AssertionError("explicit symbol resolution must not scan CIK*.json")

    monkeypatch.setattr(Path, "glob", fail_glob)

    assert resolve_companyfacts_files(
        tmp_path,
        symbols=["AAPL"],
        ticker_map=ticker_map,
    ) == [(expected, "AAPL", "320193")]
