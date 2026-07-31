[CmdletBinding()]
param(
    [string]$AsOfDate = "",
    [string]$DownloadEndDate = (Get-Date -Format "yyyy-MM-dd"),
    [int]$PriceWorkers = 8,
    [double]$CompleteUniverseRatio = 0.99,
    [switch]$SkipPriceDownload,
    [switch]$SkipDividendLoad,
    [switch]$SkipTests
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepoRoot ".venv-llama\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python virtual environment not found: $Python"
}

Set-Location $RepoRoot

function Invoke-PythonBlock {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Code,
        [Parameter(Mandatory = $true)]
        [string]$Step
    )

    Write-Host "[STEP] $Step"
    $Code | & $Python -u -
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

if (-not $SkipTests) {
    Write-Host "[STEP] focused tests"
    & $Python -m unittest `
        tests.test_dividend_normalizer.DividendNormalizerTest.test_create_all_stock_dividend_uses_local_price_universe `
        tests.test_factor_elt.FactorEltTest.test_resolve_kr_stock_codes_uses_local_price_universe
    if ($LASTEXITCODE -ne 0) {
        throw "Focused tests failed with exit code $LASTEXITCODE"
    }
}

$env:ARCANA_DOWNLOAD_END_DATE = $DownloadEndDate
$env:ARCANA_PRICE_WORKERS = [string]$PriceWorkers

if (-not $SkipPriceDownload) {
    Invoke-PythonBlock -Step "incremental KRX price download" -Code @'
from datetime import date, timedelta
from pathlib import Path
import csv
import os

from engine.workflows._internal.refresh_workflow import download_incremental_krx_dataset


def last_csv_date(path: Path):
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 4096))
        lines = handle.read().decode("utf-8-sig", errors="replace").splitlines()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            return date.fromisoformat(next(csv.reader([line]))[0].strip())
        except (ValueError, IndexError, csv.Error):
            return None
    return None


root = Path("data-lake/bronze/krx/price")
dated_paths = [(path, last_csv_date(path)) for path in root.glob("kr_*.csv")]
latest = max((item for _, item in dated_paths if item is not None), default=None)
if latest is None:
    raise SystemExit("No local KRX price files found")

# Include files active within the last week.  This keeps temporarily lagging
# symbols in the universe while excluding long-delisted historical files.
active_floor = latest - timedelta(days=7)
stock_codes = sorted(
    path.stem.removeprefix("kr_")
    for path, last_date in dated_paths
    if last_date is not None and last_date >= active_floor
)
print(
    f"[INFO] local price universe latest={latest}, active_floor={active_floor}, "
    f"stocks={len(stock_codes):,}",
    flush=True,
)
download_incremental_krx_dataset(
    "price",
    stock_codes,
    end_date=os.environ["ARCANA_DOWNLOAD_END_DATE"],
    workers=int(os.environ["ARCANA_PRICE_WORKERS"]),
    progress_interval=250,
)
'@
}

$env:ARCANA_REQUESTED_ASOF = $AsOfDate
$env:ARCANA_COMPLETE_UNIVERSE_RATIO = [string]$CompleteUniverseRatio

$resolvedAsOf = (@'
import math
import os
import pandas as pd

from engine.transformers.market_data import normalize_price

frame = normalize_price(r"data-lake\bronze\krx\price\*")
frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
counts = frame.dropna(subset=["trade_date"]).groupby("trade_date")["security_id"].nunique().sort_index()
recent = counts.tail(20)
if recent.empty:
    raise SystemExit("Normalized KRX price data is empty")

ratio = float(os.environ["ARCANA_COMPLETE_UNIVERSE_RATIO"])
reference_count = int(recent.max())
minimum_count = math.ceil(reference_count * ratio)
complete_dates = recent.loc[recent >= minimum_count]
if complete_dates.empty:
    raise SystemExit("No complete KRX trade-date cross-section found")

requested = os.environ.get("ARCANA_REQUESTED_ASOF", "").strip()
if requested:
    selected = pd.Timestamp(requested).normalize()
    selected_count = int(counts.get(selected, 0))
    if selected_count < minimum_count:
        raise SystemExit(
            f"Requested as-of date is incomplete: date={selected.date()}, "
            f"securities={selected_count:,}, required={minimum_count:,}"
        )
else:
    selected = complete_dates.index.max()

print(selected.strftime("%Y-%m-%d"))
'@ | & $Python -u -).Trim()

if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($resolvedAsOf)) {
    throw "Failed to resolve a complete factor as-of date"
}
$AsOfDate = $resolvedAsOf.Split([Environment]::NewLine)[-1].Trim()
$env:ARCANA_FACTOR_ASOF = $AsOfDate
Write-Host "[INFO] factor as-of date=$AsOfDate"

