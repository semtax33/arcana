from __future__ import annotations

import pytest

from engine.core.clickhouse import clickhouse_config
from engine.extractors._internal import krx_market_universe
from engine.extractors._internal.hankyung_consensus import _resolve_token
from scripts import get_api_key


class RequestStarted(RuntimeError):
    pass


def test_dart_api_key_is_read_from_environment(monkeypatch):
    captured = {}
    monkeypatch.setenv("DART_API_KEY", "dart-runtime-secret")

    def stop_after_request(url, *, params, timeout):
        captured.update(url=url, params=params, timeout=timeout)
        raise RequestStarted

    monkeypatch.setattr(krx_market_universe.requests, "get", stop_after_request)

    with pytest.raises(RequestStarted):
        krx_market_universe.fetch_corp_list()

    assert captured["params"] == {"crtfc_key": "dart-runtime-secret"}


def test_dart_api_key_is_required(monkeypatch):
    monkeypatch.delenv("DART_API_KEY", raising=False)

    with pytest.raises(ValueError, match="DART_API_KEY"):
        krx_market_universe.fetch_corp_list()


def test_hankyung_token_is_read_from_environment(monkeypatch):
    monkeypatch.setenv("HANKYUNG_CONSENSUS_TOKEN", "hankyung-runtime-secret")

    assert _resolve_token(None) == "hankyung-runtime-secret"


def test_hankyung_token_is_required(monkeypatch):
    monkeypatch.delenv("HANKYUNG_CONSENSUS_TOKEN", raising=False)

    with pytest.raises(ValueError, match="HANKYUNG_CONSENSUS_TOKEN"):
        _resolve_token(None)


def test_clickhouse_password_is_read_from_environment(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "clickhouse-runtime-secret")

    assert clickhouse_config()["password"] == "clickhouse-runtime-secret"


def test_clickhouse_password_has_no_hardcoded_fallback(monkeypatch):
    monkeypatch.delenv("CLICKHOUSE_PASSWORD", raising=False)

    assert clickhouse_config()["password"] == ""


def test_alpha_vantage_csrf_token_is_read_from_environment(monkeypatch, capsys):
    captured = {}
    generated_key = "ABCDEFGHIJKLMNOP"
    monkeypatch.setenv("ALPHA_VANTAGE_CSRF_TOKEN", "csrf-runtime-secret")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"text": generated_key}

    def fake_post(url, *, headers, cookies, data):
        captured.update(url=url, headers=headers, cookies=cookies, data=data)
        return Response()

    monkeypatch.setattr(get_api_key.requests, "post", fake_post)

    result = get_api_key.collect_api_keys(count=1, delay_seconds=0)

    assert captured["headers"]["x-csrftoken"] == "csrf-runtime-secret"
    assert captured["cookies"] == {"csrftoken": "csrf-runtime-secret"}
    assert result["API_KEY"].tolist() == [generated_key]
    assert generated_key not in capsys.readouterr().out


def test_alpha_vantage_csrf_token_is_required(monkeypatch):
    monkeypatch.delenv("ALPHA_VANTAGE_CSRF_TOKEN", raising=False)

    with pytest.raises(ValueError, match="ALPHA_VANTAGE_CSRF_TOKEN"):
        get_api_key.collect_api_keys(count=0)
