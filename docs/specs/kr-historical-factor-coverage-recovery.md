# KR Historical Factor Coverage Recovery

## Problem Statement

The 2002–2012 KR historical load materializes only 56 of the global 293-factor contract and 226,735,692 of 1,484,898,509 possible global factor cells. The current headline percentages combine several different conditions: factors that are not applicable to Korea, factors whose source did not exist during the period, factors blocked by missing point-in-time filing provenance, missing raw filings, normalization failures, missing canonical dependencies, lookback warm-up, and legitimate formula-domain nulls. Treating all of these as one “missing” state hides actionable defects and makes denominator changes look like data recovery.

The recovery must preserve strict point-in-time semantics. A fiscal period end is not evidence that the market knew the filing, and a missing observation must never be converted to zero solely to improve coverage. K-GAAP and IFRS may coexist for filings from 2009 through 2012, so regime must be detected per filing rather than selected solely by year.

## Solution

Build a resumable recovery and audit path that traces each historical factor cell through filing availability, raw disclosure, semantic normalization, canonical account dependencies, and factor evaluation. Repair proven collection and normalization defects, recollect the missing historical disclosures, rebuild all three financial bases, and publish both global-contract coverage and KR-applicable coverage.

The audit assigns one mutually exclusive primary reason to every absent cell, while retaining secondary evidence for diagnosis. The recovery accepts only observed filing dates for point-in-time joins. Accounting identities constrain candidate mappings and route contradictions to review; they do not silently force a convenient mapping.

## User Stories

1. As a factor consumer, I can distinguish the global 293-factor contract from the KR-applicable factor contract so that cross-market compatibility is preserved without penalizing Korea for explicitly US-only factors.
2. As a data-quality reviewer, I can see a mutually exclusive root-cause code for every absent historical factor cell so that percentages are actionable.
3. As a point-in-time researcher, I can trace every financial factor observation to the actual disclosure date used by the as-of join.
4. As a point-in-time researcher, I am protected from fiscal-period-end fallback when disclosure metadata is absent.
5. As a recovery operator, I can resume DART metadata and statement collection without repeating completed work.
6. As a recovery operator, long historical DART searches are split into bounded date windows and their results are deduplicated.
7. As a recovery operator, every search result is either downloaded or recorded with an explicit terminal or retryable reason.
8. As a parser operator, an unencodable character in source text cannot turn a parseable filing into a failed normalization job.
9. As an accounting reviewer, I can see balance-sheet, income-flow, and cash-flow identity residuals with tolerance and unit evidence.
10. As an accounting reviewer, an identity conflict routes a mapping to review rather than automatically replacing it.
11. As a semantic-normalization consumer, K-GAAP and IFRS are detected per filing throughout the 2009–2012 transition window.
12. As a semantic-normalization consumer, sign, unit, inflow, and outflow decisions remain in the lineage of every mapped amount.
13. As a semantic-normalization consumer, account mentions recovered from unstructured text retain section, parent, statement, filing, and sector context.
14. As a factor developer, I can tell whether a factor is blocked by source availability, canonical inputs, lookback warm-up, formula domain, or an implementation defect.
15. As a release reviewer, I can compare before/after factor IDs, finite cells, yearly coverage, and reason distributions on the exact same target manifest.
16. As a release reviewer, I can verify exact row counts and checksums for source and snapshot tables across annual, quarterly, and TTM bases.
17. As a release reviewer, I can see that all evidence-calculable factors are materialized without fabricating unavailable consensus or dividend history.

## Implementation Decisions

1. Keep the global factor contract immutable at 293 entries for compatibility and publish a separate market-applicability view.
2. Mark a factor as not applicable only through explicit contract metadata. Names or current data sparsity are not sufficient evidence.
3. Partition DART searches into bounded date windows before pagination, then merge and deduplicate by filing identity.
4. Use observed DART receipt dates as availability timestamps. Never substitute the fiscal period end.
5. Model 2009–2012 as an overlapping accounting-regime interval. Detect the regime from filing evidence and preserve uncertainty.
6. Use a staged, resumable collection manifest. Each target filing records discovery, download, parse, and normalization state.
7. Preserve raw reported facts separately from canonical facts and derived factor values.
8. Assign missing cells one primary reason in this precedence order: not applicable, source outside historical range, no filing provenance, raw disclosure absent, normalization failed, canonical dependency absent, lookback warm-up, formula domain, implementation gap.
9. Retain all subordinate evidence even though reporting uses one primary reason.
10. Treat diagnostic output as non-critical: console encoding failures must not affect extraction or normalization.
11. Use accounting identities as validation constraints with scale-aware tolerances. Identity failures create review evidence, not forced mappings.
12. Rebuild all three financial bases from the same recovered source scope and regenerate all 33 historical snapshots only after source validation succeeds.
13. Use a new recovery contract/status version so an earlier completed checkpoint cannot suppress corrected work.
14. Report both factor-ID coverage and finite-cell coverage. For each, report global and KR-applicable denominators and disclose denominator exclusions.

## Testing Decisions

1. Test long-range filing metadata queries at the HTTP boundary: multiple bounded windows must be requested and their rows merged without duplication.
2. Reproduce Windows CP949 output with strict encoding and a legacy DART header containing a non-breaking space; extraction must complete.
3. Test market applicability without changing the 293-factor global list.
4. Test primary-reason precedence so every missing cell receives exactly one root cause.
5. Test K-GAAP, IFRS, and ambiguous transition filings from 2009–2012.
6. Test sign, unit, inflow, and outflow validation independently and in full semantic-normalization integration.
7. Test accounting identities with exact matches, rounding tolerance, unit-scale mistakes, contradictory candidates, and genuinely incomplete statements.
8. Run a representative historical differential test before the full rebuild and require recovered provenance to increase evidence-calculable financial-factor coverage.
9. Run a complete 2002–2012 target-manifest audit after rebuilding and require zero unexplained missing states.
10. Verify 33 source keys and 33 snapshot keys by exact counts and checksums.
11. Require that the rebuilt ID and finite-cell coverage do not regress from the baseline and that all recovered/evidence-calculable factors are present.

## Out of Scope

1. Inventing pre-source-history consensus, dividend, share-count, or benchmark observations.
2. Filling unavailable values with zero, forward-looking filings, or period-end timestamps.
3. Removing global factors solely to raise the reported percentage.
4. Automatically accepting an account mapping only because it closes an accounting identity.
5. Using HMRB as a runtime library; its ideas may inform rule composition and evidence ranking only.

## Further Notes

The baseline global coverage is 56/293 factor IDs and 226,735,692/1,484,898,509 finite cells. The first confirmed collection defect was an unsplit long-range DART metadata query, which returned no historical rows even though bounded queries did. The first confirmed normalization defect was a Windows console encoding exception caused by a non-breaking space in a warning message. Both defects require regression tests before recovery proceeds.

The environment used for this work does not expose an issue-tracker integration or repository label vocabulary. This repository specification is therefore the authoritative local artifact for the recovery work.
