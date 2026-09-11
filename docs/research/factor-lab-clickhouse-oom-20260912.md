# 팩터랩 ClickHouse OOM 진단 — 2026-09-12

## 확인된 장애

ClickHouse 자체 예외 처리로 끝난 쿼리 실패가 아니라, Linux OOM killer가
ClickHouse 프로세스를 강제 종료한 장애다. WSL에 할당된 메모리는 약 19 GiB이며,
종료 당시 ClickHouse의 anonymous RSS는 거의 이 전체 용량에 도달했다.

수집한 커널 로그:

```text
Sep 12 02:04:49 ... ConcurrentJoin invoked oom-killer
Sep 12 02:04:49 ... global_oom ... task=clickhouse,pid=3031
Sep 12 02:04:49 ... Out of memory: Killed process 3031 (clickhouse)
    total-vm:40794708kB, anon-rss:19864652kB

[1166.952165] Out of memory: Killed process 942 (clickhouse)
    total-vm:37717028kB, anon-rss:19880084kB
```

두 번째 기록의 벽시계 시각은 확보하지 못했다. 커널 로그 시각과 애플리케이션
로그 시각은 원문 그대로이며, 별도로 KST/UTC 변환을 단정하지 않는다.

## 종료 직전 실행

증거 파일: `D:\Programming\clickhouse\server.stderr.log`.
전체 파일을 읽거나 복사하지 않고 마지막 120 MB에서 해당 query ID를 추출했다.

| 항목 | 관측값 |
| --- | --- |
| query ID | `cda3aa42-9201-48ee-b35d-ffbfeb39ae2c` |
| run ID | `5877bb2f-a8b9-4286-9cda-81b0ba56459a` |
| 명령 | `INSERT INTO factor_lab_values ... SELECT ...` |
| 최종 노드 | `final_score` |
| 시장 / 모드 | US / 하루치 스크리닝으로 보이는 동일 시작·종료 날짜 조건 |
| SQL 실행 시작 로그 | `2026.09.12 02:01:54.741411` |
| 해당 쿼리의 마지막 로그 | `2026.09.12 02:04:46.669744` |
| 다음 커널 OOM 기록 | `Sep 12 02:04:49` |
| 서버가 출력한 SQL | 약 100,000자에서 잘림; 뒤에 89,065자 생략 표시 |
| 관측된 최대 내부 테이블 별칭 | `__table8657` |
| 해당 쿼리 Planner 로그 | 25,099건 |
| JoinOrderOptimizer 로그 | 3,022건 |

`__table8657`은 실제 테이블 8,657개나 외부 요청 8,657건을 뜻하지 않는다.
하나의 SQL을 분석하면서 만들어진 내부 테이블 표현의 규모를 보여준다.
SQL 원문은 서버에서 이미 잘렸으므로 완전한 실패 SQL이라고 취급하면 안 된다.
임시 보관 위치는 `tmp/factor_lab_oom/failed_insert.sql`이다.

직전 날짜 확인 쿼리는 11,481,942행 / 217.12 MiB를 읽고 약 4.63초에 완료했다.
그 뒤의 점수 저장 SQL에서는 수만 건의 계획·JOIN 최적화 로그가 이어졌다.
따라서 현재 증거에서는 HTTP 요청 수 자체보다 **단일 SQL의 계획 확장과 메모리
사용**이 우선 조사 대상이다. 다른 동시 작업의 기여까지 배제한 것은 아니다.

실험을 읽은 마지막 요청에는 `ffb1923d-6246-4712-876f-6ba5095e31f9`가 등장한다.
읽기 요청과 실행 요청의 연결을 확정할 run 정의를 아직 조회하지 못했으므로
이 ID를 장애 실험으로 단정하지 않는다.

## 코드에서 확인한 부하 구조

