# Arcana

## API 키 및 토큰 설정

API 키와 토큰은 저장소에 기록하지 않고 실행할 PowerShell 프로세스의 환경변수로 전달합니다.

```powershell
$env:DART_API_KEY = "<YOUR_DART_API_KEY>"
$env:HANKYUNG_CONSENSUS_TOKEN = "<YOUR_HANKYUNG_TOKEN>"
$env:ALPHA_VANTAGE_CSRF_TOKEN = "<YOUR_ALPHA_VANTAGE_CSRF_TOKEN>" # scripts/get_api_key.py 전용
$env:CLICKHOUSE_PASSWORD = "<YOUR_CLICKHOUSE_PASSWORD>"
```

저장소에 이미 노출된 적이 있는 키와 토큰은 코드에서 제거한 뒤에도 Git 이력에 남아 있으므로 폐기하고 재발급해야 합니다. `.env` 파일과 `scripts/token_output.csv`는 Git에서 제외됩니다.

DART 공시와 KRX 시장 데이터를 다루는 한국 시장 재무제표 파싱 및 ELT 파이프라인입니다.
팩터, 배당, 벤치마크, 스타일 점수, ClickHouse 적재를 지원합니다.

## 환경 설정

PowerShell에서 저장소 루트로 이동한 뒤 가상환경을 활성화합니다.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& D:\Programming\python_example\Arcana\.venv-llama\Scripts\Activate.ps1
```

가상환경이 삭제된 Python 설치 경로를 가리키면 테스트나 파이프라인 명령을
실행하기 전에 venv를 다시 생성해야 합니다.

## 통합 최신 데이터 갱신

아래 명령 하나로 각 시장의 지원되는 원천 데이터부터 정규화 데이터,
ClickHouse raw factor와 factor snapshot까지 순서대로 갱신합니다.

```powershell
# 한국 시장 전체 최신 갱신
python -m engine.workflows.refresh --market kr

# 미국 시장 전체 최신 갱신
python -m engine.workflows.refresh --market us
```

`--targets all`이 기본값입니다. KR은 시장 데이터, 공시, 기업개황, 배당,
컨센서스, 벤치마크/WACC, 최신 운영지표, raw factor, factor snapshot을
갱신합니다. US는 현재 지원되는 시장 데이터, SEC 공시, 배당,
벤치마크/WACC, raw factor, factor snapshot을 갱신합니다. factor/style
score는 이 명령의 대상이 아닙니다.

전체 종목 대신 일부 종목만 갱신하거나 실행 내용을 미리 확인할 수 있습니다.

```powershell
python -m engine.workflows.refresh --market kr --symbols 005930,000660 --workers 4
python -m engine.workflows.refresh --market us --symbols AAPL,MSFT --workers 4
python -m engine.workflows.refresh --market kr --dry-run
```

갱신은 필수 원천 다운로드, 인증, 정규화 또는 적재 단계가 실패하면 즉시
실패합니다. 실패 원인을 해결한 뒤 같은 명령을 실행하면
`data-lake/meta/refresh_state/{market}_refresh_state.json`의 완료 단계부터
재개합니다. 처음부터 다시 실행하려면 `--no-resume`, 원천 전체 기간을 다시
받아야 할 때만 `--force-full`을 사용합니다.

### 원천 데이터 보존

통합 갱신 명령은 시장별 잠금을 획득하며, 기존 원천 파일을 바로 덮어쓰지
않습니다. 새 파일의 형식과 내용을 먼저 검증하고 기존 파일의 SHA-256
보관본을 아래 경로에 만든 뒤, 보관본 검증이 끝난 경우에만 현재 파일을
원자적으로 교체합니다.

```text
data-lake/source-archive/{market}/{run_id}/...
data-lake/meta/refresh-manifests/{market}/{run_id}.json
data-lake/meta/refresh_locks/{market}.lock
```

내용이 동일하면 불필요한 보관본을 만들지 않습니다. 검증이나 다운로드가
실패하면 기존 파일은 그대로 유지되며, 보관본은 통합 갱신 과정에서 자동
삭제하지 않습니다. Silver/Gold와 ClickHouse 데이터는 원천이 아니라 재생성
가능한 파생 데이터이므로 해당 시장과 갱신 기간 범위만 교체합니다.

운영 갱신은 최근 가격 단면에서 최대 종목 수의 99% 이상이 존재하는 가장
최근 거래일을 완전한 기준일로 선택합니다. raw factor가 비어 있으면 해당
기준일을 먼저 생성하고, factor snapshot이 비어 있으면 과거 전체를 만들지
않고 그 기준일 스냅샷만 생성합니다. 이후 실행은 마지막 적재일 다음 날부터
증분 갱신합니다. 임계값은 `--complete-universe-ratio`로 변경할 수 있습니다.

2010년 이후 전체 raw factor와 snapshot 이력은 운영 갱신과 분리해 필요한
시장별로 명시적으로 백필합니다.

```powershell
$EndDate = Get-Date -Format yyyy-MM-dd

python -m engine.loaders.factors --market kr --financial-basis annual --start-date 2010-01-01 --end-date $EndDate
python -m engine.loaders.factor_snapshots --market kr --financial-basis annual --start-date 2010-01-01 --end-date $EndDate --truncate

python -m engine.loaders.factors --market us --financial-basis annual --start-date 2010-01-01 --end-date $EndDate
python -m engine.loaders.factor_snapshots --market us --financial-basis annual --start-date 2010-01-01 --end-date $EndDate --truncate
```

## 엔진 구조

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

## 데이터 레이크 구조

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

## ELT 흐름

### 1. 추출 / 다운로드

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
python -m engine.workflows.download --market us statements --symbols AAPL,MSFT --limit 2
python -m engine.workflows.download --market us prices --symbols AAPL,MSFT --limit 2
python -m engine.workflows.download --market us prices --offset 100 --limit 500 --sleep-seconds 0.2
python -m engine.workflows.download --market kr benchmarks
python -m engine.workflows.download --market us benchmarks
python -m engine.workflows.download --market us --benchmark-ids S&P500,NASDAQ benchmarks
python -m engine.workflows.download consensus --market kr
python -m engine.workflows.download consensus --market kr --consensus-sources html --consensus-html-pages 3
python -m engine.workflows.normalize --target consensus --market kr
python -m engine.loaders.consensus --market kr --dry-run
python -m engine.loaders.consensus --market kr
```

`--start-date`, `--end-date`는 `YYYYMMDD` 또는 `YYYY-MM-DD` 형식을 허용하며 다운로드
범위를 제한합니다. `--start-year`, `--end-year`는 `YYYY` 형식을 허용하며 연간 전체
날짜 범위로 확장됩니다. DART 재무제표·주석·사업 정보 검색은 10년을 초과하는 범위를
여러 DART 검색 요청으로 자동 분할하므로 과거 20~30년 구간도 다운로드할 수 있습니다.

