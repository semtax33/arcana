# ADR-0002: Semantic v5 PIT coverage and rebuild gates

- Status: Accepted
- Date: 2026-09-05
- Supersedes: none; extends ADR-0001

## Context

v4 reported 82/107 financial-factor input coverage and 49.380269% materialized
factor-cell coverage. Two different defects were hidden in those figures:

1. the static dependency graph flattened executable fallback alternatives into
   one AND-set, although the factor code selects one of several paths;
2. the standalone coverage calculator treated fiscal period-end as fact
   availability and referenced a missing dividend file.

Raising either percentage by guessing absent facts, treating non-disclosure as
zero, or moving a filing value before its publication date would reduce semantic
accuracy and is therefore not an acceptable improvement.

## Decision

1. Dependency coverage exposes two metrics. `strict_union_*` retains the old
   all-mentioned-input denominator for comparison. The primary executable metric
   accepts a factor only when at least one complete, code-equivalent alternative
   path exists. It does not invent source data.
2. Annual financial facts become available on the latest ingested DART statement
   `report_date` for that company-year. A missing report date produces abstention;
   period-end fallback is forbidden in v5 rebuilds.
3. Dividend DPS and payout ratios become available on the DART receipt date.
   Only explicitly identified common-stock DPS is admitted. Preferred and
   unlabeled share classes are not guessed. Dividend yield is recomputed from
   known DPS and contemporaneous close.
4. CAPEX aggregation admits `outflow` rows and excludes `inflow` rows. An
   inflow-only disclosure produces no CAPEX fact. Sign display, economic cash
   direction, and factor magnitude remain separate fields.
5. A materialized rebuild must create a hash-scoped backup, delete only the eight
   CAPEX-dependent annual factors for the audited securities, rebuild with strict
   report metadata, checkpoint every batch, and compare value, percentile,
   decile, portfolio membership, IC, long-short return, and turnover. Count,
   backup, delete, rebuild, and comparison share one date scope. Backup and
   rebuild inserts are split by the table's monthly partition key; every backup
   year is counted against source before it becomes a resume checkpoint.
6. `missing_required_fact` is decomposed into one mutually exclusive root cause.
   The detailed counts must sum to the original NOT_TESTABLE count.
7. Narrative candidates remain discovery evidence. No cluster is promoted without
   a reviewed deterministic rule and an independent test.
8. The 961-case source-rule contract corpus is a migration regression corpus, not
   an independently human-labelled accuracy sample. Its precision/recall is never
   presented as out-of-sample semantic accuracy.
9. Exact value and availability drift is counted over every affected daily cell.
   Percentile, decile, IC, spread, and membership drift uses the last KR price
   date of each calendar month and reconstructs the full KR universe: unaffected
   current rows stand in for their unchanged old values, while affected old rows
   come from the verified backup.

## Rejected alternatives

- Counting every fallback symbol as mandatory: understates executable coverage.
- Treating period-end as publication date: leaks future information.
- Connecting the legacy year-expanded dividend CSV: leaks later dividend reports
  into earlier trading dates.
- Imputing missing dividends, buybacks, debt issuance, or R&D as zero without a
  closed disclosure-completeness rule: creates false positives.
- Letting accounting-equation residuals auto-remap accounts: a residual is only
  review evidence and can be caused by scope, period, currency, unit, dimensions,
  or incomplete presentation.

## Consequences

The executable dependency percentage can increase without changing a single
fact value. The PIT-safe factor-cell percentage can be lower than the v4 figure
because a previously leaked interval is removed; that decrease is an accuracy
correction. Coverage comparisons therefore retain the v4 baseline and v5 PIT
result side by side with their policies.
