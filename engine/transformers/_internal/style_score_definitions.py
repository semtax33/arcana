from __future__ import annotations

from dataclasses import dataclass


STYLE_VALUE = "VALUE"
STYLE_QUALITY = "QUALITY"
STYLE_GROWTH = "GROWTH"
STYLE_CONSENSUS = "CONSENSUS"
STYLE_MOMENTUM = "MOMENTUM"
STYLE_RISK = "RISK"
STYLE_DIVIDEND = "DIVIDEND"

HIGHER_BETTER = 1
LOWER_BETTER = -1


@dataclass(frozen=True)
class FactorDefinition:
    factor_id: str
    canonical_id: str
    style_group: str
    direction: int
    weight: float
    winsor_low: float = 0.01
    winsor_high: float = 0.99
    apply_to_financials: bool = True


FACTOR_ALIASES = {
    "EARNINGS_YIELD": "epr",
    "EBITDA_YIELD": "ebitda_to_ev",
    "FCF_YIELD": "fcfpr",
    "BOOK_TO_MARKET": "bpr",
    "SALES_YIELD": "spr",
    "ROE": "roe",
    "ROIC": "roic_operational",
    "OPERATING_MARGIN": "opm",
    "CFO_TO_NET_INCOME": "cfo_to_net_income",
    "ASSET_TURNOVER": "asset_turnover",
    "DEBT_TO_EQUITY": "debt_to_equity",
    "FCF_TO_NET_INCOME": "fcf_to_net_income",
    "SALES_GROWTH_YOY": "sales_yoy_pct",
    "SALES_CAGR_3Y": "sales_cagr_3y",
    "OPERATING_PROFIT_GROWTH_YOY": "op_yoy_pct",
    "EPS_GROWTH_YOY": "eps_yoy_pct",
    "CFO_GROWTH_YOY": "cfo_yoy_pct",
    "REAL_EPS_REVISION_1M": "real_eps_revision_1m_pct",
    "REAL_EPS_EXPECTED_GROWTH": "real_eps_expected_growth",
    "REAL_REVENUE_EXPECTED_GROWTH": "real_revenue_expected_growth",
    "REAL_OPERATING_INCOME_EXPECTED_GROWTH": "real_operating_income_expected_growth",
    "REAL_NET_INCOME_EXPECTED_GROWTH": "real_net_income_expected_growth",
    "REAL_EPS_SURPRISE": "real_eps_surprise_pct",
    "REAL_REVENUE_SURPRISE": "real_revenue_surprise_pct",
    "REAL_OPERATING_INCOME_SURPRISE": "real_operating_income_surprise_pct",
    "REAL_NET_INCOME_SURPRISE": "real_net_income_surprise_pct",
    "MOM_12M_1M": "tr_12_1",
    "MOM_6M": "tr_6_1",
    "MOM_3M": "tr_3_1",
    "HIGH_52W_PROXIMITY": "high52w_gap_pct",
    "VOLUME_ACCELERATION": "adturn_pct_12_1",
    "NET_DEBT_TO_EBITDA": "net_debt_to_ebitda",
    "INTEREST_COVERAGE": "interest_coverage",
    "VOLATILITY_1Y": "vol_12_1_ann",
    "MDD_1Y": "mdd1yr_12_1_pct",
    "DIVIDEND_YIELD": "dividend_yield",
    "MARKET_DIVIDEND_YIELD": "dividend_yield",
    "SHAREHOLDER_DIVIDEND_YIELD": "sharehold_div_yield",
    "NET_BUYBACK_YIELD": "sharehold_net_buyback_yield",
    "SHAREHOLDER_RETURN": "sharehold_return",
    "SHAREHOLDER_YIELD": "shareholder_yield",
    "PAYOUT_RATIO": "payout_ratio",
    "EARNINGS_PAYOUT_RATIO": "earnings_payout_ratio",
    "FCF_PAYOUT_RATIO": "fcf_payout_ratio",
    "FCF_DIVIDEND_COVERAGE": "fcf_dividend_coverage",
    "FCF_AFTER_DIVIDENDS": "fcf_after_dividends",
    "FCF_AFTER_DIVIDENDS_TO_MARKET_CAP": "fcf_after_dividends_to_market_cap_pct",
    "SHAREHOLDER_RETURN_FCF_COVERAGE": "shareholder_return_fcf_coverage",
    "FCFE_DIVIDEND_COVERAGE": "fcfe_dividend_coverage",
    "FCFE_PAYOUT_RATIO": "fcfe_payout_ratio",
    "FCF_YIELD_DIVIDEND_YIELD_SPREAD": "fcf_yield_dividend_yield_spread",
    "EPS_DIVIDEND_COVERAGE": "eps_dividend_coverage",
    "DPS_CAGR_5Y": "dps_cagr_5y",
    "DIVIDEND_CONSISTENCY_STREAK": "dividend_consistency_streak",
    "DPS_VOLATILITY_5Y": "dps_volatility_5y",
    "DIVIDEND_CUT": "dividend_cut",
}


