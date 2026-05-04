from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from bs4 import BeautifulSoup, Tag


# amount는 하위 호환용으로 normalized_amount와 동일하게 저장한다.
EXPECTED_HEADER = [
    "canonical_account_id",
    "canonical_account_name",
    "original_account_name",
    "statement_type",
    "period",
    "amount",
    "raw_amount",
    "normalized_amount",
    "cash_effect_amount",
    "amount_policy",
    "cash_direction",
]

DEBUG_COLUMNS = [
    "rule_id",
    "reason",
    "raw_account_name",
    "normalized_name",
    "indent_level",
    "has_children",
    "section_context",
    "parent_context",
    "context_path",
    "context_rule_id",
    "context_reason",
    "amount_raw",
    "unit_factor",
]

UNITS = {
    "천만원": 10_000_000,
    "백만원": 1_000_000,
    "십만원": 100_000,
    "만원": 10_000,
    "천원": 1_000,
    "백원": 100,
    "십원": 10,
    "원": 1,
}

VALID_AMOUNT_POLICIES = {"as_reported", "abs", "neg_abs"}
VALID_CASH_DIRECTIONS = {"", "inflow", "outflow"}


def safe_str(value: Any) -> str:
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
        return format(value, ".15g")

    if isinstance(value, Decimal):
        return format(value, "f")

    return str(value)


def normalize_account_name(value: Any) -> str:
    """
    매칭용 계정명 정규화.
    출력 original_account_name은 절대 이 값으로 바꾸지 않는다.
    """
    if value is None:
        return ""

    s = str(value).strip()

    # 앞뒤 따옴표 제거
    s = s.strip().strip('"').strip("'").strip("“”‘’")

    # 꺾쇠 wrapper 제거
    s = re.sub(r"^\s*[<〈《]\s*", "", s)
    s = re.sub(r"\s*[>〉》]\s*$", "", s)

    # DART 주석 제거: (주26), (주29,35,36,48)
    s = re.sub(r"\(주\s*\d+(?:\s*[,\.]\s*\d+)*\)", "", s)

    # DART 주석 제거: (주석3.5.12.22.28), (주석8,29,30)
    s = re.sub(r"\(주석\s*[^)]*\)", "", s)

    # 순수 주석 번호 제거: (19,35,36,37), (10.23.29)
    # 단, (유동), (장기), (결손금) 같은 의미 있는 괄호는 여기서 제거하지 않는다.
    s = re.sub(r"\(\s*\d+(?:\s*[,\.]\s*\d+)*\s*\)", "", s)

    # 단위 제거: (단위 : 원)
    s = re.sub(r"\(단위\s*[:：]\s*[^)]*\)", "", s)

    # [개요] 같은 대괄호 설명 제거
    s = re.sub(r"\[[^\]]*개요[^\]]*\]", "", s)

    # (*) 같은 별표 주석 제거
    s = re.sub(r"\(\s*\*\s*\)", "", s)
    s = s.replace("*", "")

    # 업종/시장 suffix 제거
    s = re.sub(r"[-,，]\s*(금융업|증권업)\s*$", "", s)
    s = re.sub(r"\(\s*(금융업|증권업)\s*\)", "", s)

    # 앞 번호/로마자/괄호/마침표 prefix 제거
    # 주의: 한글/영문 prefix는 반드시 구분자가 있을 때만 제거한다.
    prefix_patterns = [
        r"^\s*\(\s*\d+\s*\)\s*",                    # (1)
        r"^\s*\(?\s*\d+\s*[.)．、]\s*",             # 1. / 1)
        r"^\s*\(?[IVXLCDMivxlcdmⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+\)?\s*[.)．、]\s*",  # I. / XIII. / ⅩⅢ. / XⅢ.
        r"^\s*[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]\s*",      # ①
        r"^\s*(?:\([가-힣]\)|[가-힣][.)．、])\s*",    # 가. / 나) / (다)
        r"^\s*(?:\([A-Za-z]\)|[A-Za-z][.)．、])\s*",  # A. / b)
    ]

    changed = True
    while changed:
        before = s
        for pattern in prefix_patterns:
            s = re.sub(pattern, "", s)
        changed = before != s

    # 후행 설명 제거
    s = re.sub(r"[,，]\s*(총액|합계|계)\s*$", "", s)

    # IFRS 표기에서 괄호 안의 손실/수익은 매칭용으로 붙여서 본다.
    # 예: 법인세비용(수익) -> 법인세비용수익
    s = s.replace("(", "").replace(")", "")

    # 공백 제거
    s = s.replace("\u3000", "")
    s = re.sub(r"\s+", "", s)

    # 구분자 제거
    s = s.replace("ㆍ", "")
    s = s.replace("·", "")
    s = s.replace("/", "")
    s = s.replace("-", "")
    s = s.replace(",", "")
    s = s.replace("，", "")

    # 끝 마침표 제거
    s = re.sub(r"[.．。]+$", "", s)

    # 끝 숫자 suffix 제거: 단기차입금1 -> 단기차입금
    s = re.sub(r"(?<=[가-힣])\d+$", "", s)

    return s.strip()


