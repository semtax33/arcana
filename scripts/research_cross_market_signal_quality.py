"""Conservative provenance checks for price-history-dependent entry signals."""
import numpy as np
import pandas as pd

from engine.transformers.style_score_definitions import style_weights_for_country
from engine.workflows._internal.score_workflow import STYLE_SCORE_COLUMNS


# Bounds follow factor_metrics.add_price_momentum_factors. Including the
# skipped latest month is deliberately conservative; these flags never change
# factor values, ranking populations, holdings, or realized returns.
TECHNICAL_LOOKBACK_ROWS = {
    'tr_12_1': 253, 'tr_6_1': 127, 'tr_3_1': 64, 'ret_1m': 22,
    'high52w_gap_pct': 252, 'vol_12_1_ann': 253, 'risk_adj_mom': 253,
    'mdd1yr_12_1_pct': 253, 'k_ratio_3y': 756,
    'bb_width_pct': 20, 'bb_percent_b': 20, 'bb_upper': 20,
    'bb_middle': 20, 'bb_lower': 20, 'williams_r_14': 14,
    'cmf_20': 20, 'mfi_14': 15,
    # Exponential/cumulative indicators have no exact finite history cutoff.
    'rsi_14': np.inf, 'macd': np.inf, 'macd_signal': np.inf,
    'macd_hist': np.inf, 'ati': np.inf,
    **{f'na_{n}': n for n in (5, 20, 50, 150, 200)},
    **{f'ma_{n}': n for n in (50, 120, 150, 200)},
}

BETA_DEPENDENT_FACTORS = {
    'beta', 'cost_of_equity', 'wacc', 'roic_wacc_spread', 'economic_profit',
    'economic_profit_yield', 'delta_economic_profit', 'roic_wacc_spread_growth_1y',
    'roe_cost_of_equity_spread_pct', 'roiic_wacc_spread',
    'intangible_adjusted_roe_spread_pct', 'equity_duration_20y', 'rim_upside_potential',
}


def underlying_factors(factors):
    result=set(factors)
    for country in ['KR','US']:
        styles=style_weights_for_country(country)
        for style,weights in styles.items():
            if ('style_'+STYLE_SCORE_COLUMNS[style] in result or 'style_total_score' in result):
                result.update(weights)
    return result


def uses_beta_history(factors):
    return any(f in BETA_DEPENDENT_FACTORS or 'pvgo' in f for f in underlying_factors(factors))


def economic_validity(frame):
    """Apply declared positive-capital conditions within one financial basis.

    Raw per-stock caches remain untouched. Missing denominator evidence cannot
    become a valid ranking observation, and the dated size universe is intact.
    """
    result=frame.copy();counts={}
    for factor,denominators in {'roe':('avg_parent_equity','ceq'),
                                'debt_to_equity':('seq',)}.items():
        if factor not in result:continue
        valid=pd.Series(True,index=result.index)
        for denominator in denominators:
            values=pd.to_numeric(result.get(denominator,pd.Series(np.nan,index=result.index)),errors='coerce')
            valid &= values.gt(0)&np.isfinite(values)
        counts[factor]=int((result[factor].notna()&~valid).sum())
        result[factor]=result[factor].where(valid)
    return result,counts


def price_review_age_rows(frame):
    """Age of the last unresolved observation, reset at confirmed new listings."""
    positions = pd.Series(np.arange(len(frame), dtype=float), index=frame.index)
    last = positions.where(frame.unresolved_price_event.astype(bool))
    last = last.groupby(frame.listing_episode).ffill()
    return positions - last


def beta_price_source_review(frame):
    """Flag observations the existing weekly beta path has not source-cleared.

    That path consumes adjusted quotes without volume or listing-episode
    controls. A changed nontraded quote or relisting boundary therefore needs
    a separate check. Carry the review conservatively instead of pretending a
    row count proves 104 valid benchmark-matched weeks have elapsed.
    """
    close=frame.split_adj_close.where(frame.split_adj_close.gt(0),frame.close)
    traded=frame.volume.gt(0)&close.gt(0)
    last=close.where(traded).ffill()
    changed=(~traded)&close.gt(0)&last.gt(0)&(~np.isclose(close,last,rtol=1e-10,atol=1e-12))
    episode=frame.listing_episode.diff().fillna(0).ne(0)
    return (frame.unresolved_price_event.astype(bool)|changed|episode).cummax()


def technical_lookback(factors, *, gate=None):
    factors = underlying_factors(factors)
    if gate in {'positive_momentum', 'quality_momentum'}:
        factors.add('tr_12_1')
    return max((TECHNICAL_LOOKBACK_ROWS.get(f, 0) for f in factors), default=0)
