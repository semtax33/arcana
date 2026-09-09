# FactorLab research nodes: second release

## Authorized scope and verification

Complete rolling_mean, rolling_std, filter/mask and residualize in both repositories;
implement usable initial earnings-event and previous-fiscal-quarter operations.
Preserve graph/node v1 and existing v2 behavior. New node types start at node v1
and require graph v2. Fiscal/event operations use distinct node types so existing
lag and forward_outcome definitions retain their meaning.

Reuse the previously agreed public seams: graph validate/compile/run, evaluation
HTTP persistence, and frontend editor save/load/configuration/results. Numerical
tests execute actual ClickHouse SQL in isolated fixtures. Work in red/green slices.

## Contracts to implement

- rolling_mean/std: trailing inclusive row or market-trading-day window; min_count
  valid observations; std ddof 0 or 1; no fill and no future values. Existing rolling
  max/min remain unchanged.
- mask(input, condition): false or unavailable conditions invalidate the value;
  filter uses the same gate and removes rejected rows. Place before rank/zscore to
  change their eligible population. Conditions use nonzero=true.
- residualize(target, named numeric exposures): per-date cross-sectional OLS or
  ridge with an unpenalized intercept. Complete-case fit; min_count, finite checks,
  rank-deficiency abstention. Industry must be numeric dummy exposures or handled
  by neutralize; this is not a time-series market-model regression.
- fiscal lag: select a previous reported quarter using period identity and only
  values available at the signal date; never equate 90 days with a quarter.
- next earnings: identify an actual subsequent reported earnings event, never a
  change in a forward-filled surprise factor. Preserve announcement/availability
  and fiscal-period identity; distinguish no known event from missing event target.

## Implemented nodes

| Node | Configuration | Meaning |
| --- | --- | --- |
| rolling_mean | window, unit=row/trading_day, min_count | Inclusive trailing mean of valid observations |
| rolling_std | above plus ddof=0/1 | Population/sample standard deviation; requires more valid values than ddof |
| filter | input + condition ports | Remove failed/unknown condition rows |
| mask | input + condition ports | Retain failed rows as invalid/null |
| residualize | target + 1–8 named exposures, method=ols/ridge, min_count, alpha | Per-date complete-case regression residual; intercept included and unpenalized |
| fiscal_lag | direct quarterly factor_input, period | Previous quarter by explicit fiscal identity; missing quarters are not skipped |
| earnings_outcome | score, provider=ALPHA_VANTAGE, target_field, horizons, max_wait_days, bucket_count, score_order | First/nth actual subsequent earnings release |

Residualize uses deterministic modified Gram-Schmidt QR in SQL. Ridge augments
centered exposures with sqrt(alpha) I; no server UDF or random optimizer is needed.
min_count must exceed exposure count plus the intercept. Collinear OLS or insufficient
complete cases abstain. The ridge penalty depends on input scale; normalize numeric
exposures first if comparable penalization is intended. Categorical industry codes
are not automatically encoded. This is a cross-sectional regression, not a rolling
time-series factor model.

## Fiscal-quarter operation

fiscal_lag reads fact_daily_factor_snapshot and dart_report_metadata. The latter
is the repository's shared report-availability table despite its historical name.
An exact period_end_date joins the snapshot to fiscal_year/fiscal_month. Quarter
identity requires fiscal_month in 3/6/9/12 and an unambiguous ordinal. Both current
and previous metadata must have been published by the signal date. Previous values
must be snapshots no later than the signal; a later snapshot revision is excluded.

This operation intentionally requires a direct quarterly input with drop missing
policy. It does not infer periods for arbitrary composite graphs, imputed values,
annual/TTM data, or absent/ambiguous report metadata. No 90-day approximation or
previous-available-quarter fallback occurs. Snapshot coverage is required; existing
ETL must have produced quarterly factor values and corresponding report metadata.

## Earnings operation

earnings_outcome uses us_consensus_events EARNINGS_RELEASE records and stores the
event date, availability date and fiscal period with observations. Supported target
fields are surprise_pct, reported_eps and estimated_eps. Horizons are event ordinals
1–8 within max_wait_days (1–1460). Same-day events are excluded conservatively because
signal time is date-only. Missing EPS does not cause selection of a later event;
unavailable first-event data remains pending. After the wait window, absence of a
release is reported separately from a missing event value.

Initial support is US / Alpha Vantage: its normalization uses explicit reportedDate.
Yahoo earnings_history is not selectable because its index/date is not established
as an actual release date in the current normalization path. Korea and other event
providers need a verified release-date source before being enabled. No speculative
future earnings calendar is fabricated. Historical provider vintages may be incomplete;
evaluation manifests freeze the stored observations used rather than recreating an
unavailable historical provider response.

## UI and execution

New nodes appear under Research operations. residualize creates a port for each
configured exposure name; wire a numeric input to each port. Connect fiscal_lag
directly to a quarterly factor_input. Both earnings_outcome and forward_outcome
are terminal evaluations and can share the same score. Select evaluation inclusion,
then use period evaluation and the existing future-results tab, saved-result lookup
and frozen-score re-evaluation actions. Event results label horizons as nth releases.

PIT-dependent graphs normalize their execution source to snapshots. The v2 PIT
screening-date resolver uses snapshot coverage directly, including when raw factor
rows are absent. Legacy v1 screening-date behavior is unchanged. No deployment or
new production-data ingestion was performed.

## Completion evidence (2026-09-09)

- Backend regression run: 158 tests passed across new research/event tests and
  existing graph/outcome/API/snapshot/FactorLab/backtest tests, including real
  ClickHouse fixtures. A subsequent provider-validation regression passed separately.
- Numerical coverage: row/trading windows, missing days, sample ddof, condition
  gates, OLS/ridge with intercept, collinearity, missing inputs, eight independent
  exposures, irregular period-end dates, missing quarters, future snapshot exclusion,
  fiscal screening through the public run service, actual-event ordering/availability,
  and persistent earnings results.
- Frontend: 15 tests passed, covering legacy serialization, every new node's graph
  version, residual/event controls, and loading/running saved snapshot/event graphs
  through the real page with a network fixture. TypeScript/Vite production build passed.
- Existing warnings: unrelated Pydantic schema-name warning and Vite bundle-size
  warning. Tests do not establish production dataset coverage or a live browser
  visual audit; database fixtures are isolated from production data.