Hankyung 컨센서스 다운로드는 원본 JSON 파일을
`data-lake/bronze/consensus/hankyung/`에 저장합니다. ValueFinder와 EQUITY의 애널리스트
의견 목록 HTML은 BeautifulSoup으로 파싱하여 `data-lake/bronze/consensus/valuefinder/`와
`data-lake/bronze/consensus/equity/`에 저장합니다. 쿠키는 `--valuefinder-cookie` /
`--equity-cookie`로 전달하거나 `VALUEFINDER_CONSENSUS_COOKIE` /
`EQUITY_CONSENSUS_COOKIE` 환경 변수로 설정합니다. EQUITY의 페이지 수가 매우 많으므로
HTML 원천은 기본적으로 한 페이지만 가져옵니다. 정규화 과정은 silver CSV 파일을
`data-lake/silver/consensus/hankyung/`에 저장하며 ClickHouse 컨센서스 로더는 이 silver
CSV 파일만 읽습니다.

Hankyung 목표주가는 정규화 과정에서
`data-lake/silver/consensus/hankyung/kr_hankyung_target_price_consensus.csv`로
별도 저장됩니다. `kr_price_to_target_price` 팩터는 최근 120일 동안
증권사·애널리스트별 최신 목표주가가 3건 이상일 때 `종가 / 평균 목표주가`로 계산하며,
보고서 날짜의 다음 거래일부터 반영됩니다. 목표주가 대비 시장가격의 자기강화적 선행을
포착하기 위해 factor catalog에서는 `HIGHER_BETTER`로 등록됩니다.

### 미국 컨센서스 (FMP + Alpha Vantage + Yahoo Finance)

미국 컨센서스는 한국 컨센서스와 원본·정규화 테이블·팩터를 분리한다. FMP는 현재
재무 추정치와 목표주가를 우선 제공하고, Alpha Vantage는 과거 이벤트 상대 리비전,
실적발표 결과 및 분할을 담당한다. Yahoo Finance/yfinance는 상위 공급자에 없는 현재
리비전 breadth와 surprise를 보완한다. API 키는 소스나 CLI 인자가 아닌 실행 환경에서만
읽는다.

```powershell
$env:FINNWORLDS_API_KEY = "<YOUR_FINNWORLDS_KEY>"
$env:FMP_API_KEY = "<YOUR_FMP_KEY>"
$env:ALPHA_VANTAGE_API_KEY = "<YOUR_ALPHA_VANTAGE_KEY>"

# 전체 미국 개별주식 또는 지정 종목 수집
python -m engine.workflows.download `
  --market us `
  --us-consensus-sources finnworlds `
  --finnworlds-date-from 2000-01-01 `
  --finnworlds-date-to 2026-07-31 `
  --finnworlds-max-calls-per-minute 120 `
  consensus
python -m engine.workflows.download --market us --symbols AAPL,MSFT consensus

# 각 공급자만 수집할 수도 있음
python -m engine.workflows.download --market us --symbols AAPL consensus --us-consensus-sources finnworlds
python -m engine.workflows.download --market us --symbols AAPL consensus --us-consensus-sources fmp
python -m engine.workflows.download --market us --symbols AAPL consensus --us-consensus-sources alpha-vantage
python -m engine.workflows.download --market us --symbols AAPL consensus --us-consensus-sources yfinance

# Bronze -> Silver 정규화, Silver -> ClickHouse 적재
python -m engine.workflows.normalize --market us --target consensus
python -m engine.loaders.consensus --market us
```

미국 컨센서스의 기본 공급자 순서는 `finnworlds,fmp,alpha-vantage,yfinance`다.
Finnworlds Developer 멤버십에 맞춰 물리 요청을 rolling 60초당 최대 120회로 제한하고,
`companyratings`의 과금 배수 10을 별도 집계한다. 전체 미국 보통주 5,315종목은 재시도를
제외하면 약 45분, 과금 호출량 약 53,150회다. API 키는 `FINNWORLDS_API_KEY`
환경변수에서만 읽으며 Bronze·체크포인트·로그에는 저장하지 않는다.

Finnworlds 백필은 종목 단위로 자동 이어받는다. 기간·유니버스 해시·공급자·스키마
버전으로 실행 서명을 만들고 `data-lake/meta/consensus/finnworlds_backfill_*.json`에
진행 상태를 원자적으로 기록한다. 재시작할 때 체크포인트뿐 아니라 완료된 Bronze JSON의
무결성도 재검증하므로 정상 파일은 건너뛰고 손상 파일과 실패 종목만 다시 요청한다.
`--force`는 해당 실행의 캐시와 체크포인트를 무시하고 전 종목을 다시 받는다. rolling
rate-limit 상태는 별도 `finnworlds_rate_limit.json`에 보존된다. `429`는
`Retry-After`, 5xx와 네트워크 오류는 지수 백오프로 기본 3회 재시도한다.

FMP는 `analyst-estimates`의 annual/quarter 전체 페이지와 `price-target-summary`를
수집한다. rolling 60초 기본 720회(`--fmp-max-calls-per-minute`, 최대 750회)로 제한하며,
요청 시각은 `data-lake/meta/consensus/fmp_rate_limit.json`에 보존한다. 키 누락·무효·
만료 또는 인증/구독 권한 오류가 발생하면 FMP 호출을 중단하고 Alpha Vantage, yfinance
순으로 전환한다. `429`는 공급자를 바꾸지 않고 `Retry-After`와 백오프를 적용한다.

Alpha Vantage 요청은 `EARNINGS_ESTIMATES`, `EARNINGS`, `OVERVIEW`, `SPLITS` 엔드포인트를
사용하며 API 키 전체에서 rolling 60초 최대 75회로 제한된다. 재시도도 호출 한 건으로
차감하고, 제한 응답(`429`, `Note`, `Information`)은 최소 60초 후 지수 백오프로 재시도한다.
최근 요청 시각은 `data-lake/meta/consensus/alpha_vantage_rate_limit.json`에 보존된다.

```text
data-lake/bronze/consensus/finnworlds/company-ratings/snapshot_date=YYYY-MM-DD/ticker=AAPL.json
data-lake/bronze/consensus/fmp/analyst-estimates/period=annual/snapshot_date=YYYY-MM-DD/ticker=AAPL.json
data-lake/bronze/consensus/fmp/analyst-estimates/period=quarter/snapshot_date=YYYY-MM-DD/ticker=AAPL.json
data-lake/bronze/consensus/fmp/price-target-summary/snapshot_date=YYYY-MM-DD/ticker=AAPL.json
data-lake/bronze/consensus/alpha-vantage/earnings-estimates/snapshot_date=YYYY-MM-DD/ticker=AAPL.json
data-lake/bronze/consensus/alpha-vantage/earnings/snapshot_date=YYYY-MM-DD/ticker=AAPL.json
data-lake/bronze/consensus/alpha-vantage/splits/snapshot_date=YYYY-MM-DD/ticker=AAPL.json
data-lake/bronze/consensus/yahoo/snapshot_date=YYYY-MM-DD/ticker=AAPL.json

data-lake/silver/consensus/us/us_consensus_observations.csv
data-lake/silver/consensus/us/us_consensus_events.csv
data-lake/silver/consensus/us/us_consensus_factors.csv
data-lake/silver/consensus/us/us_target_price_ratings.csv
data-lake/silver/consensus/us/us_target_price_consensus.csv
```

