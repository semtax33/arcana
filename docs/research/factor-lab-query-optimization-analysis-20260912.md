# Arcana 팩터랩 쿼리 최적화 분석

분석일: 2026-09-12. 사용자 기준: 하루치 screen과 장기간 history를 동등하게 지원한다. 이번 산출물은 분석과 설계이며 DB 성능 개선 완료 보고가 아니다.

## 결론

가장 큰 개선점은 **그래프를 한 개의 큰 SQL로 전개하는 방식에서, 공통 입력과 중간 점수를 재사용하는 예산 기반 단계 실행으로 바꾸는 것**이다. 여기에 history의 필요한 과거 범위를 계산하고 날짜 묶음별로 실행하는 기능을 함께 설계해야 한다. 스레드 수를 낮추거나 SQL 문자열만 줄이는 것으로는 충분하지 않다.

## 장애 증거와 분석 범위

- query_id `cda3aa42-9201-48ee-b35d-ffbfeb39ae2c`, run_id `5877bb2f-a8b9-4286-9cda-81b0ba56459a`, 최종 노드 `final_score`.
- `INSERT INTO factor_lab_values … SELECT …`의 기록된 앞부분 약 10만 자에 JOIN 104개 / SELECT 257개. 서버가 뒤의 89,065자를 생략했으므로 완전한 SQL은 아니다.
- 02:04:49 커널: ConcurrentJoin에서 OOM 발생, ClickHouse RSS 약 18.94 GiB, WSL swap 5 GiB 소진.
- 프로세스 OOM은 확정이다. 이 SQL은 직전 실행 중인 유력한 유발 쿼리이며, 다른 동시 쿼리의 메모리 기여까지 분리한 것은 아니다.
- 현재 작업 트리에는 이미 `factor_lab_execution.py`와 이를 호출하는 변경이 있다. 장애 당시와 동일한 실행 코드라고 취급하지 않았다. 이번 분석에서 제품 코드는 수정하지 않았다.

## 코드별 병목

| 코드 위치 | 현재 동작 | 개선 방향 |
|---|---|---|
| `factor_lab_query.py:262`, `:524` | 노드별 CTE를 한 WITH로 결합 | SQL 생성 전 단계·공유 결과를 정하는 실행 계획 도입 |
| `universe_query.py:16` | `uv_eligible` 아래에 종목, 상장, 거래정지, 시총 JOIN/집계 연결 | 실행 날짜별 유니버스를 한 번 산출해 재사용 |
| `factor_lab_query.py:1394` | 중앙값 보정에서 source_values를 관측값·중앙값 양쪽에서 사용 | 공통 원본 입력을 먼저 저장; 결측치 처리는 이후 적용 |
| `factor_lab_query.py:1819` | winsor 입력을 경계 계산과 실제 변환에 각각 참조 | 공통 입력 재사용, 하·상위 분위수를 한 집계 상태로 계산 |
| `factor_lab_query.py:1918` | 각 shrunk_zscore가 종목 메타데이터 JOIN 및 시장·업종 window 수행 | 메타데이터 공통화; 동일 조건의 통계 계산 묶기 |
| `factor_lab_query.py:2161` | weighted_score가 base에 분기별 LEFT JOIN | 완성된 분기 점수를 저장한 뒤 결합; 조건 충족 시 열 단위 계산 또는 long-form 집계 |
| `factor_lab_query.py:2427` | 단일 날짜·직접 factor→시간 노드만 일부 과거 행 제한 | 노드별 입력 기간을 역전파하는 history planner |
| `factor_lab_evaluation_service.py:49` | 평가 입력 점수마다 전체 그래프를 다시 컴파일·실행 | 동일 run의 원래 점수 결과 재사용; 그 외 평가 분기도 공통 단계 실행기 사용 |

