# Arcana Financial Semantic Rule Engine v5

v5 keeps the ReportedFact → CanonicalFact → HarmonizedFact separation and every
v3/v4 migrated rule, while adding point-in-time-safe factor inputs and evidence
needed to interpret coverage correctly. The design decision is recorded in
[`ADR-0002`](adr/ADR-0002-semantic-v5-pit-coverage-and-rebuild.md).

## Main additions

- executable alternative-path factor dependency graph plus legacy strict-union metric;
- eight-way missing-fact root-cause taxonomy;
- company-year BS/IS/CF presence, core-fact, factor-ready, and invariant-testable audit;
- 2000–2012 year × accounting-regime × document-dialect matrix;
- narrative cluster company/year/syntax/relation/amount/period/scope statistics;
- PIT DART annual-financial availability and common-stock dividend event files;
- cash-direction-aware CAPEX selection in the actual annual factor input path;
- hash-scoped, resumable CAPEX materialized rebuild and portfolio drift audit;
- immutable v5 bundle manifest and 961-case rule-contract corpus.

## Reproduction

```powershell
& .\.venv-llama\Scripts\python.exe -m scripts.build_kr_pit_financial_availability
& .\.venv-llama\Scripts\python.exe -m scripts.build_kr_pit_dividend_events
& .\.venv-llama\Scripts\python.exe -m scripts.audit_historical_semantic_parsing --start-year 2000 --end-year 2012 --output deliverables/historical_semantic_audit_2000_2012_v5.json
& .\.venv-llama\Scripts\python.exe -m scripts.audit_company_year_financial_completeness
& .\.venv-llama\Scripts\python.exe -m scripts.build_semantic_golden_corpus
node scripts/calculate_factor_coverage.js
& .\.venv-llama\Scripts\python.exe -m scripts.audit_capex_factor_drift
& .\.venv-llama\Scripts\python.exe -m scripts.rebuild_capex_daily_factors_v5 --apply
& .\.venv-llama\Scripts\python.exe -m scripts.audit_capex_daily_portfolio_drift_v5
& .\.venv-llama\Scripts\python.exe -m scripts.build_semantic_v5_coverage_report
```

ClickHouse commands require `CLICKHOUSE_PASSWORD` to be supplied by the runtime.
The rebuild command first makes a scoped backup and refuses target/status hash
mismatches.

## Accuracy interpretation

The regression corpus proves that the migrated rule contract remains executable.
It is not independent ground truth. Statistical precision/recall remains
`NOT_CLAIMED` until independently reviewed filing facts are added. Accounting
identities, context gates, sign/unit/direction checks, and zero automatic narrative
promotion are safety constraints, not substitutes for independent labels.

## Measured evidence (2026-09-05)

- Legacy YAML migration: 192/192 source entries (100%); all four source hashes match.
- Canonical catalog: 133/133 IDs (BS 59/59, IS 40/40, CF 34/34).
- Operational replay: 5,626,655/11,358,849 rows mapped (49.535433%) and
  13.401399% of valid absolute monetary value across 2,642 normalized files;
  legacy replay maps 45.401237% of rows, so v5 adds 469,597 mapped rows.
- Executable factor dependencies: 107/107 (100%); legacy strict-union comparison remains 82/107 (76.635514%).
- PIT-safe factor cells: 673,787,706/1,390,510,221 (48.456149%). The v4 period-end-fallback baseline was 686,637,683/1,390,510,221 (49.380269%); the -0.924120 percentage-point change is retained as an accuracy correction.
- Company-year completeness: 19,987/22,553 (88.622356%); mean core-fact coverage 96.160156%, mean executable factor readiness 65.839636%, invariant-testable 22,345/22,553 (99.077728%).
- 2000–2012 local sample: 9,183/28,071 rows mapped (32.713477%) and 74.701864% of absolute monetary value; 112/112 sampled files parsed. No 2010 financial-statement source is present locally.
- Historical accounting identities: PASS 91, REVIEW 7, NOT_TESTABLE 574. The 574 missing-required-fact cases split exactly into concept-not-mapped 354, concept-not-reported 143, statement-incomplete 59, and historical-dialect-gap 18.
- Narrative discovery: 476 business-content candidates grouped into 129 clusters; automatic production promotion and observed false emission are both zero.
- CAPEX annual counterfactual: 3,186 inflow-selected facts across 1,271 securities and 2,943 security-years; 18,737 factor cells change across eight dependent factors.
- CAPEX daily materialization: the 21,472,606-row hash-scoped backup was
  verified before mutation and 21,145,771 strict-PIT rows were rebuilt exactly;
  326,835 prior rows (1.522102%) were deliberately abstained rather than filled
  from unavailable or direction-invalid evidence.
- CAPEX portfolio drift: 4,048,788 daily values changed. Across 140 month-end
  observations per factor and the full KR universe, 1,768,140 percentiles,
  121,978 deciles, and 12,122 top-decile memberships changed. Mean cross-version
  top-decile turnover ranges from 3.032098% to 12.325537%; factor-level IC deltas
  range from -0.001696 to +0.002962 and long-short-return deltas from -0.001139
  to +0.003980.

The generated v5 coverage report contains the exact factor-by-factor daily and
portfolio evidence.
