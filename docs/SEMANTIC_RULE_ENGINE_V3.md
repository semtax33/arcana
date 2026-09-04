# Arcana Financial Semantic Rule Engine v3

## 결과

v3는 K-GAAP·일반기업회계기준·K-IFRS/IFRS 재무제표와 재무제표 주석, `사업의 내용`을
동일한 typed semantic pipeline으로 처리한다. 원문 의미를 보존하면서 캐노니컬 계정,
기간·범위·단위·통화·부호·현금 유입/유출·관계·한정어를 정규화한다.

```text
HTML/XBRL -> Document IR -> match -> capture -> constraint -> emit
                                  |                       |
                                  v                       v
                         hierarchical context     CanonicalFact
                                  |                       |
                                  +-> invariant evidence-+
                                                          |
                                            HarmonizedFact / factor graph
```

- HMRB 라이브러리는 직접 사용하지 않는다. HMRB의 유용한 아이디어인 작은 predicate 조합,
  `match → capture → constraint → emit`, 닫힌 연산 집합, 결정적 실행 순서를 자체 typed IR로
  구현했다.
- spaCy `PhraseMatcher`와 `Matcher`를 계정 alias, 문서 관계 cue, 정규식 인덱스에 사용한다.
  한국어 형태소 모델 설치 여부에 결과가 달라지지 않도록 정규화된 character `Doc`을 쓴다.
- ML/LLM은 production mapping 경로에 사용하지 않는다. 과거 표현 유사도 역시 자동 규칙이
  아니라 `REVIEW_REQUIRED` lexicon 후보만 만든다.

## 의미 계층과 손실 추적

`ReportedFact → CanonicalFact → HarmonizedFact`를 분리하고 각 단계의 의미 손실을
`PRESERVED`, `LOST`, `UNKNOWN`으로 기록한다. scope, period, granularity, measurement
qualifier가 보존됐는지를 별도 필드로 남기므로 현대 IFRS 계정으로 무조건 덮어쓰지 않는다.

fact identity에는 entity, metric, economic period, scope, accounting regime, 발표시각,
filing/revision을 포함한다. 따라서 2009–2012년의 K-GAAP 원수치와 K-IFRS 조기적용/비교수치는
같은 기간이라도 별도 fact다. 회계기준은 연도로 강제하지 않고 문서의 회계정책 문구,
taxonomy namespace, 감사보고서, 표시 어휘를 증거 가중치로 판정한다.

## 계층형 Context

`SemanticContext`는 다음 상위 정보를 함께 보존한다.

| 차원 | 예 |
| --- | --- |
| 문서 영역 | `FINANCIAL_STATEMENT`, `FINANCIAL_NOTES`, `BUSINESS_CONTENT` |
| 재무제표 | `BS`, `IS`, `CIS`, `CF`, `CE`, `NOTES` |
| 구조 | section path, parent-account path, table kind |
| 회사 | GICS sector code, industry-group code |
| 의미 | consolidated/separate scope, accounting regime, period |

같은 `예수금`도 금융 섹터(`40`)의 재무상태표에서만 금융업 예수부채로 확정한다. 산업재
회사의 재무상태표나 `사업의 내용`에서는 `UNMAPPED` 또는 검토 후보로 남긴다. Context
필드는 `RuleEngine` 캐시 키와 `MatchProvenance.normalized_inputs`에도 포함해 서로 다른
회사의 판정이 캐시로 섞이지 않게 한다.

섹터가 없으면 추측하지 않는다. workflow와 역사 감사 CLI는 외부에서 PIT sector/
industry-group registry를 주입할 수 있고, 없는 경우 sector-gated 규칙은 발화하지 않는다.

## v3 규칙 문법

실행 bundle은 경로 호환을 위해 파일명은
[`semantic_kr_v2.yaml`](../data-lake/meta/rules/semantic_kr_v2.yaml)을 유지하지만,
내부 `schema_version`과 `engine`은 각각 `3`, `arcana-financial-semantic-v3`다.