일반 CTE는 기본적으로 참조 지점에 하위 쿼리를 전개한다. 결과를 한 번 계산해 공유하는 캐시와 다르다. 이 특성 때문에 작은 그래프 분기가 복잡한 유니버스를 여러 번 끌고 들어온다. [ClickHouse WITH 문서](https://clickhouse.com/docs/reference/statements/select/with)

## 현재 단계 실행 변경을 오프라인으로 확인한 결과

현재 `tests/test_factor_lab_api_resources.py`의 합성 그래프 생성 함수를 읽어 컴파일했다. DB 호출은 기록 객체로 대체했으며 **SQL을 DB에서 실행하지 않았다**. 원래 장애 그래프도 아니다.

| 점수 분기 | 그래프 노드 | 원래 SQL 문자 수 | uv_eligible 전개 추정 | 현재 코드가 만드는 임시 테이블 | 최종 SQL 문자 수 |
|---:|---:|---:|---:|---:|---:|
| 1 | 4 | 14,490 | 6 | 6 | 5,291 |
| 4 | 13 | 35,463 | 21 | 9 | 17,942 |
| 16 | 49 | 119,661 | 81 | 21 | 68,839 |
| 32 | 97 | 232,333 | 161 | 37 | 137,079 |

전개 추정은 최상위 CTE의 문자 수준 참조를 따라 센 수치다. 중첩 로컬 CTE의 추가 전개와 ClickHouse 최적화를 반영하지 않는다. 실제 scan/JOIN 횟수, 메모리, 실행시간으로 해석하면 안 된다.

32분기에서 각 CREATE SQL은 최대 3,380자지만 최종 쿼리는 137,079자다. 즉 공유 부분을 잘라도 마지막에 많은 분기를 한 번에 결합하는 비용은 남는다. 모든 단계의 SQL 길이 합도 248,083자로 원래보다 크다. 단계화의 목적은 SQL 전송량 감소가 아니라 중복 계획·실행과 한 시점의 작업량 감소다.

재현 명령:

```powershell
& .\.venv-llama\Scripts\python.exe .\output\diagnostics\clickhouse-wsl-20260912\analyze_compilation.py
```

### 현재 변경의 보완점

1. `factor_lab_execution.py:52`는 직접 참조가 2회 이상인 최상위 CTE만 저장한다. 한 번 쓰이지만 매우 무거운 분기, 중첩 source_values, 거대한 최종 결합도 예산을 기준으로 분할해야 한다.
2. `:65`는 모두 ENGINE=Memory다. 큰 history 결과를 담으면 중간 결과 자체가 RAM을 소진할 수 있다. Memory 엔진의 용량 제한을 순환 버퍼처럼 사용하면 오래된 행이 빠질 수 있으므로 계산 결과 저장에 그런 식의 제한을 쓰면 안 된다. [Memory 엔진 문서](https://clickhouse.com/docs/reference/engines/table-engines/special/memory)
3. 기존 `max_threads=2`는 시간 노드가 있는 최종 INSERT에 붙는다. 단계별 CREATE AS SELECT와 평가 쿼리까지 공통 자원 예산을 적용해야 한다.
4. 평가 코드는 새 단계 실행기를 통과하지 않는다. 점수 저장만 개선하면 평가에서 큰 SQL이 재등장할 수 있다.
5. SQL 토큰으로 의존성을 추정하기보다 컴파일러가 `node_id`, 입력 노드, 키, 출력 컬럼, 필요 기간을 구조적으로 반환하는 편이 정확하다. 중첩 CTE와 로컬 이름 범위도 명시할 수 있다.

## 권장 설계: 두 모드 공통 실행 계획

```mermaid
flowchart LR
  G[팩터 그래프] --> P[의존성·필요 기간·단계 예산 계산]
  P --> U[날짜별 유니버스·종목 정보]
  U --> F[필요 팩터 공통 입력]
  F --> S[중앙값·winsor·점수 분기]
  S --> C[완성된 중간 점수 결합]
  C --> O[최종 결과·평가 입력 공유]
```

### 1. 공통 입력과 고비용 분기를 한 번 계산

- 우선 저장 대상: `uv_eligible`, `security_universe`, 필요한 `lab_base_universe`, 반복 사용되는 원본 factor 값, 여러 하위 노드가 공유하는 점수.
- 직접 사용 횟수 외에도 누적 JOIN 수·중간 행 수·과거 범위·최종 결합 분기 수를 보고 단계 경계를 추가한다.
- `factor_input`의 의미가 같은 경우 공유한다. 키에는 factor_id뿐 아니라 financial_basis, source table/mode, 기간, PIT 기준, 유니버스, 결측 정책 등 계산 의미를 포함한다. 원본 관측 단계는 공유해도 결측 정책 적용 결과는 다를 수 있다.
- 마지막 소비자가 끝나면 중간 결과를 해제한다. 요청 실패·취소 시 정리하고, 서버 종료로 정리가 실패한 경우를 위한 만료 청소를 별도로 둔다.
- 단계 실행 중 원본이 갱신되면 같은 run 안에서 입력이 달라질 수 있다. 가능하면 실행 초반에 필요한 원본/분류를 고정하고 이후 그 결과만 읽는다. 단계화가 자동으로 다중 쿼리 snapshot 일관성을 보장한다고 가정하지 않는다.

### 2. 팩터별 반복 원본 scan을 묶기

현재 `_compile_factor_input`은 팩터마다 최신 값 argMax 집계와 종목 JOIN을 만든다. 필요한 `(factor_id, financial_basis)` 목록과 날짜를 모아 원본을 읽고, `(trade_date, security_id, factor_id, financial_basis)` 기준으로 최신 관측을 만든 뒤 각 노드가 재사용하도록 한다.

이때 원본 테이블·PIT 방식·기간 요구가 다른 입력은 무조건 하나로 합치지 않는다. 동률 updated_at의 선택 정책, NULL, 유효성, source_trade_date를 보존해야 한다. 실제 read_rows/read_bytes 개선은 현재 테이블 정렬키와 EXPLAIN indexes 결과를 보고 판단한다. 현재 배포 DDL을 확인하지 않은 상태에서 인덱스 변경부터 권하지 않는다.

### 3. 계산 체인에서 불필요한 JOIN 줄이기

- winsor 하·상위 경계는 `quantilesExact(lower, upper)(value)`로 함께 계산하는 후보가 된다. 여러 분위수의 상태를 공유하는 공식 기능이며, 근사 분위수로 바꿀 필요는 없다. [quantilesExact 문서](https://clickhouse.com/docs/reference/functions/aggregate-functions/quantilesExact)
- 동일 키·모집단의 단순 산술/부호변환/클리핑 체인은 같은 행의 열 계산으로 합칠 수 있다. 두 입력의 키 집합이 다르거나 INNER/LEFT 의미가 다르면 먼저 기존 행 존재·결측 의미를 복원해야 한다.
- 큰 weighted_score는 각 분기의 완성 점수를 저장한 뒤 결합한다. 이후 long-form `(date, security, input, value, valid, weight)` 집계로 JOIN 수를 줄일 수 있지만, 입력별 키 유일성·음수/0 가중치·모든 입력 결측·active_weight=0·invalid_reason 우선순위를 검증해야 한다.
- quantile, winsor, rank, zscore의 대상 종목을 먼저 Top N으로 줄이지 않는다. 그것은 속도 최적화가 아니라 결과 정의 변경이다.

## screen / history를 동등하게 지원하는 방식

| 항목 | Screen | History |
|---|---|---|
| 처리 단위 | 출력 날짜 하나와 필요한 과거 입력 | 날짜 묶음과 해당 묶음에 필요한 과거 입력 |
| 중간 결과 | 작다고 확인된 결과는 세션 임시 Memory 가능 | 크기가 큰 결과는 run별 디스크 저장 단계 또는 제한된 날짜 묶음 |
| 원본 입력 | 해당 날짜와 노드별 warm-up | 출력 기간보다 앞선 warm-up 포함; 청크 간 중복 읽기/상태 관리 |
| 횡단면 통계 | 해당 날짜의 전체 대상 종목 | 각 날짜의 전체 대상 종목을 유지 |
| 시간 연산 | row/trading-day/fiscal 의미에 맞는 과거 조회 | 청크 경계를 넘어 lag/rolling 입력 유지 |
| 완료 공개 | 최종 결과가 모두 준비된 뒤 | 부분 청크 성공을 완료 run으로 노출하지 않음 |

권장은 **단계 계획기는 공통으로 두고, 날짜 묶음과 저장 방식을 실행 정책으로 선택**하는 것이다. `temporary=Memory`와 별개로, 큰 결과는 전용 스테이징 DB의 MergeTree 등 디스크 테이블을 설계해야 한다. 이 경우 run_id/단계별 격리와 중복 방지, 실패 정리 및 TTL/청소가 필수다. TTL의 즉시 실행을 전제로 메모리·디스크 예산을 계산하지 않는다.

History의 기간 절단은 단순히 시작일에서 252일 빼는 방식으로 하면 안 된다.

- `lag(20)`의 row 기준은 거래일·달력일과 다르다.
- rolling이 연결되면 필요한 관측 수가 합성된다. 분기별 요구의 합집합을 역전파해야 한다.
- 시간 노드 앞의 횡단면 정규화는 warm-up 날짜의 전체 대상 종목을 필요로 한다.
- fiscal_lag는 발표 가능일과 회계기간 관계를 유지해야 한다.
- 현재 직접 시간 입력 행 제한은 이미 있으나 일반 복합 그래프/history 전체를 처리하는 planner는 아니다.

## 자원 예산과 평가 경로

모든 단계·최종 INSERT·평가 준비·보조 고비용 쿼리가 하나의 실행 컨텍스트를 사용하도록 한다. 여기에서 병렬도, query memory, JOIN 정책, 외부 집계/정렬 한도, 취소와 timeout, 최대 동시 run을 관리한다.

메모리는 `동시에 실행하는 쿼리 + 살아 있는 중간 테이블 + 서버 기본 사용량`의 합으로 봐야 한다. 쿼리 하나의 max_memory_usage만 낮춰도 Memory 테이블 총량이나 다른 요청을 모두 제어할 수 있는 것은 아니다. 약 19 GiB WSL에서 시작하는 격리 시험은 1개 run·2스레드·명시적 쿼리 제한으로 하고, 실제 RSS를 측정해 배분한다. 아직 적정 운영 한도를 실측하지 않았으므로 특정 GB 값을 확정 권장값으로 제시하지 않는다.

평가 입력이 final_score와 같으면 해당 run의 최종 점수를 재사용한다. 다만 `factor_lab_values`는 컴파일러 최종 필터 `is_valid=1`을 거친 결과이므로, 평가가 invalid 행의 진단까지 필요로 한다면 필터 전 중간 결과를 보존해야 한다. 다른 평가 노드 역시 한 번 만든 공통 입력/점수를 공유하고, 기존 거대 SQL로 되돌아가지 않게 한다.

## 구현 순서 및 검증

1. **공통 단계 실행 계획 + 자원 예산**: 현재 변경을 토대로 중첩 공유 입력, 큰 단일 분기, 큰 최종 결합을 단계화하고 평가 경로도 포함한다.
2. **원본 팩터 일괄 읽기 + 유니버스 고정**: 모든 모드의 반복 scan/JOIN을 줄인다.
3. **History 날짜 묶음 + 기간 역전파 + 디스크 스테이징**: screen 개선과 동등한 출시 조건으로 둔다.
4. **winsor 다중 분위수·산술 체인 통합·weighted_score 결합 개선**: 의미가 보존되는 부분부터 적용한다.
5. **관측 결과에 따른 JOIN/정렬키 튜닝**: 실제 read/plan/RSS 증거로 선택한다.

검증은 실제 `/api/factor-lab/runs`에서 입력 검증부터 결과 조회까지 포함해야 한다. 안정적인 격리 DB에서 기존/새 방식을 비교한다.

- Screen과 history 각각: 원래 장애 그래프를 확보해 값, 순위, 종목 집합, 유효성/결측 사유를 비교.
- 시간 연산/청크 경계, 부족한 과거 입력, 상장·상폐·거래정지, 업종 변경, 시총 컷 경계, PIT 위반 입력을 포함.
- FLOAT 결과 허용오차와 순위 동률 처리 기준을 명시. exact quantile은 그대로 유지.
- 실패·취소·동시 요청에서 단계 테이블/세션이 남지 않는지와 부분 결과 노출 방지 확인.
- query별 계획 규모, read_rows/read_bytes, peak query memory, 프로세스 RSS, 살아 있는 staging bytes, 실행시간을 함께 측정.

현재 합성 resource API 테스트는 screen 3종목/하루/동일 roe 분기다. 이것만 통과해서 원래 19 GiB OOM이나 history까지 해결됐다고 보고하면 안 된다. 본 분석에서는 해당 DB 테스트나 실패 SQL을 재실행하지 않았다.

## 산출물

- 재실행 가능한 오프라인 분석: `output/diagnostics/clickhouse-wsl-20260912/analyze_compilation.py`
- 측정 JSON: `output/diagnostics/clickhouse-wsl-20260912/compilation-metrics.json`
- 사건별 로그: 같은 폴더의 `diagnosis.md`, `evidence-0204.txt`, `suspect-query-log.txt`

제품 코드의 기존 변경을 추가 수정하지 않았으며, 원본 그래프 실행 성공이나 메모리 감소율은 아직 검증되지 않았다. `grill-me`가 요구한 `grilling` 스킬/호출 도구는 검색했으나 없어 실행하지 못했다. 대신 사용자에게 우선 모드를 확인했고 두 모드를 동등하게 반영했다.

