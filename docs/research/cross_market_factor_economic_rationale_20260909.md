# 미국·한국 공통 팩터 전략: 경제적 사전 가설과 검증 기준

- 작성일: 2026-09-09
- 범위: `lab_*`를 제외한 Arcana 팩터 전체를 검토 대상으로 삼는 탐색의 경제적 기준. 이 문서는 전략 성과를 계산하거나 수익을 입증한 결과가 아니다.
- 근거: 아래 연결한 연구자·학술지·데이터 제공기관의 1차 자료와 저장소 구현. 웹 자료는 작성일에 확인했다.

## 실험에 바로 적용할 판단

**가치 + 수익성/현금흐름의 질 + 중기 모멘텀을 공통 핵심으로 두고, 저위험·보수적 투자는 보조 축으로 검증하는 것이 타당하다.** 모든 비실험 팩터를 살펴본다는 것은 모든 열에 같은 방향과 양의 비중을 부여한다는 뜻이 아니다. 절대 금액, 주당 금액, 위험 추정 입력, 중복 비율도 포함되어 있기 때문이다. 경제적 역할·가용성·단위를 분류한 후 신호, 조건, 중립화 변수, 파생식 입력 중 역할을 정해야 한다. 이는 이 문서의 연구 설계 제안이며, 근거가 있는 팩터도 양국에서 샤프 1을 보장하지 않는다.

