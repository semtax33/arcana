from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


DEFAULT_GICS_RULES_PATH = Path(__file__).resolve().parents[1] / "data-lake" / "meta" / "rules" / "gics_rules.yaml"
UNMAPPED = "UNMAPPED"


def _load_company_functions():
    try:
        from company import fetch_sector, kospi_kosdaq_corp_list
    except ModuleNotFoundError:  # pragma: no cover - package import path for tests/tools
        from engine.company import fetch_sector, kospi_kosdaq_corp_list
    return fetch_sector, kospi_kosdaq_corp_list


def load_gics_config(path: str | Path = DEFAULT_GICS_RULES_PATH) -> dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}

    required_keys = ["sectors", "sector_rules", "industry_groups"]
    for key in required_keys:
        if key not in config:
            raise ValueError(f"YAML config is missing required key '{key}': {path}")

    _validate_gics_config(config)
    return config


def _validate_gics_config(config: dict[str, Any]) -> None:
    sectors = {str(code) for code in config.get("sectors", {})}
    industry_groups = {str(code) for code in config.get("industry_groups", {})}

    for sector_code in config.get("sector_rules", {}):
        if str(sector_code) not in sectors:
            raise ValueError(f"unknown sector_code in sector_rules: {sector_code}")

    for group_code in industry_groups:
        parent_sector = _parent_sector_code(group_code)
        if parent_sector not in sectors:
            raise ValueError(f"unknown parent sector for industry_group {group_code}: {parent_sector}")

    for group_code, rule in config.get("industry_group_rules", {}).items():
        group_code = str(group_code)
        if group_code not in industry_groups:
            raise ValueError(f"unknown industry_group_code in industry_group_rules: {group_code}")
        sector_code = str(rule.get("sector_code", _parent_sector_code(group_code)))
        if sector_code != _parent_sector_code(group_code):
            raise ValueError(
                f"industry_group_rules sector_code mismatch for {group_code}: {sector_code}"
            )


def normalize_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def pick_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for column in candidates:
        if column in df.columns:
            return column
    return None


def match_patterns(
    patterns: list[str] | None,
    text: str,
    *,
    flags: int = re.IGNORECASE,
) -> list[str]:
    if not patterns:
        return []
    return [pattern for pattern in patterns if re.search(str(pattern), text, flags=flags)]


def get_row_fields(row: pd.Series) -> dict[str, str]:
    company = normalize_text(row.get("회사명", ""))
    industry = normalize_text(row.get("업종", ""))
    product = normalize_text(row.get("주요제품", row.get("주요 제품", "")))
    return {
        "company": company,
        "industry": industry,
        "product": product,
        "any_text": f"{company} {industry} {product}",
    }


def condition_matches(
    conditions: dict[str, Any],
    fields: dict[str, str],
) -> tuple[bool, list[str]]:
    if not conditions:
        return False, []

    evidence: list[str] = []
    condition_to_field = {
        "company_any": "company",
        "industry_any": "industry",
        "product_any": "product",
        "any_text_any": "any_text",
    }

    for condition_key, field_key in condition_to_field.items():
        patterns = conditions.get(condition_key)
        if patterns is None:
            continue

        matched = match_patterns(patterns, fields.get(field_key, ""))
        if not matched:
            return False, []
        evidence.extend(f"{condition_key}:{pattern}" for pattern in matched)

    return True, evidence


