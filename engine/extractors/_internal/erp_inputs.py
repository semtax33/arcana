from __future__ import annotations

import json
import os
from pathlib import Path
import ssl
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from engine.core.paths import DATA_LAKE


DAMODARAN_COUNTRY_ERP_URL = "https://www.stern.nyu.edu/~adamodar/pc/datasets/ctryprem.xlsx"
BRONZE_DAMODARAN_COUNTRY_ERP_PATH = DATA_LAKE.bronze(
    "damodaran",
    "country_risk_premiums",
    "ctryprem.xlsx",
)
BRONZE_FRED_RATE_DIR = DATA_LAKE.bronze("fred", "rates")
BRONZE_US_SP500_BENCHMARK_PATH = DATA_LAKE.bronze("yfinance", "benchmark", "us_sp500.csv")
FRED_SERIES_IDS = {
    "us_10y_treasury": "DGS10",
    "kr_10y_government_bond": "IRLTLT01KRM156N",
}
FRED_OUTPUT_NAMES = {
    "DGS10": "us_dgs10.csv",
    "IRLTLT01KRM156N": "kr_10y_gov_bond.csv",
}


def download_damodaran_country_erp(
    *,
    url: str = DAMODARAN_COUNTRY_ERP_URL,
    output_path: str | Path = BRONZE_DAMODARAN_COUNTRY_ERP_PATH,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_download_bytes(url, user_agent="Arcana Damodaran ERP loader"))
    return output


def download_fred_series(
    series_id: str,
    *,
    output_path: str | Path | None = None,
    api_key: str | None = None,
    timeout: int = 45,
) -> Path:
    series_id = str(series_id or "").strip().upper()
    if not series_id:
        raise ValueError("series_id must not be empty")

    output = Path(output_path) if output_path is not None else BRONZE_FRED_RATE_DIR / FRED_OUTPUT_NAMES.get(series_id, f"{series_id.lower()}.csv")
    output.parent.mkdir(parents=True, exist_ok=True)
    api_key = api_key or os.environ.get("FRED_API_KEY")
    if api_key:
        output.write_text(
            _download_fred_api_csv(series_id, api_key=api_key, timeout=timeout),
            encoding="utf-8",
        )
        return output

    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    output.write_bytes(_download_bytes(url, user_agent="Arcana FRED loader", timeout=timeout))
    return output


def _download_fred_api_csv(series_id: str, *, api_key: str, timeout: int) -> str:
    query = urlencode(
        {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
        }
    )
    url = f"https://api.stlouisfed.org/fred/series/observations?{query}"
    payload = json.loads(_download_bytes(url, user_agent="Arcana FRED API loader", timeout=timeout).decode("utf-8"))
    observations = payload.get("observations") or []
    lines = [f"DATE,{series_id}"]
    for observation in observations:
        date_value = str(observation.get("date") or "").strip()
        rate_value = str(observation.get("value") or "").strip()
        if not date_value:
            continue
        lines.append(f"{date_value},{'' if rate_value == '.' else rate_value}")
    return "\n".join(lines) + "\n"


def download_us_sp500_benchmark(
    *,
    output_path: str | Path = BRONZE_US_SP500_BENCHMARK_PATH,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Path:
    from engine.extractors._internal.yfinance_market_prices import fetch_yfinance_price

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    frame = fetch_yfinance_price("^GSPC", start_date=start_date, end_date=end_date)
    if frame.empty:
        raise RuntimeError("empty yfinance result for S&P 500 benchmark")
    frame.to_csv(output, index=False, encoding="utf-8-sig")
    return output


def download_default_erp_inputs(
    *,
    market: str | None = None,
    raise_on_error: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[Path]:
    paths = []
    errors = []
    try:
        paths.append(download_damodaran_country_erp())
    except Exception as exc:
        errors.append(exc)

    for series_id in FRED_SERIES_IDS.values():
        try:
            paths.append(download_fred_series(series_id))
        except Exception as exc:
            errors.append(exc)

    if str(market or "").strip().lower() == "us":
        try:
            paths.append(download_us_sp500_benchmark(start_date=start_date, end_date=end_date))
        except Exception as exc:
            errors.append(exc)

    if raise_on_error and errors:
        raise RuntimeError("one or more ERP input downloads failed") from errors[0]
    return paths


def _download_bytes(url: str, *, user_agent: str, timeout: int = 120) -> bytes:
    request = Request(url, headers={"User-Agent": user_agent})
    try:
        return _read_url_bytes(request, timeout=timeout)
    except URLError as exc:
        if not _is_ssl_certificate_error(exc):
            raise

        context = _certifi_ssl_context()
        if context is None:
            raise RuntimeError(
                "SSL certificate verification failed. Install certifi or fix the local Python certificate store."
            ) from exc
        return _read_url_bytes(request, context=context, timeout=timeout)


def _read_url_bytes(request: Request, *, context: ssl.SSLContext | None = None, timeout: int = 120) -> bytes:
    kwargs = {"timeout": timeout}
    if context is not None:
        kwargs["context"] = context
    with urlopen(request, **kwargs) as response:
        return response.read()


def _is_ssl_certificate_error(exc: URLError) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc)


def _certifi_ssl_context() -> ssl.SSLContext | None:
    try:
        import certifi
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())
