# Arcana Financial Semantic Rule Engine v4

v4는 v3의 192개 기존 YAML 규칙 무손실 이관과 typed spaCy 실행기를 유지하면서, 정확도를
낮추지 않고 coverage 병목을 넓힐 수 있도록 검증·측정 계층을 추가한다. 설계 결정은
[`ADR-0001`](adr/ADR-0001-semantic-v4-precision-first-quality-gates.md), 용어는
[`SEMANTIC_GLOSSARY.md`](SEMANTIC_GLOSSARY.md)에 정리했다.

## 추가된 기능

- `NOT_TESTABLE` 폐쇄형 taxonomy와 testability KPI
- 연도×회계체계×재무제표×scope×sector×문서 dialect 행/금액 coverage
- Document IR→ReportedFact→CanonicalFact→HarmonizedFact→factor input→factor cell waterfall
- CAPEX inflow 오분류의 8개 downstream factor 값/부호/백분위 drift 감사
- 주석·사업내용 후보의 모호성 군집과 false-semantic-emit=0 게이트
- `semantic_kr_v3.yaml` immutable bundle, SHA-256 manifest, 안정/호환 alias
- unit/semantic/integration/data-heavy 테스트 tier

## 실행

```powershell
& .\.venv-llama\Scripts\python.exe scripts\audit_historical_semantic_parsing.py --start-year 2000 --end-year 2012
& .\.venv-llama\Scripts\python.exe scripts\semantic_rule_coverage.py
& .\.venv-llama\Scripts\python.exe scripts\audit_capex_factor_drift.py
& .\.venv-llama\Scripts\python.exe scripts\build_semantic_v4_coverage_report.py
& .\scripts\run_test_tier.ps1 -Tier semantic
```

## 2026-09-05 실측 결과

| 지표 | 결과 |
| --- | ---: |
| 기존 YAML 이관 | 192/192 (100%) |
| 캐노니컬 계정 rule coverage | 133/133 (100%) |
| 운영 행 매핑 | 5,626,655/11,358,849 (49.535433%) |
| 운영 금액가중 매핑 | 13.401399% |
| 2000–2012 표본 행 매핑 | 9,183/28,071 (32.713477%) |
| 2000–2012 표본 금액가중 매핑 | 74.701864% |
| 핵심 경제개념 | 31/31 (100%) |
| 역사 표본 factor input dependency | 82/107 (76.635514%) |
| materialized factor cells | 686,637,683/1,390,510,221 (49.380269%) |
| 회계 항등식 testability | 98/672 (14.583333%) |
| 서술형 false semantic emit | 0 |

운영 corpus의 strict harmonized-ready는 0/5,626,655다. 기존 debug 파일에 scope가 없는
5,626,541행과 유효 금액이 아닌 114행을 fail-closed로 제외했기 때문이다. canonical mapping
coverage가 0이라는 뜻이 아니며, 다음 개선 우선순위가 scope/regime 포함 재정규화임을 뜻한다.

2009 표본은 K-GAAP 1개와 UNKNOWN 1개, 2010은 로컬 파일 0개, 2011·2012는 K-IFRS 각 1개다.
전환연도를 IFRS로 강제하지 않았다. 전체 112개 재무제표 표본의 탐지 결과는 K-GAAP 107,
K-IFRS 2, UNKNOWN 3이다.

CAPEX 교정 ledger의 원시 92,805행 중 연간 대표 입력에 실제 선택된 것은 3,186개이며,
1,271종목·2,943 종목연도에 영향을 줬다. 의존성 그래프상 129개 공개 팩터 중 8개가 영향받고,
재무입력 의존 107개 중 99개는 구조적으로 비영향이다. 상세 변경 셀과 순위 이동은
[`capex_factor_drift_v4.json`](../deliverables/capex_factor_drift_v4.json)에 있다.

8개 팩터에서 변경된 연간 factor cell은 합계 18,737개다. 상호 배타적 우선순위 분류로
부호 전환 971, 20%p 이상 백분위 이동 536, 5% 이상 값 변화 7,115, 경미 변화 8,688,
coverage loss 1,424, gain 3개다. 현재 materialized 일별 factor snapshot은 재빌드하지 않았으므로
49.380269% 셀 coverage와 이 counterfactual drift는 별도 지표다.

## 테스트 결과

- fast tier(data-heavy 제외): 644 passed
- data-heavy tier: 83 passed
- 세부 실행: unit 155 passed, semantic 222 passed, integration 274 passed
- 신규 v4/회귀 집중 실행: 59 passed
- Python compileall 및 `git diff --check`: 통과

세부 tier는 일부 파일이 문맥상 두 범주에 들어가므로 합산하지 않는다. 전체 비중복 테스트는
fast 644 + data-heavy 83 = 727개다.

## 해석 제한

정답 라벨 gold corpus가 없으므로 이 결과는 통계적 precision/recall이 아니다. 정확도는 문맥
게이트, 부호/단위 정적 감사, 항등식 증거, 모호 후보 자동승격 금지로 방어한다. 미매핑 상위
군집의 `예수금`, `차입금`, 파생상품 손익 등은 sector·부모 문맥 없이 하나의 canonical ID로
승격하지 않는다. 숫자를 높이기 위한 추측 매핑은 coverage 개선으로 인정하지 않는다.