def classify_gics_sector(row: pd.Series, config: dict[str, Any]) -> dict[str, Any]:
    fields = get_row_fields(row)
    sectors: dict[str, str] = {str(code): str(name) for code, name in config["sectors"].items()}
    sector_rules: dict[str, Any] = config["sector_rules"]
    weights = config.get("weights", {})

    scores = {code: 0.0 for code in sectors}
    matched_rules = {code: [] for code in sectors}
    _score_rules(
        scores=scores,
        matched_rules=matched_rules,
        rules=sector_rules,
        fields=fields,
        weights=weights,
        allowed_codes=set(sectors),
    )
    _score_overrides(
        scores=scores,
        matched_rules=matched_rules,
        overrides=config.get("score_overrides", []),
        fields=fields,
        allowed_codes=set(sectors),
    )

    code, score = _pick_best_code(scores, config.get("sector_priority", list(sectors)))
    if code is None:
        return _unmapped_sector_result()

    denominator = float(weights.get("confidence_denominator", 6.0))
    return {
        "gics_sector_code": code,
        "gics_sector_name": sectors[code],
        "gics_confidence": round(min(score / denominator, 1.0), 3),
        "gics_score": round(score, 3),
        "gics_matched_rules": " | ".join(matched_rules[code]),
    }


def classify_gics_industry_group(
    row: pd.Series,
    config: dict[str, Any],
    sector_code: str | None,
) -> dict[str, Any]:
    sector_code = str(sector_code or "")
    if not sector_code or sector_code == UNMAPPED:
        return _unmapped_industry_group_result()

    fields = get_row_fields(row)
    industry_groups = {
        str(code): str(name)
        for code, name in config.get("industry_groups", {}).items()
        if _parent_sector_code(str(code)) == sector_code
    }
    if not industry_groups:
        return _unmapped_industry_group_result()

    weights = config.get("industry_group_weights", config.get("weights", {}))
    scores = {code: 0.0 for code in industry_groups}
    matched_rules = {code: [] for code in industry_groups}
    _score_rules(
        scores=scores,
        matched_rules=matched_rules,
        rules=config.get("industry_group_rules", {}),
        fields=fields,
        weights=weights,
        allowed_codes=set(industry_groups),
    )
    _score_overrides(
        scores=scores,
        matched_rules=matched_rules,
        overrides=config.get("industry_group_score_overrides", []),
        fields=fields,
        allowed_codes=set(industry_groups),
    )

    code, score = _pick_best_code(
        scores,
        config.get("industry_group_priority", list(industry_groups)),
    )
    if code is None:
        return _unmapped_industry_group_result()

    denominator = float(weights.get("confidence_denominator", 6.0))
    return {
        "gics_industry_group_code": code,
        "gics_industry_group_name": industry_groups[code],
        "gics_industry_group_confidence": round(min(score / denominator, 1.0), 3),
        "gics_industry_group_score": round(score, 3),
        "gics_industry_group_matched_rules": " | ".join(matched_rules[code]),
    }


def classify_gics(row: pd.Series, config: dict[str, Any]) -> dict[str, Any]:
    sector_result = classify_gics_sector(row, config)
    group_result = classify_gics_industry_group(
        row,
        config,
        sector_result.get("gics_sector_code"),
    )
    return {**sector_result, **group_result}


def _score_rules(
    *,
    scores: dict[str, float],
    matched_rules: dict[str, list[str]],
    rules: dict[str, Any],
    fields: dict[str, str],
    weights: dict[str, Any],
    allowed_codes: set[str],
) -> None:
    industry_weight = float(weights.get("industry", 3.0))
    product_weight = float(weights.get("product", 1.5))
    any_text_weight = float(weights.get("any_text", 1.0))

    for code, rule in rules.items():
        code = str(code)
        if code not in allowed_codes:
            continue

        matches = {
            "industry": match_patterns(rule.get("industry_patterns", []), fields["industry"]),
            "product": match_patterns(rule.get("product_patterns", []), fields["product"]),
            "any_text": match_patterns(rule.get("any_text_patterns", []), fields["any_text"]),
        }
        for field_name, matched in matches.items():
            if not matched:
                continue
            weight = {
                "industry": industry_weight,
                "product": product_weight,
                "any_text": any_text_weight,
            }[field_name]
            scores[code] += weight * len(matched)
            matched_rules[code].extend(f"{field_name}:{pattern}" for pattern in matched)