```yaml
- id: v3_bs_financial_sector_generic_deposits
  version: 3
  phase: normalize
  priority: 790
  applies:
    statement_types: [BS]
    source_types: [FINANCIAL_STATEMENT]
    sector_codes: ['40', FINANCIALS]
    accounting_regimes: [K_GAAP, GENERAL_K_GAAP, K_IFRS, UNKNOWN]
  match:
    label:
      exact_any: [예수금]
    context:
      excludes_any: [선수금]
    constraints:
      has_children: false
  emit:
    canonical_id: DEPOSIT_LIABILITIES_FINANCIAL
    comparability: AGGREGATION_DIFFERENCE
```

지원 applicability는 statement type, source type, accounting regime, document dialect,
sector, industry group, table kind, effective date다. text predicate는 `exact_any`,
`contains_all`, 그룹 간 AND/그룹 내부 OR인 `contains_any_groups`, `excludes_any`,
`regex_any`다. structural constraint는 child/zero-or-blank/accounting relation을 검사한다.
정의되지 않은 필드는 compile 단계에서 즉시 거부하고, canonical id가 없으면 명시한
fallback을 거친 뒤 `UNMAPPED`로 fail-closed한다.

## 주석과 사업의 내용

[`DisclosureHtmlParser`](../engine/semantic/disclosures.py)는 보이는 모든 문단과 표를
section/table IR로 보존한다. spaCy matcher로 다음 tuple의 후보를 추출한다.

```text
ACCOUNT + RELATION + AMOUNT + PERIOD + SCOPE + QUALIFIER + CASH_DIRECTION
```

관계는 지급·발생·취득·처분·증가/감소·수주잔고·매출·생산능력·생산실적·가동률·계획·잔액
등을 구분한다. `133조 8,734억원` 같은 복합 금액은 한 금액으로 읽는다. 계정/금액/기간이
여러 개이거나 Context가 규칙과 맞지 않으면 원 후보는 버리지 않되 `review_required=true`,
`context_eligible=false`로 강등한다. 승인된 golden rule이 없으므로 현재 비정형 후보의
`auto_emit_eligible`은 항상 false다.

```powershell
python -m engine.workflows._internal.normalize_workflow --market kr --target notes
python -m engine.workflows._internal.normalize_workflow --market kr --target business-info
```

출력은 종목별 sections, tables, facts, review CSV다. 기존 사업의 내용 전용 구조화 출력도
그대로 유지한다.

## 단위·부호·흐름 방향

`raw_amount`, `normalized_amount`, `cash_effect_amount`를 분리한다. 원문의 괄호·△·▲·음수와
표시단위를 먼저 원화로 환산하고, `as_reported`/`abs`/`neg_abs` 정책을 적용한 뒤 inflow는
양수, outflow는 음수인 cash effect를 별도로 만든다. 취득과 처분은 같은 CAPEX로 합치지
않는다.

과거 저장분에서 CAPEX 유입으로 잘못 분류된 92,805건은 원본을 고치지 않고 append-only
[`semantic_correction_ledger_v3.csv`](../deliverables/semantic_correction_ledger_v3.csv)에
old/new fact identity와 원문 SHA-256을 기록했다. 유형자산 62,513건, 무형자산 30,292건이며
2,213개 종목·41개 기간에 걸친다.

## 회계 항등식 검증

항등식은 매핑을 자동 변경하지 않고 `PASS`, `REVIEW`, `NOT_TESTABLE` 후보 증거만 만든다.

- 자산 = 부채 + 자본
- 매출 − 매출원가 = 매출총이익
- 법인세비용차감전순이익 − 법인세비용 = 당기순이익
- CFO + CFI + CFF = 환율효과 전 현금변동
- 기말현금 − 기초현금 = 현금변동
- 기초현금 + CFO + CFI + CFF + 환율효과 = 기말현금

scope, period, currency, unit, accounting basis가 섞였거나 dimensional duplicate가 있으면
검사하지 않는다. 환율효과 누락을 0으로 가정하지 않는다. current/non-current component
합계식은 완전하고 중복되지 않은 집계임이 증명된 경우에만 실행한다. 대안 매핑의 residual
개선도 검토 우선순위일 뿐 자동 정답으로 쓰지 않는다. 이 보수적 gate가 subtotal,
연결/별도 혼합, 기간 혼합 때문에 생기는 false positive를 줄인다.

