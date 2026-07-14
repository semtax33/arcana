# Arcana API 명세

이 문서는 `api` 폴더의 현재 구현을 기준으로 작성한 REST API 및 MCP 제공 명세입니다. README와 별도로 API 사용자가 바로 참조할 수 있도록 엔드포인트, 주요 입력, 응답 구조, MCP tool 매핑을 함께 정리합니다.

## 1. 개요

- REST 앱: `api.main:app`
- 프레임워크: FastAPI
- API 제목: `Arcana API`
- OpenAPI 문서: 서버 실행 후 `/docs`, `/openapi.json`
- 헬스체크: `GET /health`
- MCP HTTP transport: `GET /`, `POST /`
- MCP stdio 서버: `python -m api.mcp`

`api/main.py`는 REST 컨트롤러를 FastAPI 앱에 등록하고, 루트 경로(`/`)를 MCP JSON-RPC transport로 사용합니다. `api/mcp.py`는 FastAPI 라우트 중 `include_in_schema=True`인 REST 엔드포인트를 MCP tool로 자동 변환합니다.

## 2. 공통 규칙

### 2.1 응답 형식

- REST 응답은 JSON입니다.
- MCP `tools/call` 응답은 MCP content 배열의 text 항목에 REST 결과 JSON 문자열을 담습니다.
- 날짜는 ISO-8601 문자열(`YYYY-MM-DD`)을 사용합니다.
- 실패 응답은 FastAPI 표준 오류 형태인 `{"detail": "..."}`를 사용합니다.

### 2.2 주요 HTTP 오류

| 상태 코드 | 의미 |
| --- | --- |
| `400` | 잘못된 입력, 지원하지 않는 값, 검증 실패 |
| `404` | 종목/전략/재무 데이터 등 조회 대상 없음 |
| `422` | FastAPI/Pydantic 요청 스키마 검증 실패 |
| `500` | 서비스 처리 중 예외 |

### 2.3 공통 열거값

| 이름 | 값 |
| --- | --- |
| `ChartRange` | `1M`, `3M`, `6M`, `1Y`, `5Y`, `MAX` |
| `FinancialStatementPeriod` | `annual`, `quarter`, `ttm` |
| `FinancialStatementFilter` | `all`, `IS`, `BS`, `CF` |
| `StyleProfile` | `DEFAULT`, `MINERVINI_ZWEIG`, `DIVIDEND_QUALITY` |
| `ConditionMode` | `top_percent`, `threshold` |
| `MatchMode` | `all`, `any` |
| `RankDirection` | `catalog`, `higher`, `lower` |
| `PercentileSide` | `top`, `bottom` |
| `RebalanceFrequency` | `monthly`, `quarterly`, `semiannual`, `annual` |
| `SectorLeaderSortBy` | `strong_stock_ratio`, `eps_expected_growth`, `return_1d`, `return_1w`, `roe`, `per`, `pbr` |
| `SectorLeaderLevel` | `sector`, `industry_group` |
| `SortDirection` | `asc`, `desc` |
| `MultipleValuationBandBasis` | `blend`, `historical`, `industry`, `market`, `listing_market` |

## 3. REST API 명세

### 3.1 시스템

| 메서드 | 경로 | 설명 | 응답 |
| --- | --- | --- | --- |
| `GET` | `/health` | 서버 상태 확인 | `{"status": "ok"}` |

### 3.2 차트

#### `GET /api/chart/{stock_code}`

종목의 가격 차트, 이동평균, 최근 기술 지표 행을 조회합니다.

| 구분 | 이름 | 타입 | 필수 | 기본값 | 설명 |
| --- | --- | --- | --- | --- | --- |
| path | `stock_code` | string | 예 | - | 조회할 종목 코드 |
| query | `range` | `ChartRange` | 아니오 | `1Y` | 차트 기간 |

주요 응답 필드:

