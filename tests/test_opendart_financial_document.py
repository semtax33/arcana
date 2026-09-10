from pathlib import Path
from hashlib import sha256

import pandas as pd
import pytest

from engine.transformers.filings import (
    extract_rows_from_dart_html,
    extract_rows_from_dart_comment_html,
    normalize_financial_statement_rule_based,
)


def statement(amount):
    return f'''<TABLE-GROUP ACLASS="{{XBRL}}BS_C">
      <TABLE BORDER="0"><TR><TD>재무상태표</TD></TR><TR><TD>(단위: 백만원)</TD></TR></TABLE>
      <TABLE BORDER="1"><TR><TH>과목</TH><TH>2022년</TH><TH>2021년</TH></TR>
      <TR><TD>자산총계</TD><TD>{amount}</TD><TD>99</TD></TR></TABLE>
    </TABLE-GROUP>'''


def document(consolidated=True):
    return f'''<?xml version="1.0" encoding="utf-8"?>
    <DOCUMENT><DOCUMENT-HEADER><DOCUMENT-NAME>사업보고서</DOCUMENT-NAME>
      <COMPANY-NAME AREGCIK="00123456">예제회사</COMPANY-NAME></DOCUMENT-HEADER>
    <SECTION-1><TITLE>III. 재무에 관한 사항</TITLE>
      <SECTION-2><TITLE>2. 연결재무제표</TITLE>{statement(120) if consolidated else '<P>해당사항 없습니다.</P>'}</SECTION-2>
      <SECTION-2><TITLE>3. 연결재무제표 주석</TITLE><P>연결 주석</P><P>(단위: 백만원)</P>{statement(900)}</SECTION-2>
      <SECTION-2><TITLE>4. 재무제표</TITLE>{statement(600)}</SECTION-2>
      <SECTION-2><TITLE>5. 재무제표 주석</TITLE><P>별도 주석</P><P>(단위: 백만원)</P>{statement(950)}</SECTION-2>
    </SECTION-1></DOCUMENT>'''


@pytest.mark.parametrize("encoding", ["utf-8", "cp949"])
@pytest.mark.parametrize("consolidated", [True, False])
def test_full_opendart_document_preserves_korean_and_one_statement_scope(tmp_path, encoding, consolidated):
    path = tmp_path / "source.html"
    original = document(consolidated).encode(encoding)
    path.write_bytes(original)
    rows = extract_rows_from_dart_html(path, "123456", "2022.12")
    assets = [row for row in rows if row["original_account_name"] == "자산총계"]
    assert len(assets) == 1
    assert assets[0]["raw_amount"] == str((120 if consolidated else 600) * 1000000)
    assert path.read_bytes() == original


def test_unreadable_bytes_are_not_silently_dropped(tmp_path):
    path = tmp_path / "source.html"
    path.write_bytes(b'<DOCUMENT><P>\xff</P></DOCUMENT>')
    with pytest.raises(UnicodeError):
        extract_rows_from_dart_html(path, "123456", "2022.12")


@pytest.mark.parametrize("consolidated,expected", [(True, 900_000_000), (False, 950_000_000)])
def test_notes_use_the_same_financial_scope_as_primary_statements(tmp_path, consolidated, expected):
    path = tmp_path / "source.html"
    path.write_bytes(document(consolidated).encode("cp949"))
    rows = extract_rows_from_dart_comment_html(
        path, "123456", "2022.12", section_name="*",
        target_patterns={"TOTAL_ASSETS": r"^자산총계$"},
    )
    assert len(rows) == 1
    assert rows[0]["value"] == expected


def test_explicit_consolidated_non_applicability_with_subject_particle(tmp_path):
    source = tmp_path / "source.html"
    source.write_text(document(False).replace("해당사항 없습니다.", "- 해당사항이 없습니다."), encoding="utf-8")
    rows = extract_rows_from_dart_html(source, "123456", "2022.12")
    assets = [row for row in rows if row["original_account_name"] == "자산총계"]
    assert len(assets) == 1 and assets[0]["raw_amount"] == "600000000"