Yahoo 스냅샷에는 `get_earnings_estimate()`, `get_revenue_estimate()`,
`get_eps_trend()`, `get_eps_revisions()`, `get_earnings_history()`,
`get_earnings_dates()`와 목표주가·추천등급 보조 데이터를 저장한다. `0q`, `+1q`,
`0y`, `+1y`는 각각 `FQ1`, `FQ2`, `FY1`, `FY2`로 표시용 정규화하되, Yahoo의
원본 슬롯도 보존한다. Alpha는 `period_type + fiscal_period_end`를 원본 기간 키로
유지한다. 두 공급자의 EPS 수준이나 리비전을 직접 빼지 않는다.

#### 미국 컨센서스 팩터 계산

US Consensus Score는 현재 운용 구간에서 FY1을 주 기준으로 계산하고 FQ1/FQ2/FY2
원시 값도 Silver에 보관한다. 동일한 최신 스냅샷에서는 팩터 필드별로 FMP, Alpha Vantage,
yfinance 순으로 유효 값을 선택한다. FMP가 제공하지 않는 revision breadth와 surprise는
yfinance로 보완한다. Alpha Vantage가 제공하는 역사 추정치는 대부분 분기이므로,
과거 백테스트 구간은 FQ1 `ALPHA_VANTAGE_HISTORICAL` 팩터를 사용한다. 이 값은 해당
`fiscal_period_end`의 `EARNINGS.reportedDate` 다음 미국 거래일부터 이용 가능한 이벤트
상대 PIT 프록시다. 연결된 실적발표일이 없는 미래 Alpha 추정치는 역사 팩터로 만들지
않으며, Alpha 원본의 `snapshot_date`는 실제 수집일로 보존한다. Yahoo 일별 값은
`YAHOO_CURRENT`의 FY1 엄밀 스냅샷 PIT이며, 최초 Yahoo FY1 스냅샷 이후에는 Yahoo가
Alpha 역사 FQ1을 대체한다. 모든 과거 EPS 빈티지는 관측일과 기준일 사이의 주식분할
`split_factor` 누적곱으로 나누어 분할 후 기준으로 맞춘다.

목표주가는 다른 컨센서스 필드와 분리해
`FINNWORLDS → FMP → ALPHA_VANTAGE → YAHOO_FINANCE` 순으로 선택한다. Finnworlds
공식 컨센서스 또는 애널리스트별 최신 목표가로 재구성한 `pit_120d` 상태가 120일 이내이고
유효 애널리스트가 3명 이상이면 하위 공급자가 더 최신이어도 Finnworlds를 사용한다.
공식값은 수집 스냅샷 다음 XNYS 거래일부터만 사용할 수 있고, 그 이전 구간은 개별 rating
기반 PIT 평균을 사용한다. 결과에는 `us_target_price`, `us_target_price_analyst_count`,
`us_target_price_provider`, `us_target_price_source_regime`을 남긴다.
`us_price_to_target_price`는 기존과 같이 `close / us_target_price`다.

```text
revision_30d_pct = 100 * (EPS_current - EPS_30d_ago) / max(abs(EPS_30d_ago), 0.1)

recent = (EPS_current - EPS_30d_ago) / max(abs(EPS_30d_ago), 0.1)
prior_monthly = ((EPS_30d_ago - EPS_90d_ago) / max(abs(EPS_90d_ago), 0.1)) / 2
revision_acceleration_30d_pct = recent - prior_monthly

revision_breadth_30d_pct = (up_30d - down_30d) / max(up_30d + down_30d, 1)
eps_dispersion_pct = (eps_high - eps_low) / max(abs(eps_average), 0.1)
revenue_dispersion_pct = (revenue_high - revenue_low) / max(abs(revenue_average), 1)
```

`up_30d`와 `down_30d`가 모두 0이면 breadth는 0이며, 둘 중 하나라도 없으면 결측이다.
EPS 및 매출 분산도는 낮을수록 좋게 순위를 반전한다. EPS 애널리스트 수가 3명 미만이면
US Consensus Score를 계산하지 않는다. 핵심 네 팩터(EPS 30일 리비전, breadth, 가속도,
EPS 분산도)는 모두 필요하고, 매출 분산도와 최근 120일 EPS 서프라이즈는 존재할 때만
가중치를 재정규화한다.

| US Consensus Score 구성 | 가중치 |
| --- | ---: |
| EPS 30일 리비전 | 35% |
| EPS 리비전 breadth | 20% |
| EPS 리비전 가속도 | 15% |
| EPS 컨센서스 분산도 역순위 | 10% |
| 매출 컨센서스 분산도 역순위 | 5% |
| 최근 EPS 서프라이즈 | 15% |

정규화는 존재하는 모든 Bronze 스냅샷을 읽으며, 누락된 날짜의 스냅샷을 인위적으로
만들지는 않는다. US 팩터 로딩은 거래일 이전의 가장 최근 FY1 스냅샷을 `as-of`로
참조한다. 현재 구현에는 최대 유효기간이 없으므로 장기간 수집이 중단되면 마지막 값이
계속 사용될 수 있다. 운영 환경에서는 수집 상태를 모니터링하고 필요하면 stale 정책을
추가해야 한다.

US 원시 팩터를 ClickHouse의 일반 factor 테이블에 적재하려면 다음처럼 실행한다.

```powershell
python -m engine.loaders.factors --market us --stock-codes AAPL,MSFT --financial-basis annual `
  --factor-ids us_eps_revision_30d_pct,us_eps_revision_breadth_30d_pct,us_eps_revision_acceleration_30d_pct,us_eps_dispersion_pct,us_revenue_dispersion_pct,us_eps_surprise_pct,eps_implied_operating_income_surprise_pct
```

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

`--market us statements`는 SEC companyfacts JSON 파일을
`data-lake/bronze/sec/companyfacts/`에 다운로드합니다. 일부 종목만 받으려면
`--symbols AAPL,MSFT`를 사용하고, SEC 티커 맵의 전체 종목을 받으려면 `--symbols`를
생략합니다.

미국 가격 다운로드는 NasdaqTrader 심볼 디렉터리에서 종목 유니버스를 가져오고,
yfinance의 `period=max` 이력 파일을
`data-lake/bronze/yfinance/price/{TICKER}.csv`에 저장합니다. Windows에서는 `CON`처럼
장치 이름과 같은 티커에 내부 안전 접두사를 붙여 저장하고 정규화할 때 원래 티커로
복원합니다. 각 요청의 응답 제한 시간은 15초이며 지수 백오프로 두 번 재시도합니다.
Yahoo 가격 복구 처리가 필요한 경우에만 `--yfinance-repair`를 사용합니다. 필요한 경우
먼저 `pip install yfinance`로 yfinance를 설치합니다.

WACC 입력 다운로드:

```powershell
python -m engine.workflows.download --market kr wacc-inputs
python -m engine.workflows.download --market us wacc-inputs
```

`wacc-inputs`는 공통으로 사용하는 Damodaran NYU 국가별 ERP 통합 문서를
`data-lake/bronze/damodaran/country_risk_premiums/ctryprem.xlsx`에, FRED 금리를
`data-lake/bronze/fred/rates/`에 다운로드합니다. 미국 WACC의 경우 S&P 500 벤치마크도
`data-lake/bronze/yfinance/benchmark/us_sp500.csv`에 다운로드합니다. KR beta는
`data-lake/bronze/krx/`에 이미 저장된 KRX 가격 및 벤치마크 데이터를 사용하고, US beta는
기존 `data-lake/bronze/yfinance/price/{TICKER}.csv` 가격을 사용합니다. Koscom 데이터는
사용하지 않습니다.

#### K-Ratio / Equity Duration / RIM 원천 데이터

| 팩터 | 필수 원천 데이터 | 주요 입력 |
| --- | --- | --- |
| `k_ratio_3y` | KRX 조정주가 | 756거래일 가격 VAMI |
| `equity_duration_20y` | KRX 가격, KOSPI200, FRED, Damodaran ERP, Hankyung 컨센서스 | beta, 자기자본비용, FY1 forward P/E |
| `rim_upside_potential` | KR: KRX 가격·주식수, DART 연간 재무제표, FRED, Damodaran ERP, Hankyung 컨센서스 / US annual·TTM: SEC 재무제표, 가격·주식수, FRED, Damodaran ERP, FY1 EPS·영업이익 컨센서스 | BPS, 자기자본비용, 우선순위별 예상 ROE |
| `eps_implied_operating_income_surprise_pct` | US FY1 EPS 컨센서스, SEC 재무제표 | EPS에서 유도한 FY1 영업이익의 최근 공시 영업이익 대비 변화율 |

Hankyung 토큰은 명령행에 직접 기록하지 않고 환경 변수로 전달합니다.

```powershell
$env:HANKYUNG_CONSENSUS_TOKEN = "<YOUR_HANKYUNG_TOKEN>"

