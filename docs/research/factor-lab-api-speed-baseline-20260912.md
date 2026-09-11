# 팩터랩 전체 API 속도 기준선

이 문서는 적용 전의 진단 기록이다. 이후 사용자 승인에 따라 날짜 오류와 속도 개선을 적용·검증했다. 현재 결과는 [최종 적용 결과](factor-lab-api-speed-results-20260912.md)를 참조한다. 아래의 미적용·답변 대기 표현은 당시 상태다.

2026-09-12. 이전 복구·단계화 이후, 저장된 120노드 전략을 실제 FastAPI 라우트와 운영 DB에 연결해 측정했다. 기존 실험은 수정하지 않고 측정용 run을 생성했다.

| 경로 | 결과 | 전체 API 소요 시간 |
| --- | --- | --- |
| POST /api/factor-lab/runs (screen) | HTTP 200 | 22.51초 |
| POST /api/factor-lab/runs (history, 2016년부터 분기별) | HTTP 200 | 52.50초 |
| POST /api/factor-lab/runs/{run_id}/backtest | HTTP 500 | 5.48초 후 실패 |

계측 스크립트는 `output/diagnostics/clickhouse-wsl-20260912/profile_factorlab_api.py`, 상세 응답과 SQL별 시간은 같은 폴더 `api-profile-before-speed.json`에 저장했다. 기존 SQL-only 측정과 범위가 다르므로 직접 속도 개선율을 계산하지 않는다.

## 병목

- Screen DB command 325회: 17.22초. 그중 CREATE 163회(임시 테이블 외 초기화 포함) 9.98초, DROP 156회 6.80초. query_df 7회 3.59초.
- History DB command 325회: 45.16초. CREATE 163회 42.58초, DROP 156회 2.08초. query_df 8회 5.72초.
- 따라서 단순히 Python 반복문의 비용을 줄이는 것보다 단계 생성·조회 횟수 및 비용을 줄이는 것이 우선이다.
- 별도 실험에서 임시 MergeTree만 StripeLog로 바꾸어 같은 history SELECT를 측정했다. 성능 이득을 확인하지 못했으므로 제품에 적용하지 않는다. 상세는 `candidate-validation-history-stripelog.json`.

## 백테스트 실패

`security_lifecycle_events` 뷰가 `survivorship_rows`의 JSON을 읽으며 `toDate32(JSONExtractString(payload, 'effective_date'))`를 호출한다. 소비 쿼리의 `effective_date <= end_date` 조건이 조합되면 날짜가 없는 다른 kind의 행에서도 변환이 평가되어 Code 38 `CANNOT_PARSE_DATE`가 발생한다.

현재 게시된 events 7,722행은 모두 effective_date를 Date32로 파싱할 수 있었다. listing_episodes 25,184행 및 다른 kind에는 그 필드가 없다. 즉 날짜가 없는 기업행동 이벤트를 임의로 보정할 문제가 아니라, 혼합 JSON 테이블에 대한 타입 변환과 필터 평가 순서를 안전하게 처리해야 하는 문제다. 원본 이벤트/증거는 수정하지 않는다.

다음 검증은 팩터랩 백테스트 API에서 혼합 kind 데이터를 포함해 HTTP 200, 포지션/수익률 및 날짜 의미를 확인하는 회귀 사례다. 사용자가 요청한 TDD 스킬의 테스트 경계 사전 확인 요청에 대한 응답을 기다리는 동안 진단·기준선 측정을 진행했다. 아직 새로운 속도 개선을 적용하거나 이 목표를 완료한 상태는 아니다.

## 후속 비교 실험

`probe_stage_latency.py`에서 임시 테이블 생성/삭제를 각각 15회 반복했다. Windows의 localhost를 통한 WSL HTTP 연결에서 Memory 테이블 CREATE/DROP 중앙값은 연결 재사용 시 약 45.5/42.4ms, `Connection: close` 시 약 3.8/2.5ms였다. 이 측정만으로 TCP 지연의 정확한 커널 원인까지 확정하지는 않는다.

같은 120노드 그래프의 전체 API를 재실행한 결과:

- Screen: 22.51초 → 11.69초. 반환된 100종목 순위 동일, 점수 최대 절대 차이 약 5.55e-17. 각 실행의 새 factor_id는 당연히 다르다.
- History: 52.50초 → 57.39초. 이 변경만으로 history가 개선됐다는 근거는 없다.
- Backtest: 같은 Date32 변환 오류가 다시 재현됐다.

추가로 history의 디스크 단계를 Memory로 바꾸되 매 단계 후 임시 테이블 total_bytes 합계 128 MiB 초과 시 즉시 중단하는 진단 실험을 수행했다. 이는 작은 중간 결과를 제한된 RAM에 두는 정책의 가능성을 확인하기 위한 실험이며, 한도를 넘는 경우 디스크로 전환하는 제품 구현은 아니다. statement별 기존 2 GiB 제한과 서버 cgroup 제한은 유지했다. 결과는 `candidate-validation-history-bounded-memory.json`에 저장한다. 임시 테이블 total_bytes는 프로세스 전체 메모리 사용량과 다르다.

위 실험은 진단 스크립트에만 구현했다. 제품 코드의 연결 정책이나 history 저장 정책은 아직 바꾸지 않았다.