- `stock`: 종목 메타데이터(`stock_code`, `security_id`, `stock_name`, `country`, `currency`)
- `range`, `from_date`, `to_date`
- `chart`: OHLCV 및 `ma5`, `ma20`, `ma50`, `ma150`, `ma200`
- `recent`: 월간 수익률, 연속성, 거래량 신호, RSI, 볼린저밴드, 추세, MACD
- `factor_source`, `factor_ids`

예시:

```http
GET /api/chart/005930?range=1Y
```

### 3.3 섹터

| 메서드 | 경로 | 설명 | 응답 |
| --- | --- | --- | --- |
| `GET` | `/api/sectors` | 섹터 목록 조회 | `SectorDto[]` |
| `GET` | `/api/sectors/industry-groups` | 산업그룹 목록 조회 | `IndustryGroupDto[]` |

`SectorDto`는 `sector_code`, `sector_name`, `stock_count`를 제공합니다. `IndustryGroupDto`는 산업그룹 코드/명과 상위 섹터 코드/명을 함께 제공합니다.

### 3.4 섹터 리더

#### `GET /api/sector-leaders`

섹터 또는 산업그룹 단위로 강세 종목 비율, 성장률, 수익률, ROE/PER/PBR 기준 랭킹을 조회합니다.

| 구분 | 이름 | 타입 | 필수 | 기본값 | 제약/설명 |
| --- | --- | --- | --- | --- | --- |
| query | `as_of_date` | date | 아니오 | 최신 기준 | 기준일 |
| query | `sort_by` | `SectorLeaderSortBy` | 아니오 | `strong_stock_ratio` | 정렬 지표 |
| query | `direction` | `SortDirection` | 아니오 | 서비스 기본값 | 정렬 방향 |
| query | `limit` | integer | 아니오 | 전체 | `> 0` |
| query | `market` | string | 아니오 | `KR` | 시장 |
| query | `near_high_pct` | number | 아니오 | `3.0` | `0 <= value < 100`, 신고가 근접 판단 폭 |
| query | `financial_basis` | string | 아니오 | `annual` | 재무 기준 |
| query | `level` | `SectorLeaderLevel` | 아니오 | `industry_group` | 집계 레벨 |

주요 응답 필드:

- `as_of_date`, `market`, `level`, `sort_by`, `direction`
- `factor_source`, `eps_growth_factor_id`
- `rows`: 순위, 섹터명, 종목 수, 강세 종목 수, 강세 비율, EPS 기대 성장률, 1일/1주 수익률, ROE, PER, PBR

### 3.5 팩터 카탈로그

#### `GET /api/factors`

스크리닝, 백테스트, 재무/밸류에이션 계산에 쓰이는 팩터 목록을 조회합니다.

| 구분 | 이름 | 타입 | 필수 | 기본값 | 설명 |
| --- | --- | --- | --- | --- | --- |
| query | `factor_type` | string | 아니오 | `null` | 팩터 유형 필터 |
| query | `factor_group` | string | 아니오 | `null` | 팩터 그룹 필터 |
| query | `search` | string | 아니오 | `null` | 팩터명/설명 검색 |
| query | `active_only` | boolean | 아니오 | `true` | 활성 팩터만 조회 |

응답 `FactorDto`:

- `factor_id`, `factor_name`, `factor_type`, `factor_group`
- `unit`, `value_direction`, `description`, `is_active`

### 3.6 팩터 스크리닝

#### 공통 요청 모델: `FactorConditionDto`

```json
{
  "factor_id": "string",
  "mode": "top_percent",
  "top_percent": 20,
  "rank_direction": "catalog",
  "percentile_side": "top",
  "operator": null,
  "value": null,
  "min_value": null,
  "max_value": null,
  "alias": null
}
```

- `mode=top_percent`: `top_percent`, `rank_direction`, `percentile_side`를 사용합니다.
- `mode=threshold`: `operator`, `value` 또는 `min_value`/`max_value`를 사용합니다.
- `rank_direction=catalog`는 팩터 카탈로그의 `value_direction`을 따릅니다.
- `percentile_side=top`은 좋은 방향 상위 N%, `bottom`은 좋은 방향 기준 하위 N%를 선택합니다.

