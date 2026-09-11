"""Extract separately typed, source-anchored claims from Form 25 exhibits."""
from collections import Counter
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import re

from bs4 import BeautifulSoup
import pandas as pd

from engine.core.source_storage import sha256_file
from engine.core.serving_storage import export_frame, export_json


_MONTHS = 'January February March April May June July August September October November December'.split()
_DATE = r'(?P<date>(?:' + '|'.join(_MONTHS) + r')\s+[0-9]{1,2},\s+[0-9]{4})'
_RULES = [
    ('exchange_listing_removal', 'proposed', 'opening_of_business',
     r'\bhereby notifies the (?:SEC|Securities and Exchange Commission) of its intention to remove\b.{0,600}?\bat the opening of business on ' + _DATE),
    ('trading_suspension', 'reported_completed', '',
     r'\bThe Exchange also notifies the Securities and Exchange Commission that as a result of the above indicated conditions this security was suspended (?:from trading )?on ' + _DATE),
    ('security_rights_extinguished', 'reported_completed', '',
     r'\bThe removal of the .{1,350}? is being effected because the Exchange knows or is reliably informed that on ' + _DATE +
     r',? all rights pertaining to the entire class of this security were extinguished\b'),
]


def extract_sec_notice_event_evidence(*, notices, output_dir):
    artifact = notices['observations']
    if sha256_file(artifact['path']) != artifact['sha256']:
        raise ValueError('SEC notice scope input integrity mismatch')
    frame = pd.read_parquet(artifact['path'])
    texts, evidence, outcomes, pins = [], [], [], {}
    groups = frame.groupby('source_path', sort=True) if not frame.empty else []
    for source_path, scopes in groups:
        digest = sha256_file(source_path)
        if not scopes.source_sha256.eq(digest).all():
            raise ValueError('SEC notice exhibit original integrity mismatch')
        pins[source_path] = digest
        notice = scopes.iloc[0]
        scope_reviews = []
        if len(scopes) != 1:
            scope_reviews.append('AMBIGUOUS_NOTICE_SCOPE')
        if not scopes.scope_status.eq('reported_security_class_only').all():
            scope_reviews.append('NOTICE_SCOPE_REQUIRES_REVIEW')
        try:
            filed = datetime.strptime(notice.reported_filing_date, '%Y%m%d').date().isoformat()
        except ValueError:
            filed = ''
            scope_reviews.append('INVALID_SOURCE_FILING_DATE')
        raw = Path(source_path).read_bytes()
        outcome = dict(source_path=source_path, issuer_cik=notice.issuer_cik, accession=notice.accession,
            exhibits=0, claims=0, review_reasons=scope_reviews)
        outcomes.append(outcome)
        for ordinal, document in enumerate(re.findall(rb'<DOCUMENT>(.*?)</DOCUMENT>', raw, re.S | re.I), 1):
            form_match = re.search(rb'<TYPE>[ \t]*([^\r\n<]+)', document, re.I)
            form = form_match.group(1).decode('ascii', errors='replace').strip() if form_match else ''
            if form.upper() != 'EX-99.25':
                continue
            body_match = re.search(rb'<TEXT>(.*?)</TEXT>', document, re.S | re.I)
            if not body_match:
                continue
            filename_match = re.search(rb'<FILENAME>[ \t]*([^\r\n<]+)', document, re.I)
            filename = filename_match.group(1).decode('utf-8', errors='replace').strip() if filename_match else ''
            soup = BeautifulSoup(body_match.group(1), 'lxml')
            for hidden in soup.select('script,style,noscript'):
                hidden.decompose()
            text = re.sub(r'\s+', ' ', soup.get_text(' ', strip=True)).strip()
            context_reviews = list(scope_reviews)
            if re.search(r'\b(?:this|the) (?:notice|notification|filing|form 25) (?:is|was|has been) (?:withdrawn|rescinded|superseded)\b', text, re.I):
                context_reviews.append('WITHDRAWN_OR_REPLACED_NOTICE_CONTEXT')
            exhibit_id = sha256(f'{notice.issuer_cik}:{notice.accession}:{digest}:{ordinal}'.encode()).hexdigest()
            context = dict(exhibit_id=exhibit_id, issuer_cik=notice.issuer_cik, accession=notice.accession,
                reported_security_title=notice.reported_security_title,
                reported_exchange_name=notice.reported_exchange_name, reported_filing_date=filed,
                source_path=source_path, source_sha256=digest, source_url=notice.source_url,
                document_ordinal=ordinal, document_filename=filename, document_type=form)
            texts.append(dict(**context, normalized_text=text, detected_encoding=soup.original_encoding or ''))
            outcome['exhibits'] += 1
            for kind, status, time_qualifier, pattern in _RULES:
                for match in re.finditer(pattern, text, re.I):
                    reviews = list(context_reviews)
                    literal = match.group('date')
                    try:
                        month, day, year = re.split(r'[ ,]+', literal)
                        month_number = [value.lower() for value in _MONTHS].index(month.lower()) + 1
                        parsed = date(int(year), month_number, int(day)).isoformat()
                    except ValueError:
                        parsed = ''
                        reviews.append('INVALID_REPORTED_EVENT_DATE')
                    if status == 'reported_completed' and parsed and filed and parsed > filed:
                        reviews.append('COMPLETED_CLAIM_AFTER_FILING_DATE')
                    evidence.append(dict(**context, claim_kind=kind, statement_status=status,
                        claim_status='requires_review' if reviews else status,
                        reported_date=parsed, reported_date_literal=literal, time_qualifier=time_qualifier,
                        matched_text=match.group(), text_start=match.start(), text_end=match.end(),
                        review_reasons=json.dumps(reviews), event_verified=False))
                    outcome['claims'] += 1
    claim_groups = {}
    for row in evidence:
        claim_groups.setdefault((row['source_path'],row['claim_kind']),[]).append(row)
    for rows in claim_groups.values():
        if len({row['reported_date'] for row in rows if row['reported_date']}) > 1:
            for row in rows:
                row['claim_status'] = 'requires_review'
                row['review_reasons'] = json.dumps(json.loads(row['review_reasons']) + ['CONFLICTING_DATES_FOR_CLAIM'])
    inputs = dict(notice_observations=artifact, code_sha256=sha256_file(__file__), source_pins=pins)
    generation = sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    folder = Path(output_dir)/'generations'/generation
    result = dict(as_of=notices['as_of'], generation=generation, inputs=inputs,
        exhibit_texts=export_frame(folder/'exhibit_texts.parquet',pd.DataFrame(texts)),
        evidence=export_frame(folder/'evidence.parquet',pd.DataFrame(evidence)),
        outcomes=export_json(folder/'outcomes.json',outcomes),
        source_notices=len(outcomes), source_exhibits=len(texts), claims=len(evidence),
        claim_counts=dict(Counter(row['claim_kind'] for row in evidence)),
        unstructured_notices_not_parsed=notices['counts'].get('unstructured_notice_requires_review',0),
        registered_identities=0, coverage_complete=False,
        policy='Claims preserve the reported security class, source span and proposed/completed wording. No tradeable-security mapping, issuer-wide delisting, legal effective date or terminal payment is approved.')
    export_json(folder/'manifest.json',result)
    return result