STYLE_WEIGHTS = {
    STYLE_VALUE: {
        "epr": 0.25,
        "ebitda_to_ev": 0.20,
        "fcfpr": 0.25,
        "bpr": 0.20,
        "spr": 0.10,
    },
    STYLE_QUALITY: {
        "roe": 0.20,
        "roic_operational": 0.20,
        "opm": 0.15,
        "asset_turnover": 0.10,
        "debt_to_equity": 0.10,
    },
    STYLE_GROWTH: {
        "sales_yoy_pct": 0.15,
        "op_yoy_pct": 0.15,
        "eps_yoy_pct": 0.20,
        "cfo_yoy_pct": 0.20,
        "fcf_yoy_pct": 0.30,
    },
    STYLE_CONSENSUS: {
        "real_eps_revision_1m_pct": 0.15,
        "real_eps_expected_growth": 0.20,
        "real_revenue_expected_growth": 0.15,
        "real_operating_income_expected_growth": 0.18,
        "real_net_income_expected_growth": 0.12,
        "real_eps_surprise_pct": 0.08,
        "real_revenue_surprise_pct": 0.04,
        "real_operating_income_surprise_pct": 0.04,
        "real_net_income_surprise_pct": 0.04,
    },
    STYLE_MOMENTUM: {
        "tr_12_1": 0.35,
        "tr_6_1": 0.25,
        "tr_3_1": 0.15,
        "high52w_gap_pct": 0.15,
        "adturn_pct_12_1": 0.10,
    },
    STYLE_RISK: {
        "debt_to_equity": 0.20,
        "net_debt_to_ebitda": 0.20,
        "interest_coverage": 0.20,
        "vol_12_1_ann": 0.20,
        "mdd1yr_12_1_pct": 0.20,
    },
    STYLE_DIVIDEND: {
        "dividend_yield": 0.14,
        "shareholder_yield": 0.12,
        "fcf_dividend_coverage": 0.12,
        "shareholder_return_fcf_coverage": 0.10,
        "fcfe_dividend_coverage": 0.08,
        "fcf_payout_ratio": 0.10,
        "payout_ratio": 0.07,
        "fcf_yield_dividend_yield_spread": 0.08,
        "dps_cagr_5y": 0.07,
        "dividend_consistency_streak": 0.05,
        "dps_volatility_5y": 0.04,
        "dividend_cut": 0.03,
    },
}

# Korean weights above are intentionally unchanged.  US consensus coverage has
# different stable fields, so it uses a separate provider-neutral score basket.
US_STYLE_WEIGHTS = {
    **{group: dict(weights) for group, weights in STYLE_WEIGHTS.items() if group != STYLE_CONSENSUS},
    STYLE_CONSENSUS: {
        "us_eps_revision_30d_pct": 0.35,
        "us_eps_revision_breadth_30d_pct": 0.20,
        "us_eps_revision_acceleration_30d_pct": 0.15,
        "us_eps_dispersion_pct": 0.10,
        "us_revenue_dispersion_pct": 0.05,
        "us_eps_surprise_pct": 0.15,
    },
}
US_CONSENSUS_CORE_FACTORS = {
    "us_eps_revision_30d_pct",
    "us_eps_revision_breadth_30d_pct",
    "us_eps_revision_acceleration_30d_pct",
    "us_eps_dispersion_pct",
}


TOTAL_WEIGHTS = {
    "DEFAULT": {
        STYLE_VALUE: 0.22,
        STYLE_QUALITY: 0.22,
        STYLE_GROWTH: 0.14,
        STYLE_CONSENSUS: 0.08,
        STYLE_MOMENTUM: 0.16,
        STYLE_RISK: 0.10,
        STYLE_DIVIDEND: 0.08,
    },
    "MINERVINI_ZWEIG": {
        STYLE_VALUE: 0.20,
        STYLE_QUALITY: 0.20,
        STYLE_GROWTH: 0.22,
        STYLE_CONSENSUS: 0.08,
        STYLE_MOMENTUM: 0.20,
        STYLE_RISK: 0.10,
        STYLE_DIVIDEND: 0.00,
    },
    "DIVIDEND_QUALITY": {
        STYLE_VALUE: 0.20,
        STYLE_QUALITY: 0.30,
        STYLE_GROWTH: 0.07,
        STYLE_CONSENSUS: 0.03,
        STYLE_MOMENTUM: 0.05,
        STYLE_RISK: 0.10,
        STYLE_DIVIDEND: 0.25,
    },
}