#### `POST /api/factor-screen/screen`

조건에 맞는 종목을 스크리닝합니다.

요청 본문 `FactorScreenRequestDto`:

| 필드 | 타입 | 필수 | 기본값 | 설명 |
| --- | --- | --- | --- | --- |
| `conditions` | `FactorConditionDto[]` | 예 | - | 최소 1개 조건 |
| `as_of_date` | date | 아니오 | 최신 기준 | 기준일 |
| `market` | string | 아니오 | `null` | 시장 |
| `financial_basis` | string | 아니오 | `annual` | 재무 기준 |
| `style_profile` | `StyleProfile` | 아니오 | 서비스 기본값 | 스타일 프로필 |
| `sector_codes` | string[] | 아니오 | `null` | 섹터 필터 |
| `industry_group_codes` | string[] | 아니오 | `null` | 산업그룹 필터 |
| `match_mode` | `MatchMode` | 아니오 | `all` | 조건 결합 방식 |
| `limit` | integer | 아니오 | `5000` | `1..5000` |

응답 `FactorScreenResponseDto`:

- `summary`: `screening_result`, `total_count`, `displayed_count`
- `fixed_columns`, `factor_columns`: 화면 표시용 컬럼 메타데이터
- `rows`: 순위, 종목 ID/티커/명, 시총, 섹터/산업그룹, percentile, 매칭 조건, 팩터 값

예시:

```json
{
  "conditions": [
    {
      "factor_id": "per",
      "mode": "top_percent",
      "top_percent": 20,
      "rank_direction": "lower",
      "percentile_side": "top"
    }
  ],
  "market": "KR",
  "financial_basis": "annual",
  "match_mode": "all",
  "limit": 100
}
```

#### 스크리너 전략 관리

| 메서드 | 경로 | 설명 | 요청/응답 |
| --- | --- | --- | --- |
| `GET` | `/api/factor-screen/strategies` | 저장된 전략 목록 조회 | 응답 `ScreenerStrategyListResponseDto` |
| `POST` | `/api/factor-screen/strategies` | 전략 저장 또는 갱신 | 요청 `ScreenerStrategySaveRequestDto`, 응답 `ScreenerStrategyDetailDto` |
| `GET` | `/api/factor-screen/strategies/{strategy_id}` | 전략 상세 조회 | 응답 `ScreenerStrategyDetailDto` |
| `DELETE` | `/api/factor-screen/strategies/{strategy_id}` | 전략 삭제 | 응답 `{"deleted": true}` |
| `GET` | `/api/strategies` | 저장된 전략 목록 조회 | 응답 `ScreenerStrategyListResponseDto` |
| `GET` | `/api/strategies/{strategy_id}` | 전략 상세 조회 | 응답 `ScreenerStrategyDetailDto` |
| `POST` | `/api/strategies/{strategy_id}/screen` | 저장된 전략으로 스크리닝 실행 | 응답 `FactorScreenResponseDto` |

`ScreenerStrategySaveRequestDto`:

```json
{
  "name": "저PER 전략",
  "strategy": {
    "conditions": [
      {
        "factor_id": "per",
        "mode": "top_percent",
        "top_percent": 20,
        "rank_direction": "lower",
        "percentile_side": "top"
      }
    ]
  }
}
```

### 3.7 팩터 백테스트

#### `POST /api/backtests/factor`

팩터 스크리닝 조건을 리밸런싱 규칙에 따라 과거 기간에 적용해 전략 성과를 계산합니다.

요청 본문 `FactorBacktestRequestDto`:

