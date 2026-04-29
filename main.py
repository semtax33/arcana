
from __future__ import annotations
import csv
from datetime import datetime
from pathlib import Path
import random
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlencode
import json

import math
import numpy as np
from decimal import Decimal

import io
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd
from llama_cpp import Llama

from statements import download_statements

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)

CANONICAL_CSV_PATH = "./data-lake/meta/CanonicalAccount.csv"

EXPECTED_HEADER = [
    "canonical_account_id",
    "canonical_account_name",
    "original_account_name",
    "statement_type",
    "period",
    "amount"
]

# $env:ZAI_API_KEY="너의_zai_api_key"
# $env:ZAI_MODEL="glm-5.1"

ZAI_API_KEY_ENV = "ZAI_API_KEY"
ZAI_BASE_URL = os.getenv("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4/")
ZAI_MODEL = os.getenv("ZAI_MODEL", "glm-5.1")
ZAI_TIMEOUT_SEC = float(os.getenv("ZAI_TIMEOUT_SEC", "120"))

BATCH_SIZE = int(os.getenv("LLM_BATCH_SIZE", "48"))
MAX_API_RETRIES = int(os.getenv("LLM_API_RETRIES", "4"))
MAX_NORMALIZE_RETRIES = int(os.getenv("LLM_NORMALIZE_RETRIES", "2"))
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "8192"))

RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}

UNITS = {
    "천만원": 10000000, "백만원": 1000000, "십만원": 100000, "만원": 10000, "천원": 1000, "백원": 100, "십원": 10, "원": 1
}

STATEMENT_TYPES = [
    "재무상태표", "포괄손익계산서", "손익계산서", "현금흐름표", "자본변동표", "기타"
]

@dataclass
class Account:
    name: str
    value: int

@dataclass
class Statement:
    type: str
    accounts: list[Account]

def get_units(unit_text):
    for unit in UNITS.keys():
        if unit in unit_text:
            return UNITS[unit]
    return UNITS["원"]

def get_statement_types(statement_type_text):
    for types in STATEMENT_TYPES:
        if types in statement_type_text:
            return types
    return STATEMENT_TYPES[-1]

def to_safe_str(v) -> str:
    if v is None:
        return ""

    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass

    if isinstance(v, (int, np.integer)):
        return str(v)

    if isinstance(v, (float, np.floating)):
        if math.isnan(float(v)):
            return ""
        if float(v).is_integer():
            return str(int(v))
        return format(float(v), ".15g")

    if isinstance(v, Decimal):
        return format(v, "f")

    return str(v)

def convert_account_value(account_raw_value: str, unit: int) -> int:
    """
    DART 표 금액을 원 단위 int로 변환한다.
    예:
    "1,234" + 천원 단위 -> 1,234,000
    "(1,234)" + 천원 단위 -> -1,234,000
    """
    if account_raw_value is None:
        return 0

    value = account_raw_value.strip().replace("\u3000", "").replace(" ", "")

    if value in {"", "-", "－"}:
        return 0

    sign = 1

    if len(value) >= 2 and value[0] == "(" and value[-1] == ")":
        value = value[1:-1]
        sign = -1

    value = value.replace(",", "")

    try:
        return int(value) * sign * unit
    except ValueError:
        # 숫자 아닌 값이 들어오면 일단 0 처리.
        # 실전에서는 로그로 남겨서 수동 검수 추천.
        return 0

def get_statements_from_html(html_path: str | Path) -> list[Statement]:
    html_path = Path(html_path)

    if not html_path.exists():
        raise FileNotFoundError(f"HTML 파일을 찾을 수 없습니다: {html_path}")

    html_content = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html_content, "html.parser")
    tables = soup.find_all("tbody")

    table_length = len(tables) // 2
    statements: list[Statement] = []

    for idx in range(table_length):
        head_pos = idx * 2
        body_pos = idx * 2 + 1

        head_tds = tables[head_pos].select("td")
        if not head_tds:
            continue

        statement_type = get_statement_types(head_tds[0].get_text(" ", strip=True))
        unit = get_units(head_tds[-1].get_text(" ", strip=True))

        if statement_type == "자본변동표":
            continue

        account_rows: list[Account] = []
        statement_body = tables[body_pos]
        statement_account_rows = statement_body.find_all("tr")

        for row in statement_account_rows:
            tds = row.select("td")
            if len(tds) < 2:
                continue

            account_name = tds[0].get_text(" ", strip=True).replace("\u3000", "")
            account_raw_value = tds[1].get_text(" ", strip=True).replace("\u3000", "")

            if not account_name:
                continue

            account_rows.append(
                Account(
                    name=account_name,
                    value=convert_account_value(account_raw_value, unit),
                )
            )

        if account_rows:
            statements.append(Statement(statement_type, account_rows))

    return statements


