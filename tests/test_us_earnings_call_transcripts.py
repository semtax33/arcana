from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from engine.extractors._internal.us_earnings_call_transcripts import (
    _alpha_streaming_get,
    _envelope,
    _parse_quarter,
    _transcript_path,
    _write_json,
    audit_us_earnings_call_transcripts,
    download_us_earnings_call_transcripts,
)
from engine.extractors._internal.us_consensus import RollingRateLimiter


class _Response:
    def __init__(self, payload, *, status_code: int = 200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


class _StreamingResponse:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def iter_content(self, *, chunk_size):
        assert chunk_size == 64 * 1024
        yield from self.chunks

    def close(self):
        self.closed = True


def _fmp_path(root: Path, quarter: str) -> Path:
    return (
        root
        / "fmp"
        / "ticker=AAPL"
        / f"fiscal_year={quarter[:4]}"
        / f"quarter={quarter[4:]}.json"
    )


def _alpha_path(root: Path, quarter: str) -> Path:
    return (
        root
        / "alpha-vantage"
        / "ticker=AAPL"
        / f"fiscal_year={quarter[:4]}"
        / f"quarter={quarter[4:]}.json"
    )


def test_fmp_is_primary_and_alpha_only_fetches_uncovered_quarters():
    with TemporaryDirectory() as temp, patch.dict(
        os.environ,
        {"FMP_API_KEY": "fmp-secret", "ALPHA_VANTAGE_API_KEY": "alpha-secret"},
        clear=True,
    ):
        root = Path(temp)
        calls = []

        def fake_get(url, *, params, timeout, headers=None):
            calls.append((url, params.copy(), (headers or {}).copy()))
            if url.endswith("/earning-call-transcript-dates"):
                return _Response(
                    [{"symbol": "AAPL", "fiscalYear": 2024, "quarter": 1}]
                )
            if url.endswith("/earning-call-transcript"):
                return _Response(
                    [
                        {
                            "symbol": "AAPL",
                            "year": 2024,
                            "quarter": 1,
                            "content": "FMP primary transcript",
                        }
                    ]
                )
            assert params["function"] == "EARNINGS_CALL_TRANSCRIPT"
            assert params["quarter"] == "2024Q2"
            return _Response(
                {
                    "symbol": "AAPL",
                    "quarter": "2024Q2",
                    "transcript": [
                        {"speaker": "CEO", "content": "Alpha fallback transcript"}
                    ],
                }
            )

        result = download_us_earnings_call_transcripts(
            symbols=["AAPL"],
            start_quarter="2024Q1",
            end_quarter="2024Q2",
            refresh_recent_quarters=0,
            output_root=root,
            http_get=fake_get,
            sleeper=lambda _: None,
        )

        assert result["providers"]["fmp"]["written"] == 1
        assert result["providers"]["alpha-vantage"]["written"] == 1
        assert result["priority_skipped"] == 1
        assert len(calls) == 3
        assert calls[0][2] == {"apikey": "fmp-secret"}
        assert "apikey" not in calls[0][1]
        assert calls[-1][1]["apikey"] == "alpha-secret"

        fmp = json.loads(_fmp_path(root, "2024Q1").read_text(encoding="utf-8"))
        alpha = json.loads(_alpha_path(root, "2024Q2").read_text(encoding="utf-8"))
        assert fmp["provider"] == "FMP"
        assert fmp["status"] == "ok"
        assert alpha["provider"] == "ALPHA_VANTAGE"
        assert alpha["status"] == "ok"
        assert "fmp-secret" not in json.dumps(fmp)
        assert "alpha-secret" not in json.dumps(alpha)


def test_empty_fmp_payload_falls_back_to_alpha_for_same_quarter():
    with TemporaryDirectory() as temp, patch.dict(
        os.environ,
        {"FMP_API_KEY": "fmp-secret", "ALPHA_VANTAGE_API_KEY": "alpha-secret"},
        clear=True,
    ):
        root = Path(temp)

        def fake_get(url, *, params, timeout, headers=None):
            if url.endswith("/earning-call-transcript-dates"):
                return _Response([{"fiscalYear": "2024", "quarter": "Q1"}])
            if url.endswith("/earning-call-transcript"):
                return _Response([])
            return _Response(
                {"transcript": [{"speaker": "CFO", "content": "fallback"}]}
            )

        result = download_us_earnings_call_transcripts(
            symbols=["AAPL"],
            start_quarter="2024Q1",
            end_quarter="2024Q1",
            refresh_recent_quarters=0,
            output_root=root,
            http_get=fake_get,
            sleeper=lambda _: None,
        )

        assert result["written"] == 2
        assert result["no_data"] == 1
        assert json.loads(_fmp_path(root, "2024Q1").read_text())["status"] == "no_data"
        assert json.loads(_alpha_path(root, "2024Q1").read_text())["status"] == "ok"


def test_resume_reuses_complete_quarter_files():
    with TemporaryDirectory() as temp, patch.dict(
        os.environ,
        {"FMP_API_KEY": "fmp-secret", "ALPHA_VANTAGE_API_KEY": "alpha-secret"},
        clear=True,
    ):
        root = Path(temp)

        def first_get(url, *, params, timeout, headers=None):
            if url.endswith("/earning-call-transcript-dates"):
                return _Response([{"fiscalYear": 2024, "quarter": 1}])
            if url.endswith("/earning-call-transcript"):
                return _Response([{"content": "cached FMP transcript"}])
            raise AssertionError("Alpha should not run when FMP has the transcript")

        download_us_earnings_call_transcripts(
            symbols=["AAPL"],
            start_quarter="2024Q1",
            end_quarter="2024Q1",
            refresh_recent_quarters=0,
            output_root=root,
            http_get=first_get,
            sleeper=lambda _: None,
        )

        calls = []

        def resumed_get(url, *, params, timeout, headers=None):
            calls.append(url)
            if url.endswith("/earning-call-transcript-dates"):
                return _Response([{"fiscalYear": 2024, "quarter": 1}])
            raise AssertionError("complete transcript snapshots must be reused")

        resumed = download_us_earnings_call_transcripts(
            symbols=["AAPL"],
            start_quarter="2024Q1",
            end_quarter="2024Q1",
            refresh_recent_quarters=0,
            output_root=root,
            http_get=resumed_get,
            sleeper=lambda _: None,
        )

        assert len(calls) == 1
        assert calls[0].endswith("/earning-call-transcript-dates")
        assert resumed["providers"]["fmp"]["skipped"] == 1
        assert resumed["priority_skipped"] == 1

        def empty_refresh_get(url, *, params, timeout, headers=None):
            if url.endswith("/earning-call-transcript-dates"):
                return _Response([{"fiscalYear": 2024, "quarter": 1}])
            if url.endswith("/earning-call-transcript"):
                return _Response([])
            raise AssertionError("a transient empty FMP refresh must not trigger Alpha")

        download_us_earnings_call_transcripts(
            symbols=["AAPL"],
            start_quarter="2024Q1",
            end_quarter="2024Q1",
            force=True,
            refresh_recent_quarters=0,
            output_root=root,
            http_get=empty_refresh_get,
            sleeper=lambda _: None,
        )
        preserved = json.loads(
            _fmp_path(root, "2024Q1").read_text(encoding="utf-8")
        )
        assert preserved["status"] == "ok"
        assert preserved["data"][0]["content"] == "cached FMP transcript"

        with patch.dict(
            os.environ,
            {"ALPHA_VANTAGE_API_KEY": "alpha-secret"},
            clear=True,
        ):
            cached_priority = download_us_earnings_call_transcripts(
                symbols=["AAPL"],
                start_quarter="2024Q1",
                end_quarter="2024Q1",
                refresh_recent_quarters=0,
                output_root=root,
                http_get=lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError("cached FMP must retain priority without an FMP key")
                ),
                sleeper=lambda _: None,
            )
        assert cached_priority["priority_skipped"] == 1