LOWER_BETTER_FACTORS = {
    "debt_to_equity",
    "net_debt_to_ebitda",
    "vol_12_1_ann",
    "payout_ratio",
    "earnings_payout_ratio",
    "fcf_payout_ratio",
    "fcfe_payout_ratio",
    "dps_volatility_5y",
    "dps_volatility_10y",
    "dividend_cut",
    "special_dividend_ratio_pct",
    "us_eps_dispersion_pct",
    "us_revenue_dispersion_pct",
}


VALUE_LIMITS = {
    "roe": (-500.0, 500.0),
    "roa": (-200.0, 200.0),
    "opm": (-200.0, 200.0),
    "gpm": (-200.0, 200.0),
    "debt_to_equity": (-10.0, 50.0),
    "net_debt_to_ebitda": (-20.0, 50.0),
    "vol_12_1_ann": (0.0, 5.0),
    "mdd1yr_12_1_pct": (-100.0, 0.0),
    "sharehold_div_yield": (0.0, 100.0),
    "dividend_yield": (0.0, 100.0),
    "sharehold_net_buyback_yield": (-100.0, 100.0),
    "sharehold_return": (-100.0, 100.0),
    "shareholder_yield": (-100.0, 100.0),
    "payout_ratio": (0.0, 300.0),
    "earnings_payout_ratio": (0.0, 300.0),
    "fcf_payout_ratio": (0.0, 300.0),
    "fcfe_payout_ratio": (0.0, 300.0),
    "fcf_dividend_coverage": (-50.0, 100.0),
    "shareholder_return_fcf_coverage": (-50.0, 100.0),
    "fcfe_dividend_coverage": (-50.0, 100.0),
    "fcf_yield_dividend_yield_spread": (-100.0, 100.0),
    "eps_dividend_coverage": (-50.0, 100.0),
    "dps_cagr_5y": (-100.0, 500.0),
    "dividend_consistency_streak": (0.0, 100.0),
    "dps_volatility_5y": (0.0, 1_000_000_000.0),
    "dividend_cut": (0.0, 1.0),
}


def canonical_factor_id(factor_id: str) -> str:
    text = str(factor_id).strip()
    if not text:
        raise ValueError("factor_id must not be empty")
    return FACTOR_ALIASES.get(text.upper(), text.lower())


def factor_direction(factor_id: str) -> int:
    return LOWER_BETTER if canonical_factor_id(factor_id) in LOWER_BETTER_FACTORS else HIGHER_BETTER


def style_factor_definitions() -> dict[str, FactorDefinition]:
    definitions: dict[str, FactorDefinition] = {}
    reverse_aliases = {runtime: alias for alias, runtime in FACTOR_ALIASES.items()}
    all_weights = [STYLE_WEIGHTS, US_STYLE_WEIGHTS]
    for weight_set in all_weights:
        for style_group, weights in weight_set.items():
            for factor_id, weight in weights.items():
                existing = definitions.get(factor_id)
                if existing is not None and weight <= existing.weight:
                    continue
                low, high = (
                    (0.05, 0.95)
                    if style_group in {STYLE_GROWTH, STYLE_CONSENSUS, STYLE_DIVIDEND}
                    else (0.01, 0.99)
                )
                definitions[factor_id] = FactorDefinition(
                    factor_id=factor_id,
                    canonical_id=reverse_aliases.get(factor_id, factor_id.upper()),
                    style_group=style_group,
                    direction=factor_direction(factor_id),
                    weight=weight,
                    winsor_low=low,
                    winsor_high=high,
                    apply_to_financials=factor_id
                    not in {"ebitda_to_ev", "fcfpr", "asset_turnover"},
                )
    return definitions


STYLE_FACTOR_DEFINITIONS = style_factor_definitions()


def style_profile_weights(style_profile: str) -> dict[str, float]:
    profile = str(style_profile or "DEFAULT").strip().upper()
    if profile not in TOTAL_WEIGHTS:
        allowed = ", ".join(sorted(TOTAL_WEIGHTS))
        raise ValueError(f"style_profile must be one of: {allowed}")
    return TOTAL_WEIGHTS[profile]


def style_weights_for_country(country: str | None) -> dict[str, dict[str, float]]:
    return US_STYLE_WEIGHTS if str(country or "").strip().upper() == "US" else STYLE_WEIGHTS
