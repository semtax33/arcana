"""Normalize reported notice scope without inventing issuer delisting events."""
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from engine.extractors.sec_notice_documents import download_sec_notice_documents
from test_sec_notice_documents import DOCUMENT, inventory


def collect(root, document):
    with patch('engine.extractors.sec_notice_documents.urlopen', return_value=BytesIO(document)):
        return download_sec_notice_documents(inventory=inventory(root),
            source_dir=root/'bronze/notices', output_dir=root/'silver/notices')


STRUCTURED = DOCUMENT.replace(b'<xml>Notice retained as submitted</xml>', b'''<XML>
<?xml version="1.0"?>
<notificationOfRemoval>
 <schemaVersion>X0203</schemaVersion>
 <exchange><cik>0000000444</cik><entityName>Example Exchange</entityName></exchange>
 <issuer><cik>0000000123</cik><entityName>Example Issuer</entityName><fileNumber>001-12345</fileNumber></issuer>
 <descriptionClassSecurity>Preferred Stock Purchase Rights</descriptionClassSecurity>
 <ruleProvision>17 CFR 240.12d2-2(a)(4)</ruleProvision>
 <signatureData><signatureDate>2020-01-02</signatureDate></signatureData>
</notificationOfRemoval>
</XML>''')


def test_public_normalization_preserves_rights_class_and_separate_issuer_exchange(tmp_path):
    from engine.transformers.sec_notice_observations import normalize_sec_notice_documents
    collection = collect(tmp_path, STRUCTURED)
    result = normalize_sec_notice_documents(collection=collection, output_dir=tmp_path/'silver/observations')
    rows = pd.read_parquet(result['observations']['path'])
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row.reported_issuer_cik == '0000000123'
    assert row.reported_exchange_cik == '0000000444'
    assert row.reported_security_title == 'Preferred Stock Purchase Rights'
    assert row.reported_signature_date == '2020-01-02'
    assert row.reported_filing_date == '20200103'
    assert row.scope_status == 'reported_security_class_only'
    assert 'effective_date' not in rows.columns
    assert not bool(row.event_verified)
    assert result['registered_identities'] == 0
    assert result['coverage_complete'] is False
    assert Path(row.source_path).is_relative_to(tmp_path/'bronze/notices')


def test_invalid_reported_filing_date_remains_visible_and_requires_review(tmp_path):
    from engine.transformers.sec_notice_observations import normalize_sec_notice_documents
    source = STRUCTURED.replace(b'FILED AS OF DATE: 20200103', b'FILED AS OF DATE: 20200132')
    result = normalize_sec_notice_documents(collection=collect(tmp_path, source),
        output_dir=tmp_path/'silver/observations')
    row = pd.read_parquet(result['observations']['path']).iloc[0]
    assert row.reported_filing_date == '20200132'
    assert row.scope_status == 'requires_review'
    assert 'INVALID_REPORTED_FILING_DATE' in json.loads(row.review_reasons)


@pytest.mark.parametrize('reported,expected_scope', [('123','reported_security_class_only'), ('456','requires_review')])
def test_reported_cik_padding_is_normalized_without_erasing_true_mismatch(tmp_path, reported, expected_scope):
    from engine.transformers.sec_notice_observations import normalize_sec_notice_documents
    source = STRUCTURED.replace(b'<issuer><cik>0000000123</cik>', f'<issuer><cik>{reported}</cik>'.encode())
    result = normalize_sec_notice_documents(collection=collect(tmp_path, source), output_dir=tmp_path/'silver/observations')
    row = pd.read_parquet(result['observations']['path']).iloc[0]
    assert row.reported_issuer_cik == reported
    assert row.scope_status == expected_scope
    assert row.normalized_issuer_cik == reported.zfill(10)


def test_headerless_notice_is_available_for_review_without_inventing_header_facts(tmp_path):
    from engine.transformers.sec_notice_observations import normalize_sec_notice_documents
    source = b'<DOCUMENT>\n<TYPE>25-NSE\n<TEXT>Legacy notice body</TEXT>\n</DOCUMENT>'
    result = normalize_sec_notice_documents(collection=collect(tmp_path, source), output_dir=tmp_path/'silver/observations')
    assert result['counts'].get('unstructured_notice_requires_review') == 1
    assert result['structured_observation_count'] == 0
    outcome = json.loads(Path(result['outcomes']['path']).read_bytes())[0]
    assert 'LEGACY_SOURCE_WITHOUT_HEADER' in outcome['review_reasons']