def _score_overrides(
    *,
    scores: dict[str, float],
    matched_rules: dict[str, list[str]],
    overrides: list[dict[str, Any]],
    fields: dict[str, str],
    allowed_codes: set[str],
) -> None:
    for override in overrides:
        name = override.get("name", "unnamed_override")
        ok, evidence = condition_matches(override.get("conditions", {}), fields)
        if not ok:
            continue

        for code, add_score in override.get("add_scores", {}).items():
            code = str(code)
            if code not in allowed_codes:
                continue
            scores[code] += float(add_score)
            matched_rules[code].append(f"override:{name} ({', '.join(evidence)})")


def _pick_best_code(scores: dict[str, float], priority: list[str]) -> tuple[str | None, float]:
    if not scores:
        return None, 0.0
    max_score = max(scores.values())
    if max_score <= 0:
        return None, 0.0

    best_codes = [code for code, score in scores.items() if score == max_score]
    if len(best_codes) == 1:
        return best_codes[0], max_score

    priority_rank = {str(code): index for index, code in enumerate(priority)}
    best_code = sorted(best_codes, key=lambda code: priority_rank.get(code, 9999))[0]
    return best_code, max_score


def _parent_sector_code(industry_group_code: str) -> str:
    return str(industry_group_code)[:2]


def _unmapped_sector_result() -> dict[str, Any]:
    return {
        "gics_sector_code": UNMAPPED,
        "gics_sector_name": UNMAPPED,
        "gics_confidence": 0.0,
        "gics_score": 0.0,
        "gics_matched_rules": "",
    }


def _unmapped_industry_group_result() -> dict[str, Any]:
    return {
        "gics_industry_group_code": UNMAPPED,
        "gics_industry_group_name": UNMAPPED,
        "gics_industry_group_confidence": 0.0,
        "gics_industry_group_score": 0.0,
        "gics_industry_group_matched_rules": "",
    }