def normalize_context(value: Any) -> str:
    return normalize_account_name(value)


def normalize_statement_type(value: Any) -> str:
    s = safe_str(value).strip()
    lower = s.lower()

    if s in {"재무상태표", "연결재무상태표", "별도재무상태표"} or lower in {
        "bs",
        "balance_sheet",
    }:
        return "BS"

    if s in {"현금흐름표", "연결현금흐름표", "별도현금흐름표"} or lower in {
        "cf",
        "cash_flow_statement",
    }:
        return "CF"

    if s in {
        "손익계산서",
        "포괄손익계산서",
        "연결손익계산서",
        "연결포괄손익계산서",
        "별도손익계산서",
        "별도포괄손익계산서",
    } or lower in {
        "is",
        "income_statement",
        "comprehensive_income_statement",
    }:
        return "IS"

    return "UNKNOWN"


def statement_sort_key(fs_type: str) -> int:
    return {"CF": 0, "BS": 1, "IS": 2}.get(fs_type, 99)


def parse_unit_factor(text: str) -> int:
    t = safe_str(text)
    for unit, factor in UNITS.items():
        if unit in t:
            return factor
    return 1


def parse_amount(value: Any, unit_factor: int = 1) -> int:
    """
    DART HTML 표 금액을 원 단위 int로 변환.
    괄호, △, ▲, - 금액은 음수 처리한다.
    """
    if value is None:
        return 0

    s = safe_str(value)
    s = s.replace("\u3000", "")
    s = s.replace(",", "")
    s = s.strip()

    if s in {"", "-", "－", "—", "–"}:
        return 0

    sign = 1

    if len(s) >= 2 and s[0] == "(" and s[-1] == ")":
        sign = -1
        s = s[1:-1].strip()

    if s.startswith(("△", "▲")):
        sign = -1
        s = s[1:].strip()

    if s.startswith(("-", "－")):
        sign = -1
        s = s[1:].strip()

    s = re.sub(r"[^0-9.]", "", s)

    if not s:
        return 0

    try:
        return int(float(s)) * sign * unit_factor
    except ValueError:
        return 0


def amount_to_int(value: Any) -> int:
    s = safe_str(value).replace(",", "").strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except Exception:
        return 0


def apply_amount_policy(raw_amount: int, amount_policy: str) -> int:
    policy = safe_str(amount_policy).strip() or "as_reported"

    if policy not in VALID_AMOUNT_POLICIES:
        policy = "as_reported"

    if policy == "abs":
        return abs(raw_amount)

    if policy == "neg_abs":
        return -abs(raw_amount)

    return raw_amount


def apply_cash_direction(normalized_amount: int, cash_direction: str) -> int:
    direction = safe_str(cash_direction).strip()

    if direction == "inflow":
        return abs(normalized_amount)

    if direction == "outflow":
        return -abs(normalized_amount)

    return normalized_amount


