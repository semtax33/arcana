# Arcana Financial Semantic Rule Engine v6

v6 is the active Korean financial semantic parser/normalizer for K-GAAP,
general K-GAAP, and K-IFRS. It keeps the immutable Reported Fact → Canonical
Fact → Harmonized Fact boundary, migrates every legacy YAML rule, and fails
closed when period, scope, unit, sign, direction, or statement structure is not
comparable.

## Rule language

Rules are typed, ordered programs rather than a flat alias dictionary:

```yaml
- id: cf_special_cash_change
  version: 2
  phase: normalize
  priority: 880
  applies:
    statement_types: [CF]
    accounting_regimes: [K_GAAP, K_IFRS, UNKNOWN]
    source_types: [FINANCIAL_STATEMENT]
  match:
    label:
      contains_any_groups:
        - [연결범위변동, 합병으로인한현금증가, 사업양수에의한현금증가]
      excludes_any: [투자활동현금유출]
    context:
      contains_all: [현금흐름표]
    constraints:
      amount_is_zero_or_blank: false
  emit:
    canonical_id: SPECIAL_CASH_CHANGE
    amount_policy: as_reported
    comparability: EXACT
```

The executable grammar supports parse/context/normalize/validate/harmonize
phases, priority, exact/contains/regex/exclusion predicates, grouped Boolean
predicates, statement/regime/dialect/source/sector/table/effective-date gates,
hierarchical parent paths, structural constraints, captures, signed amount and
cash-direction policies, comparability labels, provenance, and fail-closed
fallbacks.

## Matcher and document model

The matcher follows HMRB's useful ideas—composable matcher algebra, typed
captures, ordered rules, and explicit actions—without importing HMRB. spaCy is
used directly:

- `PhraseMatcher` for deterministic account aliases in financial statements,
  notes, and business-content prose;
- `Matcher` for token-pattern and regex-backed predicates;
- `Doc` extensions for normalized text and document context;
- batched vocabulary reuse and candidate caching for large historical runs.

Each candidate carries source type, statement type, section/parent path,
table kind, accounting regime, document dialect, period, scope, currency,
sector/industry context when evidenced, rule identity, and source-rule
provenance. Unknown sector context is never guessed.

## Sign, unit, direction, and identity gates

- Tax benefits remain signed negative facts, so
  `PBT - TAX_EXPENSE = NET_INCOME` works for expenses and benefits.
- Parentheses in legacy packed balance-sheet subtotal columns are treated as a
  presentation marker only in that dialect; income-statement and cash-flow
  parentheses retain negative/outflow meaning.
- Invalid unit factors, implausible temporal unit jumps, and mismatched
  canonical cash directions are rejected or sent to review.
- Balance-sheet validation supports both IFRS total equity and the legacy
  K-GAAP presentation `assets = liabilities + outside shareholder interest + equity`.
- Cash validation includes translation differences and the signed sum of all
  special changes caused by consolidation scope, mergers, or held-for-sale
  reclassification.
- Packed cells with incomplete vertical alignment and income bridges with an
  unmodelled operand are `NOT_TESTABLE`, not false mapping failures.
- Identity evidence never changes a mapping automatically.

## 2000–2012 validation

The local stratified audit parsed 116 sampled statement files without a parser
failure and did not force 2009–2012 into one accounting regime. Evidence found
108 K-GAAP, 5 K-IFRS, and 3 UNKNOWN files. In 2009 the sample contains two
K-GAAP and one UNKNOWN file; 2010–2012 locally available files are K-IFRS.

- Row mapping: 9,809 / 29,542 (33.203575%).
- Absolute monetary mapping: 70.659681%.
- Accounting identities: PASS 99, REVIEW 0, NOT_TESTABLE 597; 576 lack a
  required fact and 21 have insufficient statement structure.
- Invalid unit factors: 0; canonical cash-direction mismatches: 0; ambiguous
  automatic emits: 0.
- Business-content discovery: 789 candidates, 768 review-required, 0 automatic
  production promotions.
- Full materialized 2000–2012 outputs: 981,515 / 2,753,323 rows (35.648378%)
  and 55.759491% of absolute monetary value; 254 malformed or implausibly
  concatenated amounts are excluded only from the monetary denominator.
- Company-year completeness: 20,181 / 24,716 company-years; average core-fact
  coverage 90.627754%, factor-ready coverage 62.617843%, and 23,599
  invariant-testable company-years.
- PIT-safe 2002–2012 factor backfill: 2,678 / 2,678 target securities and
  33 / 33 year-basis snapshots verified equal to the source. Against the 279
  KR-applicable factors, factor-ID coverage is 85.304659% annual, 82.795699%
  quarterly, and 82.437276% TTM. Finite-cell coverage is 14.880009%,
  15.480193%, and 15.630530%, respectively (650,284,911 / 4,241,843,181,
  or 15.330244%, across all three bases).
- The pre-rebuild materialization had only 56 / 293 global factor IDs. For 16
  zero-imputation-risk factors, at least 29,942,600 baseline cells disappeared
  from the PIT-safe annual result. This is a confirmed-removed lower bound;
  the resulting 13.917989% historical honest-coverage estimate is an upper
  bound, not an accuracy claim.

These figures measure the local source inventory, not all filings published by
DART. Missing local filings remain `source_not_ingested`; an empty or partial
network response is not interpreted as evidence that a company filed nothing.

## Migration and reproducibility gates

- Legacy YAML migration: 192 / 192 source entries (100%).
- Source integrity: all four source-file SHA-256 hashes match.
- Canonical catalog rule coverage: 133 / 133 (BS 59, IS 40, CF 34).
- Rule-contract corpus: 961 cases; TP 898, true abstain 63, FP 0, FN 0.
- Relevant regression suite: 220 tests plus 9 subtests passed.

The contract corpus proves deterministic migration conformance. It is not an
independently relabelled out-of-sample accuracy study, so statistical accuracy
is not claimed from those 961 cases alone.

## Reproduction

```powershell
& .\.venv-llama\Scripts\python.exe -m scripts.recover_kr_historical_disclosures --stage normalize --normalize-start-year 2000 --normalize-end-year 2012 --normalize-workers 12
& .\.venv-llama\Scripts\python.exe -m scripts.audit_historical_semantic_parsing --start-year 2000 --end-year 2012 --statement-files-per-year 12 --disclosure-files-per-year 1000 --output deliverables/historical_semantic_audit_2000_2012_v6.json
& .\.venv-llama\Scripts\python.exe -m scripts.audit_company_year_financial_completeness
& .\.venv-llama\Scripts\python.exe -m scripts.build_semantic_golden_corpus
& .\.venv-llama\Scripts\python.exe -m scripts.backfill_kr_historical_factors all --apply
& .\.venv-llama\Scripts\python.exe -m scripts.build_semantic_v6_coverage_report
```

ClickHouse commands require `CLICKHOUSE_PASSWORD`. Historical recovery and
factor rebuilds are hash-scoped and resumable; destructive scope replacement
first creates and verifies a backup.