def normalize_statement_type(statement_type: str) -> str:
    text = statement_type.strip().lower()

    if statement_type in {"재무상태표"} or text in {"bs", "balance_sheet"}:
        return "BS"

    if statement_type in {"현금흐름표"} or text in {"cf", "cash_flow_statement"}:
        return "CF"

    if statement_type in {"손익계산서", "포괄손익계산서"} or text in {
        "is",
        "income_statement",
        "comprehensive_income_statement",
    }:
        return "IS"

    return "UNKNOWN"


def statement_sort_key(fs_type: str) -> int:
    order = {
        "CF": 0,
        "BS": 1,
        "IS": 2,
    }
    return order.get(fs_type, 99)


def normalize_input_row(row: dict) -> dict:
    r = dict(row)
    r["company_name"] = to_safe_str(r.get("company_name"))
    r["original_account_name"] = to_safe_str(r.get("original_account_name"))
    r["statement_type"] = to_safe_str(r.get("statement_type"))
    r["period"] = to_safe_str(r.get("period"))
    r["amount"] = to_safe_str(r.get("amount"))
    return r

# 입력 row 생성
def build_input_rows(
    statements: list[Statement],
    company_name: str,
    period: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for statement in statements:
        fs_type = normalize_statement_type(statement.type)

        if fs_type == "UNKNOWN":
            continue

        for account in statement.accounts:
            rows.append(
                normalize_input_row(
                    {
                        "company_name": company_name,
                        "original_account_name": account.name,
                        "statement_type": fs_type,
                        "amount": account.value,
                        "period": period,
                    }
                )
            )

    rows.sort(key=lambda r: statement_sort_key(r["statement_type"]))
    return rows


# 캐노니컬 CSV 로딩: pandas 사용

def load_canonical_accounts(csv_path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, dtype=str).fillna("")

    required_cols = {
        "canonical_id",
        "canonical_nm",
        "fs_type",
        "is_derived",
        "formula",
        "description",
        "비고",
    }

    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"캐노니컬 CSV에 필요한 컬럼이 없습니다: {missing}")

    return df


def canonical_candidates_to_csv_text(
    canonical_df: pd.DataFrame,
    fs_type: str,
) -> str:
    keep_cols = [
        "canonical_id",
        "canonical_nm",
        "fs_type",
        "is_derived",
        "formula",
        "description",
        "비고",
    ]

    return (
        canonical_df
        .query("fs_type == @fs_type")
        .loc[:, keep_cols]
        .to_csv(index=False)
    )

EXPECTED_HEADER = [
    "canonical_account_id",
    "canonical_account_name",
    "original_account_name",
    "statement_type",
    "period",
    "amount"
]


def extract_json_text(model_output: str) -> str:
    text = model_output.strip()

    # ```json ... ``` 제거
    text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text).strip()

    # 앞뒤 설명이 섞인 경우 JSON 객체만 추출
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        return match.group(0)

    return text


def parse_llm_json(model_output: str) -> pd.DataFrame:
    json_text = extract_json_text(model_output)
    data = json.loads(json_text)

    items = data.get("items", [])
    if not isinstance(items, list):
        raise ValueError(f"items가 list가 아닙니다: {data}")

    df = pd.DataFrame(items)

    for col in EXPECTED_HEADER:
        if col not in df.columns:
            df[col] = ""

    return sanitize_output_df(df)


# 검증: LLM이 없는 canonical_id 만들면 UNMAPPED 처리
def validate_against_canonical(
    normalized_df: pd.DataFrame,
    canonical_df: pd.DataFrame,
) -> pd.DataFrame:
    out = normalized_df.copy()

    id_to_name = dict(
        zip(
            canonical_df["canonical_id"].astype(str),
            canonical_df["canonical_nm"].astype(str),
        )
    )
    valid_ids = set(id_to_name.keys())

    out["canonical_account_id"] = out["canonical_account_id"].astype(str).str.strip()

    invalid_mask = (
        out["canonical_account_id"].ne("UNMAPPED")
        & ~out["canonical_account_id"].isin(valid_ids)
    )
    out.loc[invalid_mask, "canonical_account_id"] = "UNMAPPED"

    valid_mask = out["canonical_account_id"].isin(valid_ids)
    out.loc[valid_mask, "canonical_account_name"] = out.loc[
        valid_mask, "canonical_account_id"
    ].map(id_to_name)

    unmapped_mask = out["canonical_account_id"].eq("UNMAPPED")
    out.loc[unmapped_mask, "canonical_account_name"] = "미매핑"

    return sanitize_output_df(out)

def sanitize_output_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for col in EXPECTED_HEADER:
        if col not in out.columns:
            out[col] = ""

    for col in EXPECTED_HEADER:
        out[col] = out[col].map(to_safe_str)

    return out.loc[:, EXPECTED_HEADER]

def force_input_fields(
    normalized_df: pd.DataFrame,
    input_rows: list[dict[str, Any]],
) -> pd.DataFrame:
    if len(normalized_df) != len(input_rows):
        raise ValueError(
            f"입력/출력 row 수 불일치: input={len(input_rows)}, output={len(normalized_df)}"
        )

    out = normalized_df.copy()

    for i, row in enumerate(input_rows):
        row = normalize_input_row(row)

        out.at[i, "original_account_name"] = row["original_account_name"]
        out.at[i, "statement_type"] = row["statement_type"]
        out.at[i, "period"] = row["period"]
        out.at[i, "amount"] = row["amount"]

    return sanitize_output_df(out)