def detect_indent_level(raw_name: str, td_style: str = "") -> int:
    s = safe_str(raw_name)
    s = re.sub(r"^[\r\n\t]+", "", s)

    level = 0
    for ch in s:
        if ch == "\u3000":
            level += 1
        elif ch == " ":
            continue
        else:
            break

    style = safe_str(td_style)
    m = re.search(r"padding-left\s*:\s*(\d+)px", style)

    if m:
        px = int(m.group(1))
        level = max(level, px // 20)

    return level


def normalize_input_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["company_name"] = safe_str(out.get("company_name"))
    out["original_account_name"] = safe_str(out.get("original_account_name"))
    out["raw_account_name"] = safe_str(out.get("raw_account_name", out["original_account_name"]))
    out["statement_type"] = normalize_statement_type(out.get("statement_type"))
    out["period"] = safe_str(out.get("period"))
    out["amount"] = safe_str(out.get("amount"))
    out["amount_raw"] = safe_str(out.get("amount_raw"))
    out["unit_factor"] = safe_str(out.get("unit_factor"))
    return out


def get_statement_type_from_text(text: str) -> str:
    t = safe_str(text)

    if "재무상태표" in t:
        return "BS"

    if "현금흐름표" in t:
        return "CF"

    if "손익계산서" in t or "포괄손익계산서" in t:
        return "IS"

    return "UNKNOWN"


def is_data_table(table) -> bool:
    return safe_str(table.get("border")) == "1"


def is_header_table(table) -> bool:
    if is_data_table(table):
        return False

    text = table.get_text(" ", strip=True)

    if "첨부된 연결재무제표에 대한 주석" in text:
        return False

    return any(
        keyword in text
        for keyword in [
            "재무상태표",
            "손익계산서",
            "포괄손익계산서",
            "현금흐름표",
            "자본변동표",
        ]
    )


def find_next_data_table(header_table):
    node = header_table

    while node is not None:
        node = node.find_next_sibling()

        if node is None:
            return None

        if getattr(node, "name", None) != "table":
            continue

        if is_data_table(node):
            return node

        if is_header_table(node):
            return None

    return None


def extract_rows_from_dart_html(
    html_path: str | Path,
    company_name: str,
    period: str,
) -> list[dict[str, Any]]:
    html_path = Path(html_path)

    if not html_path.exists():
        raise FileNotFoundError(f"HTML 파일을 찾을 수 없습니다: {html_path}")

    html = html_path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "lxml")

    rows: list[dict[str, Any]] = []
    table_index = 0

    for header_table in soup.find_all("table"):
        if not is_header_table(header_table):
            continue

        header_text = header_table.get_text(" ", strip=True)
        fs_type = get_statement_type_from_text(header_text)

        if "자본변동표" in header_text:
            continue

        if fs_type == "UNKNOWN":
            continue

        body_table = find_next_data_table(header_table)

        if body_table is None:
            print(f"[WARN] 본문 테이블을 찾지 못함: {header_text[:80]}")
            continue

        unit_factor = parse_unit_factor(header_text)

        for row_index, tr in enumerate(body_table.find_all("tr")):
            tds = tr.find_all("td")

            if len(tds) < 2:
                continue

            account_td = tds[0]
            amount_td = tds[1]

            raw_account_name = account_td.get_text("", strip=False)
            original_account_name = raw_account_name.strip()

            if not original_account_name:
                continue

            td_style = safe_str(account_td.get("style", ""))
            amount_raw = amount_td.get_text(" ", strip=True)
            raw_amount = parse_amount(amount_raw, unit_factor)

            rows.append(
                {
                    "company_name": company_name,
                    "statement_type": fs_type,
                    "period": period,
                    "table_index": table_index,
                    "row_index": row_index,
                    "raw_account_name": raw_account_name,
                    "original_account_name": original_account_name,
                    "normalized_name": normalize_account_name(original_account_name),
                    "indent_level": detect_indent_level(raw_account_name, td_style),
                    "amount": safe_str(raw_amount),
                    "raw_amount": safe_str(raw_amount),
                    "amount_raw": amount_raw,
                    "unit_factor": safe_str(unit_factor),
                    "table_title": header_text,
                }
            )

        table_index += 1

    rows.sort(
        key=lambda r: (
            statement_sort_key(r["statement_type"]),
            int(r["table_index"]),
            int(r["row_index"]),
        )
    )

    return [normalize_input_row(r) for r in rows]



def text_of(tag: Tag) -> str:
    return " ".join(tag.get_text(" ", strip=True).split())

def iter_section_tables(soup: BeautifulSoup, section_name: str):
    section_headers = soup.select("p.table-group-xbrl")
    unit_re = re.compile(r"\(\s*단위\s*:\s*[^)]+?원\s*\)")

    for header in section_headers:
        section_title = text_of(header)

        if section_name not in section_title:
            continue

        node = header.find_next_sibling()
        while node:
            # 다음 주석/섹션 제목 만나면 현재 섹션 종료
            if isinstance(node, Tag) and node.name == "p" and "table-group-xbrl" in node.get("class", []):
                break
            
            if isinstance(node, Tag) and node.name == "table":
                unit_matches = unit_re.findall(node.get_text())
                if len(unit_matches) > 0:
                    unit_value = parse_unit_factor(unit_matches[0])
                # 실제 숫자 테이블 후보
                elif node.get("border") == "1":
                    yield {
                        "unit": safe_str(unit_value),
                        "section_title": section_title,
                        "table": node,
                    }

            node = node.find_next_sibling()

def clean_text(tag: Tag | None) -> str:
    if tag is None:
        return ""
    return " ".join(
        tag.get_text(" ", strip=True)
        .replace("\xa0", " ")
        .replace("　", " ")
        .split()
    )

def parse_number(value: str):
    """
    '8,705'     -> 8705
    '(131)'     -> -131
    '0.380'     -> 0.38
    """
    if value is None:
        return None

    s = value.strip().replace(",", "").replace(" ", "")
    if not s:
        return None

    is_negative = s.startswith("(") and s.endswith(")")
    if is_negative:
        s = s[1:-1]

    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return None

    num = float(s) if "." in s else int(s)
    return -num if is_negative else num