@pytest.mark.parametrize("separate_note_table", [True, False])
def test_prior_statement_reference_does_not_reclassify_next_income_statement(tmp_path, separate_note_table):
    source = tmp_path / "source.html"
    note = "(*) 연결 재무상태표는 제1116호를 적용하여 작성되었으며 비교표시는 소급재작성되지 않았습니다."
    note_table = f'<table border="0"><tr><td>{note}</td></tr></table>' if separate_note_table else ""
    note_row = "" if separate_note_table else f'<tr><td>{note}</td></tr>'
    source.write_text(f'''<html><body>{note_table}
      <table border="0">{note_row}<tr><td>연 결 포 괄 손 익 계 산 서</td></tr>
      <tr><td>2020년 1월 1일부터 2020년 12월 31일까지 (단위: 원)</td></tr></table>
      <table border="1"><tr><th>과목</th><th>2020</th><th>2019</th></tr>
      <tr><td>당기순이익</td><td>59</td><td>40</td></tr></table></body></html>''', encoding="utf-8")
    rows = extract_rows_from_dart_html(source, "033660", "2020.12")
    assert len(rows) == 1
    assert rows[0]["statement_type"] == "CIS"
    assert rows[0]["raw_amount"] == "59"


@pytest.mark.parametrize("caption,kind", [("연 결 포 괄 손 익 계 산 서", "CIS"), ("연 결 현 금 흐 름 표", "CF")])
def test_separate_note_span_does_not_hide_a_direct_statement_caption(tmp_path, caption, kind):
    source = tmp_path / "source.html"
    source.write_text(f'''<html><body>
      <p><span>주) 주주총회 승인 전 재무정보이며 수정 시 정정보고서를 제출합니다.</span>{caption}</p>
      <table border="0"><tr><td>2022년 1월 1일부터 2022년 12월 31일까지 (단위: 원)</td></tr></table>
      <table border="1"><tr><th>과목</th><th>2022</th><th>2021</th></tr>
      <tr><td>당기순이익</td><td>90</td><td>80</td></tr></table></body></html>''', encoding="utf-8")
    rows = extract_rows_from_dart_html(source, "010050", "2022.12")
    assert len(rows) == 1 and rows[0]["statement_type"] == kind
    assert rows[0]["raw_amount"] == "90"


def test_normalized_debug_output_retains_original_document_and_section_evidence(tmp_path):
    source = tmp_path / "source.html"
    original = document().encode("cp949")
    source.write_bytes(original)
    root = Path(__file__).resolve().parents[1]
    rule = root / "data-lake/meta/rules/semantic_kr_current.yaml"
    target = tmp_path / "statement.csv"
    normalize_financial_statement_rule_based(
        source, "123456", "2022.12", target,
        canonical_csv_path=root / "data-lake/meta/CanonicalAccount.csv",
        context_rule_path=rule, mapping_rule_paths=[rule], sign_policy_path=rule,
        save_debug=True, verbose=False,
    )
    frame = pd.read_csv(target.with_suffix(".debug.csv"), dtype=str)
    assets = frame.loc[frame.canonical_account_id == "TOTAL_ASSETS"].iloc[0]
    assert assets.normalized_amount == "120000000"
    assert assets.source_document_sha256 == sha256(original).hexdigest()
    assert assets.source_document_encoding == "cp949"
    assert assets.source_section == "2. 연결재무제표"
    assert assets.source_financial_scope == "CFS"