python -m engine.workflows.download --market kr prices
python -m engine.workflows.download --market kr shares
python -m engine.workflows.download --market kr statements
python -m engine.workflows.download --market kr benchmarks
python -m engine.workflows.download --market kr wacc-inputs
python -m engine.workflows.download --market kr consensus
```

K-Ratio가 사용하는 KRX OHLCV는 `adjusted=True`로 요청합니다. Equity Duration과
RIM에 사용되는 `forward_per`, `forward_roe`는 Hankyung의
`STOCK_PRE_PER`, `STOCK_PRE_ROE`에서 생성됩니다. ValueFinder/EQUITY HTML
목록은 현재 투자의견 자료이며 이 두 forward metric의 대체 입력으로 사용하지 않습니다.
단, KR RIM은 forward ROE가 없거나 180일 만료된 거래일에 한해 공시가 완료된 최근
3개 연속 연간 실적 ROE의 평균을 fallback으로 사용합니다. US RIM은 FY1 실제 애널리스트
영업이익 컨센서스가 들어오면 이를 우선 사용하고, 없으면 EPS 컨센서스와 최신 공시
영업이익/순이익 관계로 유도한 영업이익 surprise를 사용합니다. 둘 다 없으면 US annual은
최근 3개 연속 연간 ROE 평균, US TTM은 최근 12개 연속 분기(3년) TTM ROE 평균을 사용합니다.

### 2. 변환 / 정규화

```powershell
python -m engine.workflows.normalize
python -m engine.workflows.normalize --market kr
python -m engine.workflows.normalize --market kr --target statements --start-year 2021 --end-year 2026
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
KR `statements`와 `business-info`에서 `--start-year`, `--end-year`는 파일명의 연도를 기준으로 포함 범위를 지정합니다. KR statements는 기간별 스냅샷을 유지하고, 종목별 통합 CSV를 모든 기존 스냅샷에서 다시 생성하므로 서로 다른 기간을 나눠 실행해도 기존 결과가 사라지지 않습니다.
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

business-info silver CSV에서 P/Q/C/ASP 기반 운영지표와 FY1 추정치를 만들 수 있습니다.
입력은 종목별 `kr_business_info_tables.csv`, `kr_business_info_rows.csv`이며, 변환기는 먼저
gold CSV를 만든 뒤 loader가 이 gold CSV만 읽어 ClickHouse에 적재합니다. 운영지표 룰과
제품 alias seed는 아래 파일에서 관리합니다.

```text
data-lake/meta/rules/operating_metric_rules.yaml
data-lake/meta/rules/product_alias_rules.yaml
```

gold 산출물:

```text
data-lake/gold/operating-metrics/{stock_code}/business_operating_metric_raw.csv
data-lake/gold/operating-metrics/{stock_code}/business_operating_metric.csv
data-lake/gold/operating-metrics/{stock_code}/business_unit_economics.csv
data-lake/gold/operating-metrics/{stock_code}/business_unit_economics_driver.csv
data-lake/gold/estimates/{stock_code}/arcana_estimate_component.csv
data-lake/gold/estimates/{stock_code}/arcana_estimate_consensus.csv
```

US fallback 값은 선택 의존성인 edgartools 패키지를 사용합니다. 패키지가
설치되지 않았거나 SEC 조회가 실패하면 edgartools fallback만 건너뛰고
companyfacts/Notes 기반 정규화는 계속 진행합니다. US 가격과 주식수 다운로드는
이 workflow에 포함되지 않으므로, 팩터 계산에는 별도 silver price/share 파일이
필요합니다.

KRX price/shares, dividend, benchmark 정규화는 아래 경로를 사용합니다.

가격/주식수 silver CSV만 정규화하여 갱신:

```powershell
python -c "from engine.transformers.market_data import normalize_price, normalize_shares; normalize_price(r'data-lake\bronze\krx\price\*'); normalize_shares(r'data-lake\bronze\krx\shares\*')"
```

배당 silver CSV만 정규화하여 갱신:

```powershell
python -c "from engine.loaders.dividends import refresh_silver_dividend_files; refresh_silver_dividend_files()"
```

미국 배당 bronze 다운로드 및 silver 갱신:

```powershell
$env:ALPHA_VANTAGE_API_KEY = "<YOUR_ALPHA_VANTAGE_KEY>"

# 기본값은 KR 배당 다운로드
python -m engine.workflows.download dividend

# 미국 보통주/ADR 배당 이력(필터링된 미국 심볼 전체)
python -m engine.workflows.download --market us dividend

# 선택한 미국 심볼만 처리
python -m engine.workflows.download --market us --symbols AAPL,MSFT dividend

# Bronze -> 미국 배당 이벤트/일별 CSV -> ClickHouse
python -m engine.workflows.normalize --market us --target dividend
python -m engine.loaders.dividends --market us
```

미국 배당 다운로드는 Alpha Vantage의 `DIVIDENDS`를 먼저 사용하며 각 공급자의 응답을
날짜별 bronze JSON 스냅샷으로 저장합니다. 원천 fallback 순서는 Alpha Vantage, 예약된
edgartools `999.Ex` 스텁, yfinance입니다. 우선순위가 더 높은 원천에 해당 티커의 유효한
이벤트가 없을 때만 다음 원천을 호출합니다. `ALPHA_VANTAGE_API_KEY`는 프로세스 환경
변수에서만 읽습니다.

```text
data-lake/bronze/dividend/alpha-vantage/snapshot_date=YYYY-MM-DD/ticker=AAPL.json
data-lake/bronze/dividend/yfinance/snapshot_date=YYYY-MM-DD/ticker=MSFT.json
```

산출물:

```text
data-lake/silver/us/dividend/us_dividend_events.csv
data-lake/silver/us/dividend/us_dividend_normalized.csv
```

