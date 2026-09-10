import io
from pathlib import Path
from contextlib import redirect_stdout
from tempfile import TemporaryDirectory
import unittest

from engine.transformers._internal.dart_filings import extract_rows_from_dart_html


class DartStatementPeriodTest(unittest.TestCase):
    def test_december_interim_of_march_year_end_uses_ytd(self):
        # Reduced from Woori Investment Bank receipt 20140303000380.
        html = """
        <p class="table-group-1">연 결 포 괄 손 익 계 산 서</p>
        <p>제42기 3분기 2013년 10월 01일부터 2013년 12월 31일까지</p>
        <p>제42기 누적3분기 2013년 04월 01일부터 2013년 12월 31일까지 (단위 : 원)</p>
        <table border="1">
          <tr><th rowspan="2">과목</th><th colspan="2">제42기 3분기</th>
            <th colspan="2">제41기 3분기</th><th rowspan="2">제41기</th></tr>
          <tr><th>3 개 월</th><th>누 적</th><th>3 개 월</th><th>누 적</th></tr>
          <tr><td>영업수익</td><td>20,196,725,677</td><td>97,468,804,831</td>
            <td>54,888,564,731</td><td>163,174,991,372</td><td>207,985,428,287</td></tr>
          <tr><td>당(분)기순이익(손실)</td><td>-25,451,378,457</td><td>-88,428,049,692</td>
            <td>-11,634,349,324</td><td>-26,461,704,643</td><td>-33,692,676,910</td></tr>
        </table>
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "december-interim.html"
            path.write_text(html, encoding="utf-8")
            rows = extract_rows_from_dart_html(path, "010050", "2013.12")
        amounts = {row["original_account_name"]: row["raw_amount"] for row in rows}
        self.assertEqual(amounts["영업수익"], "97468804831")
        self.assertEqual(amounts["당(분)기순이익(손실)"], "-88428049692")

    def test_legacy_packed_statement_cells_are_exploded_into_account_rows(self):
        html = """
        <html><body>
          <p class="table-group-1">연결대차대조표</p>
          <table border="1">
            <tr>
              <td>과 목</td>
              <td colspan="2">제 35 기</td>
              <td colspan="2">제 34 기</td>
            </tr>
            <tr>
              <td>자산<br/>현금및현금등가물<br/>자산총계</td>
              <td><br/>100<br/></td>
              <td>1,000<br/><br/>1,000</td>
              <td><br/>90<br/></td>
              <td>900<br/><br/>900</td>
            </tr>
          </table>
        </body></html>
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "statement.html"
            path.write_text(html, encoding="utf-8")

            rows = extract_rows_from_dart_html(path, "005930", "2004.6")

        amounts = {
            row["original_account_name"]: row["raw_amount"] for row in rows
        }
        self.assertEqual(amounts["자산"], "1000")
        self.assertEqual(amounts["현금및현금등가물"], "100")
        self.assertEqual(amounts["자산총계"], "1000")
        self.assertTrue(all(row["parse_alignment_complete"] for row in rows))

    def test_legacy_packed_statement_marks_incomplete_segment_alignment(self):
        html = """
        <html><body>
          <p class="table-group-1">연결대차대조표</p>
          <table border="1">
            <tr><td>과 목</td><td colspan="2">제 3 기</td></tr>
            <tr>
              <td>자산<br/>유동자산<br/>부채<br/>자본</td>
              <td><br/>60</td>
              <td>100<br/>60<br/>40<br/>20</td>
            </tr>
          </table>
        </body></html>
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "misaligned.html"
            path.write_text(html, encoding="utf-8")
            rows = extract_rows_from_dart_html(path, "020760", "2002.12")

        self.assertTrue(rows)
        self.assertTrue(any(not row["parse_alignment_complete"] for row in rows))

    def test_legacy_packed_subtotal_parentheses_do_not_reverse_the_sign(self):
        html = """
        <html><body>
          <p class="table-group-1">연결대차대조표</p>
          <table border="1">
            <tr>
              <td>과 목</td>
              <td colspan="2">제 35 기</td>
              <td colspan="2">제 34 기</td>
            </tr>
            <tr>
              <td>유동자산<br/>현금및현금등가물<br/>자산총계</td>
              <td><br/>100<br/></td>
              <td>(1,000)<br/><br/>1,000</td>
              <td><br/>90<br/></td>
              <td>(900)<br/><br/>900</td>
            </tr>
          </table>
        </body></html>
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "statement.html"
            path.write_text(html, encoding="utf-8")

            rows = extract_rows_from_dart_html(path, "005930", "2004.6")

        current_assets = next(
            row for row in rows if row["original_account_name"] == "유동자산"
        )
        self.assertEqual(current_assets["raw_amount"], "1000")

    def test_legacy_packed_cash_flow_parentheses_preserve_outflow_direction(self):
        html = """
        <html><body>
          <p class="table-group-1">연결현금흐름표</p>
          <table border="1">
            <tr>
              <td>과 목</td>
              <td colspan="2">제 35 기</td>
              <td colspan="2">제 34 기</td>
            </tr>
            <tr>
              <td>영업활동으로 인한 현금흐름<br/>투자활동으로 인한 현금흐름<br/>재무활동으로 인한 현금흐름</td>
              <td><br/><br/></td>
              <td>100<br/>(40)<br/>(20)</td>
              <td><br/><br/></td>
              <td>90<br/>(30)<br/>(10)</td>
            </tr>
          </table>
        </body></html>
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "cash-flow.html"
            path.write_text(html, encoding="utf-8")

            rows = extract_rows_from_dart_html(path, "005930", "2004.6")

        amounts = {
            row["original_account_name"]: row["raw_amount"] for row in rows
        }
        self.assertEqual(amounts["영업활동으로 인한 현금흐름"], "100")
        self.assertEqual(amounts["투자활동으로 인한 현금흐름"], "-40")
        self.assertEqual(amounts["재무활동으로 인한 현금흐름"], "-20")

    def test_wrapped_account_label_is_not_mistaken_for_a_packed_statement(self):
        html = """
        <html><body>
          <p class="table-group-1">연결손익계산서</p>
          <table border="1">
            <tr>
              <td>과 목</td>
              <td colspan="2">제 35 기 반기</td>
              <td colspan="2">제 34 기 반기</td>
            </tr>
            <tr>
              <td>지배기업의 소유주에게 귀속되는<br/>반기<br/>순이익</td>
              <td>100</td><td>200</td><td>90</td><td>180</td>
            </tr>
          </table>
        </body></html>
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "statement.html"
            path.write_text(html, encoding="utf-8")

            rows = extract_rows_from_dart_html(path, "005930", "2004.6")

        matching = [
            row
            for row in rows
            if "지배기업의소유주에게귀속되는반기순이익"
            in row["normalized_name"]
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["raw_amount"], "200")

    def test_legacy_parenthesized_minus_marker_is_parsed_as_negative(self):
        html = """
        <html><body>
          <p class="table-group-1">손익계산서</p>
          <table border="1">
            <tr><td>과 목</td><td>제 35 기</td><td>제 34 기</td></tr>
            <tr><td>매출총이익</td><td>(-)55,121,175,232</td><td>1</td></tr>
          </table>
        </body></html>
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "statement.html"
            path.write_text(html, encoding="utf-8")

            rows = extract_rows_from_dart_html(path, "020180", "2001.12")

        gross_profit = next(
            row for row in rows if row["original_account_name"] == "매출총이익"
        )
        self.assertEqual(gross_profit["raw_amount"], "-55121175232")

    def test_unencodable_source_text_does_not_abort_parsing_on_cp949_console(self):
        html = """
        <html><body>
          <table class="nb"><tr><td>※ 비교표시된 2002년 12월 31일로 종료되는 회계연도의 연결손익계산서는 기업회계기준서 적용에 따른 회계정책 변경으로 인해\u00a0제36기 반기 재무제표와의 비교를 위하여 재작성되었음.</td></tr></table>
        </body></html>
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "statement.html"
            path.write_text(html, encoding="utf-8")
            raw_console = io.BytesIO()
            cp949_console = io.TextIOWrapper(
                raw_console,
                encoding="cp949",
                errors="strict",
            )
            try:
                with redirect_stdout(cp949_console):
                    rows = extract_rows_from_dart_html(path, "005930", "2004.6")
                cp949_console.flush()
            finally:
                cp949_console.detach()

        self.assertEqual(rows, [])

    def test_interim_income_statement_uses_current_ytd_column(self):
        html = """
        <html><body>
          <p>연결 손익계산서</p>
          <p>제 81 기 반기 2024.01.01 부터 2024.06.30 까지 (단위 : 원)</p>
          <table border="1">
            <tr>
              <td rowspan="2"></td>
              <td colspan="2">제 81 기 반기</td>
              <td colspan="2">제 80 기 반기</td>
            </tr>
            <tr><td>3개월</td><td>누적</td><td>3개월</td><td>누적</td></tr>
            <tr><td>매출액</td><td>27</td><td>53</td><td>26</td><td>49</td></tr>
          </table>
        </body></html>
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "statement.html"
            path.write_text(html, encoding="utf-8")

            rows = extract_rows_from_dart_html(path, "000270", "2024.6")

        revenue = next(row for row in rows if row["original_account_name"] == "매출액")
        self.assertEqual(revenue["raw_amount"], "53")

    def test_annual_income_statement_keeps_current_annual_column(self):
        html = """
        <html><body>
          <p>연결 손익계산서</p>
          <p>제 81 기 2024.01.01 부터 2024.12.31 까지 (단위 : 원)</p>
          <table border="1">
            <tr><td></td><td>제 81 기</td><td>제 80 기</td></tr>
            <tr><td>매출액</td><td>107</td><td>99</td></tr>
          </table>
        </body></html>
        """
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "statement.html"
            path.write_text(html, encoding="utf-8")

            rows = extract_rows_from_dart_html(path, "000270", "2024.12")

        revenue = next(row for row in rows if row["original_account_name"] == "매출액")
        self.assertEqual(revenue["raw_amount"], "107")


if __name__ == "__main__":
    unittest.main()
