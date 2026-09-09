# 공통 투자 대상 필터

팩터랩, 퀀트 스크리너, 두 백테스트가 `api/model/universe.py`와
`api/repository/universe_query.py`의 계약과 날짜별 유니버스 SQL을 공유한다.
프런트는 `arcana-front/src/components/UniverseControls.tsx`를 공유한다.

## 요청과 단위

일반 스크리닝·백테스트는 선택적 `universe`를 받는다. 기존 `market`,
`sector_codes`, `industry_group_codes`의 위치는 유지한다.
팩터랩은 `experiment.universe`에 같은 필드를 추가한다.

```json
{
  "exchange_codes": ["KOSDAQ"],
  "market_cap_min_mil": 100000,
  "market_cap_max_mil": null,
  "size_percentile": {"side": "top", "percent": 50}
}
```

`*_mil`은 원통화 백만 단위다. 한국 화면의 1,000억 원은 100,000백만 원,
미국 화면의 100백만 달러는 100으로 전송한다. 국가 변경은 거래소·금액·산업
선택을 초기화하고 비율은 유지한다. 필드 누락, 빈 거래소 목록, null 금액·비율은
제한 없음이다. 한국은 KOSPI/KOSDAQ, 미국은 NASDAQ/NYSE/NYSE_AMERICAN/OTHER를 지원한다.

## 계산

1. 현재 국가·거래소·섹터·산업군으로 기준 집합을 만든다.
2. 평가일의 유효 시총을 가진 기준 집합 전체에서 비율 순위를 매긴다.
   팩터 결측 종목도 시총 모집단에 포함한다.
3. `ceil(N × percent / 100)`개를 선택한다. 동률은 `security_id ASC`로 고정한다.
   최소·최대 금액은 경계를 포함하며 비율과 AND로 결합한다.
4. 유니버스 안에서 팩터 계산·순위·표준화·중앙값 대체 후 최종 종목을 고른다.
   raw 입력의 lag/rolling용 과거 행과 거래일 달력은 유지한다. 순위·표준화 뒤에
   시계열 계산이 이어지면 탈락 날짜를 무효 행으로 남겨 이전 점수로 건너뛰지 않는다.

시총은 `fact_daily_factors.mcap_mil`의 **평가일과 같은 날짜**에서만 읽는다.
동일 재무 기준의 최신 수정 행을 선택한 후 유효한 annual → ttm → quarterly 순으로
선택한다. null, 비유한 값, 0·음수, 국가와 다른 통화는 유효 시총에서 제외한다.
사이즈 필터가 없으면 이 종목들도 유지한다. 미래 값·이전 날짜 값으로 대체하지 않는다.

백테스트는 리밸런싱 직전 거래일을 신호일로 사용한다. 필터가 적용된 팩터랩
백테스트는 그 신호일에 생성된 점수만 읽어 이전 점수의 재편입을 막는다.
필터로 모든 종목이 제외된 기간은 현금 보유로 처리한다.
실행 정의는 `factor_lab_run_definition`에 보존하므로 저장 전략을 수정하거나
백테스트 요청의 국가를 바꿔도 기존 실행의 투자 대상은 바뀌지 않는다.
필터를 바꾼 프런트는 새 history 실행부터 계산한다. 대체 전략도 부모 실행의
유니버스로 원본 그래프를 재계산한다. 원본 실행 정의가 없는 레거시 대체 전략은
새 필터 사용 전에 원본을 다시 실행해야 한다.

필터 적용 응답의 `universe_summary`는 적용 조건, 분류 기준일, 평가일별
`before_count`(국가·거래소·산업 조건 적용 후), `valid_market_cap_count`,
`missing_market_cap_count`(결측·비정상·통화 불일치 포함), `after_count`를 담는다.
팩터 계산·최종 종목 선택으로 결과 수가 더 줄어들 수 있다.
팩터랩 실행 요약은 저장하여 이후 조회에서도 반환한다.

## 분류 데이터와 적용

기존 식별자와 `primary_market_mic`은 보존하고 `security_master.exchange_code`를 추가한다.
한국은 KRX 적재와 기존 identifiers의 시장 구분을 사용한다. 미국은 보관 중인
`data-lake/bronze/yfinance/universe/us_equity_universe.csv`를 사용한다.
공급자 코드는 [Nasdaq Symbol Directory Definitions](https://www.nasdaqtrader.com/Trader.aspx?id=SymbolDirDefs)에 따라
정규화한다. 미분류는 빈 값이며 OTHER로 간주하지 않는다.
전체 거래소에서는 미분류를 유지하고 거래소를 명시하면 제외한다.

```powershell
# 변경 건수 확인
& .\.venv-llama\Scripts\python.exe -m scripts.backfill_exchange_codes
# 컬럼 생성 및 현재 분류 반영 (반복 실행 가능)
& .\.venv-llama\Scripts\python.exe -m scripts.backfill_exchange_codes --apply
```

신규 데이터베이스 스키마와 증분 SQL도 함께 갱신했다. 기존 종목정보 적재 끝에
분류 갱신이 실행된다. 팩터랩 테이블의 실행 요약 컬럼은 서비스의 기존 DDL
초기화 경로에서 추가한다. 백엔드·프런트를 함께 반영한다.
과거 이전상장 이력을 복원하지 않으며 화면에 **거래소: 현재 분류 / 시총: 각 신호일 기준**을 표시한다.

## 검증

`tests/test_universe_filters.py`는 경계·비율·동률·결측·수정 행·통화·날짜별
편입, rank/zscore/median, lag 과거 입력, 정확한 신호일 점수, 실행 정의 보존을 검증한다.
ClickHouse 테스트는 임시 테이블 및 독립 테스트 DB에서 실행한다.

```powershell
$env:ARCANA_TEST_CLICKHOUSE='1'
& .\.venv-llama\Scripts\python.exe -m pytest tests/test_universe_filters.py tests/test_factor_lab_v2_clickhouse.py -q
```

프런트의 `tests/universe.test.tsx`, `tests/backtestUniverse.test.tsx`는 단위 전환,
전략 저장·복원, 요청 전달, 캐시 무효화와 history 재실행을 검증한다.
기존 팩터랩 v1/v2 및 연구 기능 테스트도 함께 실행한다.