| 필드 | 타입 | 필수 | 기본값 | 설명 |
| --- | --- | --- | --- | --- |
| `conditions` | `FactorConditionDto[]` | 예 | - | 최소 1개 조건 |
| `start_date` | date | 예 | - | 백테스트 시작일 |
| `end_date` | date | 예 | - | 백테스트 종료일 |
| `rebalance_frequency` | `RebalanceFrequency` | 예 | - | 리밸런싱 주기 |
| `market` | string | 아니오 | `null` | 시장 |
| `financial_basis` | string | 아니오 | `annual` | 재무 기준 |
| `style_profile` | `StyleProfile` | 아니오 | `DEFAULT` | 스타일 프로필 |
| `sector_codes` | string[] | 아니오 | `null` | 섹터 필터 |
| `industry_group_codes` | string[] | 아니오 | `null` | 산업그룹 필터 |
| `match_mode` | `MatchMode` | 아니오 | `all` | 조건 결합 방식 |
| `benchmarks` | string[] | 아니오 | `["KOSPI200", "KOSDAQ"]` | 비교 벤치마크 |
| `max_positions` | integer | 아니오 | `null` | 최대 편입 종목 수, `> 0` |
| `transaction_cost_bps` | number | 아니오 | `0` | 거래비용 bps, `>= 0` |

응답 `FactorBacktestResponseDto`:

- `summary`: 누적수익률, CAGR, MDD, 변동성, 샤프, 승률, 리밸런싱 횟수
- `equity_curve`: 날짜별 전략 NAV 및 벤치마크 NAV
- `rebalance_history`: 리밸런싱별 보유/진입/청산 종목
- `annual_returns`: 연도별 전략/벤치마크/초과수익률
- `warnings`

### 3.8 종목 소개

#### `POST /api/factor-lab/runs/{run_id}/backtest`

Runs a factor backtest against the saved output of a completed Factor Lab run.
The run must already have rows in `factor_lab_values`; this endpoint does not
compile or execute a graph by itself.

Path parameters:

| name | type | required | description |
| --- | --- | --- | --- |
| `run_id` | UUID string | yes | Completed Factor Lab run ID |

Request body `FactorLabBacktestRequestDto`:

| field | type | required | default | description |
| --- | --- | --- | --- | --- |
| `top_percent` | number | no | `20` | Selects top-ranked lab factor rows, `0 < value <= 100` |
| `start_date` | date | yes | - | Backtest start date |
| `end_date` | date | yes | - | Backtest end date |
| `rebalance_frequency` | `RebalanceFrequency` | no | `quarterly` | Rebalance frequency |
| `market` | string | no | `null` | Market filter |
| `benchmarks` | string[] | no | `["KOSPI200", "KOSDAQ"]` | Benchmark symbols |
| `max_positions` | integer | no | `null` | Maximum selected positions, `> 0` |
| `transaction_cost_bps` | number | no | `0` | Transaction cost in bps |

Response: `FactorBacktestResponseDto`.

Error behavior:

- `404 factor_lab_run_not_found`: `run_id` was not found.
- `422 factor_lab_invalid_backtest`: run is not completed, has no valid Factor Lab values, or request validation fails.

Example:

```json
{
  "top_percent": 20,
  "start_date": "2021-01-01",
  "end_date": "2025-12-31",
  "rebalance_frequency": "quarterly",
  "market": "KR",
  "benchmarks": ["KOSPI200", "KOSDAQ"],
  "max_positions": 50,
  "transaction_cost_bps": 5
}
```

#### `GET /api/introduction/{stock_code}`

종목 기본 정보, 시가총액/밸류에이션/52주 범위, 회사 설명, 사업 영역 배지를 조회합니다.

| 구분 | 이름 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- | --- |
| path | `stock_code` | string | 예 | 조회할 종목 코드 |

주요 응답 필드:

- `stock`: 종목 코드, 내부 ID, 한글/영문명, 국가, 통화
- `metrics`: 시총, trailing PER, 배당수익률, 52주 범위/고가/저가, 최근 종가/거래일
- `company.description`
- `business_areas`: 섹터/산업그룹 코드 및 이름, 분류 체계
- `factor_source`

### 3.9 재무제표 및 재무비율

#### `GET /api/financials/{stock_code}`