1. `api/repository/factor_lab_query.py::compile_factor_lab_graph`는 그래프 전체를
   하나의 `WITH ... SELECT`로 만든다. `FactorLabService.run_graph`는 이를
   단일 `INSERT ... SELECT`로 실행한다.
2. 유니버스가 활성화되면 입력 결측치의 중앙값, winsorize, 점수화, 최종 필터에서
   같은 `uv_eligible`을 반복 참조한다. 그 하위에는 상장 이력·거래정지·종목 정보·
   시가총액 등의 JOIN/집계가 연결된다.
3. `_compile_winsorize`는 동일 입력을 경계값 계산과 실제 값 변환에서 각각
   참조한다. `_compile_factor_input_with_cross_sectional_median`도 원본 입력을
   관측값과 중앙값 계산에서 다시 참조한다. 여러 가중 점수 블록에서 이를
   조합하면 공통 계산이 큰 실행 계획으로 복제될 수 있다.
4. `build_factor_lab_insert_query`의 `max_threads = 2`는 시간 연산 노드가 있는
   경우에만 적용된다. 일반적인 하루치 복합 스크리닝에는 이 제한이 없다.
   팩터랩 전용 `max_memory_usage`도 설정하지 않는다.
5. `prepare_evaluation_run`은 평가 입력 점수를 다시 컴파일·계산한다. 평가 점수가
   이미 저장한 최종 점수와 같아도 재계산한다. 다만 이번 기록은 첫 점수 INSERT
   중 종료돼, 이것을 이번 장애의 직접 원인으로 보지는 않는다.

ClickHouse의 일반 CTE는 결과 캐시가 아니다. 참조 위치에서 하위 쿼리를
다시 평가할 수 있다. 공식 자료:

- [WITH 문서](https://clickhouse.com/docs/sql-reference/statements/select/with)
- [26.3 릴리스: materialized CTE](https://clickhouse.com/blog/clickhouse-release-26-03)

실행 파일에서 확인한 버전은 `26.5.1.426`이다. `AS MATERIALIZED`는 버전만으로
바로 적용할 해결책으로 취급하지 않는다. 실험적 설정과 실제 IN/JOIN 조합의
호환성 검증이 필요하다.

## 최적화 순서

### 1. 공통 계산을 실행 단위로 한 번만 생성

가장 우선할 변경이다. 유니버스, 종목 메타데이터, 재사용되는 중간 노드를
실행별 임시 결과로 만들어 다음 단계가 그 결과를 읽도록 한다.
목표는 계산식이나 종목 모집단을 바꾸는 것이 아니라, 같은 하위 계산을
반복해서 계획·실행하는 비용을 제거하는 것이다.

운영 가능한 구현에는 다음 조건이 필요하다.

- 실행별로 충돌하지 않는 임시 테이블 이름과 동일 DB 세션 사용.
- 필요한 공통 노드만 물리화하고, 마지막 사용 이후 해제.
- 실패·취소 경로에서도 임시 결과 정리.
- 관측값/결측값/유효성 컬럼을 유지하고, 마지막 단계까지 기존 필터 의미 보존.
- 기간을 임의로 잘라 lag/rolling의 과거 입력이나 횡단면 모집단을 바꾸지 않음.
- 장기 history에서는 임시 결과 자체의 총 메모리도 측정·제한.

`AS MATERIALIZED`와 단계별 임시 테이블을 소규모 실제 DB 재현에서 비교해
호환성과 메모리 사용이 확인된 방식을 택한다. 단순히 모든 CTE를 Memory
테이블에 쌓는 방식은 history 실행에서 새로운 메모리 문제를 만들 수 있다.

### 2. 팩터랩 쿼리에 일관된 자원 예산 적용

시간 연산 여부와 무관하게 팩터랩의 고비용 쿼리에 낮은 병렬도와 메모리
예산을 적용한다. 약 19 GiB 환경의 초기 시험값으로는 스레드 2,
쿼리 메모리 2 GiB, 외부 집계/정렬 시작 256 MiB 정도를 검증할 수 있다.
이는 실측으로 확정한 운영 권장값이 아니다.

JOIN의 메모리 예산과 디스크로 넘기는 JOIN 방식도 실제 쿼리 호환성을 확인해야
한다. 쿼리 메모리 제한이 모든 분석기·서버 할당을 완벽하게 막는다고 가정하지
않으며, 프로세스 RSS도 함께 측정한다. 제한만 낮추면 서버는 보호해도 팩터랩
실행 자체가 계속 실패할 수 있으므로 1번과 함께 해결해야 한다.

### 3. 같은 최종 점수의 평가용 재계산 제거

평가 입력 노드가 최종 점수인 경우, 해당 run의 `factor_lab_values`에서
`factor_lab_node_cache`로 복사한다. 다른 평가 입력 노드는 별도로 계산하되
가능한 공통 단계는 재사용한다. 입력 데이터가 중간에 바뀌어도 평가와 최종
점수가 어긋나지 않는지 함께 검사한다.

### 4. 중첩 실행과 보조 쿼리 개선

동시 실행이 기여하는지는 별도 측정한다. 필요하면 팩터랩 작업의 입장 제한을
두고 명시적인 busy 응답을 제공한다. 날짜 확인 쿼리의 읽기량과 반복 DDL도
줄일 수 있지만, 현재 관측한 거대한 단일 실행 계획보다 후순위다.

운영 설정의 로그 수준은 `trace`이며 stderr 파일은 약 5.12 GB였다.
진단이 끝나면 로그 회전과 수준을 정리하는 것이 좋다. 로그량을 OOM의 직접
원인으로 확인한 것은 아니다.

## 검증 상태와 다음 재현

사용자가 합의한 회귀 테스트 경계는 **실행 API 전체**다.
`POST /api/factor-lab/runs`에서 실제 그래프 검증·컴파일·실행·결과 조회까지
거치고, 외부 DB만 격리된 테스트 데이터베이스로 연결한다.

기존 테스트 결과:

```text
.venv-llama\Scripts\python.exe -m pytest tests/test_factor_lab_query.py tests/test_factor_lab_service.py -q --disable-warnings
55 passed, 1 warning in 14.84s
```

이 결과는 기존 기능 테스트이며 OOM 재현이나 수정 성공을 뜻하지 않는다.
운영 DB에는 실패 쿼리를 다시 보내지 않았다.

별도 경로·18123 포트·서버 메모리 2 GiB·쿼리 메모리 1 GiB의 격리 서버를
시도했으나 WSL 정지, `Wsl/Service/E_UNEXPECTED`, systemd 세션 생성 실패,
HTTP timeout/refused가 반복돼 안정적인 테스트 환경을 확보하지 못했다.
커널에는 unclean journal 및 loop 장치 I/O 오류도 관측됐다. 이 환경 오류의
원인을 이번 팩터랩 SQL로 확정하지 않는다.

따라서 현재는 원인 증거와 최적화 설계를 확보한 상태이며, **최적화 코드 적용,
OOM 회귀 테스트의 red → green, 성능 개선 수치 검증은 아직 완료하지 않았다.**

안정적인 테스트 환경에서의 완료 조건:

1. 원래 장애 그래프를 확보하고, 최소한의 종목·날짜·분기 구조로 축소한다.
2. 메모리 제한 아래 기존 API가 계획/메모리 예산을 초과함을 확인한다.
3. 한 가지 최적화씩 적용해 같은 API 요청이 성공하고 기대한 값·순위·유니버스를
   보존하는지 확인한다.
4. 원본 그래프·screen/history·결측치·PIT·시간 연산으로 범위를 확장한다.
5. 실행 계획 규모, read_rows/read_bytes, peak query memory, 프로세스 RSS,
   실행 시간, 동시 요청 영향을 비교한다. 그래프만 수정한 성능 대체물을
   원래 실험의 성공으로 보고하지 않는다.