def test_missing_fmp_key_uses_alpha_without_leaking_key(capsys):
    with TemporaryDirectory() as temp, patch.dict(
        os.environ,
        {"ALPHA_VANTAGE_API_KEY": "alpha-runtime-secret"},
        clear=True,
    ):
        calls = []

        def fake_get(url, *, params, timeout, headers=None):
            calls.append((url, params.copy()))
            return _Response({"transcript": [{"content": "alpha only"}]})

        result = download_us_earnings_call_transcripts(
            symbols=["AAPL"],
            start_quarter="2024Q1",
            end_quarter="2024Q1",
            refresh_recent_quarters=0,
            output_root=temp,
            http_get=fake_get,
            sleeper=lambda _: None,
        )

        assert result["providers"]["fmp"]["auth_disabled"]
        assert len(calls) == 1
        assert calls[0][0].endswith("/query")
        assert "alpha-runtime-secret" not in capsys.readouterr().out


def test_fmp_subscription_restriction_immediately_uses_alpha():
    with TemporaryDirectory() as temp, patch.dict(
        os.environ,
        {"FMP_API_KEY": "restricted-key", "ALPHA_VANTAGE_API_KEY": "alpha-key"},
        clear=True,
    ):
        calls = []

        def fake_get(url, *, params, timeout, headers=None):
            calls.append(url)
            if url.endswith("/earning-call-transcript-dates"):
                return _Response(
                    "Restricted Endpoint: upgrade your subscription",
                    status_code=402,
                )
            return _Response({"transcript": [{"content": "Alpha fallback"}]})

        result = download_us_earnings_call_transcripts(
            symbols=["AAPL"],
            start_quarter="2024Q1",
            end_quarter="2024Q1",
            refresh_recent_quarters=0,
            output_root=temp,
            http_get=fake_get,
            sleeper=lambda _: None,
        )

        assert len(calls) == 2
        assert result["providers"]["fmp"]["auth_disabled"]
        assert result["providers"]["alpha-vantage"]["written"] == 1


