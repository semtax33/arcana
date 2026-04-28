
from __future__ import annotations
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

import io
import re
from pathlib import Path
from typing import Any

import pandas as pd
from llama_cpp import Llama

from statements import download_statements

BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = BASE_DIR / "models" / "google_gemma-3n-E2B-it-Q4_K_M.gguf" #"Qwen3-4B-Q4_K_M.gguf" # "Qwen3-4B-Instruct-2507-Q4_K_M.gguf" 
CANONICAL_CSV_PATH = "./data-lake/meta/CanonicalAccount.csv"

EXPECTED_HEADER = [
    "canonical_account_id",
    "canonical_account_name",
    "original_account_name",
    "statement_type",
    "period",
    "amount"
]

N_CTX = 4096
N_GPU_LAYERS = -1
BATCH_SIZE = 6

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
                {
                    "company_name": company_name,
                    "original_account_name": account.name,
                    "statement_type": fs_type,
                    "amount": account.value,
                    "period": period,
                }
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

    return df.loc[:, EXPECTED_HEADER].fillna("")


# 검증: LLM이 없는 canonical_id 만들면 UNMAPPED 처리
def validate_against_canonical(
    normalized_df: pd.DataFrame,
    canonical_df: pd.DataFrame,
) -> pd.DataFrame:
    valid_ids = set(canonical_df["canonical_id"].astype(str))

    invalid_mask = (
        normalized_df["canonical_account_id"].ne("UNMAPPED")
        & ~normalized_df["canonical_account_id"].isin(valid_ids)
    )

    normalized_df.loc[invalid_mask, "canonical_account_id"] = "UNMAPPED"
    normalized_df.loc[invalid_mask, "canonical_account_name"] = "미매핑"

    unmapped_mask = normalized_df["canonical_account_id"].eq("UNMAPPED")
    normalized_df.loc[unmapped_mask, "canonical_account_name"] = "미매핑"

    return normalized_df