def fallback_unmapped_df(input_rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "canonical_account_id": "UNMAPPED",
                "canonical_account_name": "미매핑",
                "original_account_name": to_safe_str(row["original_account_name"]),
                "statement_type": to_safe_str(row["statement_type"]),
                "period": to_safe_str(row["period"]),
                "amount": to_safe_str(row["amount"])
            }
            for row in input_rows
        ],
        columns=EXPECTED_HEADER,
    )


# 프롬프트

def build_prompt(
    canonical_accounts_csv: str,
    input_rows: list[dict[str, Any]],
) -> str:
    input_rows = [normalize_input_row(r) for r in input_rows]
    input_rows_json = json.dumps(input_rows, ensure_ascii=False, indent=2)

    return f"""
너는 한국 DART 재무제표 계정과목을 캐노니컬 계정과목으로 매핑하는 회계 데이터 정규화 엔진이다.

목표:
입력된 원본 재무제표 계정과목을 제공된 캐노니컬 계정과목 CSV 기준으로만 정규화한다.
모든 매핑은 가치평가, 퀀트 팩터 계산, 재무제표 분석에 재사용 가능해야 한다.

절대 규칙:
- CSV에 없는 canonical_account_id를 만들지 마라.
- CSV에 없는 canonical_account_name을 만들지 마라.
- canonical_account_id가 "UNMAPPED"가 아니면 반드시 캐노니컬 CSV에 존재해야 한다.
- canonical_account_name은 canonical_account_id에 대응되는 CSV의 canonical_nm과 정확히 같아야 한다.
- 적절한 매핑이 없으면 canonical_account_id는 "UNMAPPED", canonical_account_name은 "미매핑"으로 출력하라.
- 금액 계산, 단위 변환, 합계 계산, 차감 계산, 파생 계정 생성은 하지 마라.
- amount는 문자열로 제공된다. 숫자로 변환하지 말고 입력 문자열을 그대로 출력하라.
- original_account_name은 입력값을 문자 하나도 바꾸지 말고 그대로 보존하라.
- period는 입력값 그대로 보존하라.
- 입력 row 1개당 출력 item 1개를 반드시 같은 순서로 생성하라.
- 출력 items 길이는 입력 rows 길이와 반드시 같아야 한다.
- 어떤 행도 누락하지 마라.
- 어떤 행도 추가하지 마라.
- 설명문, 마크다운, 코드블록 없이 유효한 JSON 객체 하나만 출력하라.
- JSON에는 trailing comma를 넣지 마라.

statement_type 변환:
- "재무상태표", "연결재무상태표", "별도재무상태표", "BS", "balance_sheet" → "BS"
- "현금흐름표", "연결현금흐름표", "별도현금흐름표", "CF", "cash_flow_statement" → "CF"
- "손익계산서", "포괄손익계산서", "연결손익계산서", "연결포괄손익계산서", "별도손익계산서", "별도포괄손익계산서", "IS", "income_statement", "comprehensive_income_statement" → "IS"

계정명 전처리 규칙:
- 매핑 판단 시 원본 계정과목명 안의 주석 번호는 무시한다.
- 예: "(주26)", "(주43)", "(주29,35,36,48)" 같은 괄호 주석은 매핑 판단에서 제거하고 생각한다.
- "연결", "별도", "당기", "전기", "제 N 기" 같은 보고서 형식 표현은 매핑 판단에서 핵심 의미가 아니다.
- 공백, 전각공백, 괄호 주석 때문에 명확한 계정을 UNMAPPED로 두지 마라.
- 단, 출력 original_account_name에는 입력값을 그대로 보존한다.

후보 선택 규칙:
1. 입력 statement_type을 먼저 정규화한다.
2. 정규화된 statement_type과 canonical CSV의 fs_type이 같은 후보만 고려한다.
3. fs_type이 다른 canonical 계정은 절대 선택하지 않는다.
4. 원본 계정과목명과 canonical_nm, description, 비고의 의미가 가장 가까운 항목을 선택한다.
5. 아래의 명시적 매핑 규칙에 해당하면 "모호하면 UNMAPPED"보다 명시적 매핑 규칙을 우선한다.
6. 명시적 매핑 규칙에도 없고 의미가 불명확하면 UNMAPPED로 둔다.
7. amount의 부호만 보고 매핑을 바꾸지 마라.
8. 금액이 0이어도 계정명 자체가 명확하면 매핑할 수 있다.
9. 금액이 0이고 계정명이 섹션 제목 또는 분류 제목이면 UNMAPPED로 둔다.

명시적 매핑 규칙:

1. 현금흐름표 subtotal 매핑:
   statement_type이 CF인 경우 아래 조건을 반드시 적용한다.

   1-1. 원본 계정과목명에 "영업활동"과 "현금흐름"이 모두 포함되면 CFO로 매핑한다.
        단, 아래 예외는 CFO가 아니다.
        - "영업활동에서 창출된 현금흐름" → UNMAPPED
        - "영업에서 창출된 현금" → UNMAPPED
        - "영업으로부터 창출된 현금" → UNMAPPED
        - "영업에서 창출된 현금흐름" → UNMAPPED

   1-2. 원본 계정과목명에 "투자활동"과 "현금흐름"이 모두 포함되면 CFI로 매핑한다.

   1-3. 원본 계정과목명에 "재무활동"과 "현금흐름"이 모두 포함되면 CFF로 매핑한다.

   1-4. 다음 표현은 모두 최종 현금흐름 subtotal 표현으로 본다.
        - "순현금흐름"
        - "현금흐름"
        - "인한 순현금흐름"
        - "인한 현금흐름"
        - "활동현금흐름"
        - "순현금유입"
        - "순현금유출"

   1-5. 다음은 반드시 이렇게 매핑한다.
        - "영업활동으로 인한 순현금흐름" → CFO
        - "영업활동으로 인한 순현금흐름 (주43)" → CFO
        - "영업활동으로 인한 현금흐름" → CFO
        - "영업활동현금흐름" → CFO
        - "투자활동으로 인한 순현금흐름" → CFI
        - "투자활동으로 인한 순현금흐름 (주26)" → CFI
        - "투자활동으로 인한 현금흐름" → CFI
        - "투자활동현금흐름" → CFI
        - "재무활동으로 인한 순현금흐름" → CFF
        - "재무활동으로 인한 순현금흐름 (주26)" → CFF
        - "재무활동으로 인한 현금흐름" → CFF
        - "재무활동현금흐름" → CFF

2. 현금흐름표의 현금및현금성자산 관련:
   - "기초의 현금및현금성자산" → UNMAPPED
   - "기말의 현금및현금성자산" → UNMAPPED
   - "현금및현금성자산의 증가" → UNMAPPED
   - "현금및현금성자산의 감소" → UNMAPPED
   - "현금및현금성자산의 증가(감소)" → UNMAPPED
   - "현금및현금성자산에 대한 환율변동효과" → UNMAPPED
   - 단, 재무상태표의 "현금및현금성자산"은 CASH_AND_EQUIVALENTS로 매핑한다.

3. CapEx 및 투자활동:
   - "유형자산의 취득" → CAPEX_PPE
   - "유형자산 취득" → CAPEX_PPE
   - "유형자산의 증가"는 현금흐름표에서 취득 의미가 명확할 때만 CAPEX_PPE로 매핑한다.
   - "무형자산의 취득" → CAPEX_INTANG
   - "무형자산 취득" → CAPEX_INTANG
   - "유형자산의 처분"은 CAPEX가 아니다.
     canonical CSV에 PPE_DISPOSAL_PROCEEDS가 있으면 PPE_DISPOSAL_PROCEEDS로 매핑하고, 없으면 UNMAPPED로 둔다.
   - "무형자산의 처분"은 CAPEX가 아니다.
     canonical CSV에 INTANGIBLE_DISPOSAL_PROCEEDS가 있으면 INTANGIBLE_DISPOSAL_PROCEEDS로 매핑하고, 없으면 UNMAPPED로 둔다.
   - "투자부동산의 취득", "관계기업투자의 취득", "장기금융자산의 증가"는 CAPEX_PPE가 아니다. 정확한 캐노니컬 항목이 없으면 UNMAPPED로 둔다.

4. 이자, 세금, 배당:
   - "이자의 지급" → INT_PAID
   - "이자 지급" → INT_PAID
   - "법인세의 납부" → TAX_PAID
   - "법인세납부" → TAX_PAID
   - "법인세 지급" → TAX_PAID
   - "배당금의 지급" → DIV_PAID
   - "배당의 지급" → DIV_PAID
   - "배당"은 현금흐름표 재무활동 항목이면 DIV_PAID로 매핑한다.
   - "이자의 수취"는 canonical CSV에 INTEREST_RECEIVED 또는 유사 항목이 있으면 그 항목으로 매핑하고, 없으면 UNMAPPED로 둔다.
   - "배당금의 수취"는 canonical CSV에 DIVIDENDS_RECEIVED 또는 유사 항목이 있으면 그 항목으로 매핑하고, 없으면 UNMAPPED로 둔다.

5. 차입금/사채/금융부채 현금흐름:
   - "차입금", "사채", "금융부채"의 증가, 발행, 차입은 DEBT_ISSUE로 매핑한다.
   - "차입금", "사채", "금융부채"의 감소, 상환, 변제는 DEBT_REPAY로 매핑한다.
   - "단기금융부채의 증가" → DEBT_ISSUE
   - "장기금융부채의 증가" → DEBT_ISSUE
   - "단기금융부채의 감소" → DEBT_REPAY
   - "장기금융부채의 감소" → DEBT_REPAY
   - "단기차입금의 차입" → DEBT_ISSUE
   - "장기차입금의 차입" → DEBT_ISSUE
   - "사채의 발행" → DEBT_ISSUE
   - "단기차입금의 상환" → DEBT_REPAY
   - "장기차입금의 상환" → DEBT_REPAY
   - "유동성장기부채의 상환" → DEBT_REPAY
   - "사채의 상환" → DEBT_REPAY
   - "리스부채의 상환"은 차입금/사채 상환이 아니다.
     canonical CSV에 LEASE_REPAYMENT가 있으면 LEASE_REPAYMENT로 매핑하고, 없으면 UNMAPPED로 둔다.
   - "전환우선주의 상환", "상환전환우선주의 상환"은 차입금/사채 상환과 다를 수 있다. 명확한 캐노니컬 항목이 없으면 UNMAPPED로 둔다.

6. TOTAL 계정:
   - TOTAL 계정은 원본 계정과목명에 "총계" 또는 "합계"가 명확히 있을 때만 매핑한다.
   - "자산총계" → TOTAL_ASSETS
   - "자산 합계" → TOTAL_ASSETS
   - "부채총계" → TOTAL_LIABILITIES
   - "부채 합계" → TOTAL_LIABILITIES
   - "자본총계" → TOTAL_EQUITY
   - "자본 합계" → TOTAL_EQUITY
   - "부채및자본총계", "부채와자본총계", "부채와 자본 총계" → UNMAPPED
   - "자산" → UNMAPPED
   - "부채" → UNMAPPED
   - "자본" → UNMAPPED
   - amount가 0이더라도 계정명만 보고 TOTAL 계정으로 매핑하지 마라.

7. 재무상태표 자산:
   - "유동자산" → CURRENT_ASSETS
   - "비유동자산" → NON_CURRENT_ASSETS
   - "현금및현금성자산" → CASH_AND_EQUIVALENTS
   - "단기금융자산", "단기금융상품", "단기투자자산" → SHORT_TERM_FINANCIAL_ASSETS
   - "재고자산" → INVENTORIES
   - "유형자산" → PPE
   - "무형자산" → INTANGIBLE_ASSETS
   - "사용권자산"은 PPE가 아니다. canonical CSV에 RIGHT_OF_USE_ASSETS가 있으면 매핑하고, 없으면 UNMAPPED로 둔다.
   - "투자부동산"은 PPE가 아니다. canonical CSV에 INVESTMENT_PROPERTY가 있으면 매핑하고, 없으면 UNMAPPED로 둔다.
   - "계약자산"은 canonical CSV에 CONTRACT_ASSETS가 있으면 CONTRACT_ASSETS로 매핑하고, 없으면 UNMAPPED로 둔다.
   - "매출채권" → TRADE_RECEIVABLES
   - "매출채권및기타채권"은 canonical CSV에 TRADE_AND_OTHER_RECEIVABLES가 있으면 TRADE_AND_OTHER_RECEIVABLES로 매핑한다.
     없고 OTHER_RECEIVABLES만 있으면 OTHER_RECEIVABLES로 매핑할 수 있다.
     둘 다 없으면 TRADE_RECEIVABLES로 매핑할 수 있다.
   - "장기매출채권및기타채권"은 canonical CSV에 NON_CURRENT_RECEIVABLES 또는 OTHER_RECEIVABLES가 있으면 적절한 항목으로 매핑하고, 없으면 UNMAPPED로 둔다.
   - "파생상품자산", "당기법인세자산", "이연법인세자산", "기타유동자산", "기타비유동자산"은 정확한 캐노니컬 항목이 없으면 UNMAPPED로 둔다.

8. 재무상태표 부채:
   - "유동부채" → CURRENT_LIABILITIES
   - "비유동부채"는 canonical CSV에 NON_CURRENT_LIABILITIES가 있으면 매핑하고, 없으면 UNMAPPED로 둔다.
   - "부채총계" → TOTAL_LIABILITIES
   - "단기차입금", "유동성장기부채", "단기금융부채" → SHORT_TERM_DEBT
   - "장기차입금", "사채", "장기금융부채" → LONG_TERM_DEBT
   - "리스부채", "유동리스부채", "비유동리스부채" → LEASE_LIABILITY
   - "계약부채"는 canonical CSV에 CONTRACT_LIABILITIES가 있으면 CONTRACT_LIABILITIES로 매핑하고, 없으면 UNMAPPED로 둔다.
   - "선수금"은 canonical CSV에 ADVANCES_FROM_CUSTOMERS가 있으면 ADVANCES_FROM_CUSTOMERS로 매핑하고, 없으면 UNMAPPED로 둔다.
   - "매입채무" → TRADE_PAYABLES
   - "매입채무및기타채무"는 canonical CSV에 TRADE_AND_OTHER_PAYABLES가 있으면 TRADE_AND_OTHER_PAYABLES로 매핑한다.
     없고 OTHER_PAYABLES만 있으면 OTHER_PAYABLES로 매핑할 수 있다.
     둘 다 없으면 TRADE_PAYABLES로 매핑할 수 있다.
   - "장기매입채무및기타채무"는 canonical CSV에 NON_CURRENT_PAYABLES 또는 OTHER_PAYABLES가 있으면 적절한 항목으로 매핑하고, 없으면 UNMAPPED로 둔다.
   - "파생상품부채", "당기법인세부채", "이연법인세부채", "충당부채", "확정급여부채", "기타유동부채", "기타비유동부채"는 정확한 캐노니컬 항목이 없으면 UNMAPPED로 둔다.

9. 재무상태표 자본:
   - "자본총계" → TOTAL_EQUITY
   - "지배기업의 소유주에게 귀속되는 자본" → EAOP
   - "지배기업소유주지분" → EAOP
   - "지배주주지분" → EAOP
   - "비지배지분" → NEAOP
   - "이익잉여금" → RETAINED_EARNINGS
   - "자본금", "자본잉여금", "자본조정", "기타포괄손익누계액", "기타자본항목"은 canonical CSV에 정확한 항목이 없으면 UNMAPPED로 둔다.

10. 손익계산서:
   - "매출액", "영업수익", "수익" → REVENUE
   - "매출원가", "영업비용"은 의미가 매출원가로 명확하면 COGS로 매핑한다.
   - "매출총이익" → GROSS_PROFIT
   - "판매비와관리비", "판매비 및 관리비", "판매관리비" → SGNA
   - "연구개발비"는 canonical CSV에 RND가 있으면 RND로 매핑하고, 없으면 UNMAPPED로 둔다.
   - "영업이익", "영업이익(손실)" → OPERATING_INCOME
   - "법인세비용차감전순이익", "법인세비용차감전순이익(손실)", "세전이익"은 canonical CSV에 PBT가 있으면 PBT로 매핑하고, 없으면 UNMAPPED로 둔다.
   - "법인세비용", "법인세비용(수익)"은 canonical CSV에 TAX_EXPENSE가 있으면 TAX_EXPENSE로 매핑하고, 없으면 UNMAPPED로 둔다.
   - "당기순이익", "당기순이익(손실)", "당기순손익"은 canonical CSV에 NET_INCOME이 있으면 NET_INCOME으로 매핑하고, 없으면 UNMAPPED로 둔다.
   - "지배기업의 소유주지분"이 "당기순이익의 귀속" 문맥에서 나온 행이면 canonical CSV에 NET_INCOME_PARENT가 있을 때만 NET_INCOME_PARENT로 매핑한다.
   - "비지배지분"이 "당기순이익의 귀속" 문맥에서 나온 행이면 canonical CSV에 NET_INCOME_NCI가 있을 때만 NET_INCOME_NCI로 매핑한다.
   - 문맥이 없고 단순히 "지배기업의 소유주지분", "비지배지분"만 있으면 UNMAPPED로 둔다.
   - "금융수익", "금융비용", "기타수익", "기타비용", "기타영업외수익", "기타영업외비용", "지분법손익"은 canonical CSV에 정확한 항목이 없으면 UNMAPPED로 둔다.
   - "기본주당이익", "희석주당이익", "주당이익", "지배기업의 소유주지분에 대한 주당이익"은 canonical CSV에 정확한 항목이 없으면 UNMAPPED로 둔다.

11. 보험/금융업 특화 계정:
   - 보험수익, 보험서비스수익, 보험계약수익은 canonical CSV에 해당 항목이 있으면 매핑하고, 없으면 REVENUE로 매핑할 수 있다.
   - 보험서비스비용, 보험계약비용은 canonical CSV에 해당 항목이 있으면 매핑하고, 없으면 COGS 또는 UNMAPPED 중 의미가 더 가까운 쪽을 선택한다.
   - 이자수익, 이자비용, 수수료수익, 수수료비용, 금융상품평가손익, 대손상각비는 canonical CSV에 정확한 항목이 없으면 UNMAPPED로 둔다.
   - 은행/보험/증권업에서는 일반 제조업의 매출채권, 재고자산, 매입채무 회전율 개념이 맞지 않을 수 있으므로 억지로 제조업 계정에 매핑하지 마라.

12. 모호한 항목:
   - "기타", "그 밖의", "잡손익", "기타자산", "기타부채", "기타채권", "기타채무"처럼 의미가 모호한 항목은 명확한 캐노니컬 항목이 없으면 UNMAPPED로 둔다.
   - 표의 섹션 제목 또는 분류 제목은 UNMAPPED로 둔다.
   - 예: "자산", "부채", "자본", "주당손익", "당기순이익의 귀속", "총포괄이익의 귀속", "기타포괄손익", "후속적으로 당기손익으로 재분류되는 항목", "후속적으로 당기손익으로 재분류되지 않는 항목"

대표 매핑 예시:
- "영업활동으로 인한 순현금흐름" → CFO
- "영업활동으로 인한 순현금흐름 (주43)" → CFO
- "영업활동에서 창출된 현금흐름 (주43)" → UNMAPPED
- "투자활동으로 인한 순현금흐름" → CFI
- "투자활동으로 인한 순현금흐름 (주26)" → CFI
- "재무활동으로 인한 순현금흐름" → CFF
- "재무활동으로 인한 순현금흐름 (주26)" → CFF
- "현금및현금성자산" → CASH_AND_EQUIVALENTS
- "기말의 현금및현금성자산" → UNMAPPED
- "유형자산의 취득" → CAPEX_PPE
- "유형자산의 처분" → PPE_DISPOSAL_PROCEEDS 또는 UNMAPPED
- "무형자산의 취득" → CAPEX_INTANG
- "무형자산의 처분" → INTANGIBLE_DISPOSAL_PROCEEDS 또는 UNMAPPED
- "리스부채의 상환" → LEASE_REPAYMENT 또는 UNMAPPED
- "자산" → UNMAPPED
- "자산총계" → TOTAL_ASSETS
- "부채" → UNMAPPED
- "부채총계" → TOTAL_LIABILITIES
- "자본" → UNMAPPED
- "자본총계" → TOTAL_EQUITY
- "부채및자본총계" → UNMAPPED
- "매출액" → REVENUE
- "매출원가" → COGS
- "매출총이익" → GROSS_PROFIT
- "판매비와관리비" → SGNA
- "영업이익" → OPERATING_INCOME
- "법인세비용차감전순이익(손실)" → PBT 또는 UNMAPPED
- "법인세비용(수익)" → TAX_EXPENSE 또는 UNMAPPED
- "당기순이익 (주36,43)" → NET_INCOME 또는 UNMAPPED

출력 JSON 스키마:
{{
  "items": [
    {{
      "canonical_account_id": "캐노니컬 ID 또는 UNMAPPED",
      "canonical_account_name": "캐노니컬 계정과목명 또는 미매핑",
      "original_account_name": "입력 original_account_name 그대로",
      "statement_type": "CF 또는 BS 또는 IS",
      "period": "입력 period 그대로",
      "amount": "입력 amount 그대로"
    }}
  ]
}}

검증 조건:
- items 배열 길이 = 입력 row 개수
- items[i].original_account_name = 입력 rows[i].original_account_name
- items[i].period = 입력 rows[i].period
- items[i].amount = 입력 rows[i].amount
- items[i].statement_type은 정규화된 값이어야 한다.
- canonical_account_id가 "UNMAPPED"가 아니면 반드시 캐노니컬 CSV에 존재해야 한다.
- canonical_account_name은 canonical_account_id에 대응되는 CSV의 계정명과 정확히 같아야 한다.
- 어떤 행도 누락하지 마라.
- 어떤 행도 추가하지 마라.
- 확실한 명시적 매핑 규칙에 해당하는 행을 UNMAPPED로 두지 마라.
- 단, 명시적 규칙에서 "canonical CSV에 있으면 매핑하고 없으면 UNMAPPED"라고 한 항목은 CSV에 해당 canonical_id가 있을 때만 매핑한다.

캐노니컬 계정과목 CSV:
{canonical_accounts_csv}

입력 row JSON:
{input_rows_json}

유효한 JSON 객체 하나만 출력하라.
""".strip()