def test_statement_amounts_skip_note_references_and_use_current_detail_or_total(tmp_path):
    # DART 002550 / 20160330004139: the note references are not amounts.
    source = tmp_path / "source.html"
    source.write_text('''<html><body>
      <p class="table-group-1">연결재무상태표</p>
      <table border="1"><thead><tr><th>과목</th><th>주석</th>
        <th colspan="2">제58(당)기</th><th colspan="2">제57(전)기</th></tr></thead>
      <tbody>
        <tr><td>I. 현금및현금성자산</td><td>4,5,8,23,32</td>
          <td></td><td>769,207,926,792</td><td></td><td>566,224,007,639</td></tr>
        <tr><td>1. 당기손익인식금융자산</td><td>4,5,9,14,32</td>
          <td>979,945,507,137</td><td></td><td>1,521,499,395,198</td><td></td></tr>
      </tbody></table></body></html>''', encoding="utf-8")
    rows = extract_rows_from_dart_html(source, "002550", "2015.12")
    values = {row["original_account_name"]: row["raw_amount"] for row in rows}
    assert values["I. 현금및현금성자산"] == "769207926792"
    assert values["1. 당기손익인식금융자산"] == "979945507137"


def test_raw_dart_preserves_space_indentation_for_parent_and_child_accounts(tmp_path):
    source = tmp_path / "source.html"
    markup = document().replace(
        '<TR><TD>자산총계</TD><TD>120</TD><TD>99</TD></TR>',
        '<TR><TD>Ⅱ. 금융자산</TD><TD>120</TD><TD>99</TD></TR>'
        '<TR><TD>    2. 당기손익인식금융자산</TD><TD>20</TD><TD>9</TD></TR>',
    )
    source.write_bytes(markup.encode("cp949"))
    rows = extract_rows_from_dart_html(source, "123456", "2022.12")
    parent = next(r for r in rows if r["original_account_name"] == "Ⅱ. 금융자산")
    child = next(r for r in rows if r["original_account_name"] == "2. 당기손익인식금융자산")
    assert child["indent_level"] > parent["indent_level"]


def test_normalized_note_fact_retains_its_own_selected_section_evidence(tmp_path):
    source = tmp_path / "source.html"
    markup = document().replace(statement(900), statement(900).replace("자산총계", "이자비용"))
    original = markup.encode("cp949")
    source.write_bytes(original)
    root = Path(__file__).resolve().parents[1]
    rule = root / "data-lake/meta/rules/semantic_kr_current.yaml"
    comments = tmp_path / "comments.yaml"
    comments.write_text('''comment_rules:
  - id: interest_expense
    section_name: '*'
    target_patterns:
      INTEREST_EXPENSE: '^이자비용$'
''', encoding="utf-8")
    target = tmp_path / "statement.csv"
    normalize_financial_statement_rule_based(
        source, "123456", "2022.12", target,
        canonical_csv_path=root / "data-lake/meta/CanonicalAccount.csv",
        context_rule_path=rule, mapping_rule_paths=[rule], sign_policy_path=rule,
        comment_rule_paths=[comments], comment_html_path=source, save_debug=True, verbose=False,
    )
    frame = pd.read_csv(target.with_suffix(".debug.csv"), dtype=str)
    interest = frame.loc[frame.canonical_account_id == "INTEREST_EXPENSE"].iloc[0]
    assert interest.normalized_amount == "900000000"
    assert interest.source_document_sha256 == sha256(original).hexdigest()
    assert interest.source_section == "3. 연결재무제표 주석"
    assert interest.source_financial_scope == "CFS"


def test_concatenated_opendart_amounts_are_rejected_instead_of_becoming_one_large_fact(tmp_path):
    # The retained 20230331004390 ZIP itself lost line breaks between three
    # amounts; only the same-receipt viewer retains their separate rows.
    source = tmp_path / "source.html"
    source.write_bytes(document().replace(
        '<TD>120</TD>', '<TD>854,832,667,063838,355,327,508835,748,369,481</TD>'
    ).encode("cp949"))
    with pytest.raises(ValueError, match="thousands grouping"):
        extract_rows_from_dart_html(source, "000060", "2022.12")


def test_opendart_editable_table_cells_are_read_as_financial_cells(tmp_path):
    # 003410 / 20240327001183 stores the actual table body in TE cells.
    source = tmp_path / "source.html"
    markup = document().replace("<TD>", "<TE AUPDATECONT=\"N\">").replace("</TD>", "</TE>")
    source.write_text(markup, encoding="utf-8")
    rows = extract_rows_from_dart_html(source, "003410", "2023.12")
    assets = [row for row in rows if row["original_account_name"] == "자산총계"]
    assert len(assets) == 1
    assert assets[0]["raw_amount"] == "120000000"