def test_alpha_premium_error_stops_remaining_requests():
    with TemporaryDirectory() as temp, patch.dict(
        os.environ,
        {"ALPHA_VANTAGE_API_KEY": "alpha-secret"},
        clear=True,
    ):
        calls = []

        def fake_get(url, *, params, timeout, headers=None):
            calls.append(params["quarter"])
            return _Response(
                {"Information": "This is a premium endpoint. Please subscribe."}
            )

        result = download_us_earnings_call_transcripts(
            symbols=["AAPL"],
            sources=["alpha-vantage"],
            start_quarter="2024Q1",
            end_quarter="2024Q4",
            refresh_recent_quarters=0,
            output_root=temp,
            http_get=fake_get,
            sleeper=lambda _: None,
        )

        assert calls == ["2024Q1"]
        assert result["providers"]["alpha-vantage"]["auth_disabled"]


def test_failed_alpha_quarter_is_retried_after_primary_pass():
    with TemporaryDirectory() as temp, patch.dict(
        os.environ,
        {"ALPHA_VANTAGE_API_KEY": "alpha-secret"},
        clear=True,
    ):
        calls = []

        def fake_get(url, *, params, timeout, headers=None):
            quarter = params["quarter"]
            calls.append(quarter)
            if calls == ["2024Q1"]:
                return _Response({}, status_code=500)
            return _Response({"transcript": [{"content": f"transcript {quarter}"}]})

        result = download_us_earnings_call_transcripts(
            symbols=["AAPL"],
            sources=["alpha-vantage"],
            start_quarter="2024Q1",
            end_quarter="2024Q2",
            refresh_recent_quarters=0,
            alpha_retries=0,
            alpha_retry_passes=1,
            output_root=temp,
            http_get=fake_get,
            sleeper=lambda _: None,
        )

        assert calls == ["2024Q1", "2024Q2", "2024Q1"]
        assert result["failed"] == 0
        assert result["recovered"] == 1
        assert result["providers"]["alpha-vantage"]["recovered"] == 1
        assert json.loads(_alpha_path(Path(temp), "2024Q1").read_text())["status"] == "ok"
        assert json.loads(_alpha_path(Path(temp), "2024Q2").read_text())["status"] == "ok"


def test_real_alpha_streaming_read_enforces_total_deadline():
    response = _StreamingResponse([b'{"transcript":'])
    calls = []

    def fake_get(url, *, params, timeout, stream):
        calls.append((url, params, timeout, stream))
        return response

    with patch(
        "engine.extractors._internal.us_earnings_call_transcripts.time.monotonic",
        side_effect=[100.0, 176.0],
    ):
        try:
            _alpha_streaming_get(
                fake_get,
                params={"apikey": "secret"},
                total_timeout=75.0,
            )
        except Exception as exc:
            assert type(exc).__name__ == "Timeout"
        else:
            raise AssertionError("expected total response timeout")

    assert response.closed
    assert calls == [
        (
            "https://www.alphavantage.co/query",
            {"apikey": "secret"},
            (10.0, 15.0),
            True,
        )
    ]