def build_zai_client() -> OpenAI:
    api_key = os.getenv(ZAI_API_KEY_ENV)

    if not api_key:
        raise RuntimeError(
            f"환경변수 {ZAI_API_KEY_ENV}가 없습니다. "
            f"예: PowerShell에서 $env:{ZAI_API_KEY_ENV}='your-api-key'"
        )

    return OpenAI(
        api_key=api_key,
        base_url=ZAI_BASE_URL,
        timeout=ZAI_TIMEOUT_SEC,
        max_retries=0,  # 자체 재시도 로직 사용
    )


def _sleep_for_retry(attempt: int) -> None:
    sleep_s = min(30.0, 0.8 * (2 ** attempt)) + random.uniform(0, 0.4)
    time.sleep(sleep_s)


def call_llm_json(client: OpenAI, prompt: str) -> str:
    last_error: Exception | None = None

    for attempt in range(MAX_API_RETRIES + 1):
        try:
            result = client.chat.completions.create(
                model=ZAI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "너는 회계 계정과목 정규화 엔진이다. 반드시 JSON만 출력한다.",
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                temperature=0.0,
                top_p=0.95,
                max_tokens=MAX_TOKENS,
                response_format={"type": "json_object"},
                extra_body={
                    "do_sample": False,
                    "thinking": {"type": "disabled"},
                },
            )

            content = result.choices[0].message.content
            if not content:
                raise ValueError("Z.AI 응답 content가 비어 있습니다.")

            return content

        except (RateLimitError, APITimeoutError, APIConnectionError) as e:
            last_error = e
            if attempt >= MAX_API_RETRIES:
                break
            _sleep_for_retry(attempt)

        except APIStatusError as e:
            last_error = e
            if e.status_code not in RETRYABLE_STATUS_CODES or attempt >= MAX_API_RETRIES:
                break
            _sleep_for_retry(attempt)

    assert last_error is not None
    raise last_error