`us_dividend_events.csv` 컬럼:

```text
ticker,cik,company_name,exchange,dividend_ex_date,dividend_declared_date,
dividend_record_date,dividend_payment_date,dividend_amount_per_share,source,
source_snapshot_date,sec_filing_date,source_form,
annual_dps,annual_eps,payout_ratio_dps_over_eps,
payout_ratio_total_dividends_over_net_income
```

`us_dividend_normalized.csv`는 기존 `stock_dividend` 적재 스키마를 유지하며
`trade_date=dividend_payment_date`, `dividend=dividend_amount_per_share`로 생성됩니다.
배당락일(`dividend_ex_date`), 공시일(`dividend_declared_date`), 기준일,
지급일을 이벤트 CSV에 별도 보존합니다. yfinance fallback은 배당락일과 금액만
보존하므로 지급일이 없는 행은 이벤트 CSV에는 남지만 daily/ClickHouse 행으로는
변환하지 않습니다.

벤치마크 silver CSV만 정규화하여 갱신:

```powershell
python -c "from engine.loaders.benchmarks import normalize_downloaded_benchmark_prices; normalize_downloaded_benchmark_prices(r'data-lake\bronze\krx\benchmark\*.csv')"
python -c "from engine.loaders.benchmarks import normalize_downloaded_benchmark_prices; normalize_downloaded_benchmark_prices(market='us')"
```

WACC silver 입력 CSV 정규화:

```powershell
python -c "from pathlib import Path; from engine.transformers.erp import normalize_country_erp, normalize_fred_risk_free_rates; normalize_country_erp(); paths=list(Path(r'data-lake\bronze\fred\rates').glob('*.csv')); normalize_fred_risk_free_rates(paths) if paths else None"
python -c "from engine.transformers.wacc import create_default_wacc_assumptions; create_default_wacc_assumptions()"
python -c "from engine.transformers.wacc import normalize_market_benchmark_weekly_returns; normalize_market_benchmark_weekly_returns('kr')"
python -c "from engine.transformers.wacc import normalize_market_benchmark_weekly_returns; normalize_market_benchmark_weekly_returns('us')"
```

세 신규 팩터에 필요한 KR silver와 ClickHouse 입력을 순서대로 갱신하는 예시는
다음과 같습니다.

```powershell
python -m engine.loaders.market_data --market kr --target all --source bronze --dry-run
python -m engine.loaders.market_data --market kr --target all --source bronze
python -m engine.workflows.normalize --market kr --target statements
python -m engine.workflows.normalize --market kr --target consensus --consensus-stale-days 180
python -m engine.loaders.consensus --market kr --dry-run
python -m engine.loaders.consensus --market kr
python -c "from engine.loaders.benchmarks import normalize_downloaded_benchmark_prices; normalize_downloaded_benchmark_prices(market='kr')"
python -c "from pathlib import Path; from engine.transformers.erp import normalize_country_erp, normalize_fred_risk_free_rates; normalize_country_erp(); normalize_fred_risk_free_rates(sorted(Path(r'data-lake\bronze\fred\rates').glob('*.csv')))"
python -c "from engine.transformers.wacc import create_default_wacc_assumptions, normalize_market_benchmark_weekly_returns; create_default_wacc_assumptions(); normalize_market_benchmark_weekly_returns('kr')"
```

`benchmark_weekly_returns.csv`를 KR용으로 갱신할 때 기존 US 행은 유지됩니다.
KR beta는 KOSPI200을 우선 사용하고, 52개 이상의 겹치는 주간 수익률이 없을 때만
`wacc_assumptions.csv`의 기본 beta로 대체됩니다.

WACC silver 산출물은 `data-lake/silver/wacc/` 아래에 저장됩니다.

```text
risk_free_rates.csv
country_equity_risk_premiums.csv
weekly_returns.csv
benchmark_weekly_returns.csv
wacc_assumptions.csv
```

Damodaran ERP를 우선 사용합니다. Damodaran 통합 문서를 읽을 수 없으면 KR ERP는 KOSPI의
2년 연환산 기대수익률에서 최신 한국 국채 금리를 뺀 값으로 대체합니다. 주간 beta는 금요일을
주간 마감일로 한 수익률을 사용하며, `adj_close`가 있으면 이를 사용하고 없으면 `close`를 사용합니다.
서로 겹치는 주간 수익률이 52개 미만이면 WACC는 `wacc_assumptions.csv`의 시장 기본
beta를 사용합니다.

아래 loader 명령들은 정규화된 silver 파일을 갱신한 뒤 ClickHouse 적재까지
이어 수행합니다.

### 3. 시장 데이터 적재

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

### 4. 공시 / 증권 / 배당 적재

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

### 5. 운영 지표 및 추정치 생성 / 적재

business-info silver CSV를 기반으로 제품/부문별 매출, 수량, 가격, ASP, 단위 원가,
driver YoY, FY1 추정치와 consensus gold CSV를 생성합니다.

gold CSV만 생성:

```powershell
python -m engine.transformers.operating_metrics --stock-codes 005930
python -m engine.workflows.operating_metrics --stock-codes 005930
python -m engine.transformers.operating_metrics --all-periods
python -m engine.transformers.operating_metrics --start-period 2023.12 --end-period 2026.03
python -m engine.workflows.operating_metrics --stock-codes 005930 --as-of-date 2026-06-28 --write-history
```

gold CSV를 ClickHouse에 적재:

```powershell
python -m engine.loaders.operating_metrics --stock-codes 005930 --dry-run
python -m engine.loaders.operating_metrics --stock-codes 005930
python -m engine.workflows.operating_metrics --stock-codes 005930 --load --dry-run
python -m engine.workflows.operating_metrics --stock-codes 005930 --load
python -m engine.workflows.operating_metrics --all-periods --load --dry-run
python -m engine.workflows.operating_metrics --start-period 2023.12 --end-period 2026.03 --load
python -m engine.workflows.operating_metrics --stock-codes 005930 --as-of-date 2026-06-28 --write-history --load --load-history --dry-run
python -m engine.loaders.operating_metrics --load-history --as-of-date 2026-06-28
```

`--dry-run`은 gold CSV row count와 schema 준비만 확인하고 ClickHouse insert는 수행하지
않습니다. `--stock-codes`를 생략하면 `data-lake/silver/dart/business-info/` 또는
`data-lake/gold/operating-metrics/` 아래의 종목 디렉터리를 기준으로 처리합니다.
transformer/workflow/loader는 기본적으로 stock 단위 진행상황을 출력합니다. `--progress-interval 100`으로
출력 간격을 조정하거나 `--no-progress`로 끌 수 있고, 특정 종목 실패 시 즉시 중단하려면
`--fail-fast`를 사용합니다.

추정치는 기본적으로 각 종목의 최신 source actual period만 계산합니다. 과거 기간까지 함께
계산하려면 `--all-periods`를 사용합니다. 기간을 제한하려면 `--start-period YYYY.MM`,
`--end-period YYYY.MM`을 지정합니다. 예를 들어 source actual period `2023.12`를 기준으로
계산한 추정치는 target period `2024.12`로 저장됩니다.

