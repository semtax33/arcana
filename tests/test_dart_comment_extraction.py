import unittest

from bs4 import BeautifulSoup

from engine.transformers._internal.dart_filings import (
    extract_comment_hits_by_rule,
    extract_rows_from_dart_comment_soup,
)


class DartCommentExtractionTest(unittest.TestCase):
    def test_extracts_rnd_from_relaxed_sgna_table_with_unit(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <div>21. 판매비와 관리비</div>
              <p>(단위 : 천원)</p>
              <table>
                <tr><th>계정과목</th><th>당기</th><th>전기</th></tr>
                <tr><td>임상시험비</td><td>1,234</td><td>900</td></tr>
              </table>
            </body></html>
            """,
            "lxml",
        )

        rows = extract_rows_from_dart_comment_soup(
            soup,
            "판매비와관리비",
            {"RND": [r"^임상시험비$"]},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "RND")
        self.assertEqual(rows[0]["value"], 1_234_000)
        self.assertEqual(rows[0]["unit"], "1000")

    def test_low_confidence_generic_development_asset_is_not_rnd(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <p>15. 무형자산</p>
              <table border="1">
                <tr><th>구분</th><th>기말 장부금액</th></tr>
                <tr><td>개발비</td><td>5,000</td></tr>
              </table>
            </body></html>
            """,
            "lxml",
        )
        rules = [
            {
                "id": "canonical_note_all_sections_common",
                "section_name": "*",
                "priority": 900,
                "confidence": "low",
                "target_patterns": {"RND": [r"^개발비$"]},
            }
        ]

        hits = extract_comment_hits_by_rule(soup, rules)

        self.assertEqual(hits, {})

    def test_trade_receivables_balance_note_is_allowed(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <p>7. 매출채권 및 기타채권</p>
              <p>(단위 : 백만원)</p>
              <table>
                <tr><th>구분</th><th>당기말</th><th>전기말</th></tr>
                <tr><td>매출채권</td><td>2,500</td><td>2,100</td></tr>
              </table>
            </body></html>
            """,
            "lxml",
        )
        rules = [
            {
                "id": "trade_receivables_note_balance",
                "section_name": "매출채권",
                "priority": 180,
                "confidence": "high",
                "target_patterns": {"TRADE_RECEIVABLES": [r"^매출채권$"]},
            }
        ]

        hits = extract_comment_hits_by_rule(soup, rules)
        row = hits[id(rules[0])][0]

        self.assertEqual(row["key"], "TRADE_RECEIVABLES")
        self.assertEqual(row["value"], 2_500_000_000)

    def test_trade_and_other_receivables_combined_label_is_not_pure_receivables(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <p>7. 매출채권 및 기타채권</p>
              <table>
                <tr><th>구분</th><th>당기말</th></tr>
                <tr><td>매출채권및기타채권</td><td>900</td></tr>
              </table>
            </body></html>
            """,
            "lxml",
        )
        rules = [
            {
                "id": "canonical_note_all_sections_common",
                "section_name": "*",
                "priority": 900,
                "confidence": "low",
                "target_patterns": {
                    "TRADE_RECEIVABLES": [r"^매출채권$"],
                    "TRADE_AND_OTHER_RECEIVABLES": [r"^매출채권및기타채권$"],
                },
            }
        ]

        hits = extract_comment_hits_by_rule(soup, rules)
        keys = {row["key"] for row in hits[id(rules[0])]}

        self.assertEqual(keys, {"TRADE_AND_OTHER_RECEIVABLES"})

    def test_trade_receivables_cash_flow_change_is_rejected(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <p>25. 영업활동 현금흐름</p>
              <table>
                <tr><th>구분</th><th>당기</th></tr>
                <tr><td>매출채권</td><td>(300)</td></tr>
              </table>
            </body></html>
            """,
            "lxml",
        )
        rules = [
            {
                "id": "cash_flow_note_common",
                "section_name": "현금흐름",
                "priority": 100,
                "confidence": "high",
                "target_patterns": {"TRADE_RECEIVABLES": [r"^매출채권$"]},
            }
        ]

        hits = extract_comment_hits_by_rule(soup, rules)

        self.assertEqual(hits, {})

    def test_inventory_total_in_inventory_note_is_allowed(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <p>8. 재고자산</p>
              <table>
                <tr><th>구분</th><th>당기말</th><th>전기말</th></tr>
                <tr><td>상품</td><td>100</td><td>90</td></tr>
                <tr><td>제품</td><td>200</td><td>180</td></tr>
                <tr><td>합계</td><td>300</td><td>270</td></tr>
              </table>
            </body></html>
            """,
            "lxml",
        )
        rules = [
            {
                "id": "inventories_note_balance",
                "section_name": "재고자산",
                "priority": 180,
                "confidence": "high",
                "target_patterns": {"INVENTORIES": [r"^합계$"]},
            }
        ]

        hits = extract_comment_hits_by_rule(soup, rules)
        row = hits[id(rules[0])][0]

        self.assertEqual(row["key"], "INVENTORIES")
        self.assertEqual(row["value"], 300)

    def test_sgna_note_uses_only_total_row(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <p>20. 판매비와관리비</p>
              <table>
                <tr><th>구분</th><th>당기</th></tr>
                <tr><td>급여</td><td>100</td></tr>
                <tr><td>합계</td><td>700</td></tr>
              </table>
            </body></html>
            """,
            "lxml",
        )
        rules = [
            {
                "id": "sgna_note_amortization",
                "section_name": "판매비와관리비",
                "priority": 100,
                "confidence": "high",
                "target_patterns": {"SGNA": [r"^급여$", r"^합계$"]},
            }
        ]

        hits = extract_comment_hits_by_rule(soup, rules)

        self.assertEqual(len(hits[id(rules[0])]), 1)
        self.assertEqual(hits[id(rules[0])][0]["label"], "합계")
        self.assertEqual(hits[id(rules[0])][0]["value"], 700)

    def test_amortization_note_accepts_expense_not_accumulated_balance(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <p>12. 무형자산</p>
              <table>
                <tr><th>구분</th><th>당기</th></tr>
                <tr><td>상각비</td><td>77</td></tr>
                <tr><td>상각누계액</td><td>500</td></tr>
              </table>
            </body></html>
            """,
            "lxml",
        )
        rules = [
            {
                "id": "intangible_assets_note_common",
                "section_name": "무형자산",
                "priority": 300,
                "confidence": "medium",
                "target_patterns": {
                    "AMORTIZATION": [r"^상각비$", r"^상각누계액$"],
                },
            }
        ]

        hits = extract_comment_hits_by_rule(soup, rules)

        self.assertEqual(len(hits[id(rules[0])]), 1)
        self.assertEqual(hits[id(rules[0])][0]["label"], "상각비")


if __name__ == "__main__":
    unittest.main()
