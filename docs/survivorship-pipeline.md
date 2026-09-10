# 상장 이력과 상장폐지 권리 처리

2026-09-10 기준으로 다운로드·검토 자료 반영·과거 종목군·백테스트 권리 계산을 연결했다. **전체 시장의 생존편향 제거가 완료된 상태는 아니다.** 현재 DB에는 개별 원문을 검토한 미국 상장 이력 4건·기업행위 5건, 한국 상장 이력 12건·기업행위 12건이 들어 있다. 나머지 수집 자료는 확인된 사건으로 자동 승격하지 않는다.

## 실행

```powershell
& .\.venv-llama\Scripts\python.exe -X utf8 -m engine.workflows.refresh --market us --targets survivorship
& .\.venv-llama\Scripts\python.exe -X utf8 -m engine.workflows.refresh --market kr --targets survivorship
```

`python -m engine.workflows.refresh`의 `all`, `market-data` 실행도 시장자료 갱신 직후 이 단계를 실행한다. `all`에서는 팩터·스냅샷 계산 전에 복원을 마친다. 직접 호출하는 `run_refresh`도 같은 순서를 따른다. `--dry-run`은 계획만 출력한다. `--survivorship-no-download`는 저장된 원문만 사용하며, `--skip-clickhouse`는 silver 산출물까지만 만든다. 기본 비밀키 로더는 환경변수 또는 사용자별 DPAPI 저장소를 사용한다. 키는 원문 출처 URL과 보고서에 넣지 않는다.

| 옵션 | 용도 |
| --- | --- |
| `--survivorship-manifest` | 원문 해시를 고정한 검토 목록. 기본값 `data-lake/meta/survivorship/{market}_reviewed.json` |
| `--survivorship-output` | 기본값 `data-lake/silver/survivorship/{market}` |
| `--survivorship-panel-dir` | 별도 검증용 팩터 입력 가격 경로. 미지정 시 기존 수정주가 경로 사용 |
| `--survivorship-source-dir` | 다운로드 원본 저장 경로 |
| `--survivorship-start-date` | DART 과거 조회 시작일. 미지정 시 최근 완료일과 7일을 겹쳐 조회하며, 최초 실행은 종료일 이전 31일부터 조회 |

