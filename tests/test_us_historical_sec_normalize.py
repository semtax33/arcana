from __future__ import annotations

from pathlib import Path

from scripts.normalize_us_historical_sec import read_target_symbols


def test_normalize_launcher_uses_frozen_price_universe(tmp_path: Path):
    target = tmp_path / "targets.csv"
    target.write_text(
        "security_id,symbol\nSEC_US_MSFT,MSFT\nSEC_US_AAPL,AAPL\n",
        encoding="utf-8",
    )

    assert read_target_symbols(target) == ["AAPL", "MSFT"]
