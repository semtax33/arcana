import io
import re
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml

from company import fetch_sector, kospi_kosdaq_corp_list


# ------------------------------------------------------------
# 2. YAML 로드
# ------------------------------------------------------------
def load_gics_config(path: str | Path = "gics_rules.yaml") -> dict[str, Any]:
    path = Path(path)

    with path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    required_keys = ["sectors", "sector_rules"]
    for key in required_keys:
        if key not in config:
            raise ValueError(f"YAML 설정에 '{key}' 항목이 없습니다: {path}")

    return config


# ------------------------------------------------------------
# 3. 유틸
# ------------------------------------------------------------
def normalize_text(x: Any) -> str:
    if pd.isna(x):
        return ""

    x = str(x)
    x = x.replace("\u3000", " ")
    x = re.sub(r"\s+", " ", x)
    return x.strip()


def pick_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def match_patterns(
    patterns: list[str] | None,
    text: str,
    *,
    flags: int = re.IGNORECASE,
) -> list[str]:
    if not patterns:
        return []

    matched = []
    for pat in patterns:
        if re.search(pat, text, flags=flags):
            matched.append(pat)

    return matched


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


# ------------------------------------------------------------
# 4. override 조건 판정
# ------------------------------------------------------------
def condition_matches(
    conditions: dict[str, Any],
    fields: dict[str, str],
) -> tuple[bool, list[str]]:
    """
    conditions 예시:

    conditions:
      company_any:
        - '스팩|SPAC'

    conditions:
      industry_any:
        - '기계|장비'
      product_any:
        - '전기차\s*충전|충전기'

    기본 동작:
    - conditions 안의 각 조건은 AND
    - 각 조건의 patterns 내부는 OR
    """

    if not conditions:
        return False, []

    evidence = []

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

        text = fields.get(field_key, "")
        matched = match_patterns(patterns, text)

        # 해당 조건이 있는데 하나도 안 맞으면 override 실패
        if not matched:
            return False, []

        for pat in matched:
            evidence.append(f"{condition_key}:{pat}")

    return True, evidence


# ------------------------------------------------------------
# 5. 단일 row GICS Sector 분류
# ------------------------------------------------------------
def classify_gics_sector(
    row: pd.Series,
    config: dict[str, Any],
) -> dict[str, Any]:
    sectors: dict[str, str] = config["sectors"]
    sector_rules: dict[str, Any] = config["sector_rules"]

    weights = config.get("weights", {})
    industry_weight = float(weights.get("industry", 3.0))
    product_weight = float(weights.get("product", 1.5))
    confidence_denominator = float(weights.get("confidence_denominator", 6.0))

    fields = get_row_fields(row)

    scores = {code: 0.0 for code in sectors.keys()}
    matched_rules = {code: [] for code in sectors.keys()}

    # --------------------------------------------------------
    # 5-1. 기본 sector rule 매칭
    # --------------------------------------------------------
    for sector_code, rule in sector_rules.items():
        if sector_code not in sectors:
            raise ValueError(f"sector_rules에 알 수 없는 sector_code가 있습니다: {sector_code}")

        industry_patterns = rule.get("industry_patterns", [])
        product_patterns = rule.get("product_patterns", [])

        industry_matched = match_patterns(industry_patterns, fields["industry"])
        product_matched = match_patterns(product_patterns, fields["product"])

        if industry_matched:
            scores[sector_code] += industry_weight * len(industry_matched)
            for pat in industry_matched:
                matched_rules[sector_code].append(f"industry:{pat}")

        if product_matched:
            scores[sector_code] += product_weight * len(product_matched)
            for pat in product_matched:
                matched_rules[sector_code].append(f"product:{pat}")

    # --------------------------------------------------------
    # 5-2. 특수 보정 rule 매칭
    # --------------------------------------------------------
    for override in config.get("score_overrides", []):
        name = override.get("name", "unnamed_override")
        conditions = override.get("conditions", {})
        add_scores = override.get("add_scores", {})

        ok, evidence = condition_matches(conditions, fields)

        if not ok:
            continue

        for sector_code, add_score in add_scores.items():
            if sector_code not in sectors:
                raise ValueError(
                    f"score_overrides '{name}'에 알 수 없는 sector_code가 있습니다: {sector_code}"
                )

            scores[sector_code] += float(add_score)
            matched_rules[sector_code].append(
                f"override:{name} ({', '.join(evidence)})"
            )

    # --------------------------------------------------------
    # 5-3. 최고 점수 sector 선택
    # --------------------------------------------------------
    max_score = max(scores.values())

    if max_score <= 0:
        return {
            "gics_sector_code": "UNMAPPED",
            "gics_sector_name": "UNMAPPED",
            "gics_confidence": 0.0,
            "gics_score": 0.0,
            "gics_matched_rules": "",
        }

    best_codes = [
        code
        for code, score in scores.items()
        if score == max_score
    ]

    if len(best_codes) == 1:
        best_code = best_codes[0]
    else:
        # 동점이면 YAML의 sector_priority 기준으로 선택
        priority = config.get("sector_priority", list(sectors.keys()))
        priority_rank = {code: i for i, code in enumerate(priority)}

        best_code = sorted(
            best_codes,
            key=lambda code: priority_rank.get(code, 9999),
        )[0]

    confidence = min(max_score / confidence_denominator, 1.0)

    return {
        "gics_sector_code": best_code,
        "gics_sector_name": sectors[best_code],
        "gics_confidence": round(confidence, 3),
        "gics_score": round(max_score, 3),
        "gics_matched_rules": " | ".join(matched_rules[best_code]),
    }


