# Factor Lab 속도 개선 및 백테스트 날짜 오류 수정

검증일: 2026-09-12. Windows → WSL Ubuntu, ClickHouse 26.5.1.426, 운영 arcana DB.

## 결과

저장된 120노드 전략으로 실제 FastAPI 라우트부터 ClickHouse까지 실행했다. 스크리닝과 기간 실행은 새 측정용 run을 생성했고, 백테스트는 생성된 기간 실행 결과를 사용했다. 기존 실험 정의는 수정하지 않았다.

| 전체 API 경로 | 개선 전 | 개선 후 | 변화 |
| --- | ---: | ---: | --- |
| 하루 스크리닝 | 22.51초 | 8.75초 | 약 61% 단축 |
| 기간 점수 계산: 2016-01-04~2026-09-04, 분기별 | 52.50초 | 25.02초 | 약 52% 단축 |
| 같은 기간 백테스트 | 날짜 오류로 HTTP 500 | 17.17초, HTTP 200 | 오류 해결 |

날짜 오류만 고친 상태의 별도 백테스트 성능 테스트는 32.38초였다. 종목 조건을 최적화한 최종 실행은 17.17초로 약 47% 짧았다. 프로파일러를 켠 45.09초 기록은 계측 부담이 있으므로 속도 비교 기준으로 사용하지 않았다.

이는 같은 로컬 데이터에서 다른 DB 테스트를 동시에 돌리지 않고 측정한 대표 사례다. 워밍업·캐시·백그라운드 부하에 따라 시간은 달라지며, 매일 리밸런싱하는 10년 전체 실행의 성능을 측정한 수치는 아니다.

## 적용한 변경

### 백테스트 날짜 변환

`engine/loaders/survivorship.py`의 뷰 생성 코드를 수정했다. 여러 종류의 JSON이 섞인 `survivorship_rows`에서 ClickHouse가 뷰의 kind 필터보다 날짜 조건을 먼저 평가하면, 다른 종류의 행에 없는 effective_date를 빈 문자열로 읽고 엄격한 Date32 변환이 실패했다. 운영 데이터로 실제 백테스트 API의 Code 38 실패를 재현했다.

필수 날짜를 변환하기 전에 kind를 확인하도록 입력을 보호했다. 대상 kind의 실제 날짜는 계속 엄격하게 변환하며, 1970년 이전 날짜도 보존한다. 다른 kind에만 사용하는 안전한 입력은 뷰 필터에서 제외되므로 이벤트 날짜나 결과에 대체 날짜가 들어가지 않는다. 잘못된 원본 날짜를 조용히 NULL로 바꾸는 처리는 추가하지 않았다.

운영 DB의 listing_episodes, lifecycle_events, lifecycle_entitlements, trading_halts에 해당하는 네 뷰도 새 정의로 갱신했다. 원본 JSON·게시 세대·기업행동 데이터는 변경하지 않았다. 변경 전 정의는 진단 폴더의 `lifecycle-views-before.json`에 보관했다.

### 스크리닝과 기간 실행

`api/repository/factor_lab_execution.py`:

- Windows의 localhost HTTP 클라이언트에서 연결 재사용에 따른 작은 명령의 지연을 줄였다. 원격 호스트는 기존 연결 정책을 유지한다. 임시 테이블의 ClickHouse 세션은 연결이 바뀌어도 유지됨을 검증했다.
- 기간 실행의 작은 중간 결과는 RAM에 보관하고, 보관 한도를 넘으면 MergeTree 임시 테이블로 옮긴다. 필요 없어진 단계는 즉시 해제한다.
- RAM 보관 한도는 기본 128 MiB이며 쿼리 메모리 제한의 1/4보다 커지지 않는다. `ARCANA_FACTOR_LAB_STAGE_MEMORY_BYTES=0`이면 모든 기간 실행 단계를 디스크에 둔다.
- 128 MiB는 **보관 중인 단계 테이블 데이터**의 한도다. 진행 중인 쿼리나 프로세스 전체 메모리를 뜻하지 않는다. 쿼리별 2 GiB 또는 더 엄격한 기존 제한, 서비스의 cgroup 제한을 함께 유지한다.
- RAM 테이블 생성이 메모리 제한 Code 241로 실패하면 부분 테이블을 정리한 뒤 디스크 생성으로 재시도한다. 일반 오류는 그대로 전달하고 성공·실패 시 임시 테이블을 정리한다.
- 각 단계에서 실제 사용하는 바인딩 파라미터만 전달하여, 수백 개의 불필요한 그래프 파라미터를 매 HTTP 요청에 반복 전송하지 않도록 했다.

