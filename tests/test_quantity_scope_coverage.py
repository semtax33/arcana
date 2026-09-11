"""Coverage reports distinguish a disclosed total from an incomplete sum."""
import pandas as pd
import pytest

from engine.normalization_validator import build_factor_snapshot
from engine.us_mapping_coverage_validator import build_mapping_coverage_report
from test_us_filing_account_scope import RULE_PATH


@pytest.mark.parametrize('amounts,total', [
    ({'DNA_CF': 18, 'DEPRECIATION_EXPENSE': 13, 'AMORTIZATION': 5.1}, 18),
    ({'DEPRECIATION_EXPENSE': 13}, None),
    ({'AMORTIZATION': 5}, None),
    ({'DEPRECIATION_EXPENSE': 13, 'AMORTIZATION': 0}, 13),
    ({'DNA_IS': 18, 'DNA_CF': 19}, 18),
])
def test_reports_require_a_total_or_both_components(tmp_path, amounts, total):
    frame = pd.DataFrame([
        dict(canonical_account_id=key, normalized_amount=value, amount_num=value,
             symbol='EXAMPLE', fiscal_year=2025, fiscal_month=12, statement_type='CF')
        for key, value in {'OPERATING_INCOME': 100, **amounts}.items()
    ])
    normalized = tmp_path / 'silver/normalized'
    normalized.mkdir(parents=True)
    frame.to_csv(normalized / 'us_normalized_EXAMPLE.csv', index=False)
    report = build_mapping_coverage_report(normalized, rule_path=RULE_PATH,
        output_dir=tmp_path / 'silver/coverage', required_ids=[])
    readiness = {row['factor_id']: row['coverage_pct'] for row in report.factor_readiness}
    assert readiness['D_AND_A_AVAILABLE'] == (100 if total is not None else 0)
    assert readiness['EBITDA_CALCULATED'] == (100 if total is not None else 0)
    snapshot = build_factor_snapshot(frame, {})
    assert snapshot['depreciation_and_amortization'] == total
    assert snapshot['ebitda_calculated'] == (None if total is None else 100 + total)
