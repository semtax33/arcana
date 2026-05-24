# StatementParsing

Korean market statement parsing and ELT pipeline for DART 공시, KRX 시장 데이터,
팩터, 배당, 벤치마크, 스타일 점수, ClickHouse 적재를 다룹니다.

## Environment

PowerShell에서 저장소 루트로 이동한 뒤 가상환경을 활성화합니다.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& D:\Programming\python_example\StatementParsing\.venv-llama\Scripts\Activate.ps1
```

가상환경이 삭제된 Python 설치 경로를 가리키면 테스트나 파이프라인 명령을
실행하기 전에 venv를 다시 생성해야 합니다.

## Engine Layout

`engine`은 파이프라인 책임별 계층으로 나뉩니다. 리팩토링 이전 호환용 루트
모듈인 `engine.statements`, `engine.factor_normalizer`, `engine.factor_elt`,
`engine.style_score_pipeline` 등은 제거되었습니다.

```text
engine/
  core/          공통 경로, 식별자, 시장 헬퍼, ClickHouse 클라이언트
  markets/       시장별 설정
  extractors/    DART/KRX source 및 bronze 데이터 추출
  transformers/  정규화, 기간화, 팩터, 점수 규칙 변환
  loaders/       ClickHouse 적재 진입점
  workflows/     end-to-end 오케스트레이션 및 CLI workflow
```

코드와 테스트에서는 계층형 import 경로를 사용합니다.

```python
from engine.extractors.filings import collect_dart_report_metadata
from engine.transformers.filing_periods import add_quarter_and_ttm_amounts
from engine.transformers.factors import create_stock_factor_dataframe
from engine.loaders.factors import insert_daily_factors
from engine.workflows.score import calculate_style_scores
```

`_internal` 모듈은 구현 세부사항입니다. 외부 코드와 테스트에서는
`engine.loaders._internal.clickhouse_factors` 대신 `engine.loaders.factors`처럼
같은 계층의 public 모듈을 통해 import합니다.

## Data Lake Layout

기본 data lake 루트는 `data-lake/`입니다.

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
    sql/
```

새 CSV 산출물은 시장 prefix가 붙은 파일명을 우선 사용합니다.

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

일부 reader는 기존 파일명도 fallback으로 읽습니다. 예:
`normalized_price.csv`, `report_metadata.csv`,
`normalized_005930_2025.12.csv`.

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

`prices`와 `shares`는 KRX bronze CSV를 `data-lake/bronze/krx/...` 아래에
저장합니다. DART 재무제표, 주석, 메타데이터, 배당 공시는
`data-lake/bronze/dart/...` 아래에 저장합니다.

### 2. Transform / Normalize

```powershell
python -m engine.workflows.normalize
```

DART HTML 재무제표를 canonical account 기준 CSV로 정규화합니다. 산출물은
`data-lake/silver/dart/normalized/` 아래에 저장됩니다.

`engine.workflows.normalize`는 DART 재무제표 전용 workflow입니다. KRX
price/shares, dividend, benchmark 정규화는 아래 경로를 사용합니다.

Normalize price/shares silver CSV만 갱신:

```powershell
python -c "from engine.transformers.market_data import normalize_price, normalize_shares; normalize_price(r'data-lake\bronze\krx\price\*'); normalize_shares(r'data-lake\bronze\krx\shares\*')"
```

Normalize dividend silver CSV만 갱신:

```powershell
python -c "from engine.loaders.dividends import refresh_silver_dividend_files; refresh_silver_dividend_files()"
```

Normalize benchmark silver CSV만 갱신:

```powershell
python -c "from engine.loaders.benchmarks import normalize_downloaded_benchmark_prices; normalize_downloaded_benchmark_prices(r'data-lake\bronze\krx\benchmark\*.csv')"
```

아래 loader 명령들은 정규화된 silver 파일을 갱신한 뒤 ClickHouse 적재까지
이어 수행합니다.

### 3. Load Market Data

```powershell
python -m engine.loaders.market_data
```

정규화된 KRX price/share 데이터를 ClickHouse의 `price_daily`,
`stock_shares` 같은 테이블에 적재합니다.

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

### 6. Load Benchmarks

```powershell
python -m engine.loaders.benchmarks --benchmark-ids KOSPI200,KOSDAQ --start-date 2010-01-01 --dry-run
python -m engine.loaders.benchmarks --benchmark-ids KOSPI200,KOSDAQ --start-date 2010-01-01
```

Bronze CSV에서 읽을 때:

```powershell
python -m engine.loaders.benchmarks --source bronze --bronze-path data-lake\bronze\krx\benchmark\*.csv --dry-run
```

### 7. Build Style Scores

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
```

명령 진입점 확인:

```powershell
python -m engine.workflows.download --help
python -m engine.loaders.factors --help
python -m engine.workflows.score_cli --help
```

Git safe-directory 문제로 상태 확인이 막히면 아래처럼 일회성 옵션을 사용할 수
있습니다.

```powershell
git -c safe.directory=D:/Programming/python_example/StatementParsing status --short
```