def test_full_universe_uses_fmp_catalog_before_per_symbol_requests():
    with TemporaryDirectory() as temp, patch.dict(
        os.environ,
        {"FMP_API_KEY": "fmp-secret", "ALPHA_VANTAGE_API_KEY": "alpha-secret"},
        clear=True,
    ), patch(
        "engine.extractors._internal.us_earnings_call_transcripts._resolve_symbols",
        return_value=["AAPL", "MSFT"],
    ):
        calls = []

        def fake_get(url, *, params, timeout, headers=None):
            calls.append((url, params.copy()))
            if url.endswith("/earnings-transcript-list"):
                return _Response([{"symbol": "AAPL", "transcriptCount": 20}])
            if url.endswith("/earning-call-transcript-dates"):
                assert params["symbol"] == "AAPL"
                return _Response([{"fiscalYear": 2024, "quarter": 1}])
            if url.endswith("/earning-call-transcript"):
                assert params["symbol"] == "AAPL"
                return _Response([{"content": "FMP catalog member"}])
            assert params["symbol"] == "MSFT"
            return _Response({"transcript": [{"content": "Alpha catalog fallback"}]})

        result = download_us_earnings_call_transcripts(
            symbols=None,
            start_quarter="2024Q1",
            end_quarter="2024Q1",
            refresh_recent_quarters=0,
            output_root=temp,
            http_get=fake_get,
            sleeper=lambda _: None,
        )

        assert len(calls) == 4
        assert calls[0][0].endswith("/earnings-transcript-list")
        assert result["symbols"] == 2
        assert result["priority_skipped"] == 1
        catalog_path = (
            Path(temp) / "fmp" / "catalog" / "earnings-transcript-list.json"
        )
        assert json.loads(catalog_path.read_text(encoding="utf-8"))["status"] == "ok"


def test_quarter_parser_rejects_invalid_values():
    assert _parse_quarter("2024-Q3") == (2024, 3)

    for value in ("2024", "2024Q0", "2024Q5", "Q12024"):
        try:
            _parse_quarter(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {value}")


def test_rate_limit_state_retries_transient_windows_permission_error():
    with TemporaryDirectory() as temp:
        state_path = Path(temp) / "alpha-rate-limit.json"
        real_replace = os.replace
        attempts = []

        def flaky_replace(source, destination):
            attempts.append((source, destination))
            if len(attempts) == 1:
                raise PermissionError("transient Windows file lock")
            return real_replace(source, destination)

        limiter = RollingRateLimiter(
            max_calls_per_minute=1,
            state_path=state_path,
            sleeper=lambda _: None,
        )
        with patch(
            "engine.core.source_storage.os.replace",
            side_effect=flaky_replace,
        ), patch("engine.core.source_storage.time.sleep") as sleep_mock:
            limiter.acquire()

        assert state_path.exists()
        assert len(attempts) == 2
        sleep_mock.assert_called_once_with(0.05)


def test_audit_accepts_fmp_priority_and_alpha_no_data_coverage():
    with TemporaryDirectory() as temp:
        root = Path(temp)
        _write_json(
            _transcript_path(root, "fmp", "AAPL", 2024, 1),
            _envelope(
                provider="FMP",
                dataset="EARNING_CALL_TRANSCRIPT",
                status="ok",
                symbol="AAPL",
                year=2024,
                quarter=1,
                data=[{"content": "FMP primary"}],
            ),
        )
        _write_json(
            _transcript_path(root, "alpha-vantage", "AAPL", 2024, 2),
            _envelope(
                provider="ALPHA_VANTAGE",
                dataset="EARNINGS_CALL_TRANSCRIPT",
                status="no_data",
                symbol="AAPL",
                year=2024,
                quarter=2,
                data={},
            ),
        )

        report = audit_us_earnings_call_transcripts(
            symbols=["AAPL"],
            start_quarter="2024Q1",
            end_quarter="2024Q2",
            output_root=root,
            verification_path=root / "verification.json",
        )

        assert report["complete"]
        assert report["expected"] == 2
        assert report["covered"] == 2
        assert report["transcripts"] == 1
        assert report["no_data"] == 1
        assert report["providers"] == {"fmp": 1, "alpha-vantage": 1}
        assert json.loads((root / "verification.json").read_text())["complete"]


def test_audit_reports_missing_and_invalid_quarters():
    with TemporaryDirectory() as temp:
        root = Path(temp)
        invalid_path = _transcript_path(
            root,
            "alpha-vantage",
            "AAPL",
            2024,
            1,
        )
        invalid_path.parent.mkdir(parents=True)
        invalid_path.write_text("{}", encoding="utf-8")

        report = audit_us_earnings_call_transcripts(
            symbols=["AAPL"],
            start_quarter="2024Q1",
            end_quarter="2024Q2",
            output_root=root,
        )

        assert not report["complete"]
        assert report["invalid"] == 1
        assert report["missing"] == 1
        assert report["covered"] == 0