가치와 모멘텀은 여러 자산·시장에 걸친 연구에서 각각 수익 프리미엄과 상호 음의 상관을 보였다. 수익성 통제가 가치 전략의 성과를 개선한 연구도 있다. 따라서 단일 팩터 최고값을 추종하기보다 서로 다른 경제적 정보의 결합을 첫 가설로 삼는다. 다만 해당 연구의 매수·매도 포트폴리오와 Arcana의 매수 보유 전략은 별도 검증 대상이다. [Asness, Moskowitz & Pedersen (2013)](https://www.aqr.com/Insights/Research/Journal-Article/Value-and-Momentum-Everywhere), [Novy-Marx (2013), 저자 작업논문](https://www.nber.org/papers/w15940).

## 경제적 방향과 Arcana 대응

`↑`는 높을수록 우선, `↓`는 낮을수록 우선이라는 사전 가설이다. 아래 방향은 최종 성과를 본 뒤 뒤집을 탐색 자유도가 아니라 사전에 고정할 가설이다. 같은 개념의 역수·별칭·기간 변형은 하나의 묶음으로 취급한다. 팩터 ID와 정의는 [카탈로그 구현](../../engine/loaders/_internal/clickhouse_factors.py), [계산 구현](../../engine/transformers/_internal/factor_metrics.py), [스타일 정의](../../engine/transformers/_internal/style_score_definitions.py)를 확인했다.

| 경제적 축 | 방향이 있는 후보 | 해석·조건 |
| --- | --- | --- |
| 가치 | `epr`, `bpr`, `fcfpr`, `fcf_to_ev_yield`, `ebitda_to_ev`, `economic_profit_yield` ↑ | 지불 가격 대비 이익·순자산·현금흐름을 산다. `per/pbr/ev_to_ebitda` 등 역수는 ↓이나 음수 분모를 저평가로 오인하지 않아야 한다. `epr/per`, `bpr/pbr`를 별개 독립 정보로 중복 가중하지 않는다. |
| 수익성·질 | `gross_profitability_pct`, `roa`, `roe`, `roic_operational`, `roic_wacc_spread`, `roe_cost_of_equity_spread_pct`, `opm`, `f_score` ↑; `accrual_ratio`, `percent_total_accruals_pct` ↓ | 장부 성장보다 현금 창출과 자본 효율을 우선한다. ROE는 음의 자기자본·과도한 부채를 걸러 해석한다. 금융업과 비금융업에 동일한 EV·ROIC·부채비율 순위를 강제하지 않는다. |
| 보수적 투자 | `asset_yoy_pct`, `book_equity_growth_1y_pct`, `current_operating_assets_change_pct`, `inventory_growth_1y_pct`, `capex_growth_2y_pct`, `net_external_financing_pct` ↓ | 과잉 투자·외부 조달을 피한다는 가설. 성장률이 낮다는 이유만으로 우량하다고 판단하지 않으며, 수익성·매출 상황과 함께 본다. `CAPEX` 자체의 낮음과 투자 효율의 높음은 다르다. |
| 중기 모멘텀 | `tr_12_1`, `tr_6_1`, `tr_3_1`, `risk_adj_mom`, `high52w_gap_pct`, `k_ratio_3y` ↑ | 시장의 정보 반영·추세 지속을 포착한다는 가설. 12–1, 6–1은 최근 약 1개월을 제외한다. 같은 가격 경로의 여러 지표를 과도하게 중복 가중하지 않는다. |
| 낮은 위험·지급능력 | `vol_12_1_ann`, `fcf_volatility_5y`, `fcf_negative_freq_5y_pct`, 유효한 `net_debt_to_ebitda` ↓; `interest_coverage`, `cash_to_debt` ↑ | 위험당 수익을 개선할 보조 축. 저변동만으로 최고 CAGR가 나오지는 않는다. 마이너스 EBITDA로 생긴 음의 부채비율을 저위험으로 오인하지 않는다. |
| 규모·유동성 | `mcap_mil`은 크기 노출·유니버스 조건으로 사용; 작은 규모 선호는 질·거래 가능성 조건부 | 대형주가 높게 점수화되어야 한다는 경제적 법칙은 없다. 소형주 효과는 질을 통제할 때 더 강하고 안정적이라는 연구가 있다. 거래비용을 빼기 전 소형·저유동성 수익은 실현 가능 수익과 다르다. |
| 성장·기대 차이 | `sales_yoy_pct`, `eps_yoy_pct`, `op_yoy_pct`, `cfo_yoy_pct`, `fcf_yoy_pct`, 유효한 실적 상향·서프라이즈 ↑; `pvgo_gap_pct`, `pvgo_compression_pct` ↑는 조건부 가설 | 높은 성장 자체보다 현재 가격에 반영된 기대를 넘는 질 좋은 성장이 중요하다. 적자에서 흑자로 전환할 때 단순 성장률이 왜곡될 수 있다. 컨센서스는 과거 관측본·공개일·공급자별 가용성이 입증되어야 한다. |
| 주주 환원 | `shareholder_yield`, `sharehold_net_buyback_yield`, 지속 가능한 `dividend_yield` ↑; `dividend_cut`, 현금흐름 대비 과도한 지급 ↓ | 가치·현금흐름과 중복되는 축이다. 높은 배당률을 사업 악화·주가 하락과 구분하고, 배당 및 자사주 매입의 실제 공개일을 사용한다. |

수익성·투자의 표준적 정의는 Fama–French의 공개 포트폴리오 구성에서 확인할 수 있다. 예컨대 투자는 전년 자산의 증가율이고, 연간 영업 수익성을 특정 회계항목과 장부 자기자본으로 계산한다. Arcana의 ROIC·마진·자산증가율은 이 정의와 동일하지 않을 수 있으므로 이름만으로 학술 팩터를 재현했다고 말하지 않는다. [Kenneth French: Operating Profitability and Investment portfolios](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/tw_5_ports_op_inv.html).

질 높은 기업의 결합 신호에 대한 국제 증거는 QMJ 연구가 제공한다. 이 연구는 수익성·성장·안전성의 묶음이 미국 및 24개국에서 위험조정 수익과 관련 있음을 보인다. 이는 위 표의 각 Arcana 파생변수를 개별적으로 입증하는 자료는 아니다. [Asness, Frazzini & Pedersen (2019)](https://link.springer.com/article/10.1007/s11142-018-9470-2).

저베타에 대한 경제적 설명 중 하나는 레버리지 제약을 받는 투자자가 고위험 주식을 비싸게 사는 현상이다. 따라서 `beta` 또는 변동성을 낮추는 가설은 설명 가능하지만, 베타와 총변동성은 동일하지 않다. [Frazzini & Pedersen (2014)](https://www.aqr.com/insights/research/journal-article/betting-against-beta). 규모는 질과 함께 검증하며 [Asness et al. (2018)](https://www.aqr.com/Insights/Research/Working-Paper/Size-Matters-If-You-Control-Your-Junk), 거래비용·유동성 위험을 따로 반영한다. [Amihud, Mendelson & Pedersen (2005)](https://www.aqr.com/insights/research/journal-article/liquidity-and-asset-prices).

성장이 가치를 더하는지는 재투자 수익률과 자본비용의 차이에 달려 있다. 따라서 `roiic_wacc_spread`, `roic_wacc_spread`, 가격 대비 현금흐름을 동반한 성장과 단순 자산 팽창을 구분하는 것이 경제적으로 타당하다. [Damodaran, 가치 증대와 EVA 유도](https://pages.stern.nyu.edu/adamodar/New_Home_Page/CFTheory/deriv/ch24der.html). Arcana `pvgo_gap_pct`는 자체 P/Q/C/I 가정에서 계산한 정당화된 PVGO와 시장 PVGO의 차이이고, `pvgo_compression_pct`는 정상 이익가치 성장과 시가총액 성장의 차이다. 따라서 독립적인 확정 알파가 아닌 모델 의존적 기대 차이로 평가해야 한다. [Arcana 계산 구현, `add_pvgo_factors`](../../engine/transformers/_internal/factor_metrics.py).

## 양국 공통성과 분리할 부분

공통 경제적 방향은 유지하되 **순위·표준화·거래비용·벤치마크·무위험수익률은 국가별로 계산**하는 설계를 제안한다. 달러와 원화 절대 금액을 한 횡단면에서 직접 비교하지 않는다. 금융업의 영업자산·부채 구조는 비금융업과 다르므로 업종별로 비교하거나 적용 불가능한 비율에서 기권한다. 이 항목은 회계 비교 가능성과 거래 가능성을 확보하기 위한 설계 제안이다.

한국 연구에서는 연간 대신 분기 수익성을 사용한 조정 5요인 모형이 비교 대상 중 한국의 특성별 포트폴리오를 가장 잘 설명했고, 가치 요인도 q 요인이 있는 상황에서 중복적이지 않다는 근거를 보고했다. **한국에서 가치·수익성을 버리고 미국 결과를 그대로 복사할 근거는 없다.** 반대로 이 결과가 매수 전략의 샤프 1을 보증하지도 않는다. [Kang, Kang & Kim (2019)](https://onlinelibrary.wiley.com/doi/10.1111/ajfs.12274).

미국 전용 `us_*` 컨센서스 팩터와 한국 `real_*` 계열을 동일 데이터로 간주하지 않는다. 현재 구현은 한국에 적용 불가능한 미국 팩터를 명시적으로 열거한다. 또 `earnings_outcome`은 실제 발표일이 확인된 미국 Alpha Vantage 이벤트만 지원하며, 한국은 검증된 발표일 공급원이 필요하다. [팩터 적용 범위](../../engine/transformers/_internal/factor_metrics.py), [Factor Lab 연구 노드 계약](../specs/factor-lab-research-nodes.md).

## 투자 방향이 자동으로 정해지지 않는 팩터

다음 열도 비실험 팩터 검토 목록에는 남기되, 무조건 높은 값을 사거나 낮은 값을 사는 독립 신호로 넣지 않는 것이 타당하다.

- **절대 금액**: `at`, `sale`, `ni`, `oancf`, `capx`, `fcf`, `fcfe`, `economic_profit`, `knowledge_capital`, `intangible_capital` 등. 규모·통화 효과가 섞여 있으므로 적절한 가격·자산·매출 분모로 변환하거나 크기 통제에 사용한다.
- **주당 금액·주식 수**: `eps`, `bps`, `sps`, `cps`, `csho`, `dvpsp`, `dvpsx` 등. 주식 분할·주식 단위에 따라 수치가 달라져 횡단면 원시 순위의 경제적 의미가 약하다.
- **가격 단위 기술지표**: `ma_50`, `ma_120`, `ma_150`, `ma_200`, `macd`, `macd_signal`, `bb_upper`, `bb_middle`, `bb_lower`. 현 주가 대비 비율·추세 조건 등 차원이 없는 변환을 먼저 정의한다.
- **조건부 기술지표**: `rsi_14`, `bb_percent_b`, `williams_r_14`, `cmf_20`, `mfi_14`, `ret_1m`. 역추세와 추세 추종이 상반된 방향을 요구할 수 있어 기간·조건을 사전에 정한다.
- **모형 입력·진단값**: `wacc_equity_weight`, `wacc_debt_weight`, `cost_of_equity`, `tax_rate`, 원시 PVGO 수준, `incremental_investment_rate_pct`, 분석가 수. 가정·노출·신뢰도 조건으로 쓸 수 있으나 개별 수익 프리미엄이 자동으로 따라오지 않는다.
- **기존 종합 점수와 구성 팩터**: `style_*` 등 기존 복합 점수는 구성요소와 중복된다. 점수와 구성요소를 동시에 넣으면 의도치 않은 중복 가중이 생길 수 있어 별도 대조군 또는 분해한 신호로 사용한다.

이는 [카탈로그의 `NEUTRAL_FACTORS`, `FUNDAMENTAL_AMOUNT_FACTORS`, `NEUTRAL_TECHNICAL_FACTORS`](../../engine/loaders/_internal/clickhouse_factors.py)와 실제 단위에 근거한 해석이다. 카탈로그가 `HIGHER_BETTER`라고 표시한 모든 열을 경제적 증거로 간주해서는 안 된다.

## 구현 확인에서 발견한 해석상 주의점

1. **MDD 방향 불일치**: `factor_metrics.max_drawdown`은 `wealth/cummax - 1`의 최솟값을 반환한다. 따라서 `mdd1yr_12_1_pct`는 음수이고 0에 가까울수록 안전하다. 그런데 카탈로그 `LOWER_IS_BETTER`에는 해당 팩터가 포함되어 있다. 스타일 정의의 허용범위는 `[-100, 0]`이고 위험 점수에서는 낮을수록 좋다고 설정하지 않는다. 연구 그래프에서 **higher** 방향을 명시하거나 이 불일치가 해소될 때까지 제외해야 한다. [계산 구현](../../engine/transformers/_internal/factor_metrics.py), [카탈로그](../../engine/loaders/_internal/clickhouse_factors.py), [스타일 정의](../../engine/transformers/_internal/style_score_definitions.py).
2. **현재 기본 샤프는 무위험수익률 0 기준**: `backtest_service._summary`는 일수익률 평균을 표준편차로 나누고 `sqrt(252)`를 곱한다. 현지 무위험수익률을 차감하지 않는다. 따라서 앱 샤프와 통상적인 초과수익 기준 샤프를 따로 표시해야 한다. [백테스트 요약 구현](../../api/service/backtest_service.py).
3. **생존 편향이 완전히 제거된 데이터가 아님**: 같은 서비스는 상장폐지 종목 이력이 불완전하다는 경고를 제공한다. 현재 `is_active` 필터를 사용하지 않는다는 사실만으로 생존 편향 제거가 성립하지 않는다. [백테스트 서비스](../../api/service/backtest_service.py).
4. **거래소·업종 시점**: 공통 유니버스 문서는 거래소가 현재 분류이며 과거 이전상장 이력을 복원하지 않는다고 명시한다. 시가총액은 신호일 값이다. 따라서 역사적 KOSDAQ/NYSE 전용 결과를 완전한 시점별 분류 결과로 부르면 안 된다. [공통 투자 대상 필터](../universe-filters.md).
5. **수익률 조정 여부**: 가격 조회와 일수익 계산은 `close`를 사용한다. 이것만으로 공급자의 `close`에 분할·배당 조정이 포함됐는지 알 수 없다. 원천 정규화 계약을 확인하기 전 결과를 배당 재투자 총수익이라고 표현하지 않는다. [백테스트 쿼리](../../api/repository/backtest_query.py).

## 성과를 인정하기 위한 검증 설계

아래 숫자·절차는 이번 탐색의 제안이며 문헌이 보장하는 통과 기준은 아니다.

1. **탐색 모집단을 먼저 고정**한다. `lab_` 접두사를 제외한 전체 카탈로그에서 시장 적용 가능성, 신호 의미, 재무 기준, 중복, 연도별 유효 셀 비율, 최초/최종 가용일을 기록한다. 낮은 커버리지 때문에 빠진 팩터도 이유와 함께 남긴다. 결측을 0이라는 재무 사실로 만들지 않는다. [Arcana 도메인 계약](../../CONTEXT.md), [PIT·커버리지 ADR](../adr/ADR-0002-semantic-v5-pit-coverage-and-rebuild.md).
2. **공개 시점과 수정본을 구분**한다. 신호일 이전 공개된 재무·컨센서스 관측본만 사용하고 다음 실제 거래 가능 시점에 체결한다. 회계기간 말일은 공개일이 아니다. 오늘 재작성한 과거 스냅샷이라는 이유만으로 진정한 당시 관측본이 되지는 않는다. SEC의 공시 이력과 XBRL 기간은 별개 정보이며, Arcana도 Financial Availability Date를 별도 개념으로 정의한다. [SEC EDGAR API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces), [Arcana 도메인 계약](../../CONTEXT.md).
3. **연속 시계열을 훈련·검증·최종 보류 구간으로 나눈다.** 가중치·부호·포트폴리오 수·리밸런싱 주기·필터는 훈련에서만 선택한다. 미래 결과 기간이 경계를 넘어가는 표본을 경계에서 제거한다. 워크포워드 각 구간의 판단은 그 시점까지의 정보로만 한다. 이미 여러 기존 전략을 최적화한 기간은 새로운 독립 최종 검증이라고 부르지 않는다.
4. **양국을 각각 통과시킨다.** 공동 후보는 두 시장의 비용 차감 후 CAGR와 샤프를 나란히 제시한다. 한 국가 최고 수익이 다른 국가 실패를 덮지 않도록 `min(US Sharpe, KR Sharpe)`를 제약으로 두고, 그 안에서 양국 CAGR의 낮은 값 또는 평균 로그성장률을 높이는 소수의 후보를 비교한다. 이는 전역 최대 수익률을 증명하는 최적화가 아니라 선언된 후보 집합 내 선택이다.
5. **경제적 묶음 단위로 조합 수를 제한**한다. 동일 개념은 묶음 내 평균 순위를 만든 후 가치·질·모멘텀 등 묶음 간 가중치를 제한적으로 비교한다. 단일 팩터 최상위 결과를 마음대로 조합하면 무신호 팩터에서도 그럴듯한 결과가 만들어진다. [Novy-Marx (2015)](https://www.nber.org/papers/w21329).
6. **시도 횟수 전체를 기록하고 선택 편향을 평가**한다. 최종 성공 후보만 기록하지 않는다. 팩터·부호·가중치·필터·시작일·보유종목 수를 바꾼 시도도 탐색 수에 포함한다. 여러 팩터를 검정하는 상황에서 통상적인 t값 2만으로 충분하다는 주장은 약하다. [Harvey, Liu & Zhu (2016)](https://people.duke.edu/~charvey/Research/Published_Papers/P118_and_the_cross.PDF).
7. **샤프 1 초과와 그 신뢰도를 분리**한다. 비용·현지 무위험수익률 차감 후 일별 초과수익 기준 샤프, 표본 길이, 왜도·첨도, 최대낙폭, 시장별 연도 성과를 낸다. 시계열 상관을 고려한 블록 재표집 구간 추정과 선택 횟수를 고려한 DSR 등을 보조적으로 사용한다. DSR은 선택·비정규성 보정 통계이며 그대로 투자 샤프 값이 아니다. 반복해서 확인한 보류 구간은 선택 편향을 제거하지 못한다. [Bailey & López de Prado (2014)](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf).
8. **실현 가능성 스트레스를 적용**한다. 리밸런싱 비용과 회전율, 현금 비중, 종목 수, 작은 종목 집중을 제시한다. 예를 들어 기본 비용의 2배, 상위 수익 기여 몇 종목 제외, 보유 수·가중치의 작은 변경, 체결 하루 지연에도 결론이 유지되는지 본다. 상장폐지·장기 거래정지의 손익은 누락 후 0수익으로 취급하지 않는다. CRSP도 상장폐지 수익을 별도 데이터·합성 수익으로 다룬다. [CRSP 수익 합성 정의](https://www.crsp.org/wp-content/uploads/appendix/FlagType_AR.html).
9. **벤치마크와 팩터 노출을 분리**한다. 미국 FF5+모멘텀 회귀는 시장·스타일 노출 파악에 유용하지만 한국 수익을 미국 요인에 회귀해 한국 알파를 입증하지 않는다. 현재 저장소의 Newey–West 진단은 선택 편향을 보정하지 않는다고 스스로 명시한다. [Kenneth French Data Library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html), [Arcana 진단 구현](../../scripts/factor_lab_research_diagnostics.py).

최종 보고에는 시장별 사용 기간·커버리지·시도 수·앱 샤프와 초과수익 샤프·CAGR·MDD·비용·생존/PIT 제한을 함께 보존한다. 제약을 통과한 후보가 없으면 “검증된 전략 없음”이 정직한 결과이며, 데이터가 허용하는 최고 후보와 어떤 조건이 미달했는지를 분리해서 제시한다.

## 추가 확인: KRX 일별 등락률로 만든 가격 진단 지수

**`ChangesRatio`의 누적 곱은 액면분할 불연속을 제거하는 보조 가격 지수로 사용할 근거가 있지만, 모든 기업행동을 반영한 투자자 총수익으로 검증된 것은 아니다.** 아래 결론은 2026-09-09에 공식 설명과 수집 소스, 삼성전자 분할 경계의 소수 행을 확인한 결과다. 대규모 데이터를 재처리하거나 성과를 재계산한 결과는 아니다.

marcap 원 프로젝트는 KRX 전종목 시세를 날짜별로 모은 데이터셋이며 `ChagesRatio`를 전일대비 등락률로 설명한다. 현재 수집 코드는 `MDCSTAT01501` 응답의 `TDD_CLSPRC`를 `Close`, `CMPPREVDD_PRC`를 `Changes`, **`FLUC_RT`를 `ChangesRatio`로 그대로 매핑**한다. marcap에서 원시 종가 차이로 등락률을 재계산하지 않는다. Arcana도 `ChangesRatio` 또는 이전 철자 `ChagesRatio`를 `등락률`로 복사한다. [marcap README](https://github.com/FinanceData/marcap/blob/master/README.md), [원 수집 코드](https://github.com/FinanceData/marcap/blob/master/krx_marcap.py), [Arcana 정규화 코드](../../engine/extractors/_internal/marcap_market_prices.py).

KRX는 통상 전일 종가를 당일 기준가격으로 쓰지만, 유상·무상증자, 주식배당, 분할·병합 때는 이론 기준가격을 조정한다고 설명한다. 반면 이론가격을 정하기 어려운 감자, 기업분할 이후 재상장·변경상장 등에서는 당일 시초가로 기준가격을 새로 정할 수 있다. **그런 재설정일의 기준가 대비 변동만 이어 붙이면 기존 투자자가 거래정지 이전부터 입은 손실·권리 변화를 누락할 수 있다.** 후자의 투자자 손익 누락 가능성은 공식 기준가격 계약에서 도출한 추론이다. [KRX 기준가격·시가기준가 공식 설명](https://regulation.krx.co.kr/contents/RGL/03/03010100/RGL03010100T6.jsp).

삼성전자 공식 공시는 50:1 분할과 신주 거래일 2018-05-04를 확인해 준다. 로컬 [삼성전자 KRX 원시 파일](../../data-lake/bronze/krx/price/kr_005930.csv)의 마지막 분할 전 종가는 2,650,000원, 분할 후 종가는 51,900원이다. 원시 종가 수익은 `51,900 / 2,650,000 - 1 = -98.0415%`지만, 분할 기준가격은 `2,650,000 / 50 = 53,000`원이므로 비교 가능한 가격 변화는 `51,900 / 53,000 - 1 = -2.07547%`이다. 저장된 등락률 `-2.08%`와 반올림 범위에서 일치한다. 따라서 **이 경계에서 원시 종가 수익은 잘못된 투자 손익이며, 거래소 등락률은 분할 후 단위를 반영한다.** [삼성전자 2018-04-20 분할 확정 일정](https://www.samsung.com/global/ir/reports-disclosures/public-disclosure-view.71265/).

연구용 진단은 `r_t = ChangesRatio_t / 100`, `I_t = I_(t-1) × (1 + r_t)`로 구성할 수 있다. 이를 **“KRX 보고 등락률을 연결한 기준가격 조정 가격 지수”**라고 표시한다. 이 해석은 원 데이터 필드·기준가격 제도·위 경계 사례를 결합한 연구상 추론이며, 거래소가 해당 누적 지수를 투자 성과로 인증했다는 뜻은 아니다. 구현 시 적용 범위는 다음과 같다.

- 단순 분할·병합에서는 변경된 주식 수를 반영한 가격 비교가 가능하다. 유상증자 권리락은 청약 현금 납입·권리 매각 등 투자자 선택이 있으므로 연결 가격 지수와 실제 보유 자산 손익을 동일시하지 않는다.
- 감자·분할·합병·재상장·장기 거래정지 후 기준가격 재설정은 별도 기업행동 증거가 필요하다. 새 시초가 대비 수익을 과거 주주의 전체 손익으로 이어 붙이지 않는다. 신규상장일의 공모가 대비 등락률도 아직 보유하지 않은 전략의 수익으로 포함하지 않는다.
- 일별 등락률은 반올림되므로 장기 누적 오차가 생긴다. 원본 `Changes`의 부호 및 `Close - Changes`가 기준가격에 해당함을 검증한 행에서는 `Changes / (Close - Changes)`가 반올림 오차를 줄이는 후보 계산식이다. 이 식 역시 양의 분모와 실제 이벤트별 비교 기준을 검증한 뒤 사용한다.
- 결측·거래정지·상장폐지를 일괄 0수익으로 대체하지 않는다. 시장별 실제 달력, 미보유 기간, 재개 이벤트를 구분하며 어떤 행을 보정·기권했는지 기록한다.
- 이 필드 연결에는 투자자의 현금 배당 수취·재투자 원장이 없다. **현금배당 재투자 총수익으로 부르지 않는다.** KRX 수정주가 설명 역시 기준가격 조정 비율을 과거 가격에 소급 적용하는 가격 비교를 설명하며 배당 재투자를 입증하지 않는다. [KRX 수정주가 설명](https://data.krx.co.kr/contents/MDC/STAT/issue/MDCSTAT202.jsp).

마지막으로 손익 패널만 보정해도 이미 잘못된 원시 종가에서 계산된 모멘텀·변동성·MDD·이동평균 신호는 남는다. 수정 가격의 시점별 비율을 사용하는 새 연구 신호를 계산하거나 원 신호를 별도 감사하기 전에는 “가격 오류가 모두 해결된 전략”이라고 결론 내리지 않는다. 이는 [가격 팩터 구현](../../engine/transformers/_internal/factor_metrics.py)의 원시 `close` 사용에서 직접 따르는 제한이다.
