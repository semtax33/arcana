from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

from engine.transformers._internal.statement_files import (
    consolidate_statement_debug_snapshots,
    consolidate_statement_snapshots,
)
from engine.workflows._internal.normalize_workflow import remove_legacy_statement_snapshots


BASE_COLUMNS = [
    "canonical_account_id",
    "canonical_account_name",
    "original_account_name",
    "statement_type",
    "period",
    "normalized_amount",
]


class KrNormalizeOutputTest(unittest.TestCase):
    def test_consolidates_snapshot_cache_into_symbol_output_dir(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot_dir = root / "normalized-snapshots"
            output_dir = root / "normalized"
            snapshot_dir.mkdir()

            for month, amount in [(3, 100), (6, 250)]:
                df = pd.DataFrame(
                    [
                        {
                            "canonical_account_id": "REVENUE",
                            "canonical_account_name": "Revenue",
                            "original_account_name": "Revenue",
                            "statement_type": "IS",
                            "period": f"2025.{month:02d}",
                            "normalized_amount": amount,
                        }
                    ]
                )
                df.to_csv(
                    snapshot_dir / f"kr_normalized_005930_2025.{month:02d}.csv",
                    index=False,
                    encoding="utf-8",
                )
                df.assign(reason="mapped").to_csv(
                    snapshot_dir / f"kr_normalized_005930_2025.{month:02d}.debug.csv",
                    index=False,
                    encoding="utf-8",
                )

            normalized_path = consolidate_statement_snapshots(
                "005930",
                snapshot_dir,
                output_dir=output_dir,
                columns=BASE_COLUMNS,
            )
            debug_path = consolidate_statement_debug_snapshots(
                "005930",
                snapshot_dir,
                output_dir=output_dir,
            )

            self.assertEqual(normalized_path, output_dir / "kr_normalized_005930.csv")
            self.assertEqual(debug_path, output_dir / "kr_normalized_005930.debug.csv")
            self.assertFalse((output_dir / "kr_normalized_005930_2025.03.csv").exists())

            normalized = pd.read_csv(normalized_path)
            self.assertEqual(normalized["fiscal_month"].tolist(), [3, 6])
            self.assertEqual(normalized["normalized_amount"].tolist(), [100, 250])

            debug = pd.read_csv(debug_path)
            self.assertEqual(debug["fiscal_month"].tolist(), [3, 6])
            self.assertEqual(debug["reason"].tolist(), ["mapped", "mapped"])

    def test_remove_legacy_statement_snapshots_only_after_consolidated_exists(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            normalized_dir = root / "normalized"
            normalized_dir.mkdir()

            (normalized_dir / "kr_normalized_005930.csv").write_text("", encoding="utf-8")
            legacy = normalized_dir / "kr_normalized_005930_2025.12.csv"
            legacy.write_text("", encoding="utf-8")
            legacy.with_suffix(".debug.csv").write_text("", encoding="utf-8")

            orphan = normalized_dir / "kr_normalized_000660_2025.12.csv"
            orphan.write_text("", encoding="utf-8")

            removed_count = remove_legacy_statement_snapshots(normalized_dir)

            self.assertEqual(removed_count, 2)
            self.assertFalse(legacy.exists())
            self.assertFalse(legacy.with_suffix(".debug.csv").exists())
            self.assertTrue(orphan.exists())


if __name__ == "__main__":
    unittest.main()