# 배치 처리
def _normalize_batch_once(
    client: OpenAI,
    canonical_df: pd.DataFrame,
    batch_rows: list[dict[str, Any]],
) -> pd.DataFrame:
    batch_rows = [normalize_input_row(r) for r in batch_rows]

    for i, r in enumerate(batch_rows):
        if not isinstance(r["amount"], str):
            raise TypeError(
                f"amount가 str이 아닙니다: i={i}, value={r['amount']}, type={type(r['amount'])}"
            )

    fs_type = batch_rows[0]["statement_type"]

    canonical_csv_text = canonical_candidates_to_csv_text(canonical_df, fs_type)
    prompt = build_prompt(canonical_csv_text, batch_rows)

    model_output = call_llm_json(client, prompt)
    df = parse_llm_json(model_output)

    df = force_input_fields(df, batch_rows)
    df = validate_against_canonical(df, canonical_df)

    return df


def normalize_batch(
    client: OpenAI,
    canonical_df: pd.DataFrame,
    batch_rows: list[dict[str, Any]],
) -> pd.DataFrame:
    batch_rows = [normalize_input_row(r) for r in batch_rows]

    last_error: Exception | None = None

    for _ in range(MAX_NORMALIZE_RETRIES + 1):
        try:
            return _normalize_batch_once(client, canonical_df, batch_rows)
        except Exception as e:
            last_error = e

    if len(batch_rows) > 1:
        mid = len(batch_rows) // 2

        print(
            f"[WARN] 배치 실패 → 반으로 분할: size={len(batch_rows)}, error={last_error}"
        )

        left = normalize_batch(client, canonical_df, batch_rows[:mid])
        right = normalize_batch(client, canonical_df, batch_rows[mid:])

        return pd.concat([left, right], ignore_index=True)

    print(f"[WARN] 단건 LLM 실패 → UNMAPPED 처리: {last_error}")
    print("[DEBUG] row =", batch_rows[0])

    return fallback_unmapped_df(batch_rows)


