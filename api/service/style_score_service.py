from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Callable
from zoneinfo import ZoneInfo

from api.config.clickhouse import get_clickhouse_client
from api.model.style_score import (
    FactorScoreBreakdown,
    StyleScoreComponent,
    StyleScoreComponentDetailResponse,
    StyleScoreComponentFactor,
    StyleScoreComponentsResponse,
    StyleScoreDetailResponse,
    StyleScoreResponse,
    StyleScoreRow,
)
from engine.transformers.style_score_definitions import (
    FACTOR_ALIASES,
    STYLE_WEIGHTS,
    style_profile_weights,
)


DEFAULT_STYLE_PROFILE = "DEFAULT"
STYLE_PROFILES = {"DEFAULT", "MINERVINI_ZWEIG", "DIVIDEND_QUALITY"}
COMPONENT_SCORE_FIELDS = {
    "COMPOSITE": "total_score",
    "VALUE": "value_score",
    "QUALITY": "quality_score",
    "GROWTH": "growth_score",
    "MOMENTUM": "momentum_score",
    "RISK": "risk_score",
    "DIVIDEND": "dividend_score",
}
COMPONENT_LABELS = {
    "COMPOSITE": "Composite Score",
    "VALUE": "Value",
    "QUALITY": "Quality",
    "GROWTH": "Growth & Real Consensus",
    "MOMENTUM": "Momentum",
    "RISK": "Risk",
    "DIVIDEND": "Dividend & Shareholder Return",
}
FACTOR_LABELS = {
    "dividend_yield": "DIVIDEND_YIELD",
    "sharehold_div_yield": "SHAREHOLDER_DIVIDEND_YIELD",
    "sharehold_net_buyback_yield": "NET_BUYBACK_YIELD",
    "sharehold_return": "SHAREHOLDER_RETURN",
    "shareholder_yield": "SHAREHOLDER_YIELD",
    "payout_ratio": "PAYOUT_RATIO",
    "fcf_payout_ratio": "FCF_PAYOUT_RATIO",
    "fcf_dividend_coverage": "FCF_DIVIDEND_COVERAGE",
    "shareholder_return_fcf_coverage": "SHAREHOLDER_RETURN_FCF_COVERAGE",
    "fcfe_dividend_coverage": "FCFE_DIVIDEND_COVERAGE",
    "fcf_yield_dividend_yield_spread": "FCF_YIELD_DIVIDEND_YIELD_SPREAD",
    "dps_cagr_5y": "DPS_CAGR_5Y",
    "dividend_consistency_streak": "DIVIDEND_CONSISTENCY_STREAK",
    "dps_volatility_5y": "DPS_VOLATILITY_5Y",
    "dividend_cut": "DIVIDEND_CUT",
    "real_eps_revision_1m_pct": "REAL_EPS_REVISION_1M",
    "real_eps_expected_growth": "REAL_EPS_EXPECTED_GROWTH",
    "real_revenue_expected_growth": "REAL_REVENUE_EXPECTED_GROWTH",
    "real_operating_income_expected_growth": "REAL_OPERATING_INCOME_EXPECTED_GROWTH",
    "real_net_income_expected_growth": "REAL_NET_INCOME_EXPECTED_GROWTH",
    "real_eps_surprise_pct": "REAL_EPS_SURPRISE",
    "real_revenue_surprise_pct": "REAL_REVENUE_SURPRISE",
    "real_operating_income_surprise_pct": "REAL_OPERATING_INCOME_SURPRISE",
    "real_net_income_surprise_pct": "REAL_NET_INCOME_SURPRISE",
}


