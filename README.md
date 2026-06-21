# Arcana

Korean market statement parsing and ELT pipeline for DART 공시, KRX 시장 데이터,
팩터, 배당, 벤치마크, 스타일 점수, ClickHouse 적재를 다룹니다.

## Environment

PowerShell에서 저장소 루트로 이동한 뒤 가상환경을 활성화합니다.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& D:\Programming\python_example\Arcana\.venv-llama\Scripts\Activate.ps1
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
    sec/
  silver/
    dart/
    krx/
    sec/
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
us_normalized_AAPL_2025.12.csv
kr_dividend_by_stock_kind.csv
kr_dividend_company_summary.csv
kr_normalized_benchmark_price.csv
```

일부 reader는 기존 파일명도 fallback으로 읽습니다. 예:
`normalized_price.csv`, `report_metadata.csv`,
`normalized_005930_2025.12.csv`.

캐노니컬 매핑 룰은 시장별로 분리합니다. KR은 `kr_mapping.yaml`,
`context_kr.yaml`, `comment_kr.yaml`을 사용하고, US는 `us_mapping.yaml`의
`companyfacts_rules`, `notes_rules`, `edgartools_fallback_rules`를 사용합니다.
이전 파일명인 `mapping_kr.yaml`, `mapping_us.yaml`은 호환 fallback으로만
참조합니다.
기존 `*_common.yaml` 파일은 호환 fallback으로 유지합니다.

## ELT Flow

### 1. Extract / Download

```powershell
python -m engine.workflows.download prices
python -m engine.workflows.download shares
python -m engine.workflows.download statements
python -m engine.workflows.download comments
python -m engine.workflows.download business-info
python -m engine.workflows.download metadata
python -m engine.workflows.download dividend
python -m engine.workflows.download --start-date 2024-01-01 --end-date 2024-03-31 prices
python -m engine.workflows.download --start-date 20240101 --end-date 20240331 statements
python -m engine.workflows.download --start-year 2000 --end-year 2025 statements
python -m engine.workflows.download --start-year 2000 --end-year 2025 comments
python -m engine.workflows.download --start-date 2024-01-01 --end-date 2024-12-31 business-info
python -m engine.workflows.download --start-year 2000 --end-year 2025 business-info
python -m engine.workflows.download --workers 4 business-info
python -m engine.workflows.download --workers 4 --sleep-seconds 3 business-info
python -m engine.workflows.download --workers 2 --sleep-seconds 8 --stock-retries 5 --stock-retry-backoff 60 business-info
python -m engine.workflows.download --force business-info
python -m engine.workflows.download --market us sec-tickers
python -m engine.workflows.download --market us prices --symbols AAPL,MSFT --limit 2
python -m engine.workflows.download --market us prices --offset 100 --limit 500 --sleep-seconds 0.2
```

`--start-date`, `--end-date` accepts `YYYYMMDD` or `YYYY-MM-DD` and limits the download range.
`--start-year`, `--end-year` accepts `YYYY` and expands to full-year date ranges.
DART statement/comment/business-info searches automatically split ranges longer than 10 years
into multiple DART search requests, so older 20-30 year windows can be downloaded.

`prices`와 `shares`는 KRX bronze CSV를 `data-lake/bronze/krx/...` 아래에
저장합니다. DART 재무제표, 주석, 메타데이터, 배당 공시는
`data-lake/bronze/dart/...` 아래에 저장합니다.
`business-info`는 DART main page의 `function makeToc()` JavaScript 목차에서
`사업의 내용` 계열 섹션 위치를 찾아 `viewer.do`로 해당 HTML만 다운로드합니다.
결과는 `data-lake/bronze/dart/business-info/{stock_code}/` 아래에 저장됩니다.
기존 파일이 있으면 자동으로 건너뛰어 이어받기처럼 동작하며, 다시 받으려면
`--force`를 사용합니다. 여러 종목을 병렬로 받으려면 `--workers 4`처럼 지정합니다.
DART HTML 요청은 User-Agent와 Accept-Language를 랜덤 풀에서 선택합니다.
병렬 다운로드 시 요청 간격을 늘리려면 `--sleep-seconds 8`처럼 지정합니다.
`--workers`를 2 이상으로 지정해도 DART 요청은 전역 limiter를 공유하므로
전체 요청 사이에 해당 간격이 적용됩니다.
일시적인 연결 종료가 발생하면 종목 단위로 `--stock-retries`만큼 재시도하고,
재시도 사이에는 `--stock-retry-backoff` 기준 exponential backoff를 적용합니다.

`--market us sec-tickers`는 SEC의 CIK/ticker 매핑을
`data-lake/meta/sec_company_tickers.csv`에 저장합니다. SEC companyfacts와
Financial Statement and Notes Data Sets 파일은 아래 위치를 사용합니다.

```text
data-lake/bronze/sec/companyfacts/
data-lake/bronze/sec/financial-statement-and-notes-data-set/
```

US price downloads use NasdaqTrader symbol directories for the universe and yfinance
`period=max` history files under `data-lake/bronze/yfinance/price/{TICKER}.csv`.
Install yfinance first if needed: `pip install yfinance`.

### 2. Transform / Normalize

```powershell
python -m engine.workflows.normalize
python -m engine.workflows.normalize --market kr
python -m engine.workflows.normalize --market us --symbols AAPL,MSFT --start-year 2020 --end-year 2025
python -m engine.workflows.normalize --target business-info
python -m engine.workflows.normalize --target business-info --symbols 005930,105560 --start-year 2026 --end-year 2026 --workers 8
python -m engine.workflows.normalize --target all --workers 8
```

KR은 DART HTML 재무제표를 canonical account 기준 CSV로 정규화합니다.
산출물은 `data-lake/silver/dart/normalized/` 아래에 저장됩니다. US는 SEC
companyfacts, SEC Notes Data Sets, edgartools fallback 순서로 값을 채워
`data-lake/silver/sec/normalized/` 아래에 저장합니다.

`--target` 기본값은 `statements`이며 기존처럼 재무제표만 파싱합니다.
`--target business-info`는 이미 다운로드된
`data-lake/bronze/dart/business-info/{stock_code}/business_info_(YYYY.MM).html`
파일을 읽어 사업의 내용 섹션과 표를 정규화합니다. `--symbols`로 종목을 제한할 수 있고,
`--start-year`, `--end-year`는 business-info 파일명의 연도를 기준으로 포함 범위를 지정합니다.
`--workers`는 business-info 파싱 스레드 수로 재사용됩니다. `--target all`은 KR 재무제표
정규화 후 business-info 정규화를 이어서 실행합니다. business-info 정규화는 KR 전용입니다.

business-info 파싱 규칙은 코드에 하드코딩하지 않고
`data-lake/meta/rules/kr_business_info.yaml`에서 관리합니다. 산출물은 종목별로 나뉘며,
한 종목 안에서는 선택된 모든 연도/분기 HTML이 합쳐집니다.

```text
data-lake/silver/dart/business-info/{stock_code}/kr_business_info_sections.csv
data-lake/silver/dart/business-info/{stock_code}/kr_business_info_tables.csv
data-lake/silver/dart/business-info/{stock_code}/kr_business_info_cells.csv
data-lake/silver/dart/business-info/{stock_code}/kr_business_info_rows.csv
```

`tables`는 table-level manifest이며 `table_id`, `table_kind`, `source_uri`,
`source_html_hash`, `header_paths_json`을 포함합니다. `cells`는 rowspan/colspan을
확장한 cell-level 구조, `rows`는 LLM 재처리와 검수에 쓰기 쉬운 row-level 구조입니다.

US fallback 값은 선택 의존성인 edgartools 패키지를 사용합니다. 패키지가
설치되지 않았거나 SEC 조회가 실패하면 edgartools fallback만 건너뛰고
companyfacts/Notes 기반 정규화는 계속 진행합니다. US 가격과 주식수 다운로드는
이 workflow에 포함되지 않으므로, 팩터 계산에는 별도 silver price/share 파일이
필요합니다.

KRX price/shares, dividend, benchmark 정규화는 아래 경로를 사용합니다.

Normalize price/shares silver CSV만 갱신:

```powershell
python -c "from engine.transformers.market_data import normalize_price, normalize_shares; normalize_price(r'data-lake\bronze\krx\price\*'); normalize_shares(r'data-lake\bronze\krx\shares\*')"
```

Normalize dividend silver CSV만 갱신:

```powershell
python -c "from engine.loaders.dividends import refresh_silver_dividend_files; refresh_silver_dividend_files()"
```

US SEC dividend event CSV와 daily dividend CSV 갱신:

```powershell
python -c "from engine.loaders.dividends import refresh_silver_dividend_files; refresh_silver_dividend_files(market='us')"
```

US 배당 정규화는 `data-lake/bronze/sec/financial-statement-and-notes-data-set/`의
SEC Notes Dataset을 1순위로 사용하고, 부족한 값은 선택 의존성인
edgartools fallback으로 보완합니다. 매핑 규칙은
`data-lake/meta/rules/us_dividend.yaml`에 있으며, `10-K`, `10-Q`, `8-K`
및 amendment form의 XBRL fact에서 선언일, 기준일, 지급일, 1주당 배당금을
추출합니다. `10-K` 원문 HTML/PDF를 직접 파싱하지 않고 SEC Notes/companyfacts
및 edgartools가 노출하는 XBRL fact를 사용합니다.

산출물:

```text
data-lake/silver/us/dividend/us_dividend_events.csv
data-lake/silver/us/dividend/us_dividend_normalized.csv
```

`us_dividend_events.csv` 컬럼:

```text
ticker,cik,company_name,exchange,dividend_declared_date,dividend_record_date,
dividend_payment_date,dividend_amount_per_share,sec_filing_date,source_form,
annual_dps,annual_eps,payout_ratio_dps_over_eps,
payout_ratio_total_dividends_over_net_income
```

`us_dividend_normalized.csv`는 기존 `stock_dividend` 적재 스키마를 유지하며
`trade_date=dividend_payment_date`, `dividend=dividend_amount_per_share`로 생성됩니다.
US 팩터 계산과 백테스트 입력 factor table은 이 SEC 기반 daily dividend CSV를
읽어 배당수익률, DPS, payout ratio, 배당 성장/삭감 관련 팩터를 계산합니다.

Normalize benchmark silver CSV만 갱신:

```powershell
python -c "from engine.loaders.benchmarks import normalize_downloaded_benchmark_prices; normalize_downloaded_benchmark_prices(r'data-lake\bronze\krx\benchmark\*.csv')"
```

아래 loader 명령들은 정규화된 silver 파일을 갱신한 뒤 ClickHouse 적재까지
이어 수행합니다.

### 3. Load Market Data

```powershell
python -m engine.loaders.market_data
python -m engine.loaders.market_data --market us --target prices --source bronze --dry-run
python -m engine.loaders.market_data --market us --target prices --source bronze
```

정규화된 KRX price/share 데이터를 ClickHouse의 `price_daily`,
`stock_shares` 같은 테이블에 적재합니다.
US `prices`/`all` 적재는 SEC/yfinance symbol universe 기반 `issuers`,
`security_master`, `identifiers` 참조 데이터도 먼저 적재합니다. 가격만 다시
적재하려면 `--skip-securities`를 사용합니다.

### 4. Load Filings / Securities / Dividends

```powershell
python -m engine.loaders.filings
python -m engine.loaders.securities
python -m engine.loaders.securities --market us
python -m engine.loaders.dividends
python -m engine.loaders.dividends --market us --dry-run
python -m engine.loaders.dividends --market us
```

`securities` 적재는 시장별 GICS 규칙을 적용해 `issuers`의 `sector_code`,
`industry_group_code`, `industry_group_name`을 함께 채웁니다. KR 규칙은
`data-lake/meta/rules/gics_rules_kr.yaml`, US 규칙은
`data-lake/meta/rules/gics_rules_us.yaml`을 사용합니다.

`engine.loaders.dividends --market us`는 먼저
`data-lake/silver/us/dividend/us_dividend_events.csv`와
`data-lake/silver/us/dividend/us_dividend_normalized.csv`를 갱신한 뒤,
아래 ClickHouse 테이블에 적재합니다. `--dry-run`은 silver CSV만 준비하고
ClickHouse insert는 수행하지 않습니다.

```sql
CREATE TABLE IF NOT EXISTS arcana.stock_dividend
(
    security_id      String,
    trade_date       Date,
    dividend         Nullable(Decimal(20, 6)),
    payout_ratio     Nullable(Float64),
    dividend_percent Nullable(Float64),
    currency         LowCardinality(String)      default '',
    updated_at       DateTime64(3, 'Asia/Seoul') default now64(3)
)
    engine = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(trade_date)
        ORDER BY (trade_date, security_id)
        SETTINGS index_granularity = 8192;
