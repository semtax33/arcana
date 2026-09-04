# Arcana Financial Semantic Rule Engine v2

## 목적

v2는 재무제표의 행 문자열을 곧바로 현대 IFRS 계정으로 덮어쓰지 않는다. 원문을 보존한
`ReportedFact`, 규칙으로 의미를 올린 `CanonicalFact`, 시계열 비교용
`HarmonizedFact`를 분리한다. 회계기준과 문서 포맷도 독립적으로 판정하므로 2009~2010년
K-IFRS 조기 적용 공시를 단순 연도 분기로 K-GAAP 처리하지 않는다.

```text
DART legacy HTML / XBRL / IFRS XBRL
                 |
                 v
        FinancialDocumentIR
                 |
          match -> capture
                 |
      structural constraints
                 |
                emit
                 v
 ReportedFact -> CanonicalFact -> HarmonizedFact
```

## 구현 원칙

- HMRB 라이브러리는 사용하지 않는다. 재사용 가능한 predicate와
  `match → capture → constraint → emit` 실행 모델만 차용했다.
- spaCy 3의 `PhraseMatcher`와 `Matcher`를 사용한다. 한국어 계정명은 Mecab 설치 여부와
  무관하게 결정적으로 실행되도록 정규화 문자열을 character `Doc`으로 변환한다.
- 임의 Python callback, 네트워크, 파일시스템 접근, mutable rule state를 규칙에서
  허용하지 않는다.
- 규칙 우선순위가 같으면 source order가 우선한다. 결과에는 rule id/version, 입력 정규화,
  capture, assertion, 원본 규칙 파일/index가 provenance로 남는다.
- 회계기준 판정 증거 가중치는 명시적 회계정책 100, taxonomy namespace 80,
  감사보고서 문구 70, 표시 어휘 30, 연도 힌트 5다.

## Document IR 및 accounting graph

`FinancialDocumentIR`은 document/section/table/row/column/cell/XBRL fact 노드와 관계를
보존한다. HTML adapter는 `rowspan`/`colspan`을 logical grid로 복원한다. 지원 관계는
다음과 같다.

```text
PARENT_OF     COMPONENT_OF    CONTRA_OF
SUBTOTAL_OF   NET_OF          RECONCILES_TO
DERIVED_FROM  ABOVE/BELOW     LEFT_OF/RIGHT_OF/NEAR
```

숫자 셀의 의미 주소는 section path, row header path, column header path, unit, currency,
period, scope를 함께 가진다. 따라서 `감가상각누계액`을 단순 문자열 하나로 처리하지 않고
유형자산의 contra 관계로 표현할 수 있다.

## 회계 의미 계층

- `ReportedFact`: 원 계정명, 원 금액, 원 단위, filing/revision, source location을 보존한다.
- `CanonicalFact`: canonical id, 부호 정책, cash direction, comparability, rule provenance를
  추가한다.
- `HarmonizedFact`: 복수 canonical fact를 분석 지표로 bridge한다. `sum`과 `last`처럼
  등록된 결정적 reducer만 허용한다.

Comparability 값은 `EXACT`, `PRESENTATION_ONLY_DIFFERENCE`,
`AGGREGATION_DIFFERENCE`, `MEASUREMENT_DIFFERENCE`, `ACCOUNTING_POLICY_BREAK`,
`DERIVED_BRIDGE`, `UNKNOWN`이다.

Fact identity는 최소한 entity, metric, economic period, scope, accounting regime,
published_at, filing id, revision id를 포함한다. 같은 2010년 수치라도 당시 발표된 K-GAAP
수치와 후일 제시된 K-IFRS 비교 수치가 서로 다른 fact로 남는다.

## 규칙 v2

실행 bundle은
[`semantic_kr_v2.yaml`](../data-lake/meta/rules/semantic_kr_v2.yaml)이다. 사람이 관리하는
K-GAAP 확장은
[`k_gaap_historical_v2.yaml`](../data-lake/meta/rules/k_gaap_historical_v2.yaml), 공통 보강은
[`semantic_common_v2.yaml`](../data-lake/meta/rules/semantic_common_v2.yaml)에 있다.

```yaml
- id: kgaap_bs_accumulated_depreciation
  version: 2
  phase: normalize
  priority: 790
  applies:
    statement_types: [BS]
    accounting_regimes: [K_GAAP, GENERAL_K_GAAP, UNKNOWN]
  match:
    label:
      exact_any: [감가상각누계액, 유형자산감가상각누계액]
  emit:
    canonical_id: ACCUMULATED_DEPRECIATION
    comparability: EXACT
    relations: [CONTRA_OF]
```

지원 predicate는 `exact_any`, `contains_all`, `contains_any_groups`, `excludes_any`,
`regex_any`다. `contains_any_groups`는 각 그룹 내부 OR, 그룹 사이 AND다. applicability는
statement type, accounting regime, document dialect, effective date를 받는다. structural
constraint는 child 유무, 0/blank 금액, accounting relation을 검사한다.