표준화된 재무제표 계정과 기간별 값을 조회합니다.

| 구분 | 이름 | 타입 | 필수 | 기본값 | 설명 |
| --- | --- | --- | --- | --- | --- |
| path | `stock_code` | string | 예 | - | 조회할 종목 코드 |
| query | `period` | `FinancialStatementPeriod` | 아니오 | `annual` | 기간 유형 |
| query | `statement` | `FinancialStatementFilter` | 아니오 | `all` | 재무제표 종류 |
| query | `market` | string | 아니오 | `kr` | 시장(`kr`, `us`) |

응답 `FinancialStatementsResponseDto`:

- `stock`, `period`, `statement`
- `columns`: 기간 키, 라벨, 회계연도/월, 기간 종료일
- `sections`: `IS`, `BS`, `CF` 섹션별 계정 목록
- 계정별 `values`, `trend`, `growth_chart`, `statistics`
- `source`

#### `GET /api/financials/{stock_code}/accounts/{canonical_id}`

단일 표준 계정의 상세 시계열과 통계를 조회합니다.

| 구분 | 이름 | 타입 | 필수 | 기본값 | 설명 |
| --- | --- | --- | --- | --- | --- |
| path | `stock_code` | string | 예 | - | 조회할 종목 코드 |
| path | `canonical_id` | string | 예 | - | 표준 계정 ID |
| query | `period` | `FinancialStatementPeriod` | 아니오 | `annual` | 기간 유형 |
| query | `market` | string | 아니오 | `kr` | 시장(`kr`, `us`) |

#### `GET /api/financials/{stock_code}/ratios`

재무비율 및 팩터 기반 지표를 그룹별로 조회합니다.

| 구분 | 이름 | 타입 | 필수 | 기본값 | 설명 |
| --- | --- | --- | --- | --- | --- |
| path | `stock_code` | string | 예 | - | 조회할 종목 코드 |
| query | `period` | string | 아니오 | `annual` | `annual` 또는 `quarter` 용도 |
| query | `market` | string | 아니오 | `kr` | 시장(`kr`, `us`) |

응답 `FinancialRatiosResponseDto`:

- `stock`, `period`, `financial_basis`
- `columns`
- `sections`: statement/group/ratio 구조
- ratio별 `factor_id`, `factor_name`, `unit`, `value_direction`, `values`, `trend`, `growth_chart`, `statistics`
- `source`, `auxiliary_sources`

### 3.10 스타일 스코어

#### `GET /api/style-scores`

스타일 프로필 기준 종목 랭킹을 조회합니다.

| 구분 | 이름 | 타입 | 필수 | 기본값 | 제약/설명 |
| --- | --- | --- | --- | --- | --- |
| query | `trade_date` | date | 아니오 | 최신 기준 | 점수 기준일 |
| query | `style_profile` | `StyleProfile` | 아니오 | `DEFAULT` | 스타일 프로필 |
| query | `limit` | integer | 아니오 | `100` | `1..1000` |
| query | `min_confidence` | number | 아니오 | `null` | `0..1` |
| query | `industry_group_code` | string | 아니오 | `null` | 산업그룹 필터 |
| query | `sector_code` | string | 아니오 | `null` | 섹터 필터 |

응답 행 `StyleScoreRowDto`:

- 종목/산업 정보: `security_id`, `issuer_id`, `stock_code`, `company_name`, `sector_code`, `industry_group_code`
- 점수: `value_score`, `quality_score`, `growth_score`, `momentum_score`, `risk_score`, `dividend_score`, `total_score`
- 신뢰도 및 커버리지: `score_confidence`, `available_factor_count`, `required_factor_count`, `missing_factor_ids`, `invalid_factor_ids`

#### 스타일 스코어 상세

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| `GET` | `/api/style-scores/{security_id}` | 종목의 스타일 종합 점수와 팩터별 분해 조회 |
| `GET` | `/api/style-scores/{security_id}/components` | 종목의 스타일 컴포넌트 목록 조회 |
| `GET` | `/api/style-scores/{security_id}/components/{component_key}` | 특정 컴포넌트와 구성 팩터 상세 조회 |

