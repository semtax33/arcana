from __future__ import annotations

import json

import pandas as pd
import pytest

from engine.core.paths import statement_symbol_name
from scripts.verify_kr_period_column_migration import verify_migration


def _write_fixture(tmp_path):
    audit_path = tmp_path / "audit.csv"
    status_path = tmp_path / "status.json"
    output_path = tmp_path / "validation.csv"
    normalized_root = tmp_path / "normalized"
    normalized_root.mkdir()

    pd.DataFrame(
        [
            {
                "symbol": "000020",
                "affected": True,
                "period": "2025.9",
                "statement_type": "CIS",
                "account_name": "매출액",
                "corrected_ytd_amount": "372698029425",
            }
        ]
    ).to_csv(audit_path, index=False)
    pd.DataFrame(
        [
            {
                "period": "2025.9",
                "statement_type": "IS",
                "original_account_name": "매출액",
                "raw_amount": "372698029425",
            }
        ]
    ).to_csv(
        normalized_root / statement_symbol_name("000020", market="kr"),
        index=False,
    )
    return audit_path, status_path, output_path, normalized_root


def test_verify_migration_matches_audited_ytd_amount(tmp_path):
    audit_path, status_path, output_path, normalized_root = _write_fixture(tmp_path)
    status_path.write_text(
        json.dumps({"completed_symbols": ["000020"], "errors": {}}),
        encoding="utf-8",
    )

    result = verify_migration(
        audit_path=audit_path,
        status_path=status_path,
        output_path=output_path,
        normalized_root=normalized_root,
        workers=1,
    )

    assert result.loc[0, "stored_match"]
    assert result.loc[0, "matched_rows"] == 1
    assert output_path.is_file()


def test_verify_migration_rejects_incomplete_checkpoint(tmp_path):
    audit_path, status_path, output_path, normalized_root = _write_fixture(tmp_path)
    status_path.write_text(
        json.dumps({"completed_symbols": [], "errors": {}}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="migration is incomplete"):
        verify_migration(
            audit_path=audit_path,
            status_path=status_path,
            output_path=output_path,
            normalized_root=normalized_root,
            workers=1,
        )


def test_verify_migration_accepts_proven_row_unit_repair(tmp_path):
    audit_path = tmp_path / "audit.csv"
    status_path = tmp_path / "status.json"
    output_path = tmp_path / "validation.csv"
    normalized_root = tmp_path / "normalized"
    snapshot_root = tmp_path / "normalized-snapshots"
    normalized_root.mkdir()
    snapshot_root.mkdir()
    pd.DataFrame(
        [
            {
                "symbol": "003610",
                "affected": True,
                "period": "2025.6",
                "statement_type": "CIS",
                "account_name": "Ⅰ.수익(매출액)",
                "corrected_ytd_amount": "88332467554000000",
            }
        ]
    ).to_csv(audit_path, index=False)
    status_path.write_text(
        json.dumps({"completed_symbols": ["003610"], "errors": {}}),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "period": "2025.6",
                "statement_type": "IS",
                "original_account_name": "Ⅰ.수익(매출액)",
                "raw_amount": "88332467554",
            }
        ]
    ).to_csv(
        normalized_root / statement_symbol_name("003610", market="kr"),
        index=False,
    )
    pd.DataFrame(
        [
            {
                "period": "2025.6",
                "statement_type": "IS",
                "original_account_name": "Ⅰ.수익(매출액)",
                "raw_amount": "88332467554",
                "amount_raw": "88,332,467,554",
                "unit_factor": "1",
                "reason": "row unit-scale repaired: divided by 1000000",
            }
        ]
    ).to_csv(snapshot_root / "kr_normalized_003610_2025.06.debug.csv", index=False)

    result = verify_migration(
        audit_path=audit_path,
        status_path=status_path,
        output_path=output_path,
        normalized_root=normalized_root,
        snapshot_root=snapshot_root,
        workers=1,
    )

    assert result.loc[0, "stored_match"]
    assert result.loc[0, "match_type"] == "row_unit_scale_repair"