# ------------------------------------------------------------
# 6. manual override 적용
# ------------------------------------------------------------
def apply_manual_overrides(
    df: pd.DataFrame,
    config: dict[str, Any],
) -> pd.DataFrame:
    df = df.copy()

    manual_overrides = config.get("manual_overrides", {})

    if not manual_overrides:
        return df

    if "종목코드" not in df.columns:
        return df

    stock_codes = df["종목코드"].astype(str).str.strip()

    for stock_code, override in manual_overrides.items():
        sector_code = str(override["sector_code"])
        sector_name = override.get(
            "sector_name",
            config["sectors"].get(sector_code, "UNKNOWN"),
        )
        reason = override.get("reason", "manual_override")

        mask = stock_codes.eq(str(stock_code).strip())

        df.loc[mask, "gics_sector_code"] = sector_code
        df.loc[mask, "gics_sector_name"] = sector_name
        df.loc[mask, "gics_confidence"] = 1.0
        df.loc[mask, "gics_score"] = 999.0
        df.loc[mask, "gics_matched_rules"] = f"manual_override:{reason}"

    return df


# ------------------------------------------------------------
# 7. 전체 DataFrame에 GICS Sector 붙이기
# ------------------------------------------------------------
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
        lambda row: classify_gics_sector(row, config),
        axis=1,
        result_type="expand",
    )

    mapped = pd.concat([df, result], axis=1)

    if apply_manual:
        mapped = apply_manual_overrides(mapped, config)

    return mapped


# ------------------------------------------------------------
# 8. 검토 대상 추출
# ------------------------------------------------------------
def extract_review_targets(
    mapped: pd.DataFrame,
    *,
    min_confidence: float = 0.6,
) -> pd.DataFrame:
    return mapped[
        mapped["gics_sector_code"].eq("UNMAPPED")
        | mapped["gics_confidence"].lt(min_confidence)
    ].copy()

def get_normalized_sector_and_issuer():
    config = load_gics_config("./data-lake/meta/rules/gics_rules.yaml")

    df = fetch_sector()

    mapped = attach_gics_sector(df, config)

    mapped["issuer_id"] = mapped["종목코드"].apply(
        lambda stock_code: f"ISSUER_ID_{str(stock_code).strip().zfill(6)}"
    )
    mapped["legal_name_ko"] = mapped["회사명"].apply(lambda x: f"{x}")
    mapped["legal_name_en"] = mapped["회사명"].apply(lambda x: f"{x}")
    mapped["domicile_country"] = mapped["지역"].apply(lambda _: "KR")
    mapped["region"] = mapped["지역"].apply(lambda region: region)
    mapped["industry_schema"] = mapped["gics_sector_code"].apply(lambda _: "GICS")
    mapped["industry_code"] = mapped["gics_sector_code"].apply(lambda code: code)
    mapped["is_active"] = mapped["종목코드"].apply(lambda _: True)

    mapped = mapped.drop(columns=["종목코드", "회사명", "지역", "gics_sector_code", "gics_sector_name"])

    return mapped[
        [
            "issuer_id",
            "legal_name_ko",
            "legal_name_en",
            "domicile_country",
            "region",
            "industry_schema",
            "industry_code",
            "is_active"
        ]
    ]


