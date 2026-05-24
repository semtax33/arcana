# StatementParsing

Korean market statement parsing and ELT pipeline for DART, KRX market data, factors, dividends, benchmarks, and ClickHouse loading.

## Environment

PowerShell에서 저장소 루트로 이동한 뒤 venv를 활성화합니다.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& D:\Programming\python_example\StatementParsing\.venv-llama\Scripts\Activate.ps1
```

이 세션 기준으로 시스템 `python`은 PATH에 없고, `.venv-llama`는 원본 Python 위치를 찾지 못할 수 있습니다. 그 경우 venv를 복구한 뒤 아래 명령을 실행합니다.

## Data Lake Layout

기본 데이터 루트는 `data-lake/`입니다.

```text
data-lake/
  bronze/
    dart/
    krx/
  silver/
    dart/
    krx/
  meta/
    CanonicalAccount.csv
    rules/
```

새 CSV 산출물은 시장 prefix를 사용합니다.

```text
kr_005930.csv
kr_normalized_price.csv
kr_normalized_shares.csv
kr_report_metadata.csv
kr_normalized_005930_2025.12.csv
kr_dividend_by_stock_kind.csv
kr_dividend_company_summary.csv
kr_normalized_benchmark_price.csv
```

기존 legacy 파일명도 읽기 fallback으로 지원합니다. 예: `normalized_price.csv`, `report_metadata.csv`, `normalized_005930_2025.12.csv`.

## ELT Flow

### 1. Extract / Download

```powershell
python -m engine.workflows.download prices
python -m engine.workflows.download shares
python -m engine.workflows.download statements
python -m engine.workflows.download comments
python -m engine.workflows.download metadata
python -m engine.workflows.download dividend
```

`prices`와 `shares`는 KRX bronze CSV를 `data-lake/bronze/krx/...` 아래에 저장합니다. DART 재무제표와 주석은 `data-lake/bronze/dart/...` 아래에 저장합니다.

### 2. Transform / Normalize

```powershell
python -m engine.workflows.normalize
```

DART HTML 재무제표를 canonical account 기준 CSV로 정규화합니다. 산출 경로는 `data-lake/silver/dart/normalized/kr_normalized_{stock_code}_{year}.{month}.csv`입니다.

### 3. Load Market Data

```powershell
python -m engine.loaders.market_data
```

KRX bronze price/share CSV를 정규화한 뒤 ClickHouse `price_daily`, `stock_shares`에 적재합니다.

### 4. Load Filings / Securities / Dividends

```powershell
python -m engine.loaders.filings
python -m engine.loaders.securities
python -m engine.loaders.dividends
```

### 5. Load Factors

Dry run:

```powershell
python -m engine.loaders.factors --financial-basis annual --dry-run
```

Insert:

```powershell
python -m engine.loaders.factors --financial-basis annual --start-date 2026-01-01 --end-date 2026-05-24
```

주요 옵션:

```text
--stock-codes 005930,000660
--financial-basis annual|quarterly|ttm
--start-date YYYY-MM-DD
--end-date YYYY-MM-DD
--skip-catalog
--dry-run
--insert-batch-size 25
--insert-max-rows 2000000
```

### 6. Benchmarks

```powershell
python -m engine.loaders.benchmarks --benchmark-ids KOSPI200,KOSDAQ --start-date 2010-01-01 --dry-run
python -m engine.loaders.benchmarks --benchmark-ids KOSPI200,KOSDAQ --start-date 2010-01-01
```

Bronze CSV에서 읽을 때:

```powershell
python -m engine.loaders.benchmarks --source bronze --bronze-path data-lake\bronze\krx\benchmark\*.csv --dry-run
```

### 7. Style Scores

```powershell
python -m engine.workflows.score_cli build-factor-scores --trade-date 2026-05-24 --factor-asof-mode asof --include-financials
python -m engine.workflows.score_cli build-style-scores --trade-date 2026-05-24 --style-profile DEFAULT
python -m engine.workflows.score_cli build-style-scores --start-date 2026-05-01 --end-date 2026-05-24 --skip-existing
python -m engine.workflows.score_cli validate-style-scores --trade-date 2026-05-24
python -m engine.workflows.score_cli debug-single-security-score --trade-date 2026-05-24 --security-id SEC_KR_005930
```

## Tests

```powershell
python -m unittest discover
python -m engine.workflows.download --help
python -m engine.loaders.factors --help
```

Git safe-directory 문제가 나면 상태 확인에는 아래처럼 일회성 옵션을 사용할 수 있습니다.

```powershell
git -c safe.directory=D:/Programming/python_example/StatementParsing status --short
```