def test_detail_amount_is_not_ambiguous_with_an_unused_subtotal_dash(tmp_path):
    source = tmp_path / "source.html"
    source.write_text('''<html><body>
      <p class="table-group-1">재무상태표</p>
      <table border="1"><tr><th>과목</th><th>주석</th>
        <th colspan="2">제28기말</th><th colspan="2">제27기말</th></tr>
      <tr><td>현금 및 현금성자산</td><td>7, 39</td>
        <td>126,938,434,104</td><td>-</td><td>246,561,443,413</td><td>-</td></tr>
      </table></body></html>''', encoding="utf-8")
    rows = extract_rows_from_dart_html(source, "021960", "2016.12")
    cash = next(row for row in rows if row["original_account_name"] == "현금 및 현금성자산")
    assert cash["raw_amount"] == "126938434104"


@pytest.mark.parametrize("label,canonical,displayed,expected", [
    ("기본주당이익(손실) (단위 : 원)", "BASIC_EPS", "(290.6)", "-290.6"),
    ("희석주당이익(손실) (단위 : 원)", "DILUTED_EPS", "34.3", "34.3"),
])
def test_eps_fraction_survives_public_normalization_without_statement_unit_scaling(tmp_path, label, canonical, displayed, expected):
    source = tmp_path / "source.html"
    source.write_text(document().replace("재무상태표", "포괄손익계산서")
                      .replace("자산총계", label).replace("<TD>120</TD>", f"<TD>{displayed}</TD>"), encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    rule = root / "data-lake/meta/rules/semantic_kr_current.yaml"
    target = tmp_path / "statement.csv"
    frame = normalize_financial_statement_rule_based(
        source, "035480", "2018.12", target,
        canonical_csv_path=root / "data-lake/meta/CanonicalAccount.csv",
        context_rule_path=rule, mapping_rule_paths=[rule], sign_policy_path=rule,
        save_debug=True, verbose=False,
    )
    row = frame.loc[frame.canonical_account_id.eq(canonical)].iloc[0]
    assert row.raw_amount == expected
    assert row.normalized_amount == expected
    debug = pd.read_csv(target.with_suffix(".debug.csv"), dtype=str)
    assert debug.loc[debug.canonical_account_id.eq(canonical), "unit_factor"].iloc[0] == "1"


def legacy_statement(amount, *, consolidated):
    return statement(amount).replace("{XBRL}BS_C", "{XBRL}BS" if consolidated else "{XBRL}BS_S").replace(
        "재무상태표", "연결 재무상태표" if consolidated else "재무상태표")


@pytest.mark.parametrize("layout", ["xbrl_library", "manual"])
@pytest.mark.parametrize("consolidated", [True, False])
def test_legacy_xi_statements_and_notes_keep_explicit_financial_scope(tmp_path, layout, consolidated):
    # Retained 035480 / 20141114000379 and 000030 / 20141114001132
    # put primary statements under XI, before other disclosure tables.
    connected = legacy_statement(120, consolidated=True)
    separate = legacy_statement(600, consolidated=False)
    connected_notes = '<P>연결재무제표에 대한 주석&cr;제17기</P>' + statement(900)
    separate_notes = '<P>재무제표에 대한 주석&cr;제17기</P>' + statement(950)
    if layout == "xbrl_library":
        body = ('<INSERTION><LIBRARY><FILENAME>report.ixd</FILENAME>'
                + (connected + connected_notes if consolidated else "")
                + separate + separate_notes + '</LIBRARY></INSERTION>')
    else:
        # Legacy editable tables do not have TABLE-GROUP containers.
        def ungroup(value):
            import re
            return re.sub(r'</?TABLE-GROUP[^>]*>', '', value)
        body = (("<P>1. 연결재무제표&cr;가. 연결재무상태표</P>" + ungroup(connected)
                 + connected_notes.replace('<P>', '<P>마. ', 1) if consolidated else "")
                + '<P>2. 재무제표&cr;가. 재무상태표</P>' + ungroup(separate)
                + separate_notes.replace('<P>', '<P>마. ', 1))
    source = tmp_path / "source.html"
    markup = ('<DOCUMENT><SECTION-1><TITLE>III. 재무에 관한 사항</TITLE>' + statement(1)
              + '</SECTION-1><SECTION-1><TITLE>XI. 재무제표 등</TITLE>' + body
              + '<P>3. 대손충당금 설정현황</P>' + statement(9999)
              + '</SECTION-1></DOCUMENT>')
    original = markup.encode("cp949")
    source.write_bytes(original)
    rows = extract_rows_from_dart_html(source, "123456", "2014.09")
    assert [r["raw_amount"] for r in rows if r["original_account_name"] == "자산총계"] == [
        str((120 if consolidated else 600) * 1_000_000)]
    notes = extract_rows_from_dart_comment_html(
        source, "123456", "2014.09", section_name="*", target_patterns={"TOTAL_ASSETS": "^자산총계$"})
    assert [r["value"] for r in notes] == [(900 if consolidated else 950) * 1_000_000]
    assert source.read_bytes() == original


def test_legacy_declared_consolidated_scope_does_not_silently_fall_back_to_separate(tmp_path):
    source = tmp_path / "source.html"
    source.write_text('<DOCUMENT><SECTION-1><TITLE>XI. 재무제표 등</TITLE>'
                      '<P>1. 연결재무제표</P><P>연결자료 검토 중</P>'
                      '<P>2. 재무제표</P>' + legacy_statement(600, consolidated=False)
                      + '</SECTION-1></DOCUMENT>', encoding="utf-8")
    with pytest.raises(ValueError, match="consolidated section has no table"):
        extract_rows_from_dart_html(source, "123456", "2014.09")


@pytest.mark.parametrize("alternate,aligned", [("", True), ("   ", True), ("9", False)])
def test_packed_current_values_align_when_only_the_alternate_column_is_empty(tmp_path, alternate, aligned):
    source = tmp_path / "source.html"
    source.write_text(f'''<html><body><p class="table-group-1">연결재무상태표</p>
      <table border="1"><tr><th>과목</th><th colspan="2">당기말</th><th colspan="2">전기말</th></tr>
      <tr><td>이익잉여금<br/>대손준비금 적립액<br/>대손준비금 전입필요액</td>
      <td>1,234<br/>15<br/>20</td><td>{alternate}</td><td>900<br/>10<br/>5</td><td></td></tr>
      </table></body></html>''', encoding="utf-8")
    rows = extract_rows_from_dart_html(source, "000030", "2018.12")
    retained = next(row for row in rows if row["original_account_name"] == "이익잉여금")
    assert retained["raw_amount"] == "1234"
    assert retained["parse_alignment_complete"] is aligned


@pytest.mark.parametrize("displayed,expected", [("((-)447,926,078)", "-447926078"), ("해당사항 없음", None)])
def test_reported_negative_is_preserved_and_non_amount_is_not_a_zero_fact(tmp_path, displayed, expected):
    source = tmp_path / "source.html"
    source.write_text(document().replace("재무상태표", "손익계산서").replace("자산총계", "매출액")
                      .replace("백만원", "원").replace("<TD>120</TD>", f"<TD>{displayed}</TD>"), encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    rule = root / "data-lake/meta/rules/semantic_kr_current.yaml"
    frame = normalize_financial_statement_rule_based(
        source, "008560", "2006.12", tmp_path / "statement.csv",
        canonical_csv_path=root / "data-lake/meta/CanonicalAccount.csv", context_rule_path=rule,
        mapping_rule_paths=[rule], sign_policy_path=rule, save_debug=True, verbose=False)
    revenue = frame.loc[frame.canonical_account_id.eq("REVENUE")]
    if expected is None:
        assert revenue.empty or revenue.normalized_amount.eq("").all()
    else:
        assert revenue.raw_amount.tolist() == [expected]


@pytest.mark.parametrize("manual_consolidated", [False, True])
def test_legacy_xbrl_title_and_following_manual_consolidated_statements(tmp_path, manual_consolidated):
    # 035480 / 20110330000471 has a TITLE outside the period table.
    # 003410 / 20120629000744 additionally has manual CFS after its OFS library.
    separate = legacy_statement(600, consolidated=False).replace("{XBRL}BS_S", "{XBRL}BS")
    separate = separate.replace('<TABLE BORDER="0"><TR><TD>재무상태표</TD></TR>',
                                '<TITLE>재무상태표(대차대조표)</TITLE><TABLE BORDER="0">')
    body = '<P>1. 재무제표</P><INSERTION><LIBRARY>' + separate + '</LIBRARY></INSERTION>'
    if manual_consolidated:
        import re
        connected = re.sub(r'</?TABLE-GROUP[^>]*>', '', legacy_statement(120, consolidated=True))
        body += '<P>2. 연결재무제표</P>' + connected
    source = tmp_path / 'source.html'
    source.write_text('<DOCUMENT><SECTION-1><TITLE>XI. 재무제표 등</TITLE>' + body
                      + '</SECTION-1></DOCUMENT>', encoding='utf-8')
    rows = extract_rows_from_dart_html(source, '035480', '2010.12')
    assert [r['raw_amount'] for r in rows if r['original_account_name'] == '자산총계'] == [
        str((120 if manual_consolidated else 600) * 1_000_000)]


def test_unqualified_notes_under_explicit_consolidated_heading_keep_that_scope(tmp_path):
    import re
    body = ('<P>가. 연결재무제표</P>' + re.sub(r'</?TABLE-GROUP[^>]*>', '', legacy_statement(120, consolidated=True))
            + '<P>나. 재무제표에 대한 주석</P>' + statement(900)
            + '<P>2. 재무제표에 관한 사항<br/>가. 재무제표</P>'
            + re.sub(r'</?TABLE-GROUP[^>]*>', '', legacy_statement(600, consolidated=False))
            + '<P>나. 재무제표에 대한 주석</P>' + statement(950))
    source = tmp_path / 'source.html'
    source.write_text('<DOCUMENT><SECTION-1><TITLE>XI. 재무제표 등</TITLE>' + body
                      + '</SECTION-1></DOCUMENT>', encoding='utf-8')
    rows = extract_rows_from_dart_html(source, '010050', '2014.09')
    assert [r['raw_amount'] for r in rows if r['original_account_name'] == '자산총계'] == ['120000000']
    notes = extract_rows_from_dart_comment_html(source, '010050', '2014.09', section_name='*',
                                               target_patterns={'TOTAL_ASSETS': '^자산총계$'})
    assert [r['value'] for r in notes] == [900000000]


def test_external_notes_follow_the_explicit_scope_after_a_two_scope_xbrl_library(tmp_path):
    # 035480 / 20140327000248 keeps both primary scopes in a library, with
    # their separately captioned notes following outside the insertion.
    body = ('<INSERTION><LIBRARY>' + legacy_statement(120, consolidated=True)
            + legacy_statement(600, consolidated=False) + '</LIBRARY></INSERTION>'
            + '<P>연결재무제표에 대한 주석</P>' + statement(900)
            + '<P>재무제표에 대한 주석</P>' + statement(950))
    source = tmp_path / 'source.html'
    source.write_text('<DOCUMENT><SECTION-1><TITLE>XI. 재무제표 등</TITLE>' + body
                      + '</SECTION-1></DOCUMENT>', encoding='utf-8')
    rows = extract_rows_from_dart_html(source, '035480', '2013.12')
    assert [r['raw_amount'] for r in rows if r['original_account_name'] == '자산총계'] == ['120000000']
    notes = extract_rows_from_dart_comment_html(source, '035480', '2013.12', section_name='*',
                                               target_patterns={'TOTAL_ASSETS': '^자산총계$'})
    assert [r['value'] for r in notes] == [900000000]
