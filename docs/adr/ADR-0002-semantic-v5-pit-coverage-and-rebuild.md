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
10. The 2002–2012 KR historical load freezes its target universe as every
    `SEC_KR_*` security with at least one `price_daily` row inside the date
    interval. The manifest stores sorted security and factor SHA-256 hashes. All
    293 current preferred factors are attempted independently for annual,
    quarterly, and TTM bases; a processed target may still have no materialized
    row when every requested value is undefined.
11. Historical snapshots are exact finite-row copies of the rebuilt daily factor
    source. `source_trade_date` equals the source row's `trade_date`; value,
    fiscal metadata, currency, and `updated_at` are preserved. Each year/basis is
    deleted before retry and accepted only after source/snapshot counts match.
12. Historical load coverage reports three separate measures per year/basis:
    security coverage uses securities with price rows as its denominator, factor
    ID coverage uses the 293-factor contract, and factor-cell coverage uses
    `price_daily rows × 293`. Processing completion is reported separately and
    is never substituted for finite-value coverage.
13. When strict PIT metadata proves that a security has no report available by
    the requested end date, its annual, quarterly, and TTM financial inputs are
    all empty. Its finite non-financial factor rows are therefore basis-invariant
    and may be copied from the completed annual result. Any security with a
    usable report remains on the normal basis-specific calculation path. The
    initial 2002–2012 load observed zero usable reports because historical DART
    metadata had not been ingested; that observation is source incompleteness,
    not evidence that the filings do not exist. The optimization is therefore
    permitted only per security after a completed, checkpointed metadata search
    records either usable reports or an explicit terminal no-data result.
14. Vertically packed legacy DART statement cells are expanded by preserving
    blank `<br>` positions across account, detail, and subtotal columns. Within
    that dialect, parentheses in the packed subtotal column are aggregation
    markers; economic negatives remain identified by minus or triangle markers.
    Expanded mappings are accepted only as candidates and accounting identities
    remain independent validation evidence.
15. v6 preserves signed tax benefits, includes legacy outside-shareholder
    interest and special/translation cash changes in their applicable accounting
    identities, and marks structurally misaligned packed statements NOT_TESTABLE.
    Accounting identities remain review evidence and never auto-remap facts.

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
- Defining “all securities” from currently listed master data: excludes delisted
  and historical securities that are present in the requested interval.
- Treating an undefined requested factor as zero or as a failed batch: creates a
  false financial fact or makes a resumable load retry forever.
- Copying annual rows for a security with any report available in the interval:
  could erase legitimate quarterly or TTM differences.
- Treating an empty or partially collected historical metadata cache as proof of
  no filings: silently converts a source-recovery failure into basis-invariance.

## Consequences

The executable dependency percentage can increase without changing a single
fact value. The PIT-safe factor-cell percentage can be lower than the v4 figure
because a previously leaked interval is removed; that decrease is an accuracy
correction. Coverage comparisons therefore retain the v4 baseline and v5 PIT
result side by side with their policies.