### Parser Rule IR v0.1과 향후 DSL 경계

이 버전은 문법보다 IR을 먼저 고정한다. 현재 YAML은 bootstrap surface이고 Python
runtime은 이를 `SemanticRule` typed IR로 compile한 뒤에만 실행한다. 향후 CUE schema나
HCL 형태의 `.arc` 문법을 추가하더라도 같은 IR로 내려가므로 matcher를 교체하지 않는다.

| 도메인 동사 | v0.1 IR/YAML 책임 |
| --- | --- |
| `source`, `select` | `applies`와 Document IR의 statement/regime/dialect/effective date |
| `match` | 닫힌 `TextPredicate` 집합과 spaCy matcher |
| `capture` | 자동 label/context capture와 `MatchProvenance` |
| `normalize`, `map` | `SemanticFieldNormalizer`와 `emit.canonical_id` |
| `assert` | `match.constraints`의 child/zero/relation 검사 |
| `fallback` | 명시적 `emit.fallback_if_missing`; 기본은 `UNMAPPED` |
| `emit`, `reject` | typed fact emit 또는 `UNMAPPED` |

DSL은 canonical Document IR만 보며 raw BeautifulSoup 탐색, table index selector, issuer/period
분기, 임의 Python callback을 허용하지 않는다. table index는 source provenance 결과로만
남는다. 네트워크·파일시스템·현재 시각 접근, source mutation, 의미가 다른 metric으로의
암묵적 fallback도 금지한다. v2 compiler는 정의되지 않은 rule/applies/match/constraint/
emit 필드를 즉시 거부해 오타나 임의 연산이 조용히 실행되지 않게 한다. 현재 legacy 이관 규칙만 동일 priority에서 source-order
first-match를 유지하며, 신규 문법의 기본 ambiguity policy는 fail-closed로 확장한다.

회사별 차이가 필요한 경우 알고리즘 규칙을 복제하지 않고 lexicon/profile과 별도의
exception ledger로 분리한다. exception은 값 수정이 아니라 상태·근거·authority를
선언하는 객체로 한정한다.

## 기존 YAML 무손실 이관

[`migrate_semantic_rules_v2.py`](../scripts/migrate_semantic_rules_v2.py)를 실행하면 기존
mapping/context/comment/sign policy를 source index 기준으로 v2 bundle에 다시 생성한다.
source SHA-256과 원본/이관 개수가 manifest에 기록된다. 원본에 중복된 rule id도 삭제하지
않고 두 번째부터 `__legacy_dup_N`을 붙이면서 `legacy_rule_id`를 보존한다.

```powershell
(Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned)
(& D:\Programming\python_example\Arcana\.venv-llama\Scripts\Activate.ps1)
python scripts/migrate_semantic_rules_v2.py
```

현재 normalization workflow는 공개 호환 상수는 유지하되 실제 worker에서 v2 bundle의
mapping/context/comment/sign policy를 사용한다. 기존 CSV 본문 스키마는 유지하고 v2의
regime/dialect/scope/currency/comparability/provenance는 debug CSV에 추가한다.

## 커버리지 정의와 계산

[`semantic_rule_coverage.py`](../scripts/semantic_rule_coverage.py)는 다음을 별도로 계산한다.

- migration coverage: 원본 YAML rule/policy entry 중 v2에 source index로 존재하는 비율
- canonical rule coverage: 비파생 canonical catalog id 중 emit 가능한 id의 비율
- observed row coverage: 실제 debug corpus 행 중 `UNMAPPED`가 아닌 행의 비율
- amount-weighted observed coverage: 절대 금액 기준 매핑 비율(계층 subtotal 중복에 유의).
  파서 오류로 여러 숫자가 이어 붙은 `10^18`원 초과 값과 비유한 값은 제외 건수를 별도로
  기록하고 분모에서 제외한다.
- historical K-GAAP validation: 2008년 실제 DART 원문을 다시 파싱한 legacy/v2 비교
- factor coverage: 전체 일별 종목-팩터 셀 중 유한값이 존재하는 비율

기존 debug CSV가 v2 도입 전에 생성되어 regime/dialect 열이 비어 있으면 observed replay는
이를 `UNKNOWN`으로 집계하고 `semantic_metadata_row_pct`로 그 한계를 함께 보고한다. 신규
정규화 실행은 원문 증거로 해당 메타데이터를 채운다.

```powershell
python scripts/semantic_rule_coverage.py
```

`--max-files N`은 smoke run, `--skip-observed`는 정적 migration/catalog 감사에 사용한다.
결과는 `deliverables/semantic_rule_engine_coverage.json`에 저장된다.

## 단위·부호·현금흐름 방향 무결성