def normalize_all_rows(
    client: OpenAI,
    canonical_df: pd.DataFrame,
    input_rows: list[dict[str, Any]],
) -> pd.DataFrame:
    dfs: list[pd.DataFrame] = []

    for fs_type in ["CF", "BS", "IS"]:
        fs_rows = [row for row in input_rows if row["statement_type"] == fs_type]

        for start in range(0, len(fs_rows), BATCH_SIZE):
            batch = fs_rows[start : start + BATCH_SIZE]
            if not batch:
                continue

            print(f"[INFO] {fs_type} batch {start} ~ {start + len(batch) - 1}")
            dfs.append(normalize_batch(client, canonical_df, batch))

    if not dfs:
        return pd.DataFrame(columns=EXPECTED_HEADER)

    return pd.concat(dfs, ignore_index=True)


# 저장
def save_result(df: pd.DataFrame, output_path: str | Path) -> None:
    statement_order = {
        "CF": 0,
        "BS": 1,
        "IS": 2,
    }

    out = (
        df.assign(_order=df["statement_type"].map(statement_order).fillna(99))
        .sort_values("_order")
        .drop(columns="_order")
        .loc[:, EXPECTED_HEADER]
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    out.to_csv(
        output_path,
        index=False,
        encoding="utf-8-sig",
        quoting=csv.QUOTE_ALL,
    )

    print(f"[SAVED] {output_path}")

def normalize_financial_statement(
    input_html_path: str | Path,
    company_name: str,
    period: str,
    output_csv_path: str | Path,
    canonical_csv_path: str | Path = CANONICAL_CSV_PATH,
) -> pd.DataFrame:
    statements = get_statements_from_html(input_html_path)

    input_rows = build_input_rows(
        statements=statements,
        company_name=company_name,
        period=period,
    )

    input_rows = [normalize_input_row(r) for r in input_rows]

    canonical_df = load_canonical_accounts(canonical_csv_path)
    client = build_zai_client()

    normalized_df = normalize_all_rows(client, canonical_df, input_rows)

    save_result(normalized_df, output_csv_path)

    return normalized_df

def main() -> None:
    input_html_path = "./data-lake/bronze/dart/finance-statement/009540/finance_statement_(2025.12).html"
    company_name = "HD한국조선해양"
    period = "2025.12"
    output_csv_path = "./normalized_009540_2025.12.csv"

    normalize_financial_statement(
        input_html_path=input_html_path,
        company_name=company_name,
        period=period,
        output_csv_path=output_csv_path,
        canonical_csv_path=CANONICAL_CSV_PATH,
    )


if __name__ == "__main__":
    main()



'''
#stock_codes = [
#    '009540'
#]

#download_statements(stock_codes, 0)

stock_code = '009540'
report_date = '2025.12'

statements = get_statements(stock_code, report_date)
print(statements[0], statements[1], statements[2])
'''