공통 query:

- `trade_date`: date, 선택
- `style_profile`: `StyleProfile`, 기본값 `DEFAULT`

`component_key`는 구현상 문자열로 받으며, 현재 DTO에서 사용하는 대표 키는 `COMPOSITE`, `VALUE`, `QUALITY`, `GROWTH`, `MOMENTUM`, `RISK`, `DIVIDEND`입니다.

### 3.11 멀티플 밸류에이션

#### `GET /api/valuations/{stock_code}/multiple-bands`

현재 멀티플과 과거/산업/시장 벤치마크를 비교하고 적정가 밴드를 계산합니다.

| 구분 | 이름 | 타입 | 필수 | 기본값 | 제약/설명 |
| --- | --- | --- | --- | --- | --- |
| path | `stock_code` | string | 예 | - | 조회할 종목 코드 |
| query | `as_of_date` | date | 아니오 | 최신 기준 | 기준일 |
| query | `factor_ids` | string[] | 아니오 | 서비스 기본값 | 비교할 멀티플 팩터 목록 |
| query | `financial_basis` | string | 아니오 | `ttm` | 재무 기준 |
| query | `lookback_years` | integer | 아니오 | `3` | `1..10` |
| query | `buy_margin_pct` | number | 아니오 | `20.0` | `0 <= value < 100` |
| query | `sell_margin_pct` | number | 아니오 | `10.0` | `0 <= value < 100` |
| query | `band_basis` | `MultipleValuationBandBasis` | 아니오 | `blend` | 밴드 산출 기준 |
| query | `market` | string | 아니오 | `kr` | 시장 |
| query | `include_history` | boolean | 아니오 | `true` | 과거 멀티플 포함 여부 |

주요 응답 필드:

- `stock`: 종목, 시장, 산업분류 메타데이터
- `as_of_date`, `price_date`, `current_price`
- `comparisons`: 팩터별 현재값과 벤치마크 차이/신호
- `bands`: 팩터별 목표 멀티플, 적정가, 매수/매도 기준가, 상승여력, 신호
- `central_band`: 종합 적정가 밴드
- `history`: 과거 멀티플 시계열
- `warnings`

## 4. MCP 제공 명세

### 4.1 Transport

#### HTTP

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| `GET` | `/` | MCP HTTP transport 메타정보 반환 |
| `POST` | `/` | MCP JSON-RPC 메시지 처리 |

`GET /` 응답:

```json
{
  "name": "arcana-api",
  "transport": "http",
  "protocol": "mcp",
  "endpoint": "/"
}
```

`POST /`는 단일 JSON-RPC 메시지 또는 batch 배열을 받습니다. notification 또는 응답이 없는 batch는 `202 Accepted`를 반환합니다.

#### stdio

```powershell
python -m api.mcp
```

stdio 서버는 줄 단위 JSON 또는 `Content-Length` 헤더가 있는 MCP 메시지를 모두 읽을 수 있습니다.

### 4.2 MCP 프로토콜

- 프로토콜 버전 상수: `2024-11-05`
- 서버 이름: `arcana-api`
- 서버 버전: `0.1.0`
- capabilities:

```json
{
  "tools": {
    "listChanged": false
  }
}
```

지원 메서드:

| MCP method | 설명 |
| --- | --- |
| `initialize` | 프로토콜 버전, capabilities, 서버 정보, Arcana 주식 분석 instructions를 반환 |
| `ping` | 빈 객체 반환 |
| `tools/list` | REST 라우트에서 생성한 tool 메타데이터 목록 반환 |
| `tools/call` | 지정한 tool을 호출하고 REST 결과를 MCP content로 반환 |

지원하지 않는 method는 JSON-RPC error `-32000`으로 반환됩니다.

### 4.3 REST 라우트와 MCP tool 매핑