def get_normalized_sector_and_issuer():
    config = load_gics_config("./data-lake/meta/rules/gics_rules.yaml")

    df = fetch_sector()
    market_df = kospi_kosdaq_corp_list()
    
    def mapping_func(stock_code: str):
        value = market_df.loc[market_df['stock_code'] == stock_code, 'corp_eng_name']
        if len(value) == 0:
            return "NONE"
        else:
            return value.iat[0]

    mapped = attach_gics_sector(df, config)

    mapped["issuer_id"] = mapped["종목코드"].apply(
        lambda stock_code: f"ISSUER_ID_{str(stock_code).strip().zfill(6)}"
    )
    mapped["legal_name_ko"] = mapped["회사명"].apply(lambda x: f"{x}")
    mapped["legal_name_en"] = mapped["종목코드"].apply(lambda stock_code: mapping_func(stock_code))
    mapped["domicile_country"] = mapped["지역"].apply(lambda _: "KR")
    mapped["region"] = mapped["지역"].apply(lambda region: str(region))
    mapped["industry_schema"] = mapped["gics_sector_code"].apply(lambda _: "GICS")
    mapped["industry_code"] = mapped["gics_sector_code"].apply(lambda code: str(code))
    mapped["is_active"] = mapped["종목코드"].apply(lambda _: True)

    mapped = mapped.drop(columns=["종목코드", "회사명", "지역", "gics_sector_code", "gics_sector_name"])

    return mapped[
        [
            "issuer_id",
            "legal_name_ko",
            "legal_name_en",
            "domicile_country",
            "region",
            "industry_schema",
            "industry_code",
            "is_active"
        ]
    ]

def get_normalized_security_master():
    config = load_gics_config("./data-lake/meta/rules/gics_rules.yaml")

    df = fetch_sector()

    mapped = attach_gics_sector(df, config)

    mapped["security_id"] = mapped["종목코드"].apply(
        lambda stock_code: f"SEC_KR_{str(stock_code).strip().zfill(6)}"
    )
    mapped["issuer_id"] = mapped["종목코드"].apply(lambda stock_code: f"ISSUER_ID_{str(stock_code).strip().zfill(6)[:-1] + '0'}")
    mapped["sec_type"] = mapped["종목코드"].apply(lambda stock_code: "COMMON" if str(stock_code).strip()[-1] == "0" else "PREF")
    mapped["asset_subtype"] = mapped["종목코드"].apply(lambda stock_code: "KR_ORD" if str(stock_code).strip()[-1] == "0" else "KR_PREF")
    mapped["share_class"] = mapped["종목코드"].apply(lambda stock_code: "ORD" if str(stock_code).strip()[-1] == "0" else "PREF")
    mapped["is_active"] = mapped["종목코드"].apply(lambda _: True)

    mapped = mapped.drop(columns=["종목코드", "회사명", "지역", "gics_sector_code", "gics_sector_name"])

    return mapped[
        [
            "security_id",
            "issuer_id",
            "sec_type",
            "asset_subtype",
            "share_class",
            "is_active"
        ]
    ]

def get_normalized_identifier():
    config = load_gics_config("./data-lake/meta/rules/gics_rules.yaml")

    df = fetch_sector()
    market_df = kospi_kosdaq_corp_list()

    def mapping_func(stock_code: str):
        value = market_df.loc[market_df['stock_code'] == stock_code, 'market']
        if len(value) == 0:
            return "NONE"
        else:
            return value.iat[0]

    mapped = attach_gics_sector(df, config)

    mapped["security_id"] = mapped["종목코드"].apply(
        lambda stock_code: f"SEC_KR_{str(stock_code).strip().zfill(6)}"
    )
    mapped["id_type"] = mapped["종목코드"].apply(lambda _: "TICKER")
    mapped["id_value"] = mapped["종목코드"].apply(lambda stock_code: stock_code)
    mapped["market_mic"] = mapped["종목코드"].apply(lambda stock_code: mapping_func(stock_code))
    mapped["is_primary"] = mapped["종목코드"].apply(lambda _: True)

    mapped = mapped.drop(columns=["종목코드", "회사명", "지역", "gics_sector_code", "gics_sector_name"])

    return mapped[
        [
            "security_id",
            "id_type",
            "id_value",
            "market_mic",
            "is_primary"
        ]
    ]