class StyleScoreService:
    def __init__(
        self,
        client_factory: Callable[[], Any] = get_clickhouse_client,
        today_factory: Callable[[], date] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._today_factory = today_factory or _today_kst

    def get_style_scores(
        self,
        *,
        trade_date: date | None = None,
        style_profile: str = DEFAULT_STYLE_PROFILE,
        limit: int = 100,
        min_confidence: float | None = None,
        industry_group_code: str | None = None,
        sector_code: str | None = None,
    ) -> StyleScoreResponse:
        requested_date = trade_date or self._today_factory()
        profile = _normalize_style_profile(style_profile)
        normalized_limit = _normalize_limit(limit)
        normalized_confidence = _normalize_min_confidence(min_confidence)

        client = self._client_factory()
        try:
            target_date = _resolve_available_trade_date(client, requested_date, profile)
            rows = _records(
                client.query_df(
                    _build_style_score_list_query(
                        has_min_confidence=normalized_confidence is not None,
                        has_industry_group_code=bool(industry_group_code),
                        has_sector_code=bool(sector_code),
                    ),
                    parameters={
                        "trade_date": target_date.isoformat(),
                        "style_profile": profile,
                        "limit": normalized_limit,
                        "min_confidence": normalized_confidence or 0.0,
                        "industry_group_code": industry_group_code or "",
                        "sector_code": sector_code or "",
                    },
                )
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        return StyleScoreResponse(
            trade_date=target_date,
            style_profile=profile,
            total_count=len(rows),
            rows=[_to_style_row(index, row) for index, row in enumerate(rows, start=1)],
        )

    def get_style_score_detail(
        self,
        security_id: str,
        *,
        trade_date: date | None = None,
        style_profile: str = DEFAULT_STYLE_PROFILE,
    ) -> StyleScoreDetailResponse:
        if not security_id:
            raise ValueError("security_id must not be empty")
        requested_date = trade_date or self._today_factory()
        profile = _normalize_style_profile(style_profile)

        client = self._client_factory()
        try:
            target_date = _resolve_available_trade_date(
                client,
                requested_date,
                profile,
                security_id=security_id,
            )
            style_rows = _records(
                client.query_df(
                    _build_style_score_detail_query(),
                    parameters={
                        "trade_date": target_date.isoformat(),
                        "style_profile": profile,
                        "security_id": security_id,
                    },
                )
            )
            factor_rows = _records(
                client.query_df(
                    _build_factor_breakdown_query(),
                    parameters={
                        "trade_date": target_date.isoformat(),
                        "security_id": security_id,
                    },
                )
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        return StyleScoreDetailResponse(
            row=_to_style_row(1, style_rows[0]) if style_rows else None,
            factors=[_to_factor_breakdown(row) for row in factor_rows],
        )

    def get_style_score_components(
        self,
        security_id: str,
        *,
        trade_date: date | None = None,
        style_profile: str = DEFAULT_STYLE_PROFILE,
    ) -> StyleScoreComponentsResponse:
        requested_date = trade_date or self._today_factory()
        profile = _normalize_style_profile(style_profile)
        detail = self.get_style_score_detail(
            security_id,
            trade_date=requested_date,
            style_profile=profile,
        )
        target_date = _resolved_detail_trade_date(detail, requested_date)
        security = _resolved_detail_security(detail, security_id)
        return StyleScoreComponentsResponse(
            trade_date=target_date,
            style_profile=profile,
            security_id=security["security_id"],
            stock_code=security["stock_code"],
            company_name=security["company_name"],
            components=_build_components(detail.row, detail.factors, profile),
        )

    def get_style_score_component_detail(
        self,
        security_id: str,
        component_key: str,
        *,
        trade_date: date | None = None,
        style_profile: str = DEFAULT_STYLE_PROFILE,
    ) -> StyleScoreComponentDetailResponse:
        requested_date = trade_date or self._today_factory()
        profile = _normalize_style_profile(style_profile)
        component = _normalize_component_key(component_key)
        detail = self.get_style_score_detail(
            security_id,
            trade_date=requested_date,
            style_profile=profile,
        )
        target_date = _resolved_detail_trade_date(detail, requested_date)
        security = _resolved_detail_security(detail, security_id)
        components = {
            item.component_key: item
            for item in _build_components(detail.row, detail.factors, profile)
        }
        return StyleScoreComponentDetailResponse(
            trade_date=target_date,
            style_profile=profile,
            security_id=security["security_id"],
            stock_code=security["stock_code"],
            company_name=security["company_name"],
            component=components[component],
            factors=_build_component_factors(detail.factors, component, profile),
        )


def _resolve_available_trade_date(
    client: Any,
    requested_date: date,
    style_profile: str,
    security_id: str | None = None,
) -> date:
    security_filter = ""
    parameters = {
        "trade_date": requested_date.isoformat(),
        "style_profile": style_profile,
    }
    if security_id:
        security_filter = """
    AND (
        s.security_id = {security_id:String}
        OR endsWith(s.security_id, concat('_', {security_id:String}))
    )"""
        parameters["security_id"] = security_id
    rows = _records(
        client.query_df(
            f"""
SELECT nullIf(max(s.trade_date), toDate(0)) AS available_trade_date
FROM arcana.fact_daily_style_score AS s FINAL
WHERE s.trade_date <= {{trade_date:Date}}
    AND s.style_profile = {{style_profile:String}}{security_filter}
""".strip(),
            parameters=parameters,
        )
    )
    if not rows:
        return requested_date
    value = rows[0].get("available_trade_date")
    if _is_missing_value(value):
        return requested_date
    return _as_date(value)


def _resolved_detail_trade_date(
    detail: StyleScoreDetailResponse,
    fallback_date: date,
) -> date:
    if detail.row is not None:
        return detail.row.trade_date
    return fallback_date


def _resolved_detail_security(
    detail: StyleScoreDetailResponse,
    fallback_security_id: str,
) -> dict[str, str]:
    if detail.row is None:
        return {
            "security_id": fallback_security_id,
            "stock_code": _clean_stock_code(None, fallback_security_id),
            "company_name": fallback_security_id,
        }
    return {
        "security_id": detail.row.security_id,
        "stock_code": detail.row.stock_code,
        "company_name": detail.row.company_name or detail.row.security_id,
    }


def _build_style_score_list_query(
    *,
    has_min_confidence: bool,
    has_industry_group_code: bool,
    has_sector_code: bool,
) -> str:
    filters = [
        "s.trade_date = {trade_date:Date}",
        "s.style_profile = {style_profile:String}",
    ]
    if has_min_confidence:
        filters.append("s.score_confidence >= {min_confidence:Float64}")
    if has_industry_group_code:
        filters.append("iss.industry_group_code = {industry_group_code:String}")
    if has_sector_code:
        filters.append("iss.sector_code = {sector_code:String}")
    where_clause = "\n    AND ".join(filters)
    return f"""
SELECT
    s.trade_date AS trade_date,
    s.security_id AS security_id,
    s.issuer_id AS issuer_id,
    s.stock_code AS stock_code,
    s.company_name AS company_name,
    s.industry_schema AS industry_schema,
    s.industry_code AS industry_code,
    s.industry_name AS industry_name,
    s.style_profile AS style_profile,
    s.value_score AS value_score,
    s.quality_score AS quality_score,
    s.growth_score AS growth_score,
    s.momentum_score AS momentum_score,
    s.risk_score AS risk_score,
    s.dividend_score AS dividend_score,
    s.total_score AS total_score,
    s.total_score_sort AS total_score_sort,
    s.available_factor_count AS available_factor_count,
    s.required_factor_count AS required_factor_count,
    s.score_confidence AS score_confidence,
    s.missing_factor_ids AS missing_factor_ids,
    s.invalid_factor_ids AS invalid_factor_ids,
    iss.sector_code AS sector_code,
    iss.industry_group_code AS industry_group_code,
    iss.industry_group_name AS industry_group_name
FROM arcana.fact_daily_style_score AS s FINAL
LEFT JOIN arcana.security_master AS sm
    ON sm.security_id = s.security_id
LEFT JOIN arcana.issuers AS iss
    ON iss.issuer_id = sm.issuer_id
WHERE {where_clause}
ORDER BY
    s.total_score_sort DESC,
    s.security_id ASC
LIMIT {{limit:UInt64}}
""".strip()


def _build_style_score_detail_query() -> str:
    return """
SELECT
    s.trade_date AS trade_date,
    s.security_id AS security_id,
    s.issuer_id AS issuer_id,
    s.stock_code AS stock_code,
    s.company_name AS company_name,
    s.industry_schema AS industry_schema,
    s.industry_code AS industry_code,
    s.industry_name AS industry_name,
    s.style_profile AS style_profile,
    s.value_score AS value_score,
    s.quality_score AS quality_score,
    s.growth_score AS growth_score,
    s.momentum_score AS momentum_score,
    s.risk_score AS risk_score,
    s.dividend_score AS dividend_score,
    s.total_score AS total_score,
    s.total_score_sort AS total_score_sort,
    s.available_factor_count AS available_factor_count,
    s.required_factor_count AS required_factor_count,
    s.score_confidence AS score_confidence,
    s.missing_factor_ids AS missing_factor_ids,
    s.invalid_factor_ids AS invalid_factor_ids,
    iss.sector_code AS sector_code,
    iss.industry_group_code AS industry_group_code,
    iss.industry_group_name AS industry_group_name
FROM arcana.fact_daily_style_score AS s FINAL
LEFT JOIN arcana.security_master AS sm
    ON sm.security_id = s.security_id
LEFT JOIN arcana.issuers AS iss
    ON iss.issuer_id = sm.issuer_id
WHERE s.trade_date = {trade_date:Date}
    AND s.style_profile = {style_profile:String}
    AND (
        s.security_id = {security_id:String}
        OR endsWith(s.security_id, concat('_', {security_id:String}))
    )
LIMIT 1
""".strip()


def _build_factor_breakdown_query() -> str:
    return """
SELECT
    factor_id,
    style_group,
    factor_direction,
    raw_factor_value,
    winsorized_value,
    percentile_score,
    robust_z_score,
    n_peers,
    industry_level,
    industry_code,
    industry_name,
    is_valid,
    invalid_reason,
    is_winsorized,
    score_confidence
FROM arcana.fact_daily_factor_score FINAL
WHERE trade_date = {trade_date:Date}
    AND (
        security_id = {security_id:String}
        OR endsWith(security_id, concat('_', {security_id:String}))
    )
ORDER BY
    style_group ASC,
    factor_id ASC
""".strip()


def _to_style_row(rank: int, row: dict[str, Any]) -> StyleScoreRow:
    security_id = str(row.get("security_id") or "")
    return StyleScoreRow(
        trade_date=_as_date(row.get("trade_date")),
        rank=rank,
        security_id=security_id,
        issuer_id=_optional_str(row.get("issuer_id")) or "",
        stock_code=_clean_stock_code(row.get("stock_code"), security_id),
        company_name=_optional_str(row.get("company_name")) or "",
        industry_schema=_optional_str(row.get("industry_schema")) or "",
        sector_code=_optional_str(row.get("sector_code")) or "",
        industry_group_code=_optional_str(row.get("industry_group_code")) or "",
        industry_group_name=_optional_str(row.get("industry_group_name")) or "",
        style_profile=_optional_str(row.get("style_profile")) or DEFAULT_STYLE_PROFILE,
        value_score=_float_or_none(row.get("value_score")),
        quality_score=_float_or_none(row.get("quality_score")),
        growth_score=_float_or_none(row.get("growth_score")),
        momentum_score=_float_or_none(row.get("momentum_score")),
        risk_score=_float_or_none(row.get("risk_score")),
        dividend_score=_float_or_none(row.get("dividend_score")),
        total_score=_float_or_none(row.get("total_score")),
        score_confidence=_float_or_none(row.get("score_confidence")) or 0.0,
        available_factor_count=int(_float_or_none(row.get("available_factor_count")) or 0),
        required_factor_count=int(_float_or_none(row.get("required_factor_count")) or 0),
        missing_factor_ids=_as_string_list(row.get("missing_factor_ids")),
        invalid_factor_ids=_as_string_list(row.get("invalid_factor_ids")),
    )


def _to_factor_breakdown(row: dict[str, Any]) -> FactorScoreBreakdown:
    return FactorScoreBreakdown(
        factor_id=str(row.get("factor_id") or ""),
        style_group=_optional_str(row.get("style_group")) or "",
        factor_direction=int(_float_or_none(row.get("factor_direction")) or 0),
        raw_factor_value=_float_or_none(row.get("raw_factor_value")),
        winsorized_value=_float_or_none(row.get("winsorized_value")),
        percentile_score=_float_or_none(row.get("percentile_score")),
        robust_z_score=_float_or_none(row.get("robust_z_score")),
        n_peers=int(_float_or_none(row.get("n_peers")) or 0),
        industry_level=_optional_str(row.get("industry_level")) or "",
        industry_code=_optional_str(row.get("industry_code")) or "",
        industry_name=_optional_str(row.get("industry_name")) or "",
        is_valid=bool(row.get("is_valid", True)),
        invalid_reason=_optional_str(row.get("invalid_reason")) or "",
        is_winsorized=bool(row.get("is_winsorized", False)),
        score_confidence=_float_or_none(row.get("score_confidence")) or 0.0,
    )


def _build_components(
    row: StyleScoreRow | None,
    factors: list[FactorScoreBreakdown],
    style_profile: str,
) -> list[StyleScoreComponent]:
    return [
        _build_component(row, factors, "COMPOSITE", style_profile),
        _build_component(row, factors, "VALUE", style_profile),
        _build_component(row, factors, "QUALITY", style_profile),
        _build_component(row, factors, "GROWTH", style_profile),
        _build_component(row, factors, "MOMENTUM", style_profile),
        _build_component(row, factors, "RISK", style_profile),
        _build_component(row, factors, "DIVIDEND", style_profile),
    ]


def _build_component(
    row: StyleScoreRow | None,
    factors: list[FactorScoreBreakdown],
    component_key: str,
    style_profile: str,
) -> StyleScoreComponent:
    if row is None:
        return StyleScoreComponent(
            component_key=component_key,
            label=COMPONENT_LABELS[component_key],
        )
    factor_weights = _component_factor_weights(component_key, style_profile)
    factor_ids = set(factor_weights)
    factor_by_id = {factor.factor_id: factor for factor in factors}
    available_factors = [
        factor_by_id[factor_id]
        for factor_id in factor_ids
        if factor_id in factor_by_id
        and factor_by_id[factor_id].is_valid
        and factor_by_id[factor_id].percentile_score is not None
    ]
    available_weight = sum(factor_weights[factor.factor_id] for factor in available_factors)
    required_weight = sum(factor_weights.values())
    score = getattr(row, COMPONENT_SCORE_FIELDS[component_key])
    confidence = (
        available_weight / required_weight
        if required_weight and component_key != "COMPOSITE"
        else row.score_confidence
    )
    if component_key == "DIVIDEND" and score is None:
        confidence = 0.0
    return StyleScoreComponent(
        component_key=component_key,
        label=COMPONENT_LABELS[component_key],
        score=score,
        score_confidence=confidence,
        available_factor_count=len(available_factors),
        required_factor_count=len(factor_ids),
        available_weight=available_weight,
        required_weight=required_weight,
    )


def _build_component_factors(
    factors: list[FactorScoreBreakdown],
    component_key: str,
    style_profile: str,
) -> list[StyleScoreComponentFactor]:
    factor_weights = _component_factor_weights(component_key, style_profile)
    factor_by_id = {factor.factor_id: factor for factor in factors}
    rows = []
    for factor_id, weight in factor_weights.items():
        factor = factor_by_id.get(factor_id)
        if factor is None:
            rows.append(
                StyleScoreComponentFactor(
                    factor_id=factor_id,
                    label=_factor_label(factor_id),
                    style_group=_style_group_for_factor(factor_id),
                    factor_weight=weight,
                    invalid_reason="MISSING",
                )
            )
            continue
        weighted_score = (
            factor.percentile_score * weight
            if factor.percentile_score is not None and factor.is_valid
            else None
        )
        rows.append(
            StyleScoreComponentFactor(
                factor_id=factor.factor_id,
                label=_factor_label(factor.factor_id),
                style_group=_style_group_for_factor(factor.factor_id),
                raw_factor_value=factor.raw_factor_value,
                winsorized_value=factor.winsorized_value,
                percentile_score=factor.percentile_score,
                robust_z_score=factor.robust_z_score,
                factor_weight=weight,
                weighted_score=weighted_score,
                n_peers=factor.n_peers,
                industry_level=factor.industry_level,
                industry_code=factor.industry_code,
                industry_name=factor.industry_name,
                is_valid=factor.is_valid,
                invalid_reason=factor.invalid_reason,
                is_winsorized=factor.is_winsorized,
                score_confidence=factor.score_confidence,
            )
        )
    return rows


def _component_factor_weights(component_key: str, style_profile: str) -> dict[str, float]:
    if component_key in STYLE_WEIGHTS:
        return dict(STYLE_WEIGHTS[component_key])
    if component_key == "COMPOSITE":
        profile_weights = style_profile_weights(style_profile)
        result: dict[str, float] = {}
        for style_group, style_weight in profile_weights.items():
            for factor_id, factor_weight in STYLE_WEIGHTS.get(style_group, {}).items():
                result[factor_id] = result.get(factor_id, 0.0) + style_weight * factor_weight
        return result
    return {}


def _normalize_component_key(value: str) -> str:
    normalized = str(value or "").strip().upper()
    aliases = {
        "TOTAL": "COMPOSITE",
        "TOTAL_SCORE": "COMPOSITE",
        "COMPOSITESCORE": "COMPOSITE",
        "COMPOSITE_SCORE": "COMPOSITE",
        "UNDERVALUED": "VALUE",
        "PROFITABILITY": "QUALITY",
        "FINANCIALSTABILITY": "RISK",
        "FINANCIAL_STABILITY": "RISK",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in COMPONENT_SCORE_FIELDS:
        allowed = ", ".join(sorted(COMPONENT_SCORE_FIELDS))
        raise ValueError(f"component_key must be one of: {allowed}")
    return normalized


def _style_group_for_factor(factor_id: str) -> str:
    for style_group, factor_weights in STYLE_WEIGHTS.items():
        if factor_id in factor_weights:
            return style_group
    return ""


def _factor_label(factor_id: str) -> str:
    if factor_id in FACTOR_LABELS:
        return FACTOR_LABELS[factor_id]
    reverse_aliases = {value: key for key, value in FACTOR_ALIASES.items()}
    return reverse_aliases.get(factor_id, factor_id.upper())


def _normalize_style_profile(value: str) -> str:
    normalized = str(value or DEFAULT_STYLE_PROFILE).strip().upper()
    if normalized not in STYLE_PROFILES:
        allowed = ", ".join(sorted(STYLE_PROFILES))
        raise ValueError(f"style_profile must be one of: {allowed}")
    return normalized


def _normalize_limit(value: int | None) -> int:
    normalized = 100 if value is None else int(value)
    if normalized <= 0 or normalized > 1000:
        raise ValueError("limit must be between 1 and 1000")
    return normalized


def _normalize_min_confidence(value: float | None) -> float | None:
    if value is None:
        return None
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0 or normalized > 1:
        raise ValueError("min_confidence must be between 0 and 1")
    return normalized


def _records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        return frame.to_dict("records")
    return list(frame)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    text = str(value)
    return text or None


def _clean_stock_code(value: Any, security_id: str = "") -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="ignore").strip("\x00").strip()
    else:
        text = _optional_str(value) or ""
    if len(text) == 6 and text.isdigit():
        return text
    suffix = str(security_id or "").rsplit("_", 1)[-1]
    if len(suffix) == 6 and suffix.isdigit():
        return suffix
    digits = "".join(char for char in text if char.isdigit())
    if len(digits) >= 6:
        return digits[:6]
    return text


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    return [str(value)]


def _as_date(value: Any) -> date:
    if _is_missing_value(value):
        raise ValueError("date value is missing")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _is_missing_value(value: Any) -> bool:
    if value is None:
        return True
    if hasattr(value, "item"):
        try:
            item = value.item()
        except Exception:
            item = value
        if item is not value:
            return _is_missing_value(item)
    try:
        if math.isnan(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip() in {"", "NaT", "NaN", "nan", "None", "<NA>"}


def _today_kst() -> date:
    try:
        return datetime.now(ZoneInfo("Asia/Seoul")).date()
    except Exception:
        return datetime.now(timezone(timedelta(hours=9))).date()
