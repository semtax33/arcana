from pathlib import Path

from scripts.backfill_us_historical_prices import read_target_symbols


def test_read_target_symbols_deduplicates_and_strips_security_prefix(tmp_path: Path) -> None:
    path = tmp_path / "targets.csv"
    path.write_text(
        "security_id,symbol\nSEC_US_MSFT,MSFT\nSEC_US_AAPL,AAPL\nSEC_US_MSFT,MSFT\n",
        encoding="utf-8",
    )

    assert read_target_symbols(path) == ["AAPL", "MSFT"]