공유 CTE를 한 번씩 계산하는 기존 단계화 구조 위에 적용한 개선이다. 팩터 공식과 시점 조건은 유지했다.

### 백테스트 가격 조회

`api/service/backtest_service.py`의 lifecycle 가격 조회에서 `has(array, security_id)`를 `security_id IN array`로 변경했다. 최신 가격 선택, 날짜 범위, 거래정지·상장폐지·현금 이벤트 처리 조건은 유지했다. EXPLAIN에서 두 표현의 인덱스 가지치기는 같았으므로, 개선을 인덱스 사용 여부의 차이로 설명하지 않는다.

## 검증

- 일반 회귀 테스트: 140 passed, 15 skipped.
- 실제 ClickHouse의 생존편향·백테스트 실행·게시 테스트: 27 passed. 1956년 날짜 보존, 잘못된 날짜 거부, 거래정지 및 상장폐지 처리 포함.
- 실제 API 리소스 테스트: 14 passed. RAM·디스크·강제 디스크 전환, 실패 시 임시 테이블 정리, 기간 워밍업·거래일 누락 처리 포함.
- 대표 데이터 성능 테스트 세 가지는 각각 실패를 확인한 뒤 통과했다. 최종 전체 API 재실행에서도 세 경로 모두 HTTP 200이었다.
- API 응답 전체를 비교했다. 새 run_id/factor_id만 정규화했으며 순위·날짜·종목·포지션·경고가 일치했다. 백테스트의 수치 114,390개를 비교한 최대 절대 차이는 4.44e-16이었다. 비교 허용 오차는 상대 1e-11, 절대 1e-12다.
- 저장된 스크리닝 250행과 기간 점수 6,632행도 전부 비교했다. 날짜·종목·유효성·오류 사유가 일치했고 점수 최대 절대 차이는 각각 1.61e-15, 4.44e-16이었다.

ClickHouse 서비스는 검증 종료 시 active, NRestarts=0이었다. cgroup MemoryPeak는 약 4.77 GiB였으며 이는 서비스 전체 메모리다. 실행 중인 API의 OpenAPI 요청도 HTTP 200이었다. 별도 리소스 테스트용 ClickHouse 인스턴스는 검증 후 종료했다.

## 재현 자료

진단 폴더: `output/diagnostics/clickhouse-wsl-20260912/` (로컬 진단 산출물, Git 제외).

- `profile_factorlab_api.py after-final`: 실제 API 세 경로 재실행.
- `api-profile-before-speed.json`, `api-profile-after-final.json`: 전체 응답과 SQL별 소요 시간.
- `api-profile-backtest-baseline.json`: 날짜 수정 직후 성공한 백테스트의 비교용 응답. 시간은 프로파일링 영향이 있으므로 제외.
- `compare_final_results.py`, `final-comparison.json`: API 응답 비교.
- `compare_final_stored_scores.py`, `final-stored-score-comparison.json`: 저장된 전체 스코어 비교.
- `repair_lifecycle_views.py`, `lifecycle-views-before.json`: 적용한 뷰 복구와 변경 전 정의.

선택 실행 테스트는 `tests/test_factor_lab_live_backtest.py`에 있다. 로컬 대표 그래프 경로를 `ARCANA_TEST_FACTOR_LAB_GRAPH`, 기존 완료 run을 `ARCANA_TEST_EXISTING_FACTOR_LAB_RUN`으로 지정한다. ClickHouse 인증은 환경변수로 전달한다. 이 테스트의 시간 한도와 6,632개 유효 행 기대치는 해당 로컬 데이터의 회귀 기준이며 일반 환경의 SLA가 아니다.

이전 기록: [복구 및 초기 단계화](factor-lab-recovery-and-optimization-20260912.md), [API 기준선과 진단 실험](factor-lab-api-speed-baseline-20260912.md).
