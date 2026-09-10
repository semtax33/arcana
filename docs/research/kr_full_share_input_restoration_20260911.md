# 한국 주식수 입력의 전체 기간 복구

1996~2026년 원본을 대조해 정규 주식수 입력을 15,384,392행으로 복구했다. 기존 7,102,067행의 값은 그대로 유지하고 8,282,325행을 추가했다. 입력 반영과 DB·스냅샷 반영 상태는 [Gold 요약](../../data-lake/gold/survivorship/kr/share_inputs/1996_2026/summary.json)에서 구분한다. 전체 생존편향 보정은 진행 중이다.

| 구분 | 결과 |
| --- | --- |
| 등록 원본 | Marcap 연도별 31개, 고정 관측일 목록 1개 |
| 기존 값 수정·삭제 | 각각 0행 |
| 후속 재계산 대상 | 5,281개 증권 식별자 |
| 정규화 부적합 원문 관측 | 13,426행, 출처를 유지해 격리 |
| 회귀 검증 | 공개 정규화·갱신·저장 경계의 39개 테스트 통과 |
| 실제 계산 대조 | 010620의 연간·분기·TTM 주식수·시총 6개 검사 통과 |

추가 조사에서 010620의 정규 주식수 CSV가 2015년에 끝나면서 2,000만 주와 당시 시총이 2024년 계산까지 이어 쓰이는 것을 재현했다. 주가는 원본과 같았다. 같은 공개 계산 함수를 다시 실행한 결과, 복구 후 2024-01-02 값은 원본의 39,942,149주와 시총 3,411,059.5246백만원에 일치했다. 이는 기존 DB 값 전체가 잘못됐다는 의미가 아니다. 이후 2024년 전체 DB 대조에서는 존재하는 시총·주식수 값이 모두 원본과 일치했다.

원본은 `bronze/registered-market-sources/sha256={hash}`에 바이트 그대로 고정했다. 격리 파일은 Silver에 있으며 정규화 시 원문의 실제 부적합 행 전체와 키·수치·해시가 정확히 같은지 검사한다. 유효 행을 임의로 제외할 수 없다. 사용자용 [미해결 관측 목록](../../data-lake/gold/survivorship/kr/share_inputs/1996_2026/quarantined_observations.parquet)에는 원본 경로·해시·출처 URL·격리 이유가 들어 있다.

2024년 전체 687,708개 원본 관측은 실제 DB 가격과 모두 일치했다. 연간·분기·TTM의 `mcap_mil`·`csho` 누락 561,669개를 DB에 추가했다. 기존 3,564,579개 값의 회계 메타데이터와 버전 시각도 보존했으며, 합계 4,126,248개를 다시 읽어 대조했다. 사용자용 월별 파일과 반영 근거는 [2024년 시장 팩터](../../data-lake/gold/survivorship/kr/capitalization_factors/2024/summary.json)에 있다. 2024년 외의 새 입력 범위와 후속 재무 팩터는 별도 미완료 상태다.

실제 FactorLab 조회로 2024년 244거래일의 시총 모집단 619,535개 관측과 상위 70% 결과 433,780개를 원본에 대조했다. 종목 수에 `ceil(N×0.7)`을 적용하며 팩터 결측 처리보다 먼저 제한한다. [Native 조회 검증](../../data-lake/gold/survivorship/kr/capitalization_factors/2024/factorlab_verification.json)과 [스냅샷 조회 검증](../../data-lake/gold/survivorship/kr/capitalization_factors/2024/snapshot_consumer_verification.json)을 따로 제공한다. 스냅샷 조회는 244거래일 외에 15개의 기준·팩터 조합도 검증했다. 전체 시장 상장 이력은 아직 완성하지 않았다.

첫 거래일의 실제 스냅샷 조회는 처음에 59개 팩터 셀이 없고 29개 셀이 2015-12-30 원천 값을 사용했다. 같은 재현 명령으로 복구 후 여섯 조합 모두 1,736종목과 원본 수치에 일치하는 것을 확인했다. 전체 2024년 원천 값이 영향을 주는 33개월 스냅샷 5,509,782개를 DB에서 대조했으며, 1,123,512개의 키를 추가하고 820,700개를 정정했다. 이후 native 사건이 나타나는 첫 날짜에서 적용을 끝내고, 원천 날짜가 스냅샷 날짜보다 늦지 않은지 검사했다.

사용자용 [스냅샷 파일 목록](../../data-lake/gold/survivorship/kr/capitalization_factors/2024/snapshot_summary.json)에는 원천 날짜와 `is_market_trading_date`가 있다. 기존 캐시의 시장 휴일 날짜도 as-of 값으로 보존·정정한 것이며, 새로운 주가 관측이나 거래일로 해석하지 않는다. 실제 2024년 가격 달력은 원본과 같은 244거래일이다.

검증 자료의 기준일은 2026-09-10이며 실제 한국 가격 달력의 마지막 관측일은 2026-09-04다. 날짜별 시장 관측을 추가하는 것으로 발행사·상장 이력을 승인하지 않는다. 스타일 점수와 `lab_*`를 계산하거나 고정 전략·2024년 이후 평가를 재실행하지 않았다.

재현 코드는 `scripts/research/check_historical_share_observation.py`, `audit_kr_full_share_source_coverage.py`, `prepare_kr_full_share_registration.py`, `publish_kr_full_share_inputs.py`, `finalize_kr_full_share_inputs.py`에 있다. 전체 연도별 비교와 변경 전후 입력, 8,282,325개 변경 관측의 증권별 날짜·행 수 대조, 실제 계산 결과·테스트의 해시는 [최종 입력 검증](../../data-lake/silver/survivorship/financial_research/kr_full_share_input_publication_20260911/closeout/audit.json)에 보존했다.