MCP tool 이름은 FastAPI endpoint 함수명을 소문자/스네이크 케이스로 정규화한 값입니다. 현재 라우트명은 이미 스네이크 케이스라 함수명과 동일합니다.

| MCP tool | REST | 필수 arguments | 선택 arguments |
| --- | --- | --- | --- |
| `health_check` | `GET /health` | - | - |
| `get_stock_chart` | `GET /api/chart/{stock_code}` | `stock_code` | `range` |
| `get_sectors` | `GET /api/sectors` | - | - |
| `get_industry_groups` | `GET /api/sectors/industry-groups` | - | - |
| `get_sector_leaders` | `GET /api/sector-leaders` | - | `as_of_date`, `sort_by`, `direction`, `limit`, `market`, `near_high_pct`, `financial_basis`, `level` |
| `get_factors` | `GET /api/factors` | - | `factor_type`, `factor_group`, `search`, `active_only` |
| `list_screener_strategies` | `GET /api/factor-screen/strategies` | - | - |
| `save_screener_strategy` | `POST /api/factor-screen/strategies` | `name`, `strategy` | - |
| `get_screener_strategy` | `GET /api/factor-screen/strategies/{strategy_id}` | `strategy_id` | - |
| `delete_screener_strategy` | `DELETE /api/factor-screen/strategies/{strategy_id}` | `strategy_id` | - |
| `list_strategies` | `GET /api/strategies` | - | - |
| `get_strategy` | `GET /api/strategies/{strategy_id}` | `strategy_id` | - |
| `screen_strategy` | `POST /api/strategies/{strategy_id}/screen` | `strategy_id` | - |
| `screen_stocks` | `POST /api/factor-screen/screen` | `conditions` | `as_of_date`, `market`, `financial_basis`, `style_profile`, `sector_codes`, `industry_group_codes`, `match_mode`, `limit` |
| `run_factor_backtest` | `POST /api/backtests/factor` | `conditions`, `start_date`, `end_date`, `rebalance_frequency` | `market`, `financial_basis`, `style_profile`, `sector_codes`, `industry_group_codes`, `match_mode`, `benchmarks`, `max_positions`, `transaction_cost_bps` |
| `get_stock_introduction` | `GET /api/introduction/{stock_code}` | `stock_code` | - |
| `get_financial_statements` | `GET /api/financials/{stock_code}` | `stock_code` | `period`, `statement`, `market` |
| `get_financial_account_detail` | `GET /api/financials/{stock_code}/accounts/{canonical_id}` | `stock_code`, `canonical_id` | `period`, `market` |
| `get_financial_ratios` | `GET /api/financials/{stock_code}/ratios` | `stock_code` | `period`, `market` |
| `get_operating_metrics` | `GET /api/operating-metrics/{stock_code}` | `stock_code` | - |
| `get_unit_economics` | `GET /api/operating-metrics/{stock_code}/unit-economics` | `stock_code` | - |
| `get_operating_metric_drivers` | `GET /api/operating-metrics/{stock_code}/drivers` | `stock_code` | - |
| `get_estimates` | `GET /api/estimates/{stock_code}` | `stock_code` | - |
| `get_estimate_consensus` | `GET /api/estimates/{stock_code}/consensus` | `stock_code` | - |
| `get_estimate_consensus_history` | `GET /api/estimates/{stock_code}/consensus/history` | `stock_code` | `start_date`, `end_date`, `metric_id`, `target_period` |
| `get_estimate_drivers` | `GET /api/estimates/{stock_code}/drivers` | `stock_code` | - |
| `get_style_scores` | `GET /api/style-scores` | - | `trade_date`, `style_profile`, `limit`, `min_confidence`, `industry_group_code`, `sector_code` |
| `get_style_score_detail` | `GET /api/style-scores/{security_id}` | `security_id` | `trade_date`, `style_profile` |
| `get_style_score_components` | `GET /api/style-scores/{security_id}/components` | `security_id` | `trade_date`, `style_profile` |
| `get_style_score_component_detail` | `GET /api/style-scores/{security_id}/components/{component_key}` | `security_id`, `component_key` | `trade_date`, `style_profile` |
| `get_multiple_valuation_bands` | `GET /api/valuations/{stock_code}/multiple-bands` | `stock_code` | `as_of_date`, `factor_ids`, `financial_basis`, `lookback_years`, `buy_margin_pct`, `sell_margin_pct`, `band_basis`, `market`, `include_history` |

