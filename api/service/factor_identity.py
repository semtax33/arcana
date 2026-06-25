from __future__ import annotations

import re
from functools import lru_cache

from api.service.style_score_catalog import STYLE_SCORE_FACTORS
from engine.loaders.factors import create_factor_catalog_dataframe
from engine.transformers.factors import preferred_factor_columns


_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
_DUP_UNDERSCORE_RE = re.compile(r"_+")
_SAFE_FACTOR_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")


def canonical_factor_id(factor_id: str) -> str:
    """Normalize user-facing factor identifiers to runtime factor ids."""

    text = str(factor_id).strip()
    key = _alias_key(text)
    aliases = _factor_aliases()
    if key in aliases:
        return aliases[key]
    if _SAFE_FACTOR_ID_RE.match(text):
        return text.lower()
    raise ValueError(f"invalid factor_id: {factor_id!r}")


def _alias_key(value: str) -> str:
    normalized = _NON_ALNUM_RE.sub("_", str(value).strip().lower())
    normalized = _DUP_UNDERSCORE_RE.sub("_", normalized)
    return normalized.strip("_")


def _slash_to_alias_key(value: str) -> str:
    expanded = re.sub(r"\s*/\s*", " to ", str(value).strip().lower())
    return _alias_key(expanded)


@lru_cache(maxsize=1)
def _factor_aliases() -> dict[str, str]:
    factor_ids = list(preferred_factor_columns())
    aliases: dict[str, str] = {}

    def add(raw_value: str, canonical_id: str) -> None:
        if not raw_value:
            return
        aliases.setdefault(_alias_key(raw_value), canonical_id)
        if "/" in raw_value:
            aliases.setdefault(_slash_to_alias_key(raw_value), canonical_id)

    catalog = create_factor_catalog_dataframe(factor_ids)
    for factor_id in factor_ids:
        add(factor_id, factor_id)
        add(factor_id.upper(), factor_id)
        add(factor_id.replace("_", " "), factor_id)
        add(factor_id.replace("_", "-"), factor_id)

    for factor_id, definition in STYLE_SCORE_FACTORS.items():
        add(factor_id, factor_id)
        add(factor_id.upper(), factor_id)
        add(factor_id.replace("_", " "), factor_id)
        add(factor_id.replace("_", "-"), factor_id)
        add(definition.column_name, factor_id)
        add(definition.column_name.upper(), factor_id)
        add(definition.factor_name, factor_id)

    for row in catalog.to_dict("records"):
        factor_id = str(row["factor_id"])
        add(str(row.get("factor_name") or ""), factor_id)

    # Common market shorthand that cannot be inferred from plain punctuation.
    aliases.update(
        {
            "ev_nopat": "ev_to_nopat",
            "ev_ebitda": "ev_to_ebitda",
            "fcf_ev_yield": "fcf_to_ev_yield",
            "r_d_market_cap": "rnd_to_market_cap",
            "r_d_to_market_cap": "rnd_to_market_cap",
            "rd_to_market_cap": "rnd_to_market_cap",
        }
    )
    return aliases