```

로더는 insert 직전에 `stock_dividend` 스키마에 맞춰 컬럼 순서와 타입을
정규화합니다. `trade_date`는 Date, `dividend`는 Decimal(20, 6),
`payout_ratio`와 `dividend_percent`는 nullable Float64, `updated_at`은
Asia/Seoul 기준 loader 실행 시각으로 들어갑니다.

### 5. Load Factors

Dry run:

```powershell
python -m engine.loaders.factors --financial-basis annual --dry-run
python -m engine.loaders.factors --market us --stock-codes AAPL --financial-basis annual --dry-run
```

Insert:

```powershell
python -m engine.loaders.factors --financial-basis annual --start-date 2026-01-01 --end-date 2026-05-24
python -m engine.loaders.factors --market us --stock-codes AAPL --financial-basis annual --start-date 2026-01-01 --end-date 2026-05-24
```

주요 옵션:

```text
--stock-codes 005930,000660
--market kr|us
--financial-basis annual|quarterly|ttm
--start-date YYYY-MM-DD
--end-date YYYY-MM-DD
--skip-catalog
--dry-run
--insert-batch-size 100
--insert-max-rows 2000000
--progress-interval 25
--price-path data-lake\silver\us\price\us_normalized_price.csv
```

US 팩터 적재는 `data-lake/silver/sec/normalized/` 재무 CSV와
`data-lake/silver/us/price/us_normalized_price.csv`가 있을 때 daily factor
rows를 생성합니다. `data-lake/silver/us/shares/us_normalized_shares.csv`가
있으면 shares/market cap 기반 팩터까지 함께 계산합니다.

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

US mapping coverage validator:

```powershell
python -m engine.us_mapping_coverage_validator --input-dir data-lake\silver\sec\normalized --rules data-lake\meta\rules\us_mapping.yaml --out-dir data-lake\silver\sec\mapping_coverage --start-year 2020 --end-year 2025
python -m engine.us_mapping_coverage_validator --symbols AAPL,MSFT --min-required-coverage-pct 80 --min-rule-hit-pct 10 --progress-interval 1 --strict
```

`us_mapping.yaml`의 companyfacts/notes/edgartools rule을 SEC normalized/debug
CSV output과 비교하고 `mapping_coverage_validation.json`,
`canonical_coverage.csv`, `source_contribution.csv`, `rule_coverage.csv`를
생성합니다. 진행상황은 stderr에 출력되며 `--progress-interval 0`으로 끌 수
있습니다. `--strict`를 붙이면 WARN/FAIL verdict에서 non-zero exit code로 종료합니다.

```powershell
python -m unittest discover
python -m unittest tests.test_sec_filings_normalizer
python -m engine.normalization_validator stock-batch --market us --input-dir data-lake\silver\sec\normalized --rules data-lake\meta\rules\common_validation.yaml --out-dir data-lake\silver\sec\validation --start-year 2020 --end-year 2025
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
git -c safe.directory=D:/Programming/python_example/Arcana status --short
```