Invoke-PythonBlock -Step "incremental price_daily load" -Code @'
import os
import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.loaders.market_data import create_price_dataframe

as_of = pd.Timestamp(os.environ["ARCANA_FACTOR_ASOF"])
frame = create_price_dataframe(market="kr", source="silver")
frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
client = get_clickhouse_client()
try:
    latest = pd.Timestamp(
        client.query_df(
            """
SELECT max(trade_date) AS d
FROM price_daily
WHERE startsWith(security_id, 'SEC_KR_')
""".strip()
        ).iloc[0, 0]
    )
    candidate = frame.loc[(frame["trade_date"] > latest) & (frame["trade_date"] <= as_of)].copy()
    candidate["trade_date"] = candidate["trade_date"].dt.date
    if not candidate.empty:
        client.insert_df("price_daily", candidate, column_names=list(candidate.columns))
    print(
        f"[DONE] price_daily latest_before={latest.date()}, "
        f"as_of={as_of.date()}, inserted_rows={len(candidate):,}",
        flush=True,
    )
finally:
    client.close()
'@

if (-not $SkipDividendLoad) {
    Write-Host "[STEP] rebuild and load dividend Silver"
    & $Python -m engine.loaders.dividends --market kr
    if ($LASTEXITCODE -ne 0) {
        throw "Dividend Silver/ClickHouse load failed with exit code $LASTEXITCODE"
    }
}

Invoke-PythonBlock -Step "load current dividend factors" -Code @'
from datetime import datetime
from zoneinfo import ZoneInfo
import math
import os

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.loaders.dividends import dividend_output_path
from engine.loaders.factors import FACT_DAILY_FACTOR_COLUMNS, insert_factor_catalog

as_of = pd.Timestamp(os.environ["ARCANA_FACTOR_ASOF"])
parts = []
for chunk in pd.read_csv(
    dividend_output_path("kr"),
    usecols=["security_id", "trade_date", "payout_ratio", "dividend_percent"],
    dtype={"security_id": "string", "trade_date": "string"},
    chunksize=500_000,
):
    rows = chunk.loc[chunk["trade_date"].str[:10].eq(as_of.strftime("%Y-%m-%d"))].copy()
    if not rows.empty:
        parts.append(rows)
if not parts:
    raise SystemExit(f"No dividend Silver rows for {as_of.date()}")
daily = pd.concat(parts, ignore_index=True).drop_duplicates("security_id", keep="last")

metadata = pd.read_csv(
    "data-lake/silver/dart/kr_report_metadata.csv",
    dtype={"security_id": "string", "stock_code": "string", "rcept_no": "string", "source_type": "string"},
    low_memory=False,
)
metadata["report_date"] = pd.to_datetime(metadata["report_date"], errors="coerce")
metadata["period_end_date"] = pd.to_datetime(metadata["period_end_date"], errors="coerce")
metadata["fiscal_year"] = pd.to_numeric(metadata["fiscal_year"], errors="coerce").astype("Int64")
metadata["fiscal_month"] = pd.to_numeric(metadata["fiscal_month"], errors="coerce").astype("Int64")
metadata = metadata.loc[
    metadata["source_type"].eq("statement")
    & metadata["fiscal_month"].eq(12)
    & metadata["report_date"].notna()
    & metadata["report_date"].le(as_of)
].copy()
metadata = (
    metadata.sort_values(["report_date", "rcept_no"], kind="stable")
    .drop_duplicates("security_id", keep="last")
)
metadata = metadata[["security_id", "fiscal_year", "period_end_date"]].rename(
    columns={"period_end_date": "financial_period"}
)
daily = daily.merge(metadata, on="security_id", how="left")