추정 로직은 현재 MVP 기준입니다. `Revenue FY1`은 P/Q/C driver 모델의 제품별 forecast를
회사 단위로 집계합니다. Q는 `quantity_sold`, `shipment_volume`, `quantity_produced`
순서로 사용하고, ASP는 reported ASP가 있으면 우선 사용하며 없으면 `revenue / quantity`
로 계산합니다. 유사 컨센서스는 `P/Q/C Driver`, `Historical Trend`, `Industry Peer` 내부 모델을
metric별로 집계해 median/low/high/dispersion을 계산합니다. C와 gross profit은 공시 원가 단서가
있을 때만 계산하고, 없으면 비워둡니다.

normalized 재무제표 CSV가 있으면 유사 컨센서스에 `operating_income`, `net_income`,
`net_income_parent`, `basic_eps`, `diluted_eps`도 함께 추가합니다. 기본 입력 경로는
`data-lake/silver/dart/normalized/kr_normalized_{stock_code}.csv`이며, 다른 경로를 쓰려면
`--normalized-statement-dir`로 지정합니다. 영업이익/순이익은 과거 YoY trend와 P/Q/C 매출
forecast 기반 margin bridge를 사용하고, EPS는 순이익 bridge 또는 성장률 fallback으로 계산합니다.

유사 컨센서스의 `as_of_date`는 기본적으로 `data-lake/silver/dart/kr_report_metadata.csv`의
`report_date`를 사용합니다. 즉 실행일이 아니라 해당 실적 source actual period의 DART 공시일입니다.
metadata가 없을 때만 `--as-of-date` 또는 실행일을 fallback으로 사용합니다.

유사 컨센서스 히스토리가 필요하면 `--write-history`를 사용합니다. 최신
`arcana_estimate_consensus.csv`는 기존처럼 갱신하고, 추가로
`data-lake/gold/estimates/{stock_code}/history/arcana_estimate_consensus_{as_of_date}.csv`
를 저장합니다. ClickHouse에 히스토리를 적재하려면 workflow 또는 loader에 `--load-history`를
함께 지정합니다. loader는 기본적으로 CSV 내부의 `as_of_date`를 보존합니다. 이미 생성된 CSV를
재생성하지 않고 적재 기준일만 강제로 맞추려면 loader에 `--as-of-date YYYY-MM-DD`를 지정합니다.

API는 ClickHouse를 먼저 조회하고 실패하거나 데이터가 없으면 gold CSV로 fallback합니다.

```text
GET /api/operating-metrics/{stock_code}
GET /api/operating-metrics/{stock_code}/unit-economics
GET /api/operating-metrics/{stock_code}/drivers
GET /api/estimates/{stock_code}
GET /api/estimates/{stock_code}/consensus
GET /api/estimates/{stock_code}/consensus/history
GET /api/estimates/{stock_code}/drivers
```

### 6. 팩터 적재

시험 실행:

```powershell
python -m engine.loaders.factors --financial-basis annual --dry-run
python -m engine.loaders.factors --market us --stock-codes AAPL --financial-basis annual --dry-run
```

적재:

```powershell
python -m engine.loaders.factors --financial-basis annual --start-date 2026-01-01 --end-date 2026-05-24
python -m engine.loaders.factors --market us --stock-codes AAPL --financial-basis annual --start-date 2026-01-01 --end-date 2026-05-24
```

WACC 입력을 포함한 팩터 적재:

```powershell
python -m engine.loaders.factors --market kr --financial-basis annual --start-date 2026-01-01 --end-date 2026-05-24 --dry-run
python -m engine.loaders.factors --market kr --financial-basis annual --start-date 2026-01-01 --end-date 2026-05-24
python -m engine.loaders.factors --market us --stock-codes AAPL,MSFT --financial-basis annual --start-date 2026-01-01 --end-date 2026-05-24 --dry-run
python -m engine.loaders.factors --market us --stock-codes AAPL,MSFT --financial-basis annual --start-date 2026-01-01 --end-date 2026-05-24
```

로컬 bronze/silver WACC 입력이 없거나 오래된 경우 온라인 WACC 백필을 활성화할 수
있습니다.

```powershell
python -m engine.loaders.factors --market kr --financial-basis annual --start-date 2026-01-01 --end-date 2026-05-24 --wacc-online-backfill --dry-run
python -m engine.loaders.factors --market us --stock-codes AAPL --financial-basis annual --start-date 2026-01-01 --end-date 2026-05-24 --wacc-online-backfill --dry-run
```

`--wacc-online-backfill`을 사용하지 않으면 로더는 로컬 bronze/silver 파일만 읽습니다.
이 옵션을 사용하면 행을 준비하기 전에 Damodaran ERP, FRED 금리, 기본 WACC 가정 및
사용 가능한 시장 벤치마크 주간 수익률을 갱신합니다. KR 벤치마크 bronze 파일은
`engine.workflows.download --market kr benchmarks`로 별도 다운로드해야 합니다. 준비된
WACC 팩터 행은 다른 일별 팩터와 함께 기존 ClickHouse `fact_daily_factors` 테이블에
삽입됩니다.

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
--factor-ids roe,per,pbr
--only-factor-ids roe,per,pbr
--price-path data-lake\silver\us\price\us_normalized_price.csv
--wacc-assumptions-path data-lake\silver\wacc\wacc_assumptions.csv
--wacc-risk-free-path data-lake\silver\wacc\risk_free_rates.csv
--wacc-erp-path data-lake\silver\wacc\country_equity_risk_premiums.csv
--wacc-benchmark-path data-lake\silver\wacc\benchmark_weekly_returns.csv
--wacc-online-backfill
--rim-decay-factor 0.8
```

선택한 팩터 ID만 적재:

```powershell
python -m engine.loaders.factors --market kr --financial-basis annual --start-date 2020-01-01 --end-date 2026-06-26 --factor-ids roe,per,pbr
python -m engine.loaders.factors --market kr --financial-basis annual --start-date 2020-01-01 --end-date 2026-06-26 --only-factor-ids wacc,beta,cost_of_equity
python -m engine.loaders.factors --market kr --financial-basis annual --start-date 2020-01-01 --end-date 2026-06-26 --wacc-online-backfill --factor-ids wacc_bundle
```

`--factor-ids`와 `--only-factor-ids`는 별칭입니다. 쉼표로 구분한 팩터 ID를 입력받으며,
해당 ID만 ClickHouse 적재 대상으로 준비합니다. 알 수 없는 팩터 ID가 있으면 빈 적재를
생성하지 않고 즉시 실패합니다. `wacc_bundle`은 WACC 관련 모든 팩터 ID로 확장됩니다.

#### K-Ratio / Equity Duration / RIM 팩터 적재

```powershell
$AdvancedFactorIds = "k_ratio_3y,equity_duration_20y,rim_upside_potential"

# 실제 forward P/E·ROE 이력이 시작되는 구간을 먼저 검증합니다.
python -m engine.loaders.factors --market kr --financial-basis annual --start-date 2021-11-05 --end-date 2026-07-24 --factor-ids $AdvancedFactorIds --rim-decay-factor 0.8 --dry-run

# raw factor와 factor catalog를 ClickHouse에 적재합니다.
python -m engine.loaders.factors --market kr --financial-basis annual --start-date 2021-11-05 --end-date 2026-07-24 --factor-ids $AdvancedFactorIds --rim-decay-factor 0.8