미국은 Alpha Vantage `LISTING_STATUS`의 `active`, `delisted`를 함께 받는다. 한국은 OpenDART `list.json`의 I003·I001·B001·E003을 페이지 끝까지 조회하고 관련 공시의 `document.xml` 응답을 보존한다. 문서를 제공하지 않는 `014` 응답도 원문·해시를 저장한다. 실제 접수일 `rcept_dt`와 접수번호를 구분한다. 계획·정정·보류·종료 공시는 검토 대기 후보로 남는다. 공식 규격: [Alpha Vantage 목록 API](https://www.alphavantage.co/documentation/#listing-status), [OpenDART 공시검색](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001).

검토 파일이 없으면 `summary.json`에 `awaiting_review`를 기록하고 기존 확정 자료를 유지한다. 원문 해시 불일치·중복 사건·유효하지 않은 날짜는 반영을 중단한다. 과거 일괄 다운로드는 이번 실행 이전에 확보된 자료를 재사용한다. 이 단계 자체가 과거 전 종목의 가격·재무자료를 자동 복구했다고 의미하지 않는다.

## 데이터와 계산

- `security_listing_episodes`: 증권 ID·발행사·거래소·상장 구간·근거 공시일. 종료일은 첫 비편입일로 처리한다. 확인된 과거 증권은 현재 `security_master`에서 사라졌어도 종목군에 들어갈 수 있다.
- `security_lifecycle_events`: 현금합병·현금 교환(`cash_exchange`)·주식교환·무대가 소각·상장폐지의 실제 처리일과 권리 완결 여부. 법적 효력일과 일별 가격에 적용할 첫 세션이 다르면 별도로 남긴다. TWTR의 법적 종결일은 2022-10-27, 일별 처리일은 거래중단일인 2022-10-28이다.
- `security_lifecycle_entitlements`: 받을 증권의 ID·단위·수량·인도일·거래 가능일(`tradable_date`). `source_ids`와 원문은 별도 보존된다.

DB는 변경 이력을 추가로 저장하고 시장별 완료 표시를 마지막에 기록한다. 중간 실패는 직전 정상 자료를 계속 노출한다. 같은 자료의 재실행은 중복을 만들지 않으며 다른 시장을 덮어쓰지 않는다. 세 공개 테이블은 확정된 최신 시장별 자료를 읽는 뷰다.

상장·사건 날짜의 DB 투영은 `Date32`를 사용한다. 기존 `Date` 투영에서 1956년 상장일이 1970년으로 바뀌는 문제를 수정했으며, 자료가 동일한 재실행에서도 기존 뷰를 갱신한다. 현재 지원 범위인 1900~2299년 밖의 날짜는 조용히 변경하지 않고 적재를 거부한다. 원본 날짜는 JSON 원장에 그대로 보존한다.

검토한 미국 4종목(ATVI·TWTR·CELG·ALXN)의 Alpha Vantage 가격 18,796행을 수정주가 입력 패널과 `price_daily`에 연결했다. 원문은 보존하고 확인된 거래소 상장 구간 밖의 6행은 계산 패널에서 제외했다. 배당까지 반영한 공급자 수정종가는 별도 보존하며, 수익률 입력은 분할만 조정한다.

같은 발행사의 SEC DEI 발행주식수 공시 180건을 연결했다. 공시 시각을 모르면 접수 다음 날부터 사용하고, 측정일 이후 분할을 주식수에 적용한다. EPS 가중평균 주식수는 시총 분모로 사용하지 않는다. 확인된 공시 이전의 주식수는 결측으로 남는다. 이 방식으로 계산 가능한 시총은 11,059행이며, `market_cap_factors.parquet`와 `fact_daily_factors`의 `mcap_mil`·`annual`·`USD`로 기록된다. 2017년 이후 연구 구간에는 5,043행이 있다.

미국 4종목의 재무자료도 정규 파이프라인에 연결했다. SEC Company Facts 4개와 CELG의 실제 공시 묶음 12개(주요 문서 12개, XBRL 파일 72개)를 사용해 8,369개 항목을 정규화했다. 원문 직접 대조 8,242개와 파생식 대조 127개에서 값·공시일 불일치가 없었으며, 확인 가능한 177개 결산기간의 대차식도 일치했다. 증권별 CIK 별칭과 공시 묶음은 기본 SEC 경로에 저장되므로 이후 일반 정규화 실행에서도 재사용된다. 공시 원문 묶음이 있는 기간은 기존 정책대로 Company Facts보다 우선한다.

자본총계는 비지배지분을 포함한 계정을 우선하도록 `semantic_us_v3.arcana`에 수정했다. 이전 v2 파일과 매니페스트는 보존한다. CELG는 기타포괄손익 누계액 주석에 자본총계 태그를 사용한 기간이 있어 태그 우선순위만으로는 잘못 선택됐다. 새 규칙은 XBRL의 실제 표시 역할이 해당 주석으로만 제한된 태그를 제외한다. 금액을 대차식에 맞춰 교정하지 않는다. [CELG 2016년 10-K 원문](https://www.sec.gov/Archives/edgar/data/816284/000081628417000003/a2016123110k.htm), [정규 저장 후 원문 대조](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/us_v3/published_value_source_reconciliation.json).

검토한 과거 종목의 일반 팩터 적재 경로는 재무 가용일을 공시일보다 최소 하루 늦춘다. 시각을 확인하지 않은 공시가 당일 신호에 유입되지 않게 하며, 공시일 자체는 원래 날짜로 보존한다. `financial_availability_delay_days`는 기본 0이고 검토된 과거 종목은 적재기가 최소 1을 적용한다. 파생 ROE·컨센서스 비교 등 같은 계산의 후속 결합에도 이 지연을 적용한다.

2017년 이후 미국 4종목에 재무 팩터 `asset_turnover`, `gpm`, `gross_profitability_pct`, `npm`, `opm`, `roa`, `roe`를 총 33,973행 적재했다. 모든 행의 공시 후 가용일을 검사하고 순이익률 5,043행을 원문 계정으로 별도 대조했다. `percent_total_accruals_pct`는 필요한 근거가 부족해 이번 복원에서는 결측이다. 스타일 점수는 계산하지 않았다. 실제 FactorLab에서 2017-01-03 시총 상위 70% 조건을 먼저 적용한 순이익률 입력 1,172개 중 복원한 4종목 모두 유효했고, 저장한 값과 일치했다. 이는 입력 검증이며 전략 성과 재평가가 아니다. [팩터 적재 검증](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/us_v3/factor_validation.json), [FactorLab 실제 조회](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/us_v3/factorlab_verification.json).

자본총계·공시 범위 관련 테스트 54개와 재무 가용일·팩터 관련 테스트 74개가 통과했다. [SEC 규칙 검증](../deliverables/cross_market_top70_20260909/survivorship/us_equity_v3_source_scope_tests.xml), [재무 가용일 검증](../deliverables/cross_market_top70_20260909/survivorship/historical_financial_availability_tests.xml).

한국 과거 재무자료는 DART로 발행사를 확인한 12종목의 보관 공시 652건을 별도 원장으로 정리했다. 원문 해시, 공시 본문의 발행사 코드, 수집 인덱스의 종목·기간·접수일이 일치했고, UTF-8은 114건, CP949는 538건이었다. 정정 보고서를 원 보고서와 분리하고 공시 다음 날을 가용일로 기록했다. 원문을 확보하지 못한 25건(공급자 미제공 24건, 수집 오류 1건)은 결측 상태로 남긴다. 이 검증은 원문의 식별과 보존에 관한 것이며 재무 수치의 의미 검증이나 팩터 적재 완료를 뜻하지 않는다. [한국 재무 원문 원장](../deliverables/cross_market_top70_20260909/survivorship/KR_reviewed_financial_source_manifest.json).

일반 DART 정규화 함수는 보관된 전체 OpenDART 문서도 읽는다. 손실 없는 UTF-8·CP949 디코딩 후 연결 재무제표를 우선 선택하고, 연결 작성 대상이 아니라는 명시적 근거가 있으면 일반 재무제표를 선택한다. 주석은 동일 범위를 사용한다. 원문 들여쓰기와 `&cr;` 줄바꿈을 보존하며, 주석 번호 열을 금액으로 읽지 않고 당기 상세·소계 열을 구분한다. 감사 CSV에는 원문 해시·인코딩·선택 구역·연결/일반 범위를 남긴다. 새 판독기 변경도 정규화 캐시의 재생성 조건에 포함된다.

동일 접수번호의 보관 원문과 DART 뷰어를 9건 대조했다. 7건은 계정명 공백 차이를 제외하면 모든 정규화 값이 일치했다. 우리은행의 `20190401003520`은 두 원문의 줄 구분 차이로 결과가 달라 추가 검토 대상이다. 메리츠화재의 `20230331004390`은 ZIP 원문 자체에서 여러 금액이 붙어 있어 금액 판독을 거부하며, 분리가 유지된 같은 접수번호의 공식 뷰어를 별도 정규화했다. 숫자를 추측해 나누거나 0으로 채우지 않는다. 관련 127개 테스트가 통과했다. 한국 재무 팩터는 이번 단계에서 새로 적재하지 않았다. [원문 대조](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr/raw_adapter_final_validation/reconciliation.json), [테스트 결과](../deliverables/cross_market_top70_20260909/survivorship/kr_raw_adapter_tests.xml).

이후 OpenDART의 `TE` 셀, 당기 상세·소계 열, 구형 `XI. 재무제표 등` 장의 XBRL 묶음·수기 표를 처리했다. 명시된 연결·별도 표제와 주석 경계를 구분한다. 전체 652건의 v3 처리 결과는 정규화 460건, 검토 필요 95건, 사용 가능한 항목 없음 97건이다. 표준 계정 22,854행에서 저장값·정렬 경고는 1행으로 줄었다. 대차식은 397건 통과·62건 판정 불가·1건 검토 필요다. 이 결과는 계정 의미 승인이나 전체 이력 복원 완료를 뜻하지 않는다. [v3 처리 결과](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr_receipt_history_v3/normalization_report.json), [값 점검](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr_receipt_history_v3/stored_value_audit.json).

기본·희석 EPS의 소수점을 보존하고, `((-)447,926,078)`도 실제 음수로 읽는다. 비어 있거나 수치가 아닌 표시는 표준 계정의 0으로 만들지 않는다. 이 수정으로 이전 6개 공시의 잘못된 0 항목을 제거했다. 금액 열 정렬 검사는 완전히 빈 대체 열만 제외하고 일부 금액이 있는 짧은 열은 계속 검토 대상으로 남긴다. 우리은행 `20170331004833`의 이익잉여금 정렬과 KB캐피탈 `20160816001044`의 원문 대차식 40원 차이는 해결되지 않았다. 본문 금액을 요약표나 잔차에 맞춰 고치지 않는다. [원문 불일치](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr_source_reviews/021960_20160816001044_balance.json).

제이테크놀로지(035480)는 외부 주석을 둔 구형 문서 경계를 추가 수정한 v4로 68건을 재처리했다. 50건에서 정규화 항목을 얻었고 7건은 검토 필요, 11건은 사용 가능한 항목 없음이다. 2,723개 표준 계정에 저장값 경고는 없고 대차식 49건 통과·1건 판정 불가였다. v4는 이 종목만 처리한 결과이며 전체 652건 결과와 합산하지 않는다. v3와 v4 각각 판독 코드·규칙 파일 34개의 원본과 해시를 보존했다. [v4 결과](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr_receipt_history_v4_jtech/normalization_report.json).

이 종목의 실제 표제·당기 기간·원화 단위·연결 범위·개별 원문 금액을 검토해 40개 공시의 802개 항목을 운영 이력에 등록했다. 승인 계정만 담은 CSV와 원문, 검토 기록, 실제 DART 조회 응답을 함께 보관한다. 나머지 항목은 미검토 상태다. 2019년 2·3분기에는 매출을 확인하지 못했지만 확인된 다른 계정을 등록해 이전 매출을 그대로 유지하지 않게 했다. [계정별 원문 검토](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr_receipt_history_v4_jtech/reviewed/035480/source_review.json), [운영 등록 기록](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr_receipt_history_v4_jtech/reviewed/035480/production_publication.json).

재무 입력 판독기는 `financial_dir/history/<종목코드>/manifest.json`을 읽는다. 연간·분기·TTM은 각 공시 시점까지 알려진 자료로 계산하며, 과거 연도 정정이 최신 결산기간을 과거로 되돌리지 않는다. 빠진 회계기간은 결측으로 남긴다. 연결·별도 범위 또는 회계기준이 바뀌면 해당 경계 이전 자료로 성장률·평균자산·TTM을 만들지 않는다. 이후 정정으로 비교 가능한 과거 자료가 확인되는 경우 그 정정 이후 계산에서만 사용한다. 실제 40개 공시로 연간 공시 이벤트 10개, 분기·TTM 각각 37개가 생성되며 동일 날짜에 제출된 여러 공시는 그 날짜의 최신 정보로 묶인다. [연간·분기·TTM 검증](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr_receipt_history_v4_jtech/reviewed/035480/staging_validation.json).

검토 이력 등록은 일반 새로고침 명령에서 별도 대상으로 실행한다.

```powershell
& .\.venv-llama\Scripts\python.exe -X utf8 -m engine.workflows.refresh --market kr --targets financial-history --financial-history-review <검토 JSON>
```

`--financial-history-review`는 여러 번 지정할 수 있고 `--financial-history-output`으로 검증용 목적지를 선택할 수 있다. 기본 목적지는 `data-lake/silver/dart/normalized`이다. `--dry-run`은 파일을 등록하지 않는다. 이 대상은 검토 이력만 등록하며, 영향을 받은 과거 구간의 팩터 재계산은 후속 단계다. 미검토 수집 자료를 `all` 실행에서 자동 승인하지 않는다. 개별 함수 `publish_reviewed_financial_history`와 모듈 명령도 사용할 수 있다.

등록기는 발행사·실제 기간·연결 범위·확인된 회계기준·승인 계정·원문 해시·검토 근거를 요구한다. 공시일이 접수번호 앞의 날짜보다 늦을 수 있으며, 이 경우 보관된 OpenDART `list.json`의 해당 접수번호·법인 코드·`rcept_dt`와 일치해야 한다. 실제 `20170202000370`의 공시일은 2017-02-03이다. 변경된 원문·미검토 자료·기존 승인 접수번호의 내용 교체는 거부한다. 원본·정정은 각각 보존하고 반복 등록은 중복을 만들지 않는다. 파일 검증과 병합 후 마지막에 매니페스트를 교체한다. 새로고침·등록·공시 가용일 관련 61개 테스트가 통과했다. [검증 결과](../deliverables/cross_market_top70_20260909/survivorship/kr_history_refresh_tests.xml).

운영 등록과 동일한 자료에서 2017-01-02~2019-12-11의 721거래일, 순이익률·영업이익률·매출총이익률·총자산 대비 매출총이익률 2,884행을 계산했다. 모든 값과 가용일을 검토된 원문 금액으로 별도 계산해 대조했다. 비교 가능한 연속 연간 재무상태표가 부족한 ROA·ROE·자산회전율은 결측으로 유지했다. 이 검증만으로 전체 한국 재무 팩터 복원 완료를 뜻하지 않는다. [일별 팩터 대조](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr_receipt_history_v4_jtech/reviewed/035480/daily_factor_validation.json).

실제 DB에 위 2,884행을 적재한 뒤 월별로 모든 식별자·값을 대조했다. 검증기의 날짜 해상도·문자열 자료형 차이를 수정한 재검증은 추가 적재 0행으로 통과했다. 2017-01-03 팩터랩에서 시총 관측 1,769종목의 상위 1,239종목을 먼저 정한 뒤 순이익률 120종목이 유효했다. 복원한 035480은 시총 933위이며 순이익률 -8.8742667259%가 원문 기준 값과 일치했다. [DB 전체 대조](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr_receipt_history_v4_jtech/reviewed/035480/native_factor_publication.json), [실제 팩터랩 조회](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr_receipt_history_v4_jtech/reviewed/035480/factorlab_verification.json).

2016년 사업보고서 원본·정정본의 “해당사항이 없습니다”도 연결 작성 비대상 표현으로 읽도록 수정했다. v5에서 제이테크놀로지 68건 중 52건이 정규화됐고, 계정 검토를 통과한 공시는 42건·840개 항목으로 늘었다. 기존 승인 40건의 값과 근거는 유지했다. **현재 운영 이력은 v5의 42건**이며 연간 이벤트 12개, 분기·TTM 각각 39개를 읽는다. 관련 판독 테스트 31개가 통과했다. [v5 원문 검토](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr_receipt_history_v5_jtech/reviewed/035480/source_review.json), [판독 테스트](../deliverables/cross_market_top70_20260909/survivorship/kr_non_applicability_tests.xml).

복원된 2016년 자료의 가용 구간인 2017-04-03~2018-04-12에서 기존 이익률 1,004개를 갱신하고 ROA·ROE·자산회전율 753개를 추가했다. 현재 721거래일의 유효한 재무 팩터는 7종류·3,637개이며, 전체 DB 값과 날짜를 다시 대조했다. ROA·ROE·자산회전율은 비교 가능한 251거래일에만 존재한다. [차이만 반영한 기록](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr_receipt_history_v5_jtech/reviewed/035480/native_factor_publication.json).

자료 복원이 시총 조건을 무시하게 하지는 않는다. 실제 팩터랩에서 2017-03-31과 2017-04-03의 제이테크놀로지는 각각 시총 1,320위·1,329위로, 전체 1,781종목의 상위 1,247개 밖에 있어 제외됐다. 저장된 순이익률은 공시 전 -8.8743%에서 공시 후 -91.1309%로 바뀌지만, 해당 날짜의 전략 후보에는 들어가지 않는다. [상위 70% 제외 검증](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr_receipt_history_v5_jtech/reviewed/035480/factorlab_verification.json).

가격·시총 적재는 경제적 값의 해시를 기록하고 동일 값은 다시 입력하지 않는다. 검토 메모나 실행 시각만 바뀌어도 중복 적재하지 않는다. 기본 팩터 계산 대상에도 검토된 과거 종목을 포함한다. 이 종목의 재무 결측을 현재 티커 조회로 채우지 않으며, 정규화된 재무자료는 공시 가용일이 있어야 사용한다. 시총 적재만으로 다른 원시 팩터와 스냅샷의 재계산 완료 표시를 바꾸지 않는다. DB 시장별 원장 공개는 완료 표시로 제어하지만 가격·팩터 테이블의 여러 삽입을 하나의 DB 트랜잭션으로 묶는 구조는 아니다.

매 신호일의 상장 구간과 같은 날 시총을 먼저 적용하고, 종목 수 기준 `ceil(N × 70%)`를 정한 뒤 팩터 결측·점수 순위를 적용한다. `universe_summary`에는 날짜별 `listing_verified_count`, `listing_unverified_count`, 시총 결측 수가 들어간다. 확인된 이력이 없는 현재 종목은 기존 자료로 계산하는 상태가 명시된다. 업종 분류는 여전히 현재 분류다.

상장 구간은 실제 상장·주식교환·상장폐지의 날짜로 판단한다. 이후 제출된 보고서가 과거의 실제 상장을 확인할 수 있으므로, 보고서 접수일을 상장 시작일로 사용하지 않는다. 근거 접수일은 감사 추적용으로 보존한다. 재무 팩터의 공시 가용일 제한은 별도로 유지한다.

백테스트는 매 리밸런싱을 넘어 보유 주식·현금·현금 미수금·미입고 또는 거래가 제한된 주식 권리를 유지한다. 확인된 현금 지급일 전에는 그 미수금으로 새 종목을 매수하지 않는다. 주식 입고 전 분할도 받을 권리의 가치에 반영한다. 입고했어도 해당 주식의 거래 가능일 전에는 매도·재투자하지 않는다. 날짜 제한이 없는 기존 자료는 이전 인도일 기준을 유지한다. `portfolio_history`에 실제 보유 내역과 권리가 반환된다. 가격 입력은 기존 파이프라인의 **주식분할만 조정한 종가** 기준이다.

확인된 사건 이후의 공급자 잔존 가격을 소멸한 주식의 거래 가격으로 사용하지 않는다. 사건 당시 보유 주식 또는 받을 주식 권리가 남아 있고 대가가 미확정이거나 평가할 수 없으면 검증된 수익률 계산을 중단한다. 사건 전에 실제 매도한 종목은 이후 미확정 대가 때문에 계산을 막지 않는다. 거래정지로 매도할 수 없었던 보유분은 계속 확인 대상으로 남긴다. ALXN의 ADS·CELG의 CVR 및 단주 정산, 035480의 비상장 잔존 지분은 0으로 바꾸지 않는다. 현금 지급일이 확인되지 않은 ATVI·TWTR 대가는 미수금으로 남는다.

## 검증 범위와 남은 작업

공개 실행 경계에서 원문 훼손 거부, 접수일 보존, `014` 응답 보존, 재실행, 시장별 DB 반영, 실패 시 이전 자료 유지, 과거 종목 복원, 시총 상위 70% 적용 순서, 현금/주식 권리와 분할을 검증한다. 테스트 위치는 `tests/test_survivorship_workflow.py`, `tests/test_survivorship_backtest.py`다. 실제 DB 테스트는 세션 임시 테이블 또는 고유 접두어의 테스트 전용 테이블을 사용한다.

받은 주식에 다시 합병이 일어나면 그 후속 사건도 조회하며, 입고 전 권리에도 적용한다. 같은 세션에 여러 사건이 발생해 선후관계를 알 수 없으면 별도 검토를 요구한다.

전체 완료에는 나머지 과거 증권의 식별·상장 구간, 심볼 재사용, 당시 가격·재무·시총 자료와 실제 거래 가능일 연결이 필요하다. 정지 중 분할과 사건의 주당 기준, 단주 정산·조건부 권리의 실제 가치, 업종 분류의 과거 이력도 추가 검증 대상이다. 기존 고정 전략과 2024년 이후 평가 구간은 바꾸지 않았고, 양국 샤프 1 초과를 달성했다고 주장하지 않는다.

2026-09-10 실제 입력 검증: 2017-01-03 미국 시총 입력이 있는 2,048종목에서 상위 70%인 1,434종목이 팩터랩 그래프에 유효하게 들어갔다. 복원한 ATVI·TWTR·CELG·ALXN은 모두 포함됐으며, 별도 날짜별 조회에서는 각각의 확인된 거래중단 이후 제외됐다. 이 숫자는 전체 당시 상장종목 수나 전체 자료 완전성을 뜻하지 않는다. [팩터랩 입력 검증](../deliverables/cross_market_top70_20260909/survivorship/US_reviewed_factorlab_input_verification.json), [날짜별 모집단 검증](../deliverables/cross_market_top70_20260909/survivorship/US_reviewed_top70_membership_verification.json)

같은 4종목에서 스타일 점수를 제외한 가격·거래량 팩터 32개를 기존 공개 팩터 로더로 계산해 2017년 이후 161,376행을 반영했다. 12개월 모멘텀 5,043행은 수정주가로 별도 계산한 값과 대조했다. 재무 팩터·전체 시장 복원 완료를 의미하지 않는다. [가격 팩터 반영 기록](../deliverables/cross_market_top70_20260909/survivorship/reviewed_price_factors.json)

가격 팩터 DB 반영 후 `tr_12_1` 팩터랩 그래프도 실제 실행했다. 시총 상위 70%를 먼저 적용한 1,434종목 중 모멘텀 값이 유효한 종목은 1,394개였으며, 복원한 4종목은 모두 유효했다. [모멘텀 연결 검증](../deliverables/cross_market_top70_20260909/survivorship/US_reviewed_momentum_factorlab_verification.json). 전량 매도한 종목의 미확정 권리로 계산이 중단되는 오류를 수정한 뒤 권리 백테스트 14개가 통과했다.

## 한국 원문 검토와 입력 복원

상장 이력 근거는 DART 원문으로만 확정한다. 이후 보고서 419건(223개 발행사)을 확보하고 원문 해시를 대조한 뒤 검토 문단 색인을 만들었다. 회계기간 이후 사건을 확인할 수 있는 보고서와 정정본도 수집했다. 색인의 문장은 예정 일정·자회사·다른 종류의 주식에 관한 내용일 수 있으므로 그대로 사건으로 승격하지 않는다. [검토 색인](../deliverables/cross_market_top70_20260909/survivorship/KR_listing_evidence_index.json)

| 코드 | 검토된 상장 시작 | 기존 주식의 거래소 입력 종료 | 확인된 권리 |
| --- | --- | --- | --- |
| 000030 우리은행 | 2014-11-19 재상장 | 2019-01-11 주식이전 | 우리금융지주 보통주 1주 |
| 000060 메리츠화재 | 1956-07-02 | 2023-02-01 주식교환 | 메리츠금융지주 보통주 1.2657378주 |
| 002550 KB손해보험 | 1976-06-23 | 2017-07-07 주식교환 | KB금융지주 보통주 0.5728700주 |
| 003410 쌍용C&E | 1975-05-03 | 2024-06-25 현금 교환 | 주당 7,000원 미수금 |
| 008560 메리츠증권 | 1992-01-15 | 2023-04-05 주식교환 | 메리츠금융지주 보통주 0.1607327주 |
| 010050 우리종합금융 | 1974-09-11 | 2023-08-08 주식교환 | 우리금융지주 보통주 0.0624346주 |
| 021960 KB캐피탈 | 1996-11-19 거래소 상장 | 2017-07-07 주식교환 | KB금융지주 보통주 0.5201639주 |
| 033660 우리금융캐피탈 | 2009-06-25 | 2021-08-10 주식교환 | 우리금융지주 보통주 1.0567393주 |
| 035480 제이테크놀로지 | 1999-11-18 | 2019-12-12 상장폐지 | 비상장 지분 존속, 평가 미확정 |
| 079440 오렌지라이프 | 2017-05-11 | 2020-01-28 주식교환 | 신한금융지주 보통주 0.6601483주 |
| 192530 광주은행 | 2014-05-22 | 2018-10-09 주식교환 | JB금융지주 보통주 1.8814503주 |
| 298870 우리벤처파트너스 | 2021-12-16 | 2023-08-08 주식교환 | 우리금융지주 보통주 0.2234440주 |

표의 종료일은 첫 비편입일이다. 행정상 상장폐지일이 주식교환일보다 뒤인 경우 두 날짜를 구분한다. 주식 인도일·단주 정산이 확인되지 않은 교환은 권리 완결 여부가 `false`다. 쌍용C&E의 통상 교환대가는 확인했지만 실제 지급일은 아직 미확정이므로 매수 가능한 현금으로 바꾸지 않는다. 우리은행의 2014년 이전 티커 관측치는 이번 상장 구간에 합치지 않았다.

광주은행의 후속 DART 보고서는 교환 신주가 2018-10-25 교부되고 26일 상장됐다고 명시한다. 두 날짜를 별도 필드로 반영했다. 단주 정산은 아직 미확정이다. KB캐피탈의 1993년 장외시장 등록과 1996년 거래소 상장도 구분했다. 광주은행은 2014년 상장한 KJB금융지주가 자회사를 흡수한 후 상호를 바꾼 동일 존속 법인임을 공시로 확인했다.

기존 Marcap 가격 원본 1996~2024년 29개 파일은 게시자의 Git blob과 SHA를 대조했다. DART로 확인한 증권·거래소·상장 구간에 해당하는 행만 사용하며, 원시 종가 × 당일 상장주식수와 시총이 일치하는지 검증한다. 원본 파일의 시총 단위는 이 항등식으로 원화임을 확인했다. 증빙 출처는 [Marcap 게시자](https://github.com/FinanceData/marcap), [고정된 파일 검증](../deliverables/cross_market_top70_20260909/survivorship/KR_marcap_publisher_verification.json)이다. Marcap을 DART 상장폐지 근거로 사용하지 않는다. 미국 가격은 계속 Alpha Vantage만 사용한다.

한국 12종목의 수정주가 패널·가격 DB에 49,571행, 시총 팩터에 49,571행을 연결했다. 당일 상장주식수도 같은 패널에서 읽으므로 일반 팩터 계산 경로에 연결된다. 2017년 이후 가격·거래량 팩터 32개는 총 333,896행을 반영했고, 모멘텀 10,089행을 별도로 대조했다. 거래정지 행에서는 분할 단위를 맞춘 마지막 실제 거래가격을 유지한다. 기존 가격 이상 징후의 검토 기록은 복원만으로 지우지 않으며, 전체 가격·권리 검증 완료로 표시하지 않는다.

실제 팩터랩 조회에서 2017-01-03에는 시총이 있는 1,764종목 중 상위 1,235종목을 먼저 정하고 모멘텀이 유효한 1,182종목을 계산했다. 2018-08-01에는 1,879종목 중 상위 1,316종목을 먼저 정하고 1,262종목을 계산했다. 제이테크놀로지는 각각 시총 순위 928위·1,409위여서 첫 날짜에는 포함되고 두 번째 날짜에는 제외된다. [한국 6종목 실제 입력 검증](../deliverables/cross_market_top70_20260909/survivorship/KR_six_reviewed_factorlab_verification.json)

관련 6개 테스트 파일의 151개 테스트가 통과했다. 이후 기존 가격 검토 기록 유실을 공개 실행 테스트로 재현·수정하고 워크플로 테스트 11개를 다시 통과했다. 테스트 산출물 경로도 임시 디렉터리로 분리했다. [151개 테스트 결과](../deliverables/cross_market_top70_20260909/survivorship/implementation_tests_20260910.xml)

날짜 투영 수정 후 워크플로·권리 백테스트 28개가 통과했다. 실제 DB에서 1956-07-02 상장일과 기존 상위 70% 종목 수가 유지됨을 확인했고, 재실행 시 가격·시총 추가 삽입은 0행이었다. [날짜 수정 검증](../deliverables/cross_market_top70_20260909/survivorship/lifecycle_date32_tests_20260910.xml)

12종목 반영 후 실제 팩터랩에서도 검증했다. 2017-01-03 시총 관측 1,769종목에서 상위 1,239종목을 먼저 정한 뒤 모멘텀 1,186종목이 유효했다. 2023-01-03에는 시총 관측 2,280종목의 상위 1,596종목에서 모멘텀 1,534종목이 유효했다. 복원 종목의 점수 유효 집합이 시총 조건을 통과한 복원 종목 집합과 일치했고, 원문 상장 구간 12개와 광주은행의 인도일·거래 가능일을 DB에서 대조했다. [12종목 입력 검증](../deliverables/cross_market_top70_20260909/survivorship/KR_twelve_reviewed_factorlab_verification.json)

거래 가능일 처리 오류는 실제 백테스트 테스트에서 먼저 재현했다. 제한 중인 교환 주식을 매도해 다른 주식의 상승을 얻어 NAV가 2가 되던 결과를, 해당 주식을 유지해 NAV 1이 되는 결과로 바로잡았다. 수정 후 관련 테스트 29개가 통과했다. [거래 가능일 검증](../deliverables/cross_market_top70_20260909/survivorship/tradability_tests_20260910.xml)


2026-09-10 추가 재무 복원 검증에서는 나머지 한국 11종목의 보존 공시 584건을 처리했다. 재무 값을 추출한 공시는 415건이며, 형식 등의 검토가 필요한 공시는 79건, 사용할 값을 얻지 못한 공시는 90건이다. 이 수치를 곧바로 승인된 재무 이력으로 보지 않는다.

새 한국 규칙 v7은 연결 순이익과 지배주주 순이익, 총포괄이익을 구분하고 대손준비금 조정액이 계정 설명에 함께 있는 형식을 처리한다. 재무제표 제목 앞에 있는 다른 표의 참조 문구나 주총 예정일 때문에 표 종류·회계기간이 뒤바뀌는 경우도 바로잡았다. 3월 결산 기업의 12월 분기보고서는 원문 `누적` 열을 우선한다. 우리종합금융 2013년 12월 공시의 수익은 3개월치 20,196,725,677원이 아니라 9개월 누적 97,468,804,831원임을 원문과 대조했다. 영향 가능성이 있는 12월 분기보고서 24건을 다시 처리했다.

공시일·기간·본문 계정·단위·금액·회계기준·자산 부채 자본 항등식 검증에서는 187건의 2,935개 재무 항목이 승인 조건을 통과했다. 은행의 매출에 해당하는 본문 값을 확인하지 못한 경우 주석의 일부 수익으로 대체하지 않는다. 2011년 이전 회계기준과 12개월이 아닌 결산 전환기는 이번 승인 범위 밖이다. 운영 팩터에 반영하기 전에 연간·분기·TTM 입력과 일별 팩터를 별도로 검증한다. [원문 검토 입력과 결과](../deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot/kr_receipt_history_v7_review_input/normalization_report.json)

누적기간 수정까지 포함한 본문·기간·계정 테스트 51개와 고정 v6/v7 규칙을 포함한 회귀 테스트 29개가 각각 통과했다. 확장 실행 중 발생한 Windows 하위 프로세스 예외는 해당 모듈의 독립 실행에서 재현되지 않았다. 전체 시장의 재무 복원이나 생존편향 처리가 완료됐다는 의미는 아니다.
