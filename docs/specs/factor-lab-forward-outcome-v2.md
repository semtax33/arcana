# FactorLab graph v2: forward outcomes

## Scope

Implement the agreed first release in Arcana and arcana-front: independent,
multiple forward-outcome evaluations of a saved score run; explicit row/trading-day
lag; persisted evaluation distributions and UI; backwards-compatible graph and
node versions. Event/next-fiscal-period labels, residual regression, scenario
probabilities and ForecastTest handoff are later releases, not implicit meanings
of a daily snapshot outcome.

## Compatibility contract

- Missing graph/node versions mean v1. Existing v1 score and backtest behavior stays.
- Graph v2 keeps `outputs.final_node_id` for scores and adds
  `outputs.evaluation_node_ids` for terminal evaluations. Labels cannot feed scores.
- Existing nodes support node v1. Lag v2 requires `unit: row|trading_day`;
  lag v1 always retains its previous-row semantics.
- A new `forward_outcome` node v1 is available in graph v2, with input `score`.
- Unknown versions fail validation rather than being upgraded silently.

## Outcome contract

Each evaluation specifies target_factor_id, financial_basis, measure
(level/change/pct_change/direction), horizons, unit (trading_day/calendar_day),
bucket_count, score_order (higher/lower), and optional period_policy
(same_period/allow_change, default same_period). A change is the difference of two
snapshot levels; it is not a cumulative revision. Percentage change requires a
positive baseline and is expressed in percent. Direction is the sign of change.
Targets are exact-date PIT snapshots; no imputation, raw fallback, future fill or
synthetic fiscal/event interpretation. Changes across different financial periods
are excluded by default. Explicit allow_change supports comparing reported metrics
as new accounting periods become available; it does not locate the next quarter.
The check uses the snapshot's financial_period field: missing period metadata is
not evidence of a fixed consensus forecast period, and FY1 rollovers are not resolved.
Missing baseline, target, period comparability, zero/nonpositive
denominator and immature observations are reported separately.

Evaluation cutoff is explicit, cannot precede a frozen signal or be later than
today in Asia/Seoul, and is persisted with
the graph, input digest, definitions and results. Buckets are formed per signal
date from scores before inspecting target availability; Q1 is the lowest score
priority and QK the highest. Ties stay together. Results include opportunities,
valid/missing/pending counts, mean, median, p10/p90, positive/negative/zero rates,
distinct security/date counts and a same-score-cohort baseline. These are pooled
descriptive statistics, not independent-event counts, confidence or causal proof.

## Agreed verification boundaries

Following the user's acceptance of the implementation and verification proposal:
public graph validation/compilation and DTO round trips; evaluation service and
HTTP run/read boundaries with deterministic database fixtures; editor save/load,
version controls and rendered evaluation results. Use red/green vertical slices,
then existing FactorLab/backtest regressions and the frontend production build.

## Implementation checklist

- [x] Version/graph validation and old graph compatibility
- [x] Trading-day lag and numerical execution check
- [x] Forward outcome numerical behavior and maturity/coverage diagnostics
- [x] Run integration, persistence and result endpoints
- [x] Frontend editor, node controls, history evaluation and results
- [x] API/UI integration and existing regressions

## Usage

1. Open a saved FactorLab graph and keep a signal node as Final Node.
2. Add Forward outcome, connect a signal's output to its score input, and enable
   evaluation. Several outcome nodes may read the same signal or different branches.
3. Select a registered target factor, financial basis, measure and horizons.
   Use allow_change for changes in reported margin/ROIC across accounting periods.
4. Set the signal start/end dates in the experiment and the signal frequency and
   evaluation cutoff under the future-results tab. Run the period evaluation.
5. Inspect distributions and valid/missing/pending counts. Keep the run ID to load
   results or evaluate the same frozen scores at a later cutoff.

New lag nodes use node v2 and trading_day. Existing lag v1 nodes retain row
semantics; selecting v2 on an existing node starts with row to preserve meaning.
The trading calendar is inferred from observed market prices independently of
sector filters. ALL uses the union of market dates; use a single market when local
holiday semantics matter. Missing price-calendar coverage is not a holiday oracle.

## API and storage

- Run via POST `/api/factor-lab/runs` with graph v2, mode history and
  evaluation_as_of. The existing run response gains an optional evaluation batch.
- POST `/api/factor-lab/runs/{run_id}/evaluations` with `{ "as_of": "2026-01-07" }`
  creates a new immutable evaluation using frozen scores; only completed runs qualify.
- GET the same URL returns the latest 100 stored batches. The UI loads the latest.
- factor_lab_run_definition stores execution graphs; factor_lab_evaluation stores
  result JSON and the input/observation manifest. Tables are created on first use.
  Deleting an experiment removes its associated definitions and evaluations.
- Score runs remain usable if an outcome query fails; the run warning includes
  the retry endpoint. Sparse target snapshots are reported as missing, never zero.
- Evaluation requires actual PIT snapshot data. Mock mode does not invent outcomes.
  A daily snapshot difference does not implement next earnings surprise, a fiscal
  lag, cumulative EPS revisions or a causal claim. Existing v1 evaluate-node SQL
  behavior is preserved; this release implements independent forward_outcome only.

## Verification (2026-09-09)

Backend: 133 tests passed together across the new graph/outcome/service/HTTP and
real ClickHouse fixtures plus existing FactorLab/snapshot/backtest suites. A later
PIT SQL-preview regression and affected service tests passed (48 tests).
Real database fixtures use session-local tables or a dedicated temporary database;
production data is not modified. Covered cases include missing trading-day inputs,
sector-independent calendars, future-source exclusion, multiple outcomes,
immutable re-evaluation history and deletion.

Frontend: 5 tests passed for legacy graph round trips, versioned evaluation graphs,
inspector input, pending-result display and opening/running a saved graph through
the real page with a network fixture. TypeScript and Vite production build passed.
Remaining build warnings are the existing bundle-size warning; backend reports an
existing unrelated Pydantic schema-name warning. No live browser visual audit or
deployment was performed.