### 4.4 MCP tool 입력 스키마 규칙

`api/mcp.py`는 endpoint 함수 signature를 읽어 JSON Schema를 만듭니다.

- Pydantic request body만 있는 POST 라우트는 request body 모델의 필드를 tool arguments로 직접 받습니다.
- path/query parameter는 tool arguments의 top-level 필드로 받습니다.
- `Literal` 타입은 JSON Schema `enum`으로 변환됩니다.
- `date`는 `{"type": "string", "format": "date"}`로 변환됩니다.
- 리스트 타입은 배열로 변환됩니다.
- 선택 필드는 `required`에 포함되지 않으며 기본값이 schema에 포함됩니다.

예를 들어 `screen_stocks`는 REST에서 `FactorScreenRequestDto` body를 받지만 MCP 호출에서는 아래처럼 body 필드를 바로 전달합니다.

```json
{
  "name": "screen_stocks",
  "arguments": {
    "conditions": [
      {
        "factor_id": "per",
        "mode": "top_percent",
        "top_percent": 20,
        "rank_direction": "lower",
        "percentile_side": "top"
      }
    ],
    "market": "KR",
    "limit": 100
  }
}
```

### 4.5 MCP 호출 예시

#### 초기화

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2024-11-05",
    "clientInfo": {
      "name": "example-client",
      "version": "0.1.0"
    }
  }
}
```

#### tool 목록 조회

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/list",
  "params": {}
}
```

#### 차트 조회

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "get_stock_chart",
    "arguments": {
      "stock_code": "005930",
      "range": "1Y"
    }
  }
}
```

#### 밸류에이션 조회

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "tools/call",
  "params": {
    "name": "get_multiple_valuation_bands",
    "arguments": {
      "stock_code": "005930",
      "financial_basis": "ttm",
      "lookback_years": 3,
      "band_basis": "blend",
      "include_history": true
    }
  }
}
```

#### 팩터 백테스트

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "tools/call",
  "params": {
    "name": "run_factor_backtest",
    "arguments": {
      "conditions": [
        {
          "factor_id": "roe",
          "mode": "top_percent",
          "top_percent": 30,
          "rank_direction": "higher",
          "percentile_side": "top"
        }
      ],
      "start_date": "2021-01-01",
      "end_date": "2025-12-31",
      "rebalance_frequency": "quarterly",
      "market": "KR",
      "max_positions": 30,
      "transaction_cost_bps": 5
    }
  }
}
```

### 4.6 MCP 오류 응답

`tools/call` 내부에서 REST controller가 `HTTPException`을 발생시키면 MCP 결과는 `isError=true`이며, content text에는 상태 코드와 detail이 JSON 문자열로 들어갑니다.

```json
{
  "content": [
    {
      "type": "text",
      "text": "{\"status_code\": 404, \"detail\": \"screener strategy not found\"}"
    }
  ],
  "isError": true
}
```

알 수 없는 tool 이름이나 지원하지 않는 MCP method는 JSON-RPC error 객체로 반환됩니다.

## 5. 구현 파일 기준

| 영역 | 파일 |
| --- | --- |
| FastAPI 앱 등록 | `api/main.py` |
| MCP 변환 및 JSON-RPC 처리 | `api/mcp.py` |
| REST 컨트롤러 | `api/controller/*.py` |
| 요청/응답 DTO | `api/service/dto.py` |
| 서비스 계층 | `api/service/*.py` |
| Repository/query 계층 | `api/repository/*.py`, `api/factor_screen_query.py` |
