from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.markets.us import US_MARKET_CONFIG


SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_TICKER_MAP_PATH = DATA_LAKE.meta("sec_company_tickers.csv")


def download_sec_company_tickers(
    output_path: str | Path = SEC_TICKER_MAP_PATH,
    *,
    user_agent: str = "StatementParsing contact@example.com",
) -> pd.DataFrame:
    request = Request(
        SEC_COMPANY_TICKERS_URL,
        headers={
            "User-Agent": user_agent,
            "Accept-Encoding": "identity",
        },
    )
    with urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))

    rows = []
    for item in payload.values():
        rows.append(
            {
                "cik": str(int(item.get("cik_str"))),
                "ticker": US_MARKET_CONFIG.normalize_symbol(item.get("ticker")),
                "title": str(item.get("title", "")).strip(),
            }
        )

    df = pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    return df
