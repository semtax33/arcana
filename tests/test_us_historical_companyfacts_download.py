from __future__ import annotations

from pathlib import Path

from scripts.download_us_historical_companyfacts import (
    read_ticker_to_cik,
    resolve_target_ciks,
)


def test_companyfacts_targets_are_unique_ciks_and_unresolved_symbols_are_explicit(
    tmp_path: Path,
):
    aliases = tmp_path / "aliases.csv"
    current = tmp_path / "current.csv"
    target = tmp_path / "targets.csv"
    aliases.write_text("cik,ticker,title\n320193,AAPL,Apple\n", encoding="utf-8")
    current.write_text(
        "cik,ticker,title\n320193,AAPL,Apple\n320193,AAPL-A,Apple A\n789019,MSFT,Microsoft\n",
        encoding="utf-8",
    )
    target.write_text(
        "security_id\nSEC_US_AAPL\nSEC_US_AAPL-A\nSEC_US_MSFT\nSEC_US_UNKNOWN\n",
        encoding="utf-8",
    )

    mapping = read_ticker_to_cik(aliases, current)
    ciks, unresolved = resolve_target_ciks(target, ticker_to_cik=mapping)

    assert ciks == ["320193", "789019"]
    assert unresolved == ["UNKNOWN"]