def apply_manual_overrides(df: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    df = df.copy()
    manual_overrides = config.get("manual_overrides", {})
    if not manual_overrides or "종목코드" not in df.columns:
        return df

    stock_codes = df["종목코드"].astype(str).str.strip()
    sectors = {str(code): str(name) for code, name in config.get("sectors", {}).items()}
    groups = {str(code): str(name) for code, name in config.get("industry_groups", {}).items()}

    for stock_code, override in manual_overrides.items():
        reason = override.get("reason", "manual_override")
        mask = stock_codes.eq(str(stock_code).strip())

        group_code = override.get("industry_group_code")
        if group_code is not None:
            group_code = str(group_code)
            df.loc[mask, "gics_industry_group_code"] = group_code
            df.loc[mask, "gics_industry_group_name"] = override.get(
                "industry_group_name",
                groups.get(group_code, "UNKNOWN"),
            )
            df.loc[mask, "gics_industry_group_confidence"] = 1.0
            df.loc[mask, "gics_industry_group_score"] = 999.0
            df.loc[mask, "gics_industry_group_matched_rules"] = f"manual_override:{reason}"

        sector_code = override.get("sector_code")
        if sector_code is None and group_code is not None:
            sector_code = _parent_sector_code(group_code)
        if sector_code is None:
            continue

        sector_code = str(sector_code)
        df.loc[mask, "gics_sector_code"] = sector_code
        df.loc[mask, "gics_sector_name"] = override.get(
            "sector_name",
            sectors.get(sector_code, "UNKNOWN"),
        )
        df.loc[mask, "gics_confidence"] = 1.0
        df.loc[mask, "gics_score"] = 999.0
        df.loc[mask, "gics_matched_rules"] = f"manual_override:{reason}"

    return df


def attach_gics_sector(
    df: pd.DataFrame,
    config: dict[str, Any],
    *,
    apply_manual: bool = True,
) -> pd.DataFrame:
    df = df.copy()
    product_col = pick_col(df, ["주요제품", "주요 제품"])
    if product_col is None:
        df["주요제품"] = ""
    elif product_col != "주요제품":
        df["주요제품"] = df[product_col]

    if "업종" not in df.columns:
        df["업종"] = ""

    result = df.apply(
        lambda row: classify_gics(row, config),
        axis=1,
        result_type="expand",
    )
    mapped = pd.concat([df, result], axis=1)

    if apply_manual:
        mapped = apply_manual_overrides(mapped, config)
    return mapped


def extract_review_targets(
    mapped: pd.DataFrame,
    *,
    min_confidence: float = 0.6,
) -> pd.DataFrame:
    return mapped[
        mapped["gics_sector_code"].eq(UNMAPPED)
        | mapped["gics_confidence"].lt(min_confidence)
        | mapped["gics_industry_group_code"].eq(UNMAPPED)
        | mapped["gics_industry_group_confidence"].lt(min_confidence)
    ].copy()


def get_normalized_sector_and_issuer() -> pd.DataFrame:
    config = load_gics_config(DEFAULT_GICS_RULES_PATH)
    fetch_sector, kospi_kosdaq_corp_list = _load_company_functions()
    df = fetch_sector()
    market_df = kospi_kosdaq_corp_list()

    market_df = market_df.copy()
    market_df["stock_code"] = market_df["stock_code"].astype(str).str.strip().str.zfill(6)
    english_names = market_df.set_index("stock_code")["corp_eng_name"].to_dict()

    mapped = attach_gics_sector(df, config)
    stock_codes = mapped["종목코드"].astype(str).str.strip().map(
        lambda value: value.zfill(6) if value.isdigit() else value
    )
    regions = (
        mapped["지역"]
        if "지역" in mapped.columns
        else pd.Series([""] * len(mapped), index=mapped.index)
    )

    return pd.DataFrame(
        {
            "issuer_id": stock_codes.map(lambda code: f"ISSUER_ID_{code}"),
            "legal_name_ko": mapped["회사명"].map(lambda value: f"{value}"),
            "legal_name_en": stock_codes.map(lambda code: english_names.get(code, "NONE")),
            "domicile_country": "KR",
            "region": regions.map(lambda value: str(value)),
            "industry_schema": "GICS",
            "sector_code": mapped["gics_sector_code"].map(lambda code: str(code)),
            "industry_group_code": mapped["gics_industry_group_code"].map(lambda code: str(code)),
            "industry_group_name": mapped["gics_industry_group_name"].map(lambda name: str(name)),
            "is_active": True,
        }
    )


def get_normalized_security_master() -> pd.DataFrame:
    fetch_sector, _ = _load_company_functions()
    df = fetch_sector()
    stock_codes = df["종목코드"].astype(str).str.strip().map(
        lambda value: value.zfill(6) if value.isdigit() else value
    )

    return pd.DataFrame(
        {
            "security_id": stock_codes.map(lambda code: f"SEC_KR_{code}"),
            "issuer_id": stock_codes.map(lambda code: f"ISSUER_ID_{code[:-1] + '0'}"),
            "sec_type": stock_codes.map(lambda code: "COMMON" if code[-1] == "0" else "PREF"),
            "asset_subtype": stock_codes.map(lambda code: "KR_ORD" if code[-1] == "0" else "KR_PREF"),
            "share_class": stock_codes.map(lambda code: "ORD" if code[-1] == "0" else "PREF"),
            "is_active": True,
        }
    )


def get_normalized_identifier() -> pd.DataFrame:
    fetch_sector, kospi_kosdaq_corp_list = _load_company_functions()
    df = fetch_sector()
    market_df = kospi_kosdaq_corp_list()

    market_df = market_df.copy()
    market_df["stock_code"] = market_df["stock_code"].astype(str).str.strip().str.zfill(6)
    markets = market_df.set_index("stock_code")["market"].to_dict()
    stock_codes = df["종목코드"].astype(str).str.strip().map(
        lambda value: value.zfill(6) if value.isdigit() else value
    )

    return pd.DataFrame(
        {
            "security_id": stock_codes.map(lambda code: f"SEC_KR_{code}"),
            "id_type": "TICKER",
            "id_value": stock_codes,
            "market_mic": stock_codes.map(lambda code: markets.get(code, "NONE")),
            "is_primary": True,
        }
    )
