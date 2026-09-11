# ClickHouse 복구와 Factor Lab 실행 최적화 결과

이후 적용한 날짜 변환 수정과 RAM/디스크 단계 저장 정책, 최종 API 속도는 [후속 개선 결과](factor-lab-api-speed-results-20260912.md)에 정리했다. 이 문서는 03:10 시점의 복구 기록이다.

검증 시각: 2026-09-12 03:10 KST. ClickHouse 26.5.1.426 / WSL Ubuntu.

## 복구

ClickHouse를 시작해 운영 `arcana` DB 데이터 조회를 확인했다. 비동기 테이블 로딩 184개 작업이 모두 OK이며 API의 OpenAPI 요청도 HTTP 200이다.

기존 프로세스는 WSL init과 같은 `init.scope`에서 실행됐다. 커널 OOM으로 ClickHouse가 종료된 후 systemd가 해당 그룹의 init과 셸까지 정리한 기록이 있다. 이제 `/system.slice/clickhouse-arcana.service`에서 독립 실행한다.

- WSL `/etc/systemd/system/clickhouse-arcana.service`: enabled / active / running, 검증 종료 시 NRestarts=0.
- 실행 설정: `D:/Programming/clickhouse/config-recovery.xml`. 기존 `config.xml`, 데이터 경로와 인증 정보는 보존했다.
- 데이터: `/var/lib/clickhouse-arcana/data`.
- ClickHouse 서버 메모리 한도 10 GiB. systemd MemoryHigh=11 GiB, MemoryMax=12 GiB, MemorySwapMax=1 GiB.
- default 프로필: max_threads=2, max_memory_usage=2 GiB, 외부 집계/정렬 시작 한도 각각 256 MiB.
- 로그: `/var/log/clickhouse-arcana/`, information 수준, 파일당 50 MiB, 회전 설정 count=3. 이전 5 GB 이상 stderr 로그는 삭제하지 않았다.
- 재시작 정책 on-failure, 10초 대기. 300초 내 2회 시작 한도로 무한 재시작을 막는다.

서비스 단위 cgroup 제한은 쿼리 메모리 추적 밖의 사용량까지 포함하는 마지막 보호선이다. 큰 쿼리가 자원 제한으로 실패할 수는 있으며, 무제한 크기/동시 실행을 보장하는 설정은 아니다. 약 19 GiB WSL 메모리에서 서비스 상한을 분리했다.

상태 확인:

```powershell
wsl -d ubuntu -u root -- systemctl status clickhouse-arcana.service --no-pager
wsl -d ubuntu -u root -- systemctl show clickhouse-arcana.service -p NRestarts -p MemoryCurrent -p MemoryPeak
```

이후 수동 시작도 이 서비스를 사용한다. 기존 셸에서 별도의 ClickHouse 서버를 중복 실행하지 않는다.

## 제품 코드 변경