def find_last_value_in_matched_row_by_regex(table: Tag, patterns: dict[str, str], unit: str):
    """
    patterns 예:
    {
        "AMORTIZATION": r"^무형자산상각비$",
        "AR_CHANGE": r"^매출채권의 증가$",
        "AR_DISPOSAL_LOSS": r"^매출채권처분손실$",
    }
    """

    compiled = {
        key: re.compile(pattern)
        for key, pattern in patterns.items()
    }

    results = []

    for row_idx, tr in enumerate(table.select("tr")):
        cells = tr.find_all(["td", "th"], recursive=False)

        if not cells:
            continue

        cell_texts = [clean_text(cell) for cell in cells]
        row_text = " ".join(cell_texts)

        # 보통 첫 번째 또는 두 번째 셀이 계정명이고 마지막 셀이 값
        label_candidates = cell_texts[:-1]
        raw_value = cell_texts[-1]
        value = parse_number(raw_value)

        for label in label_candidates:
            for key, regex in compiled.items():
                if regex.search(label):
                    results.append({
                        "key": key,
                        "matched_label": label,
                        "row_text": row_text,
                        "raw_value": raw_value,
                        "value": value * int(unit),
                        "row_idx": row_idx,
                    })

    return results

def extract_rows_from_dart_comment_html(
    html_path: str | Path,
    company_name: str,
    period: str,
    section_name: str = "현금흐름",
    target_patterns: dict[str, str] = {
        "DEPRECIATION": r"^감가상각비$",
        "AMORTIZATION": r"^무형자산상각비$",
        "BAD_DEBT_EXPENSE": r"^대손상각비$",
        "AR": r"^매출채권$",
        "INTEREST_EXPENSE": r"^이자비용$",
    }
) -> list[dict[str, Any]]:
    html_path = Path(html_path)

    if not html_path.exists():
        raise FileNotFoundError(f"HTML 파일을 찾을 수 없습니다: {html_path}")

    html = html_path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "lxml")

    rows = []
    
    for item in iter_section_tables(soup, section_name):
        section_title = item["section_title"]
        table = item["table"]

        hits = find_last_value_in_matched_row_by_regex(table, target_patterns, item["unit"])

        for hit in hits:
            rows.append({
                "section_title": section_title,
                "key": hit["key"],
                "label": hit["matched_label"],
                "raw_value": hit["raw_value"],
                "value": hit["value"],
            })

    unique_rows = {}
    for row in rows:
        if not row["key"] in unique_rows:
            unique_rows[row["key"]] = row

    #for row in unique_rows.values():
    #    print(row)

    return unique_rows.values()



@dataclass(frozen=True)
class ContextAction:
    action_type: str
    context_label: str
    rule_id: str
    reason: str


