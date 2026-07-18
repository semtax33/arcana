from pathlib import Path
import unittest

from engine.transformers._internal.dart_filings import RuleEngine


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PATH = PROJECT_ROOT / "data-lake" / "meta" / "CanonicalAccount.csv"
KR_MAPPING_PATH = PROJECT_ROOT / "data-lake" / "meta" / "rules" / "kr_mapping.yaml"
SIGN_POLICY_PATH = PROJECT_ROOT / "data-lake" / "meta" / "rules" / "sign_policy_common.yaml"


class KrMappingRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = RuleEngine.from_files(
            canonical_csv_path=CANONICAL_PATH,
            rule_paths=[KR_MAPPING_PATH],
            sign_policy_path=SIGN_POLICY_PATH,
        )

    def test_parent_net_income_name_variants_map_without_context(self):
        names = [
            "지배기업지분순이익(손실)",
            "지배회사지분순이익(손실)",
            "지배기업소유주지분순이익(손실)",
            "지배기업의 소유주 지분 순이익(손실)",
        ]

        for name in names:
            with self.subTest(name=name):
                result = self.engine.map_row(
                    {
                        "original_account_name": name,
                        "statement_type": "IS",
                        "raw_amount": 329_905_103_957,
                    }
                )

                self.assertEqual(result.canonical_account_id, "NET_INCOME_PARENT")
                self.assertEqual(result.rule_id, "is_net_income_parent_explicit")

    def test_zero_parent_net_income_detail_header_remains_unmapped(self):
        result = self.engine.map_row(
            {
                "original_account_name": "지배기업지분순이익(손실)",
                "statement_type": "IS",
                "raw_amount": 0,
                "has_children": True,
                "section_context": "당기순이익의 귀속",
            }
        )

        self.assertEqual(result.canonical_account_id, "UNMAPPED")
        self.assertEqual(result.rule_id, "is_net_income_parent_zero_detail_header_unmapped")


if __name__ == "__main__":
    unittest.main()