`api/repository/factor_lab_query.py`는 원래 SQL과 함께 CTE 목록과 최종 SELECT를 제공한다. `api/repository/factor_lab_execution.py`가 필요한 CTE의 의존 관계를 추적해 공통 계산 및 각 그래프 노드 출력을 임시 테이블에 순차 저장한다. 여러 점수 분기와 유니버스를 최종 쿼리마다 다시 전개하던 비용을 줄인다. 일반 CTE가 참조마다 전개되는 동작은 [ClickHouse 공식 문서](https://clickhouse.com/docs/reference/statements/select/with)에 설명되어 있다.

- Screen: 세션 전용 Memory 임시 테이블.
- History 및 시간 연산이 있는 screen: 세션 전용 MergeTree 임시 테이블로 중간 결과를 디스크에 저장한다. 이 서버 버전에서 실제 생성/조회/삭제를 검증했다.
- 마지막 소비가 끝난 단계는 즉시 삭제한다. 실패한 CREATE AS SELECT의 부분 테이블도 정리 대상이다.
- Factor Lab 실행 클라이언트에 2스레드/2 GiB 및 외부 집계·정렬 한도를 적용한다. 호출자나 서버가 더 엄격한 한도를 지정했다면 유지한다.
- `factor_lab_service.py`의 최종 INSERT도 단계 결과를 읽는다.
- `factor_lab_evaluation_service.py`: 평가 입력이 최종 점수라면 같은 run의 저장된 최종 점수를 복사한다. 다른 중간 점수의 평가도 단계 실행을 사용한다.

팩터 공식, exact 분위수, 결측 정책, 원천 테이블 선택, 거래일 지연과 유니버스 조건은 변경하지 않았다. 날짜 청크 분할과 일반적인 warm-up 역전파 최적화는 이번 변경에 포함하지 않았다. 여러 날짜를 한꺼번에 실행하는 history도 단계 결과를 디스크로 내리지만, 매우 긴 일별 실행의 각 단계가 2 GiB를 넘으면 추가 분할이 필요하다.

## 검증

### 실패 재현과 회귀

실제 FastAPI `/api/factor-lab/runs` → ClickHouse HTTP → 격리 DB 경로를 사용했다. DB는 테스트별로 생성/삭제하며 운영 테이블을 사용하지 않는다.

- 변경 전: 기존 공통 CTE만 저장하던 경로에서 1/4분기는 통과, 32분기는 Code 241로 실패했다. 메시지: `would use 73.41 MiB ... maximum: 64.00 MiB`.
- 변경 후: 동일한 64 MiB 한도로 1/4/32분기를 screen/history 각각 통과했다. 3종목의 점수와 순위를 숫자로 검증했다.
- History: 거래일 지연의 시작일 이전 데이터, 현재 관측 부재, 이전 거래일 결측을 포함한 결과가 기존 단일 SQL과 일치한다.
- 평가: 최종 점수와 중간 점수의 캐시 값을 모두 확인했다.
- 실패 정리: Memory/MergeTree 각각 중간 SQL을 의도적으로 실패시킨 뒤 임시 테이블 잔존 0개를 확인했다.
- 실제 HTTP/DB 통합 테스트 총 10개 통과. 관련 단위/회귀 테스트는 별도 실행에서 105개 통과, 15개 skip(별도 환경 opt-in 등). 전체 저장소 테스트를 실행한 것은 아니다.

재현 명령:

```powershell
$env:ARCANA_TEST_FACTOR_LAB_RESOURCES='1'
& .\.venv-llama\Scripts\python.exe -m pytest tests/test_factor_lab_api_resources.py -q
```

이 테스트는 기본 `127.0.0.1:18123`의 격리 테스트 서버를 요구한다. `ARCANA_TEST_CH_HOST`, `ARCANA_TEST_CH_PORT`로 변경할 수 있다. 이번 검증에 사용한 임시 테스트 서버는 검증 후 종료했다.

### 실제 저장된 120노드 전략

실험 `ffb1923d-6246-4712-876f-6ba5095e31f9`의 저장된 그래프를 읽었다. 이름은 `Ungdroo_US_QualityValueInnovation_Reflexivity_RPR_TTM_Quarterly_Robust_v3_20160104_20260821_FactorLab`이며 입력 팩터 34개, winsorize 31개, shrunk_zscore 31개 등을 포함한다. `deltaep_score`, `rdmargin_score`, `spreadgrowth_score` 등 장애 SQL과 주요 노드명이 일치한다. 다만 장애 run 레코드와 완전한 원본 SQL을 확보하지 못했으므로 당시 그래프와 완전히 동일하다고 단정하지 않는다.

운영 데이터를 대상으로 **영구 결과 INSERT 없이 단계화 SELECT만** 실행했다. 두 모드 모두 쿼리당 2 GiB, 2스레드, statement별 60초 제한. 시간은 원천 날짜 선택, 단계 생성, 최종 집계 및 정리까지 포함하며 API 전체 응답 시간은 아니다.

| 실행 | 입력 날짜 | 유효 결과 | 실측 시간 |
| --- | --- | --- | --- |
| Screen | resolver가 선택한 PIT snapshot 2026-09-04 | 250종목 | 18.53초 |
| History | 2016년 시작 분기별 리밸런싱의 전 거래일 43개 | 40개 날짜, 6,632행 | 57.17초 |

History 신호일 범위는 2015-12-31~2026-06-30이다. 첫 리밸런싱 직전 거래일이 시작일보다 앞서는 기존 스케줄 동작을 유지했다. `allow_missing_inputs`와 기존 유효성 필터가 적용되며 요청 신호일 43개 모두에 유효 점수가 존재하는 것은 아니다.

검증 종료 시 서비스 MemoryPeak=4,455,116,800 bytes(약 4.15 GiB), NRestarts=0. 이것은 서비스 cgroup 전체 최고 사용량이며, 해당 쿼리 하나의 peak memory 또는 RSS로 해석하면 안 된다. 기존 장애와 데이터/동시 작업이 통제된 A/B 비교가 아니므로 메모리 절감률이나 속도 향상 배수는 산출하지 않았다. 10년 전체 일별 history 성능은 측정하지 않았다.

근거 파일은 `output/diagnostics/clickhouse-wsl-20260912/`에 있다: `candidate-validation-screen.json`, `candidate-validation-history.json`, `validate_candidate.py`, `clickhouse-arcana.service`, 기존 장애 증거. 이 폴더는 Git ignore 대상이며 인증 정보는 검증 JSON/서비스 파일에 기록하지 않았다.
