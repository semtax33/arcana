from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engine.transformers._internal.dart_filings import extract_rows_from_dart_html


class DartStatementPeriodTest(unittest.TestCase):
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
