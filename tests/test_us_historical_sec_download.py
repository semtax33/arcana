from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from engine.transformers._internal.edgar_identity import configure_edgar_http_cache
from scripts.download_us_historical_sec import (
    build_parser,
    partition_resolvable_symbols,
    read_target_symbols,
)


def test_read_target_symbols_freezes_deduplicated_us_price_universe(tmp_path: Path):
    target = tmp_path / "targets.csv"
    target.write_text(
        "security_id,symbol\nSEC_US_MSFT,MSFT\nSEC_US_AAPL,AAPL\nSEC_US_AAPL,AAPL\n",
        encoding="utf-8",
    )

    assert read_target_symbols(target) == ["AAPL", "MSFT"]


def test_partition_resolvable_symbols_abstains_on_unknown_sec_identity():
    resolved, unresolved = partition_resolvable_symbols(
        ["AAPL", "FRBA", "MSFT"],
        known_symbols={"AAPL", "MSFT"},
    )

    assert resolved == ["AAPL", "MSFT"]
    assert unresolved == ["FRBA"]


def test_downloader_cli_accepts_a_disjoint_company_range():
    args = build_parser().parse_args(["--offset", "951", "--limit", "951"])

    assert args.offset == 951
    assert args.limit == 951


def test_downloader_cli_can_disable_duplicate_http_response_cache():
    args = build_parser().parse_args(["--disable-http-cache"])

    assert args.disable_http_cache is True


def test_configure_edgar_http_cache_replaces_and_closes_the_old_manager():
    calls: list[dict[str, object]] = []

    class OldManager:
        closed = False

        def close(self) -> None:
            self.closed = True

    old_manager = OldManager()
    new_manager = object()
    fake_httpclient = SimpleNamespace(
        HTTP_MGR=old_manager,
        get_edgar_rate_limit_per_sec=lambda: 3,
        get_http_mgr=lambda **kwargs: calls.append(kwargs) or new_manager,
    )

    resolved = configure_edgar_http_cache(
        enabled=False,
        httpclient_module=fake_httpclient,
    )

    assert resolved is new_manager
    assert calls == [{"cache_enabled": False, "request_per_sec_limit": 3}]
    assert old_manager.closed is True
    assert fake_httpclient.HTTP_MGR is new_manager