## 미매핑 분류와 역사 lexicon

미매핑은 11개 닫힌 범주로 분류한다: unknown label, known concept/unknown expression,
structural parse failure, period ambiguity, scope ambiguity, dimensional member,
subtotal/presentation-only, disclosure-specific, entity extension, non-financial,
low-information. 과거 표현은 문자 bigram 유사도와 출현 종목·연도·부모 Context·금액 중요도로
집계하지만 사람이 승인하기 전에는 규칙이나 fact가 되지 않는다.

## 커버리지와 재현

통합 보고서는
[`semantic_rule_engine_v3_coverage.json`](../deliverables/semantic_rule_engine_v3_coverage.json),
연도별 상세는
[`historical_semantic_audit_2000_2012.json`](../deliverables/historical_semantic_audit_2000_2012.json)에
있다.

```powershell
python scripts/audit_historical_semantic_parsing.py --start-year 2000 --end-year 2012
python scripts/semantic_rule_coverage.py
python scripts/build_semantic_v3_coverage_report.py
```

PIT 섹터 CSV가 있으면 `--security-context-csv`에 `stock_code`, `sector_code` 또는
`gics_sector_code`, `industry_group_code` 또는 `gics_industry_group_code` 열을 넣는다.

### 2026-09-05 재계산 결과

| 지표 | 결과 |
| --- | ---: |
| 기존 YAML 무손실 이관 | 192/192 (100%) |
| 캐노니컬 rule coverage | 133/133 (100%) |
| 운영 코퍼스 v3 행 coverage | 5,626,655/11,358,849 (49.535433%) |
| 기존 YAML 단독 replay | 5,157,058/11,358,849 (45.401237%) |
| v3 개선 | +469,597행, +4.134195%p |
| 운영 코퍼스 v3 금액가중 coverage | 13.401399% |
| 2000–2012 표본 행 coverage | 9,183/28,071 (32.713477%) |
| 2000–2012 표본 금액가중 coverage | 74.701864% |
| 핵심 경제개념 coverage | 31/31 (100%) |
| 역사 표본의 재무입력 의존 factor coverage | 82/107 (76.635514%) |
| 실제 materialized factor cell coverage | 686,637,683/1,390,510,221 (49.380269%) |

2000–2012 로컬 데이터 가용성은 연도별로 다르다. 2000–2008은 연 12개 발행사 표본,
2009는 2개, 2010은 0개, 2011·2012는 각 1개를 실제 파싱했다. 표준 탐지 결과는 K-GAAP
107개, K-IFRS 2개, UNKNOWN 3개였으며 2009–2012를 연도로 강제하지 않았다. 주석은 로컬
2개 파일 중 1개, 사업의 내용은 63개 중 36개를 파싱했다. 사업의 내용 결과는 3,526개
section, 1,812개 table, 476개 계정-금액 후보이며 463개를 검토 대상으로 보류했다.

역사 표본에서 provenance 누락, 허용되지 않은 단위, canonical cash direction mismatch,
모호 기간의 자동 발화는 모두 0건이다. 항등식은 `PASS 91`, `REVIEW 7`,
`NOT_TESTABLE 574`이다. 원문 빈 셀을 호환값 0으로 간주하지 않아 검사 가능 건수를
보수적으로 제한했다. 이 수치는 정답 라벨 기반 precision/recall이 아니라 의미
무결성 증거다.

실제 팩터 셀 커버리지는 2026-09-04 스냅샷 기준 2,596종목, 10,779,149 일별 행,
129팩터다. 배당 입력 파일 부재로 배당 관련 6개 팩터가 0%인 상태를 분모에 포함한다.
새 의미 매핑을 factor snapshot에 재적재하기 전까지 실제 셀 coverage와 dependency coverage는
서로 다른 지표로 해석해야 한다.