# K-Ratio만 더 오래된 가격 이력까지 별도로 backfill할 수 있습니다.
python -m engine.loaders.factors --market kr --financial-basis annual --start-date 2010-01-01 --end-date 2021-11-04 --factor-ids k_ratio_3y
```

계산 정의와 결측 규칙:

- `k_ratio_3y`: 756거래일 `log(VAMI)` OLS 기울기를
  `n × SE(slope)`로 나눕니다. 최소 504개 유효 관측치가 필요합니다.
- `equity_duration_20y`: FY1 forward P/E와 CAPM 자기자본비용으로 계산한
  20년 modified duration이며 단위는 `years`입니다.
- `rim_upside_potential`: `RIM Target / Price - 1`인 비율 값입니다.
  기본 ROE 유지율은 0.8이며 KR은 forward ROE를 우선 사용합니다.
- forward 컨센서스는 거래일 당시 이용 가능했던 가장 가까운 미래 연간 결산기를
  선택하며 최종 관측 후 180일이 지나면 결측 처리합니다.
- forward P/E는 현재 PER로 대체하지 않습니다. KR RIM의 forward ROE만 결측 시 거래일 당시
  공시된 최근 3개 연속 연간 실적 ROE 평균으로 대체합니다. US RIM은 `us_operating_income_consensus`
  (향후 실제 애널리스트 영업이익 컨센서스) → `eps_implied_operating_income_surprise_pct` →
  해당 basis의 3년 실적 ROE 순으로 사용합니다. EPS-implied 값은 FY1·분석가 3명 이상인
  컨센서스에만 생성합니다. 해당 기간에 ROE가 없거나 보고 분기가 연속되지 않으면 RIM도 결측입니다.
- `equity_duration_20y`는 현재 KR만 지원합니다. `rim_upside_potential`은 KR annual과
  US annual·TTM을 지원합니다. 미국 실제 영업이익 컨센서스가 아직 없을 때에는
  EPS-implied 영업이익 surprise를 우선 적용합니다.
- `--rim-decay-factor`는 `0 <= value < 1`이어야 합니다. 값을 변경하면 동일
  factor_id의 전 기간 raw factor와 snapshot을 다시 생성해야 합니다.

스크리닝과 백테스트에 사용하는 고속 팩터 스냅샷 테이블 생성:

```powershell
python -m engine.loaders.factor_snapshots --create-only
python -m engine.loaders.factor_snapshots --financial-basis annual --start-date 2020-01-01 --end-date 2026-07-10 --factor-ids roe,per,pbr
python -m engine.loaders.factor_snapshots --financial-basis ttm --start-date 2020-01-01 --end-date 2026-07-10
python -m engine.loaders.factor_snapshots --financial-basis annual --start-date 2026-03-01 --end-date 2026-03-31 --factor-ids roe,per,pbr --factor-chunk-size 16 --max-threads 2 --max-lookback-days 540
python -m engine.loaders.factor_snapshots --financial-basis annual --start-date 2021-11-05 --end-date 2026-07-24 --factor-ids k_ratio_3y,equity_duration_20y,rim_upside_potential
python -m engine.workflows.score_cli build-factor-scores --trade-date 2026-07-24 --factor-asof-mode asof --financial-basis annual --include-financials
```

운영 갱신 순서는 `consensus → factor_catalog/raw factors → factor snapshot →
factor score`입니다. 신규 팩터는 generic screening/backtest API에서 즉시 사용할
수 있지만 기존 style 종합점수 구성에는 자동으로 추가되지 않습니다.

배당 Silver, 가격, `dividend_yield`, `payout_ratio`, 스크리닝 snapshot을 한 번에
갱신하려면 Windows PowerShell 워크플로를 사용합니다. KRX 최신 가격 다운로드를
생략하고 기존 Bronze부터 다시 만들려면 `-SkipPriceDownload`를 지정합니다.

```powershell
.\scripts\refresh_dividend_factors.ps1
.\scripts\refresh_dividend_factors.ps1 -AsOfDate 2026-07-15 -SkipPriceDownload
.\scripts\refresh_dividend_factors.ps1 -PriceWorkers 16 -SkipTests
```

`-AsOfDate`를 생략하면 최근 가격 단면 중 활성 종목 수가 최대 단면의 99% 이상인
가장 최신 거래일을 자동 선택합니다. 이 기준으로 일부 종목만 수집된 당일 가격이
스크리닝 팩터 기준일로 사용되는 것을 방지합니다.

요청한 팩터 ID와 재무 기준에 해당하는 행이 `fact_daily_factor_snapshot`에 있으면 API가
이 테이블을 자동으로 사용합니다. 스냅샷 테이블이 없거나 요청에 해당하는 행이 비어 있으면
`fact_daily_factors`로 대체합니다. 스냅샷 로더는 기본적으로 값을 이월한 일별 행을 증분
생성합니다. 첫 날짜는 raw 이력에서 초기화하고, 이후 각 날짜는 이전 스냅샷을 복사한 뒤
그 날짜에 raw 행이 있는 키를 덮어씁니다. 모든 스냅샷 날짜를 raw 이력에서 직접 다시
만들어야 할 때만 `--full-asof`를 사용하고, raw 팩터 행을 그대로 복사하려는 경우에만
`--copy-raw-only`를 사용합니다.

팩터 스크리닝은 최근 14일 안에서 준비된 스냅샷을 찾아 해당 날짜를 정확히 조회합니다.
시장의 장기 휴장이나 데이터 지연을 고려해 후보 기간을 늘려야 하면
`ARCANA_FACTOR_SNAPSHOT_CANDIDATE_DAYS`로 변경합니다. raw 테이블 fallback 조회는
기본적으로 최근 540일로 제한됩니다. 이 기간은 `ARCANA_FACTOR_RAW_LOOKBACK_DAYS`로
변경하며, fallback 날짜 제한을 비활성화하려면 `0`으로 설정합니다.

ClickHouse에 적재되는 WACC 팩터 ID:

```text
wacc
cost_of_equity
cost_of_debt_pre_tax
cost_of_debt_after_tax
wacc_equity_weight
wacc_debt_weight
beta
```

US 팩터 적재는 `data-lake/silver/sec/normalized/` 재무 CSV와
`data-lake/silver/us/price/us_normalized_price.csv`가 있을 때 daily factor
rows를 생성합니다. `data-lake/silver/us/shares/us_normalized_shares.csv`가
있으면 shares/market cap 기반 팩터까지 함께 계산합니다.

### 7. 벤치마크 적재

```powershell
python -m engine.loaders.benchmarks --benchmark-ids KOSPI200,KOSDAQ --start-date 2010-01-01 --dry-run
python -m engine.loaders.benchmarks --benchmark-ids KOSPI200,KOSDAQ --start-date 2010-01-01
python -m engine.loaders.benchmarks --market us --benchmark-ids S&P500,NASDAQ --start-date 2010-01-01 --dry-run
python -m engine.loaders.benchmarks --market us --benchmark-ids S&P500,NASDAQ --start-date 2010-01-01
```

`--market us`의 기본 공급자는 yfinance입니다. `S&P500`과 `NASDAQ`은 각각
`US_SP500`(`^GSPC`)과 `US_NASDAQ`(`^IXIC`)으로 정규화됩니다.

Bronze CSV에서 읽을 때:

```powershell
python -m engine.loaders.benchmarks --source bronze --bronze-path data-lake\bronze\krx\benchmark\*.csv --dry-run
python -m engine.loaders.benchmarks --market us --source bronze --dry-run
```

### 8. 스타일 점수 생성

```powershell
python -m engine.workflows.score_cli build-factor-scores --trade-date 2026-06-23 --factor-asof-mode asof --include-financials
python -m engine.workflows.score_cli build-style-scores --trade-date 2026-06-23 --style-profile DEFAULT
python -m engine.workflows.score_cli build-style-scores --start-date 2026-05-01 --end-date 2026-06-23 --skip-existing
python -m engine.workflows.score_cli validate-style-scores --trade-date 2026-06-23
python -m engine.workflows.score_cli debug-single-security-score --trade-date 2026-06-23 --security-id SEC_KR_005930
```

### 9. KR 매핑 및 팩터 커버리지 디버깅

KR 재무제표 정규화는 기본적으로 같은 위치에 `*.debug.csv` 파일을 생성합니다. 산출물을
정규화 검증기에 사용할 때는 `--no-debug`를 지정하지 마십시오. KR 재무제표 worker 수는
`NORMALIZE_MAX_WORKERS`로 설정합니다.

```powershell
$env:NORMALIZE_MAX_WORKERS = "4"
python -m engine.workflows.normalize --market kr --target statements
```

정규화된 재무제표와 디버그 산출물은 기간별 스냅샷 디렉터리와 종목별 통합 디렉터리에
모두 저장됩니다.

```text
data-lake\silver\dart\normalized-snapshots\kr_normalized_005930_2025.12.csv
data-lake\silver\dart\normalized-snapshots\kr_normalized_005930_2025.12.debug.csv
data-lake\silver\dart\normalized\kr_normalized_005930.csv
data-lake\silver\dart\normalized\kr_normalized_005930.debug.csv
```

현재 KR `statements` workflow는 KOSPI/KOSDAQ 전체 종목을 정규화합니다.
`--start-year`와 `--end-year`는 양 끝 회계연도를 포함하는 범위로 제한하며, 생략하면
내장된 최근 결산연도 범위를 유지합니다. 이 KR 재무제표 경로에는 `--symbols`와
`--workers`가 적용되지 않으므로 프로세스 수는 `NORMALIZE_MAX_WORKERS`로 설정합니다.
아래 검증 명령은 선택한 종목과 연도로 제한할 수 있습니다.

단일 종목-기간 쌍을 검증하고 상세 JSON 및 Markdown 보고서 생성:

```powershell
python -m engine.normalization_validator one `
  --normalized data-lake\silver\dart\normalized-snapshots\kr_normalized_005930_2025.12.csv `
  --debug data-lake\silver\dart\normalized-snapshots\kr_normalized_005930_2025.12.debug.csv `
  --rules data-lake\meta\rules\common_validation.yaml `
  --out-dir data-lake\silver\dart\validation\005930_2025 `
  --zai-mode none
```