def fallback_unmapped_df(input_rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "canonical_account_id": "UNMAPPED",
                "canonical_account_name": "미매핑",
                "original_account_name": row["original_account_name"],
                "statement_type": row["statement_type"],
                "period": row["period"],
                "amount": row["amount"]
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
    return f"""
너는 한국 DART 재무제표 계정과목을 캐노니컬 계정과목으로 매핑하는 엔진이다.

작업:
입력된 원본 재무제표 계정과목을 제공된 캐노니컬 계정과목 CSV 기준으로 정규화한다.

절대 규칙:
- CSV에 없는 canonical_id를 만들지 마라.
- 적절한 매핑이 없으면 canonical_account_id="UNMAPPED", canonical_account_name="미매핑"으로 출력하라.
- 금액 계산, 단위 변환, 합계 계산, 파생 계정 생성은 하지 마라.
- amount는 참고만 하고 출력하지 마라.
- original_account_name은 입력값 그대로 보존하라.
- 입력 row 1개당 출력 item 1개를 생성하라.
- 설명문, 마크다운, 코드블록 없이 JSON만 출력하라.

statement_type 변환:
- 재무상태표, BS, balance_sheet → BS
- 현금흐름표, CF, cash_flow_statement → CF
- 손익계산서, 포괄손익계산서, IS, income_statement → IS

매핑 기준:
1. 입력 statement_type과 canonical CSV의 fs_type이 같은 후보만 선택한다.
2. 원본 계정과목명과 canonical_nm, description, 비고의 의미가 가장 가까운 항목을 선택한다.
3. 모호하면 UNMAPPED로 둔다.
4. 총계/합계 계정은 원본에 총계/합계가 명시된 경우에만 TOTAL 계정으로 매핑한다.
5. 유형자산 취득은 CAPEX_PPE, 무형자산 취득은 CAPEX_INTANG, 배당금 지급은 DIV_PAID, 차입금/사채 조달은 DEBT_ISSUE, 차입금/사채 상환은 DEBT_REPAY로 매핑한다.
6. 이자지급은 INT_PAID, 법인세지급은 TAX_PAID로 매핑한다.
7. 영업활동현금흐름은 CFO, 투자활동현금흐름은 CFI, 재무활동현금흐름은 CFF로 매핑한다.

출력 JSON 스키마:
{{
  "items": [
    {{
      "canonical_account_id": "캐노니컬 ID 또는 UNMAPPED",
      "canonical_account_name": "캐노니컬 계정과목명 또는 미매핑",
      "original_account_name": "원본 계정과목명",
      "statement_type": "CF 또는 BS 또는 IS",
      "period": "회계일시",
      "amount": "금액"
    }}
  ]
}}

캐노니컬 계정과목 CSV:
{canonical_accounts_csv}

입력 row:
{input_rows}

JSON만 출력하라.
""".strip()


# LLM
def load_llm() -> Llama:
    return Llama(
        model_path=str(MODEL_PATH),
        n_ctx=N_CTX,
        n_gpu_layers=N_GPU_LAYERS,
        verbose=False,
    )


def call_llm_json(llm: Llama, prompt: str) -> str:
    result = llm.create_chat_completion(
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
        top_p=0.9,
        max_tokens=4096,
        response_format={"type": "json_object"},
    )

    return result["choices"][0]["message"]["content"]


# 배치 처리
def normalize_batch(
    llm: Llama,
    canonical_df: pd.DataFrame,
    batch_rows: list[dict[str, Any]],
) -> pd.DataFrame:
    fs_type = batch_rows[0]["statement_type"]

    canonical_csv_text = canonical_candidates_to_csv_text(canonical_df, fs_type)
    prompt = build_prompt(canonical_csv_text, batch_rows)

    try:
        model_output = call_llm_json(llm, prompt)
        df = parse_llm_json(model_output)

        if len(df) != len(batch_rows):
            raise ValueError(
                f"입력/출력 row 수 불일치: input={len(batch_rows)}, output={len(df)}"
            )

        return validate_against_canonical(df, canonical_df)

    except Exception as e:
        print(f"[WARN] LLM 배치 실패 → UNMAPPED 처리: {e}")
        print("[DEBUG] batch_rows =", batch_rows)
        return fallback_unmapped_df(batch_rows)


def normalize_all_rows(
    llm: Llama,
    canonical_df: pd.DataFrame,
    input_rows: list[dict[str, Any]],
) -> pd.DataFrame:
    dfs: list[pd.DataFrame] = []

    for fs_type in ["CF", "BS", "IS"]:
        fs_rows = [row for row in input_rows if row["statement_type"] == fs_type]

        for start in range(0, len(fs_rows), BATCH_SIZE):
            batch = fs_rows[start:start + BATCH_SIZE]
            if not batch:
                continue

            print(f"[INFO] {fs_type} batch {start} ~ {start + len(batch) - 1}")
            dfs.append(normalize_batch(llm, canonical_df, batch))

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

    out.to_csv(
        output_path,
        index=False,
        encoding="utf-8-sig",
        quoting=1,  # csv.QUOTE_ALL과 동일
    )

    print(f"[SAVED] {output_path}")



def main() -> None:
    # 네가 기존 main.py에서 만든 get_statements_from_html / build_input_rows를 사용한다고 가정
    statements = get_statements_from_html(
        "./data-lake/bronze/dart/finance-statement/009540/finance_statement_(2025.12).html"
    )

    input_rows = build_input_rows(
        statements=statements,
        company_name="HD한국조선해양",
        period="2025.12",
    )

    canonical_df = load_canonical_accounts(CANONICAL_CSV_PATH)
    llm = load_llm()

    normalized_df = normalize_all_rows(llm, canonical_df, input_rows)

    save_result(
        normalized_df,
        "./normalized_009540_2025.12.csv",
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