표시 단위와 경제적 방향은 한 필드로 섞지 않는다. `raw_amount`는 원문의 괄호·△·▲·마이너스
부호를 보존해 원 단위로 환산하고, `normalized_amount`에는 `as_reported`, `abs`, `neg_abs`
정책만 적용한다. 현금 유입·유출은 별도의 `cash_direction`과 `cash_effect_amount`에 기록한다.
따라서 원문이 음수로 표시된 취득액도 canonical 분석 금액은 양수, cash effect는 음수로
일관되게 표현할 수 있다.

지원 표시 단위는 원, 십원, 백원, 천원, 만원, 십만원, 백만원, 천만원, 억원, 십억원,
조원이다. 소수 표시 단위는 배율을 곱한 뒤 정수 원으로 변환하므로 `1.5억원`을
`150,000,000원`으로 처리한다. 과대 단위 복구 이력은 `unit_factor=0.001`처럼 역배율로
명시할 수 있다. EPS canonical id에는 재무제표 표시 단위를 적용하지 않는다. 하나의 셀에
복수 숫자가 이어진 손상 HTML은 숫자를 연결하지 않고 0/`None`으로 fail-closed한다.

[`semantic_value_integrity_audit.py`](../scripts/semantic_value_integrity_audit.py)는 다음을
전체 debug corpus에서 교차 검산한다.

- 원문 숫자 × 단위와 `raw_amount` 일치 여부
- 금액 정책과 `normalized_amount`/호환 `amount` 일치 여부
- inflow/outflow와 `cash_effect_amount` 방향 일치 여부
- 주요 12개 현금흐름 canonical id의 정적 방향 정책
- 허용되지 않은 단위·정책·방향 및 EPS 오배율

```powershell
python scripts/semantic_value_integrity_audit.py
```

결과는 `deliverables/semantic_value_integrity_audit.json`에 저장된다. 기존에 생성된 debug
데이터가 과거 규칙의 방향을 담고 있으면 현재 v2 정적 정책 오류와 구분해 corpus mismatch로
보고한다. 이 경우 원천을 덮어쓰지 않고 v2로 재정규화해야 한다.

## 비정형 본문 속 계정·금액 탐지

[`NarrativeAccountScanner`](../engine/semantic/narrative.py)는 표 밖의 `p`/`div`/`li` 본문을
대상으로 spaCy `PhraseMatcher` 계정 alias와 단위가 붙은 금액을 함께 탐지한다. 한 문단에
계정 또는 금액이 여러 개이거나 alias가 복수 canonical id로 연결되면 `review_required`로
fail-closed한다. 탐지 결과는 production fact로 자동 합치지 않는 discovery/capture
레이어이며 원문, 거리, 단위, 원화 환산액, 판단 이유를 남긴다.

```powershell
python scripts/audit_narrative_account_candidates.py --max-files 200
```

결과는 `deliverables/narrative_account_candidate_audit.json`에 저장된다. 표본은 저장된 DART
주석 HTML 전체 목록에서 정렬 후 균등 간격으로 선택하므로 특정 종목·최근 연도 편향을 줄인다.

## 현재 감사 스냅샷

2026-09-05 KST에 저장 코퍼스로 다시 계산한 결과다.

| 지표 | 결과 |
| --- | ---: |
| legacy YAML 이관 | 192/192 (100%): mapping 126, context 21, comment 9, sign 36 |
| canonical rule coverage | 107/107 (100%): BS 48, IS 33, CF 26 |
| 전체 저장 행 legacy replay mapping | 5,156,903/11,358,849 (45.399873%) |
| 전체 저장 행 v2 replay mapping | 5,456,988/11,358,849 (48.041734%) |
| v2 mapping 개선 | +300,085행, +2.641861%p |
| 절대금액 가중 v2 replay | 10.456760% (legacy replay 3.634261%) |
| 2008.12 실제 K-GAAP 표본 | legacy 55/348 → v2 86/348 (+8.908046%p) |
| 팩터 셀 coverage | 686,637,683/1,390,510,221 (49.380269%) |

팩터 결과는 2026-09-04 기준 2,596종목, 10,779,149 일별 행, 129개 팩터다. 선택 입력인
`kr_dividend_normalized.csv`가 없어 배당 관련 6개 팩터가 0%인 상태도 분모에 포함했다.

단위·부호 감사는 2,642개 debug 파일 11,358,849행에서 허용되지 않은 unit/policy/direction,
정규화 금액 불일치, cash effect 공식 불일치가 각각 0건이었다. 과거 저장분의 EPS 오배율
50건, 원문-저장 배율/정밀도 차이 14건, 구형 취득/처분 분류 92,805건은 재정규화 대상으로
분리했다. 현재 v2 정적 정책은 canonical 12개와 이를 emit하는 규칙 13개 모두 방향 검사를
통과한다.

비정형 DART 주석은 유효 HTML 70,792개 중 균등 표본 200개를 검사했다. 183개 파일에서
4,668개 후보를 찾았고, 169개는 단일 계정·단일 금액으로 확정 가능했으며 4,499개는
다중 계정/금액 때문에 검토 대상으로 보류했다. 파싱 실패는 0건이다.
