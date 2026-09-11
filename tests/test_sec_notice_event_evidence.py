"""Public notice evidence preserves dates, claim status and security scope."""
from pathlib import Path
import json
import pandas as pd

from engine.transformers.sec_notice_observations import normalize_sec_notice_documents
from test_sec_notice_observations import STRUCTURED, collect


EXHIBIT = b'''<DOCUMENT>
<TYPE>EX-99.25
<SEQUENCE>2
<FILENAME>ruleprovisionnotice.htm
<TEXT>
NOTIFICATION OF THE REMOVAL FROM LISTING AND REGISTRATION OF THE STATED SECURITIES
The Example Exchange hereby notifies the SEC of its intention to remove the entire class of the stated securities from listing and registration on the Exchange at the opening of business on January 13, 2020, pursuant to the provisions of Rule 12d2-2 (a).
The removal of the Preferred Stock Purchase Rights of Example Issuer is being effected because the Exchange knows or is reliably informed that on January 1, 2020, all rights pertaining to the entire class of this security were extinguished.
The Exchange also notifies the Securities and Exchange Commission that as a result of the above indicated conditions this security was suspended on January 1, 2020.
</TEXT>
</DOCUMENT>'''


def source_notices(root, exhibit=EXHIBIT):
    document = STRUCTURED.replace(b'</SEC-DOCUMENT>', exhibit + b'\n</SEC-DOCUMENT>')
    collection = collect(root, document)
    return normalize_sec_notice_documents(collection=collection, output_dir=root/'silver/observations')


def test_public_evidence_keeps_proposed_removal_separate_from_reported_suspension(tmp_path):
    from engine.transformers.sec_notice_event_evidence import extract_sec_notice_event_evidence
    result = extract_sec_notice_event_evidence(notices=source_notices(tmp_path), output_dir=tmp_path/'silver/evidence')
    frame = pd.read_parquet(result['evidence']['path']).set_index('claim_kind')
    assert len(frame) == 3
    assert frame.loc['exchange_listing_removal', 'reported_date'] == '2020-01-13'
    assert frame.loc['exchange_listing_removal', 'claim_status'] == 'proposed'
    assert frame.loc['trading_suspension', 'reported_date'] == '2020-01-01'
    assert frame.loc['trading_suspension', 'claim_status'] == 'reported_completed'
    assert frame.loc['security_rights_extinguished', 'reported_date'] == '2020-01-01'
    assert frame.reported_security_title.eq('Preferred Stock Purchase Rights').all()
    assert frame.reported_filing_date.eq('2020-01-03').all()
    assert not frame.event_verified.any()
    assert 'effective_date' not in frame.columns
    assert result['registered_identities'] == 0
    assert result['coverage_complete'] is False
    texts = pd.read_parquet(result['exhibit_texts']['path']).set_index('exhibit_id')
    for row in frame.itertuples():
        text = texts.loc[row.exhibit_id, 'normalized_text']
        assert text[row.text_start:row.text_end] == row.matched_text
        assert Path(row.source_path).is_relative_to(tmp_path/'bronze/notices')


def test_withdrawn_notice_keeps_quoted_dates_but_requires_context_review(tmp_path):
    from engine.transformers.sec_notice_event_evidence import extract_sec_notice_event_evidence
    withdrawn = EXHIBIT.replace(b'<TEXT>', b'<TEXT>This notice is withdrawn.\n')
    result = extract_sec_notice_event_evidence(notices=source_notices(tmp_path,withdrawn), output_dir=tmp_path/'silver/evidence')
    rows = pd.read_parquet(result['evidence']['path'])
    assert len(rows) == 3
    assert rows.claim_status.eq('requires_review').all()
    assert all('WITHDRAWN_OR_REPLACED_NOTICE_CONTEXT' in json.loads(value) for value in rows.review_reasons)


def test_conflicting_removal_dates_are_both_retained_without_reclassifying_suspension(tmp_path):
    from engine.transformers.sec_notice_event_evidence import extract_sec_notice_event_evidence
    sentence = EXHIBIT.split(b'\n')[6]
    assert b'January 13, 2020' in sentence
    conflicting = EXHIBIT.replace(b'</TEXT>', sentence.replace(b'January 13, 2020',b'January 20, 2020') + b'\n</TEXT>')
    result = extract_sec_notice_event_evidence(notices=source_notices(tmp_path,conflicting), output_dir=tmp_path/'silver/evidence')
    rows = pd.read_parquet(result['evidence']['path'])
    removal = rows.loc[rows.claim_kind.eq('exchange_listing_removal')]
    assert set(removal.reported_date) == {'2020-01-13','2020-01-20'}
    assert removal.claim_status.eq('requires_review').all()
    assert all('CONFLICTING_DATES_FOR_CLAIM' in json.loads(value) for value in removal.review_reasons)
    assert rows.loc[rows.claim_kind.eq('trading_suspension'),'claim_status'].tolist() == ['reported_completed']


def test_exchange_suspended_from_trading_wording_keeps_completed_status(tmp_path):
    from engine.transformers.sec_notice_event_evidence import extract_sec_notice_event_evidence
    exhibit = EXHIBIT.replace(b'was suspended on', b'was suspended from trading on')
    result = extract_sec_notice_event_evidence(notices=source_notices(tmp_path,exhibit), output_dir=tmp_path/'silver/evidence')
    rows = pd.read_parquet(result['evidence']['path'])
    suspension = rows.loc[rows.claim_kind.eq('trading_suspension')]
    assert len(suspension) == 1
    assert suspension.iloc[0].claim_status == 'reported_completed'
    assert suspension.iloc[0].reported_date == '2020-01-01'
