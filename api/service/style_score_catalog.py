from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_SCREEN_STYLE_PROFILE = "DEFAULT"
DEFAULT_FACTOR_SCREEN_STYLE_PROFILE = "MINERVINI_ZWEIG"


@dataclass(frozen=True)
class StyleScoreFactorDefinition:
    factor_id: str
    factor_name: str
    column_name: str
    factor_type: str = "style_score"
    factor_group: str = "style_score"
    unit: str = "score"
    value_direction: str = "HIGHER_BETTER"
    description: str | None = None
    is_active: bool = True


STYLE_SCORE_FACTORS: dict[str, StyleScoreFactorDefinition] = {
    "style_total_score": StyleScoreFactorDefinition(
        factor_id="style_total_score",
        factor_name="Style Score",
        column_name="total_score",
        description="Composite style score from the style score pipeline.",
    ),
    "style_value_score": StyleScoreFactorDefinition(
        factor_id="style_value_score",
        factor_name="Style Score - Value",
        column_name="value_score",
    ),
    "style_quality_score": StyleScoreFactorDefinition(
        factor_id="style_quality_score",
        factor_name="Style Score - Quality",
        column_name="quality_score",
    ),
    "style_growth_score": StyleScoreFactorDefinition(
        factor_id="style_growth_score",
        factor_name="Style Score - Growth",
        column_name="growth_score",
    ),
    "style_momentum_score": StyleScoreFactorDefinition(
        factor_id="style_momentum_score",
        factor_name="Style Score - Momentum",
        column_name="momentum_score",
    ),
    "style_risk_score": StyleScoreFactorDefinition(
        factor_id="style_risk_score",
        factor_name="Style Score - Risk",
        column_name="risk_score",
    ),
    "style_dividend_score": StyleScoreFactorDefinition(
        factor_id="style_dividend_score",
        factor_name="Style Score - Dividend",
        column_name="dividend_score",
    ),
}

STYLE_SCORE_FACTOR_ALIASES = {
    definition.column_name: factor_id
    for factor_id, definition in STYLE_SCORE_FACTORS.items()
}


def canonical_style_score_factor_id(factor_id: str) -> str:
    return STYLE_SCORE_FACTOR_ALIASES.get(factor_id, factor_id)


def is_style_score_factor(factor_id: str) -> bool:
    return canonical_style_score_factor_id(factor_id) in STYLE_SCORE_FACTORS


def style_score_factor_definition(factor_id: str) -> StyleScoreFactorDefinition:
    return STYLE_SCORE_FACTORS[canonical_style_score_factor_id(factor_id)]


def style_score_factor_metadata(factor_id: str) -> dict[str, Any]:
    definition = style_score_factor_definition(factor_id)
    return {
        "factor_id": definition.factor_id,
        "factor_name": definition.factor_name,
        "factor_type": definition.factor_type,
        "factor_group": definition.factor_group,
        "unit": definition.unit,
        "value_direction": definition.value_direction,
        "description": definition.description,
        "is_active": definition.is_active,
    }
