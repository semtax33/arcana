"""Financial issuers' reported profit must not become a segment-note amount."""
from pathlib import Path

import pandas as pd
import pytest

from engine.transformers.filings import normalize_financial_statement_rule_based

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "data-lake/meta/rules/semantic_kr_v7.yaml"


def normalize(tmp_path, body, *, statement="연결포괄손익계산서"):
    source = tmp_path / "source.html"
    source.write_text(f'''<DOCUMENT><COMPANY-NAME AREGCIK="00123456">검증회사</COMPANY-NAME>
      <SECTION-1><TITLE>III. 재무에 관한 사항</TITLE>
      <SECTION-2><TITLE>2. 연결재무제표</TITLE><P>한국채택국제회계기준</P>
      <TABLE BORDER="0"><TR><TD>{statement}</TD></TR>
      <TR><TD>2021년 1월 1일부터 2021년 12월 31일까지 (단위: 원)</TD></TR></TABLE>
      <TABLE BORDER="1"><TR><TH>과목</TH><TH>2021</TH><TH>2020</TH></TR>{body}</TABLE></SECTION-2>
      <SECTION-2><TITLE>3. 연결재무제표 주석</TITLE><P>5. 영업부문</P><P>(단위: 원)</P>
      <TABLE BORDER="1"><TR><TH>계정</TH><TH>당기</TH></TR><TR><TD>당기순이익</TD><TD>200</TD></TR></TABLE>
      </SECTION-2></SECTION-1></DOCUMENT>''', encoding="utf-8")
    comments = tmp_path / "comments.yaml"
    comments.write_text("comment_rules:\n  - id: segment_profit\n    section_name: '*'\n    target_patterns:\n      NET_INCOME: '^당기순이익$'\n", encoding="utf-8")
    target = tmp_path / "statement.csv"
    normalize_financial_statement_rule_based(source, "123456", "2021.12", target,
        canonical_csv_path=ROOT / "data-lake/meta/CanonicalAccount.csv", context_rule_path=BUNDLE,
        mapping_rule_paths=[BUNDLE], sign_policy_path=BUNDLE,
        comment_rule_paths=[comments], save_debug=True, verbose=False)
    return pd.read_csv(target.with_suffix(".debug.csv"), dtype=str).fillna("")


@pytest.mark.parametrize("label", [
    "Ⅷ. 연결당기순이익",
    "연 결 당 기 순 이 익",
    "V. 당기순이익 (대손준비금 반영후 조정이익: 당기 1,389원 전기 1,094원)",
])
def test_reported_consolidated_profit_precedes_segment_notes_and_regulatory_annotation(tmp_path, label):
    frame = normalize(tmp_path, f"<TR><TD>{label}</TD><TD>1530</TD><TD>900</TD></TR>")
    facts = frame.loc[frame.canonical_account_id.eq("NET_INCOME")]
    assert len(facts) == 1
    assert facts.iloc[0].normalized_amount == "1530"
    assert facts.iloc[0].source_type == "FINANCIAL_STATEMENT"


def test_explicit_parent_profit_is_separate_from_total_profit_and_comprehensive_income(tmp_path):
    frame = normalize(tmp_path, '''<TR><TD>연결당기순이익</TD><TD>660</TD><TD>500</TD></TR>
      <TR><TD>1. 지배주주지분당기순이익 (대손준비금 반영후 조정이익: 640원)</TD><TD>659</TD><TD>499</TD></TR>
      <TR><TD>2. 비지배주주지분당기순이익</TD><TD>1</TD><TD>1</TD></TR>
      <TR><TD>대손준비금 반영후 조정이익</TD><TD>640</TD><TD>450</TD></TR>
      <TR><TD>연결당기총포괄이익</TD><TD>120</TD><TD>100</TD></TR>
      <TR><TD>1. 지배주주지분당기총포괄이익</TD><TD>119</TD><TD>99</TD></TR>''')
    parent = frame.loc[frame.canonical_account_id.eq("NET_INCOME_PARENT")]
    assert len(parent) == 1 and parent.iloc[0].normalized_amount == "659"
    total = frame.loc[frame.canonical_account_id.eq("NET_INCOME")]
    assert len(total) == 1 and total.iloc[0].normalized_amount == "660"


def test_parent_attribution_under_current_quarter_heading_retains_net_profit_context(tmp_path):
    frame = normalize(tmp_path, '''<TR><TD>연결당기순이익</TD><TD>660</TD><TD>500</TD></TR>
      <TR><TD>1. 당(분)기순이익의 귀속</TD><TD>660</TD><TD>500</TD></TR>
      <TR><TD>    (1) 지배기업소유주지분</TD><TD>659</TD><TD>499</TD></TR>
      <TR><TD>2. 총포괄이익(손실)의 귀속</TD><TD>120</TD><TD>100</TD></TR>
      <TR><TD>    (1) 지배기업소유주지분</TD><TD>119</TD><TD>99</TD></TR>''')
    parent = frame.loc[frame.canonical_account_id.eq("NET_INCOME_PARENT")]
    assert len(parent) == 1 and parent.iloc[0].normalized_amount == "659"


def test_parent_equity_label_in_ifrs_balance_sheet_is_not_profit(tmp_path):
    frame = normalize(tmp_path, '''<TR><TD>Ⅰ. 지배주주지분</TD><TD>2385</TD><TD>2000</TD></TR>
        <TR><TD>    1. 자본금</TD><TD>2000</TD><TD>1800</TD></TR>
        <TR><TD>    2. 이익잉여금</TD><TD>385</TD><TD>200</TD></TR>''', statement="연결재무상태표")
    parent = frame.loc[frame.canonical_account_id.eq("EAOP")]
    assert len(parent) == 1 and parent.iloc[0].normalized_amount == "2385"