def add_structural_features(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = []

    for row in rows:
        out = dict(row)
        out["statement_type"] = normalize_statement_type(out.get("statement_type"))
        out["original_account_name"] = safe_str(out.get("original_account_name"))
        out["raw_account_name"] = safe_str(out.get("raw_account_name", out["original_account_name"]))
        out["normalized_name"] = normalize_account_name(out["original_account_name"])

        if "indent_level" not in out:
            out["indent_level"] = detect_indent_level(out["raw_account_name"])
        else:
            try:
                out["indent_level"] = int(out["indent_level"])
            except Exception:
                out["indent_level"] = detect_indent_level(out["raw_account_name"])

        enriched.append(out)

    for i, row in enumerate(enriched):
        has_children = False

        if i + 1 < len(enriched):
            current_group = (row.get("statement_type"), row.get("table_index"))
            next_group = (
                enriched[i + 1].get("statement_type"),
                enriched[i + 1].get("table_index"),
            )

            if current_group == next_group:
                has_children = int(enriched[i + 1]["indent_level"]) > int(row["indent_level"])

        row["has_children"] = has_children

    return enriched


class ContextEngine:
    def __init__(self, rules: list[dict[str, Any]]):
        self.rules = sorted(
            rules,
            key=lambda r: int(r.get("priority", 0)),
            reverse=True,
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ContextEngine":
        with Path(path).open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        rules = data.get("context_rules", [])

        if not isinstance(rules, list):
            raise ValueError("context_rules must be a list")

        return cls(rules)

    def enrich_context(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = add_structural_features(rows)
        output: list[dict[str, Any]] = []
        context_stack: list[dict[str, Any]] = []
        prev_group = None

        for row in rows:
            group = (row.get("statement_type"), row.get("table_index"))

            if prev_group is not None and group != prev_group:
                context_stack = []

            prev_group = group
            level = int(row.get("indent_level", 0))

            while context_stack and int(context_stack[-1]["level"]) >= level:
                context_stack.pop()

            context_path = " > ".join(c["label"] for c in context_stack)
            section_context = context_stack[-1]["label"] if context_stack else ""

            out = dict(row)
            out["parent_context"] = context_path
            out["section_context"] = section_context
            out["context_path"] = context_path

            action = self.classify(row)

            if action:
                out["context_rule_id"] = action.rule_id
                out["context_reason"] = action.reason
            else:
                out["context_rule_id"] = ""
                out["context_reason"] = ""

            output.append(out)

            if action and action.action_type == "PUSH_CONTEXT":
                context_stack.append(
                    {
                        "level": level,
                        "label": action.context_label,
                        "rule_id": action.rule_id,
                    }
                )

        return output

    def classify(self, row: dict[str, Any]) -> ContextAction | None:
        normalized_row = self._normalize_row(row)

        for rule in self.rules:
            if self._match_rule(normalized_row, rule):
                action = rule.get("action", {}) or {}
                action_type = safe_str(action.get("type", "IGNORE_CONTEXT"))

                label_spec = safe_str(action.get("context_label", ""))
                if label_spec == "SELF":
                    context_label = normalized_row["name"]
                else:
                    context_label = normalize_account_name(label_spec)

                return ContextAction(
                    action_type=action_type,
                    context_label=context_label,
                    rule_id=safe_str(rule.get("id")),
                    reason=safe_str(rule.get("reason")),
                )

        return None

    def _normalize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "fs_type": normalize_statement_type(row.get("statement_type")),
            "name": normalize_account_name(row.get("original_account_name")),
            "indent_level": int(row.get("indent_level", 0) or 0),
            "has_children": bool(row.get("has_children", False)),
            "amount": safe_str(row.get("amount")),
            "amount_is_zero_or_blank": self._amount_is_zero_or_blank(row.get("amount")),
        }

    @staticmethod
    def _amount_is_zero_or_blank(amount: Any) -> bool:
        s = safe_str(amount).replace(",", "").strip()

        if s == "":
            return True

        try:
            return int(float(s)) == 0
        except Exception:
            return False

    def _match_rule(self, row: dict[str, Any], rule: dict[str, Any]) -> bool:
        fs_type = safe_str(rule.get("fs_type", "ANY")).strip()

        if fs_type not in {"", "ANY"} and fs_type != row["fs_type"]:
            return False

        name = row["name"]

        exact_any = self._normalize_list(rule.get("exact_any", []))
        if exact_any and name not in exact_any:
            return False

        include_all = self._normalize_list(rule.get("include_all", []))
        if include_all and not all(token in name for token in include_all):
            return False

        if not _match_include_any_groups(name, rule, prefix="include_any"):
            return False

        exclude_any = self._normalize_list(rule.get("exclude_any", []))
        if exclude_any and any(token in name for token in exclude_any):
            return False

        conditions = rule.get("conditions", {}) or {}

        if "has_children" in conditions:
            if bool(conditions["has_children"]) != bool(row["has_children"]):
                return False

        if "amount_is_zero_or_blank" in conditions:
            expected = bool(conditions["amount_is_zero_or_blank"])
            if expected != bool(row["amount_is_zero_or_blank"]):
                return False

        return True

    @staticmethod
    def _normalize_list(values: Any) -> list[str]:
        return normalize_values_to_list(values)


@dataclass(frozen=True)
class MappingResult:
    canonical_account_id: str
    canonical_account_name: str
    rule_id: str
    reason: str
    amount_policy: str
    cash_direction: str


@dataclass(frozen=True)
class SignDecision:
    amount_policy: str
    cash_direction: str


class SignPolicyEngine:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.defaults = self.config.get("defaults", {}) or {}
        self.canonical_policies = self.config.get("canonical_policies", {}) or {}

    @classmethod
    def from_yaml(cls, path: str | Path | None) -> "SignPolicyEngine":
        if path is None:
            return cls({})

        p = Path(path)
        if not p.exists():
            print(f"[WARN] sign policy file not found: {p}; using defaults")
            return cls({})

        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        return cls(data)

    def decide(
        self,
        fs_type: str,
        canonical_id: str,
        rule: dict[str, Any] | None = None,
    ) -> SignDecision:
        rule = rule or {}
        fs_type = safe_str(fs_type).strip()
        canonical_id = safe_str(canonical_id).strip()

        default_policy = self.defaults.get(fs_type, {}) or {}
        canonical_policy = self.canonical_policies.get(canonical_id, {}) or {}

        amount_policy = (
            safe_str(rule.get("amount_policy"))
            or safe_str(canonical_policy.get("amount_policy"))
            or safe_str(default_policy.get("amount_policy"))
            or "as_reported"
        )

        cash_direction = (
            safe_str(rule.get("cash_direction"))
            or safe_str(rule.get("cash_effect"))
            or safe_str(canonical_policy.get("cash_direction"))
            or safe_str(default_policy.get("cash_direction"))
            or ""
        )

        if amount_policy not in VALID_AMOUNT_POLICIES:
            amount_policy = "as_reported"

        if cash_direction not in VALID_CASH_DIRECTIONS:
            cash_direction = ""

        return SignDecision(amount_policy=amount_policy, cash_direction=cash_direction)


def normalize_values_to_list(values: Any) -> list[str]:
    if values is None:
        return []

    if isinstance(values, str):
        values = [values]

    return [normalize_account_name(v) for v in values if safe_str(v).strip()]


def _match_include_any_groups(text: str, rule: dict[str, Any], prefix: str) -> bool:
    """
    include_any, include_any_2, include_any_3 ... 형태를 모두 AND 그룹으로 처리한다.
    각 그룹 안에서는 OR, 그룹 간에는 AND.
    """
    group_keys = []
    if prefix in rule:
        group_keys.append(prefix)

    numbered = []
    for key in rule.keys():
        m = re.fullmatch(rf"{re.escape(prefix)}_(\d+)", str(key))
        if m:
            numbered.append((int(m.group(1)), key))

    group_keys.extend(k for _, k in sorted(numbered))

    for key in group_keys:
        tokens = normalize_values_to_list(rule.get(key, []))
        if tokens and not any(token in text for token in tokens):
            return False

    return True


def load_mapping_rules(paths: list[str | Path]) -> list[dict[str, Any]]:
    all_rules: list[dict[str, Any]] = []

    for path in paths:
        path = Path(path)
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        rules = data.get("rules", [])

        if not isinstance(rules, list):
            raise ValueError(f"rules must be a list: {path}")

        for rule in rules:
            rule["_source"] = str(path)

        all_rules.extend(rules)

    return all_rules


class RuleEngine:
    def __init__(
        self,
        canonical_df: pd.DataFrame,
        rules: list[dict[str, Any]],
        sign_policy_engine: SignPolicyEngine | None = None,
    ):
        self.canonical_df = canonical_df.copy()
        self.rules = sorted(
            rules,
            key=lambda r: int(r.get("priority", 0)),
            reverse=True,
        )
        self.sign_policy_engine = sign_policy_engine or SignPolicyEngine()

        self.id_to_name = dict(
            zip(
                self.canonical_df["canonical_id"].astype(str),
                self.canonical_df["canonical_nm"].astype(str),
            )
        )
        self.valid_ids = set(self.id_to_name.keys())

    @classmethod
    def from_files(
        cls,
        canonical_csv_path: str | Path,
        rule_paths: list[str | Path],
        sign_policy_path: str | Path | None = None,
    ) -> "RuleEngine":
        canonical_df = pd.read_csv(canonical_csv_path, dtype=str).fillna("")
        rules = load_mapping_rules(rule_paths)
        sign_policy_engine = SignPolicyEngine.from_yaml(sign_policy_path)
        return cls(
            canonical_df=canonical_df,
            rules=rules,
            sign_policy_engine=sign_policy_engine,
        )

    def has_canonical_id(self, canonical_id: str) -> bool:
        return canonical_id == "UNMAPPED" or canonical_id in self.valid_ids

    def canonical_name(self, canonical_id: str) -> str:
        if canonical_id == "UNMAPPED":
            return "미매핑"
        return self.id_to_name.get(canonical_id, "미매핑")

    def map_row(self, row: dict[str, Any]) -> MappingResult:
        normalized_row = self._normalize_row_for_matching(row)

        for rule in self.rules:
            if not self._match_rule(normalized_row, rule):
                continue

            canonical_id = safe_str(rule.get("canonical_id", "UNMAPPED")).strip()

            if not self.has_canonical_id(canonical_id):
                canonical_id = safe_str(rule.get("fallback_if_missing", "UNMAPPED")).strip()

            if not self.has_canonical_id(canonical_id):
                canonical_id = "UNMAPPED"

            sign_decision = self.sign_policy_engine.decide(
                fs_type=normalized_row["fs_type"],
                canonical_id=canonical_id,
                rule=rule,
            )

            return MappingResult(
                canonical_account_id=canonical_id,
                canonical_account_name=self.canonical_name(canonical_id),
                rule_id=safe_str(rule.get("id")),
                reason=safe_str(rule.get("reason")),
                amount_policy=sign_decision.amount_policy,
                cash_direction=sign_decision.cash_direction,
            )

        sign_decision = self.sign_policy_engine.decide(
            fs_type=normalized_row["fs_type"],
            canonical_id="UNMAPPED",
            rule=None,
        )

        return MappingResult(
            canonical_account_id="UNMAPPED",
            canonical_account_name="미매핑",
            rule_id="default_unmapped",
            reason="매칭된 룰 없음",
            amount_policy=sign_decision.amount_policy,
            cash_direction=sign_decision.cash_direction,
        )

    def map_rows(
        self,
        rows: list[dict[str, Any]],
        include_debug_cols: bool = True,
    ) -> pd.DataFrame:
        output: list[dict[str, Any]] = []

        for row in rows:
            row = normalize_input_row(row)
            result = self.map_row(row)

            raw_amount = amount_to_int(row.get("raw_amount", row.get("amount")))
            normalized_amount = apply_amount_policy(raw_amount, result.amount_policy)
            cash_effect_amount = apply_cash_direction(normalized_amount, result.cash_direction)

            item = {
                "canonical_account_id": result.canonical_account_id,
                "canonical_account_name": result.canonical_account_name,
                "original_account_name": safe_str(row.get("original_account_name")),
                "statement_type": normalize_statement_type(row.get("statement_type")),
                "period": safe_str(row.get("period")),
                # 하위 호환: amount는 분석용 normalized_amount로 둔다.
                "amount": safe_str(normalized_amount),
                "raw_amount": safe_str(raw_amount),
                "normalized_amount": safe_str(normalized_amount),
                "cash_effect_amount": safe_str(cash_effect_amount),
                "amount_policy": result.amount_policy,
                "cash_direction": result.cash_direction,
            }

            if include_debug_cols:
                item.update(
                    {
                        "rule_id": result.rule_id,
                        "reason": result.reason,
                        "raw_account_name": safe_str(row.get("raw_account_name")),
                        "normalized_name": normalize_account_name(row.get("original_account_name")),
                        "indent_level": safe_str(row.get("indent_level")),
                        "has_children": safe_str(row.get("has_children")),
                        "section_context": safe_str(row.get("section_context")),
                        "parent_context": safe_str(row.get("parent_context")),
                        "context_path": safe_str(row.get("context_path")),
                        "context_rule_id": safe_str(row.get("context_rule_id")),
                        "context_reason": safe_str(row.get("context_reason")),
                        "amount_raw": safe_str(row.get("amount_raw")),
                        "unit_factor": safe_str(row.get("unit_factor")),
                    }
                )

            output.append(item)

        return pd.DataFrame(output)

    def _normalize_row_for_matching(self, row: dict[str, Any]) -> dict[str, str]:
        context_parts = [
            row.get("table_title", ""),
            row.get("section_context", ""),
            row.get("parent_context", ""),
            row.get("context_path", ""),
        ]

        raw_amount = amount_to_int(row.get("raw_amount", row.get("amount")))

        return {
            "fs_type": normalize_statement_type(row.get("statement_type")),
            "name": normalize_account_name(row.get("original_account_name")),
            "context": normalize_context(" ".join(map(str, context_parts))),
            "has_children": bool(row.get("has_children", False)),
            "amount_is_zero_or_blank": raw_amount == 0,
        }

    def _match_rule(
        self,
        row: dict[str, str],
        rule: dict[str, Any],
    ) -> bool:
        fs_type = safe_str(rule.get("fs_type", "")).strip()

        if fs_type and fs_type != row["fs_type"]:
            return False

        name = row["name"]
        context = row["context"]

        exact_any = self._normalize_list(rule.get("exact_any", []))
        if exact_any and name not in exact_any:
            return False

        include_all = self._normalize_list(rule.get("include_all", []))
        if include_all and not all(token in name for token in include_all):
            return False

        if not _match_include_any_groups(name, rule, prefix="include_any"):
            return False

        exclude_any = self._normalize_list(rule.get("exclude_any", []))
        if exclude_any and any(token in name for token in exclude_any):
            return False

        context_include_all = self._normalize_list(rule.get("context_include_all", []))
        if context_include_all and not all(token in context for token in context_include_all):
            return False

        if not _match_include_any_groups(context, rule, prefix="context_include_any"):
            return False

        context_exclude_any = self._normalize_list(rule.get("context_exclude_any", []))
        if context_exclude_any and any(token in context for token in context_exclude_any):
            return False
        
        conditions = rule.get("conditions", {}) or {}

        if "has_children" in conditions:
            if bool(conditions["has_children"]) != bool(row.get("has_children")):
                return False

        if "amount_is_zero_or_blank" in conditions:
            expected = bool(conditions["amount_is_zero_or_blank"])
            if expected != bool(row.get("amount_is_zero_or_blank")):
                return False

        return True

    @staticmethod
    def _normalize_list(values: Any) -> list[str]:
        return normalize_values_to_list(values)


def dedupe_duplicate_subtotals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    duplicate_sensitive_ids = {
        "EAOP",
        "NEAOP",
        "TOTAL_EQUITY",
        "TOTAL_ASSETS",
        "TOTAL_LIABILITIES",
        "CURRENT_ASSETS",
        "NON_CURRENT_ASSETS",
        "CURRENT_LIABILITIES",
        "NON_CURRENT_LIABILITIES",
    }

    for cid in duplicate_sensitive_ids:
        mask = df["canonical_account_id"].eq(cid)

        if not mask.any():
            continue

        # 같은 statement/period/canonical/amount가 중복이면 leaf 우선
        sub = df[mask].copy()

        if "has_children" in sub.columns:
            sub["_is_parent"] = sub["has_children"].astype(str).str.lower().eq("true")
        else:
            sub["_is_parent"] = False

        sub["_indent"] = pd.to_numeric(sub.get("indent_level", 0), errors="coerce").fillna(0)

        # leaf 우선, indent 깊은 row 우선
        keep_idx = (
            sub.sort_values(["_is_parent", "_indent"], ascending=[True, False])
            .groupby(["statement_type", "period", "canonical_account_id", "amount"], as_index=False)
            .head(1)
            .index
        )

        dup_idx = sub.index.difference(keep_idx)

        df.loc[dup_idx, "canonical_account_id"] = "UNMAPPED"
        df.loc[dup_idx, "canonical_account_name"] = "미매핑"
        df.loc[dup_idx, "rule_id"] = "post_duplicate_subtotal_unmapped"
        df.loc[dup_idx, "reason"] = f"중복 subtotal 제거: {cid}"

    return df


def validate_mapped_df(
    mapped_df: pd.DataFrame,
    canonical_df: pd.DataFrame,
) -> list[str]:
    errors: list[str] = []
    valid_ids = set(canonical_df["canonical_id"].astype(str))

    for idx, row in mapped_df.iterrows():
        cid = safe_str(row.get("canonical_account_id"))
        name = normalize_account_name(row.get("original_account_name"))
        ctx = normalize_context(
            f"{row.get('section_context', '')} {row.get('parent_context', '')} {row.get('context_path', '')}"
        )

        if cid != "UNMAPPED" and cid not in valid_ids:
            errors.append(f"[{idx}] 알 수 없는 canonical_id: {cid}")

        if name in {"자산", "부채", "자본"} and cid != "UNMAPPED":
            errors.append(f"[{idx}] 섹션 헤더가 매핑됨: {name} -> {cid}")

        if "영업활동에서창출된현금" in name and cid == "CFO":
            errors.append(f"[{idx}] CFO 이전 subtotal이 CFO로 매핑됨")

        if "총포괄" in ctx and cid == "NET_INCOME_PARENT":
            errors.append(f"[{idx}] 총포괄이익 귀속이 NET_INCOME_PARENT로 매핑됨")

        if name in {"부채및자본총계", "부채와자본총계"} and cid != "UNMAPPED":
            errors.append(f"[{idx}] 부채및자본총계가 매핑됨: {cid}")

        if name in {"기초의현금및현금성자산", "기말의현금및현금성자산"} and cid == "CASH_AND_EQUIVALENTS":
            errors.append(f"[{idx}] CF의 기초/기말 현금이 BS 현금 계정으로 매핑됨")

    return errors


def load_canonical_accounts(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str).fillna("")

    required = {
        "canonical_id",
        "canonical_nm",
        "fs_type",
        "is_derived",
        "formula",
        "description",
        "비고",
    }

    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Canonical CSV missing columns: {missing}")

    return df


def normalize_financial_statement_rule_based(
    input_html_path: str | Path,
    company_name: str,
    period: str,
    output_csv_path: str | Path,
    canonical_csv_path: str | Path,
    context_rule_path: str | Path,
    mapping_rule_paths: list[str | Path],
    sign_policy_path: str | Path | None = None,
    save_debug: bool = True,
) -> pd.DataFrame:
    input_rows = extract_rows_from_dart_html(
        html_path=input_html_path,
        company_name=company_name,
        period=period,
    )

    context_engine = ContextEngine.from_yaml(context_rule_path)
    enriched_rows = context_engine.enrich_context(input_rows)

    mapping_engine = RuleEngine.from_files(
        canonical_csv_path=canonical_csv_path,
        rule_paths=mapping_rule_paths,
        sign_policy_path=sign_policy_path,
    )

    mapped_df = mapping_engine.map_rows(
        enriched_rows,
        include_debug_cols=True,
    )

    mapped_df = dedupe_duplicate_subtotals(mapped_df)

    canonical_df = load_canonical_accounts(canonical_csv_path)
    errors = validate_mapped_df(mapped_df, canonical_df)

    if errors:
        print("[WARN] 매핑 검증 경고")
        for error in errors:
            print(" -", error)

    output_path = Path(output_csv_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    statement_order = {"CF": 0, "BS": 1, "IS": 2}

    final_df = (
        mapped_df
        .assign(_order=mapped_df["statement_type"].map(statement_order).fillna(99))
        .sort_values(["_order"], kind="stable")
        .drop(columns=["_order"])
        .loc[:, EXPECTED_HEADER]
    )

    final_df.to_csv(
        output_path,
        index=False,
        encoding="utf-8-sig",
        quoting=csv.QUOTE_ALL,
    )

    print(f"[SAVED] {output_path}")

    if save_debug:
        debug_path = output_path.with_suffix(".debug.csv")
        mapped_df.to_csv(
            debug_path,
            index=False,
            encoding="utf-8-sig",
            quoting=csv.QUOTE_ALL,
        )
        print(f"[SAVED] {debug_path}")

    return final_df