한 종목의 여러 연도를 검증합니다. 연도별 검증 보고서와 종목 단위 보고서, 연도별 매핑
및 팩터 커버리지 진단이 담긴 `*.stock.factor_trend.csv` 파일을 생성합니다.

```powershell
python -m engine.normalization_validator stock `
  --market kr `
  --stock-code 005930 `
  --input-dir data-lake\silver\dart\normalized-snapshots `
  --rules data-lake\meta\rules\common_validation.yaml `
  --out-dir data-lake\silver\dart\validation_by_stock `
  --start-year 2020 `
  --end-year 2025 `
  --zai-mode none
```

선택한 종목을 병렬로 검증합니다. `--stock-codes` 값은 공백으로 구분합니다. KR 전체
종목을 처리하려면 `--stock-codes`를 생략하고, 실행을 재시작할 때 완료된 종목 보고서를
건너뛰려면 `--resume`을 추가합니다.

```powershell
python -m engine.normalization_validator stock-batch `
  --market kr `
  --input-dir data-lake\silver\dart\normalized-snapshots `
  --rules data-lake\meta\rules\common_validation.yaml `
  --out-dir data-lake\silver\dart\validation_by_stock `
  --start-year 2020 `
  --end-year 2025 `
  --stock-codes 005930 000660 035420 `
  --workers 4 `
  --summary-every 3 `
  --report-mode full `
  --zai-mode none
```

일괄 실행 후 반복되는 검증 실패와 `UNMAPPED` 후보를 클러스터링:

```powershell
python -m engine.normalization_validator cluster-failures `
  --input-dir data-lake\silver\dart\validation_by_stock `
  --out-dir data-lake\silver\dart\validation_clusters
```

이 명령은 `all_validation_failure_cases.csv`와
`all_validation_failure_clusters.csv`를 생성합니다.

KR 전체 종목의 팩터 커버리지를 계산합니다. 기준일을 재현 가능하게 유지하려면
`FACTOR_COVERAGE_TODAY`를 설정하고, Asia/Seoul의 현재 날짜를 사용하려면 생략합니다.

```powershell
$env:FACTOR_COVERAGE_TODAY = "2026-07-18"
node scripts\calculate_factor_coverage.js 2>&1 |
  Tee-Object -FilePath data-lake\gold\factor_coverage\kr_factor_coverage_run.log
```

이 명령은 `kr_factor_coverage_all_stocks.csv`, `factor_coverage_summary.json` 및 위에 표시된
선택적 실행 로그를 생성합니다. 커버리지는 유한한 숫자 값만 집계하며 0도 값이 있는 것으로
간주합니다.

EBITDA와 EV/EBITDA를 집중적으로 디버깅하려면 소수의 심볼로 시작하고, 필요하면 이전
팩터 커버리지 CSV와 비교합니다.

```powershell
python scripts\ebitda_ev_coverage.py `
  --market kr `
  --symbols 005930,000660,035420 `
  --financial-basis annual `
  --start-date 2020-01-01 `
  --end-date 2026-07-18 `
  --baseline-csv data-lake\gold\kr_coverage\kr_factor_coverage.csv `
  --out-dir data-lake\gold\factor_coverage\ebitda_debug
```

지원되는 KR 전체 종목을 처리하려면 `--symbols`를 제거합니다. 집중 실행은
`kr_ebitda_ev_coverage.csv`와 `kr_ebitda_ev_coverage_summary.json`을 생성합니다.

권장 디버깅 순서:

```text
재무제표 정규화
  -> normalization_validator stock 또는 stock-batch
  -> normalization_validator cluster-failures
  -> calculate_factor_coverage.js
  -> ebitda_ev_coverage.py
```

과거 `data-lake/gold/kr_coverage/kr_canonical_account_coverage.*` 산출물에는 저장소에
커밋된 전용 KR 생성기가 없습니다. 지원되는 재현 가능한 매핑 커버리지 진단으로는
stock-batch 요약, 종목 단위 검증 보고서, 팩터 추세 CSV 및 클러스터링된 실패를
사용합니다.

## 테스트

US 매핑 커버리지 검증기:

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