updated_at = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
outputs = []
for factor_id, source_column, multiplier in [
    ("dividend_yield", "dividend_percent", 1.0),
    ("payout_ratio", "payout_ratio", 100.0),
]:
    part = daily[["security_id", "fiscal_year", "financial_period", source_column]].copy()
    part["factor_value"] = pd.to_numeric(part[source_column], errors="coerce") * multiplier
    part = part.loc[
        part["factor_value"].map(
            lambda value: value is not None and pd.notna(value) and math.isfinite(float(value))
        )
    ].copy()
    part["trade_date"] = as_of.date()
    part["factor_id"] = factor_id
    part["financial_basis"] = "annual"
    part["currency"] = "KRW"
    part["updated_at"] = updated_at
    part["fiscal_year"] = part["fiscal_year"].map(
        lambda value: None if pd.isna(value) else int(value)
    )
    part["financial_period"] = pd.to_datetime(
        part["financial_period"], errors="coerce"
    ).map(lambda value: None if pd.isna(value) else value.date())
    outputs.append(part[FACT_DAILY_FACTOR_COLUMNS])

result = pd.concat(outputs, ignore_index=True)
client = get_clickhouse_client()
try:
    as_of_sql = as_of.strftime("%Y-%m-%d")
    client.command(
        "ALTER TABLE fact_daily_factors DELETE WHERE "
        f"trade_date = toDate('{as_of_sql}') "
        "AND financial_basis = 'annual' "
        "AND factor_id IN ('dividend_yield', 'payout_ratio') "
        "SETTINGS mutations_sync = 2"
    )
    insert_factor_catalog(client, factor_ids=["dividend_yield", "payout_ratio"])
    client.insert_df("fact_daily_factors", result, column_names=FACT_DAILY_FACTOR_COLUMNS)
finally:
    client.close()
print(
    "[DONE] current factor rows "
    + str(result.groupby("factor_id").size().to_dict()),
    flush=True,
)
'@

Invoke-PythonBlock -Step "clear current factor snapshot" -Code @'
import os

from engine.core.clickhouse import get_clickhouse_client

as_of = os.environ["ARCANA_FACTOR_ASOF"]
client = get_clickhouse_client()
try:
    client.command(
        "ALTER TABLE fact_daily_factor_snapshot DELETE WHERE "
        f"trade_date = toDate('{as_of}') "
        "AND financial_basis = 'annual' "
        "AND factor_id IN ('dividend_yield', 'payout_ratio') "
        "SETTINGS mutations_sync = 2"
    )
finally:
    client.close()
'@

Write-Host "[STEP] rebuild current factor snapshot"
& $Python -m engine.loaders.factor_snapshots `
    --financial-basis annual `
    --start-date $AsOfDate `
    --end-date $AsOfDate `
    --factor-ids dividend_yield,payout_ratio `
    --copy-raw-only `
    --factor-chunk-size 2 `
    --max-threads 2
if ($LASTEXITCODE -ne 0) {
    throw "Factor snapshot load failed with exit code $LASTEXITCODE"
}

Invoke-PythonBlock -Step "verify dividend factors" -Code @'
import os

from engine.core.clickhouse import get_clickhouse_client

as_of = os.environ["ARCANA_FACTOR_ASOF"]
client = get_clickhouse_client()
try:
    for table in ("fact_daily_factors", "fact_daily_factor_snapshot"):
        query = f"""
        SELECT factor_id,
               uniqExact(security_id) AS securities,
               count() AS rows
        FROM {table} FINAL
        WHERE trade_date = toDate(%(as_of)s)
          AND financial_basis = 'annual'
          AND factor_id IN ('dividend_yield', 'payout_ratio')
        GROUP BY factor_id
        ORDER BY factor_id
        """
        print(f"[VERIFY] {table} as_of={as_of}")
        print(client.query_df(query, parameters={"as_of": as_of}).to_string(index=False))
finally:
    client.close()
'@

Remove-Item Env:ARCANA_DOWNLOAD_END_DATE -ErrorAction SilentlyContinue
Remove-Item Env:ARCANA_PRICE_WORKERS -ErrorAction SilentlyContinue
Remove-Item Env:ARCANA_REQUESTED_ASOF -ErrorAction SilentlyContinue
Remove-Item Env:ARCANA_COMPLETE_UNIVERSE_RATIO -ErrorAction SilentlyContinue
Remove-Item Env:ARCANA_FACTOR_ASOF -ErrorAction SilentlyContinue

Write-Host "[DONE] dividend factor workflow completed as_of=$AsOfDate"
