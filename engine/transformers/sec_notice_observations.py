"""Extract reported Form 25 security scope without approving lifecycle events."""
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re

from lxml import etree
import pandas as pd

from engine.core.serving_storage import export_frame, export_json
from engine.core.source_storage import sha256_file


def normalize_sec_notice_documents(*, collection, output_dir):
    artifact = collection['records']
    if sha256_file(artifact['path']) != artifact['sha256']:
        raise ValueError('SEC notice collection records integrity mismatch')
    records = pd.read_parquet(artifact['path'])
    observations, outcomes, pins = [], [], {}
    for record in records.to_dict('records'):
        outcome = dict(issuer_cik=record['issuer_cik'], accession=record['accession'],
            collection_status=record['status'], status='source_unavailable', observations=0, review_reasons=[])
        outcomes.append(outcome)
        if record['status'] not in {'retained', 'reused', 'legacy_document_requires_review'}:
            outcome['review_reasons'].append(record['status'])
            continue
        if record['status'] == 'legacy_document_requires_review':
            outcome['review_reasons'].append('LEGACY_SOURCE_WITHOUT_HEADER')
        meta_path = Path(record['metadata_path']).resolve()
        if sha256_file(meta_path) != record['metadata_sha256']:
            raise ValueError('SEC notice source metadata integrity mismatch')
        metadata = json.loads(meta_path.read_bytes())
        original = Path(metadata['source_path']).resolve()
        expected_url = f'https://www.sec.gov/Archives/edgar/data/{int(record["issuer_cik"])}/{record["accession"]}.txt'
        if (original.parent != meta_path.parent or metadata['source_url'] != expected_url
                or metadata['accession'] != record['accession'] or metadata['issuer_cik'] != record['issuer_cik']
                or sha256_file(original) != metadata['source_sha256']):
            raise ValueError('SEC notice original source integrity mismatch')
        pins[str(meta_path)] = record['metadata_sha256']
        pins[str(original)] = metadata['source_sha256']
        raw = original.read_bytes()
        header_match = re.search(rb'<SEC-HEADER>(.*?)</SEC-HEADER>', raw, re.S | re.I)
        header = header_match.group(1).decode('utf-8', errors='replace') if header_match else ''
        def header_value(label):
            match = re.search(r'^' + re.escape(label) + r':[ \t]*([^\r\n]*)', header, re.M)
            return match.group(1).strip() if match else ''
        for ordinal, document in enumerate(re.findall(rb'<DOCUMENT>(.*?)</DOCUMENT>', raw, re.S | re.I), 1):
            form_match = re.search(rb'<TYPE>\s*([^\r\n<]+)', document, re.I)
            form = form_match.group(1).decode('ascii', errors='replace').strip() if form_match else ''
            if form.removesuffix('/A') not in {'25', '25-NSE'}:
                continue
            for xml in re.findall(rb'<XML>\s*(.*?)\s*</XML>', document, re.S | re.I):
                try:
                    tree = etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True))
                    if tree.getroottree().docinfo.doctype:
                        raise ValueError('DTD is not accepted in notice observations')
                    if etree.QName(tree).localname != 'notificationOfRemoval':
                        continue
                    fields, reasons = {}, []
                    for field, path in {
                            'reported_issuer_cik':'issuer/cik', 'reported_issuer_name':'issuer/entityName',
                            'reported_exchange_cik':'exchange/cik', 'reported_exchange_name':'exchange/entityName',
                            'reported_security_title':'descriptionClassSecurity', 'reported_rule_provision':'ruleProvision',
                            'reported_signature_date':'signatureData/signatureDate', 'reported_file_number':'issuer/fileNumber'}.items():
                        nodes = tree.findall('./' + '/'.join('{*}' + part for part in path.split('/')))
                        fields[field] = ''.join(nodes[0].itertext()).strip() if len(nodes) == 1 else ''
                        if len(nodes) != 1 or not fields[field]:
                            reasons.append('MISSING_OR_AMBIGUOUS_' + field.upper())
                    reported_cik = fields['reported_issuer_cik']
                    normalized_cik = reported_cik.zfill(10) if re.fullmatch(r'[0-9]{1,10}', reported_cik) and int(reported_cik) > 0 else ''
                    if normalized_cik != record['issuer_cik']:
                        reasons.append('REPORTED_ISSUER_MISMATCH')
                    if not header:
                        reasons.append('MISSING_SUBMISSION_HEADER')
                    filed = header_value('FILED AS OF DATE')
                    try:
                        if not re.fullmatch(r'[0-9]{8}', filed):
                            raise ValueError('Expected an eight-digit SEC filing date')
                        datetime.strptime(filed, '%Y%m%d')
                    except ValueError:
                        reasons.append('INVALID_REPORTED_FILING_DATE')
                    observation = dict(issuer_cik=record['issuer_cik'], accession=record['accession'],
                        document_ordinal=ordinal, form=form, **fields, normalized_issuer_cik=normalized_cik,
                        reported_filing_date=filed,
                        reported_header_effectiveness_date=header_value('EFFECTIVENESS DATE'),
                        scope_status='requires_review' if reasons else 'reported_security_class_only',
                        review_reasons=json.dumps(reasons), event_verified=False,
                        source_path=str(original), source_sha256=metadata['source_sha256'],
                        source_url=metadata['source_url'], retrieved_at=metadata['retrieved_at'])
                    observations.append(observation)
                    outcome['observations'] += 1
                except (etree.LxmlError, ValueError) as exc:
                    outcome['review_reasons'].append('MALFORMED_OR_UNSUPPORTED_NOTICE_XML:' + type(exc).__name__)
        if outcome['observations']:
            outcome['status'] = 'structured_notice_observations'
        else:
            outcome['status'] = 'unstructured_notice_requires_review'
    inputs = dict(collection_records_sha256=artifact['sha256'], code_sha256=sha256_file(__file__), source_pins=pins)
    generation = sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    folder = Path(output_dir) / 'generations' / generation
    result = dict(as_of=collection['as_of'], generation=generation, inputs=inputs,
        observations=export_frame(folder/'observations.parquet', pd.DataFrame(observations)),
        outcomes=export_json(folder/'outcomes.json', outcomes), counts=dict(Counter(row['status'] for row in outcomes)),
        structured_observation_count=len(observations), registered_identities=0, coverage_complete=False,
        policy='Reported security class, exchange, signature and header dates are observations only. No issuer-wide delisting, trade cessation date or terminal entitlement is inferred.')
    export_json(folder/'manifest.json', result)
    return result
