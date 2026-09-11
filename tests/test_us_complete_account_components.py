"""Complete disclosed components produce totals at the public normalization seam."""
import pytest

from test_us_filing_account_scope import normalize_retained_bundle

pytestmark = pytest.mark.integration


def test_sgna_includes_both_selling_and_administrative_expenses(tmp_path):
    frame = normalize_retained_bundle(tmp_path, 'ATVI', '10-K',
        '0000718877-20-000003', 2019).set_index('canonical_account_id')
    # Official 2019 statement: selling/marketing 926m, G&A 732m.
    # SG&A comprises both, independently worked total 1,658m.
    assert frame.loc['SGNA', 'normalized_amount'] == 1658000000
