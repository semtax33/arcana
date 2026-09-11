"""Corroborate provider candidates without promoting them to legal identities."""
from collections import Counter, defaultdict
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
import unicodedata

import pandas as pd

from engine.core.serving_storage import export_frame, export_json
from engine.markets.us import US_MARKET_CONFIG


def _digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _name(value):
    return re.sub(r'[\W_]+', ' ', unicodedata.normalize('NFKC', value).casefold()).strip()


def corroborate_listing_identities(*, history, identity_dir, end_date, output_dir):
    """Join exact reported attributes; retain every provider key and its reviews.

    Provider dates are used only to reject inconsistent associations. Neither
    their interval nor a matching class observation authorizes price loading.
    """
    cutoff = date.fromisoformat(end_date).isoformat()
    artifact = history['candidate_index']
    if _digest(artifact['path']) != artifact['sha256']:
        raise ValueError('Listing candidate index integrity mismatch')
    candidates = pd.read_parquet(artifact['path'])
    reports, by_symbol = [], defaultdict(list)
    for path in sorted(Path(identity_dir).glob('*/*.json')):
        raw = path.read_bytes()
        report = json.loads(raw)
        # Future publications cannot contribute to an earlier identity slice.
        published = report.get('published_date')
        if published and date.fromisoformat(published).isoformat() > cutoff:
            continue
        source = Path(report['source_manifest'])
        if _digest(source) != report['manifest_sha256']:
            raise ValueError('SEC identity source manifest integrity mismatch')
        manifest = json.loads(source.read_bytes())
        if (str(manifest['cik']).removeprefix('CIK').zfill(10) != report['cik']
                or manifest['accession_number'] != report['accession']):
            raise ValueError('SEC identity issuer/accession mismatch')
        if report.get('observations'):
            instance = report['instance_source']
            expected = [item for item in manifest.get('xbrl_documents', []) if item.get('role') == 'instance']
            if (len(expected) != 1 or _digest(instance['path']) != instance['sha256']
                    or expected[0]['sha256'] != instance['sha256']
                    or (source.parent / expected[0]['document_name']).resolve() != Path(instance['path']).resolve()
                    or expected[0]['source_url'] != instance['source_url']
                    or published != manifest['filing_date']):
                raise ValueError('SEC identity instance/publication integrity mismatch')
        evidence = dict(report_path=str(path.resolve()), report_sha256=sha256(raw).hexdigest(),
            source_manifest=str(source.resolve()), manifest_sha256=report['manifest_sha256'],
            accession=report['accession'], published_date=published,
            review_reasons=report['review_reasons'])
        reports.append(evidence)
        for observation in report.get('observations', []):
            if (observation['cik'] != report['cik'] or observation['published_date'] != published
                    or observation['issuer_name'] != report['issuer_name']):
                raise ValueError('SEC class observation differs from its filing identity')
            symbol = US_MARKET_CONFIG.normalize_symbol(observation['reported_symbol'])
            by_symbol[symbol].append((observation, evidence))
    version = _digest(__file__)
    generation = sha256(json.dumps(dict(history=history['generation'], index=artifact['sha256'],
        reports=reports, as_of=cutoff, code=version), sort_keys=True).encode()).hexdigest()
    folder = Path(output_dir) / 'generations' / generation
    manifest_path = folder / 'manifest.json'
    if manifest_path.exists():
        saved = json.loads(manifest_path.read_bytes())
        saved_index = folder / 'candidate_links.parquet'
        if (saved.get('generation') == generation and saved_index.exists()
                and saved.get('candidate_links', {}).get('sha256') == _digest(saved_index)):
            saved['silver_manifest'] = dict(path=str(manifest_path.resolve()), sha256=_digest(manifest_path))
            saved['generation_reused'] = True
            return saved
    records = []
    for candidate in candidates.to_dict('records'):
        reasons = set(json.loads(candidate['review_reasons']))
        names = {_name(name) for name in json.loads(candidate['observed_names'])}
        terminal = json.loads(candidate['observed_delisting_dates'])
        observations = by_symbol.get(US_MARKET_CONFIG.normalize_symbol(candidate['symbol']), [])
        matches, mismatch, related_issuers, conflicting_evidence = [], set(), set(), []
        if not observations:
            mismatch.add('NO_AVAILABLE_REPORTED_CLASS')
        for observation, evidence in observations:
            failure = set()
            if observation['security_type'] != 'common_stock' or candidate['assetType'] != 'Stock':
                failure.add('SECURITY_CLASS_NOT_COMMON_STOCK')
            if _name(observation['issuer_name']) not in names:
                failure.add('ISSUER_NAME_MISMATCH')
            if observation['exchange'].strip().upper() != candidate['exchange'].strip().upper():
                failure.add('EXCHANGE_MISMATCH')
            if (observation['published_date'] < candidate['ipoDate']
                    or (terminal and observation['published_date'] > max(terminal))):
                failure.add('PUBLICATION_OUTSIDE_PROVIDER_DATES')
            if not (failure - {'ISSUER_NAME_MISMATCH'}):
                related_issuers.add(observation['cik'])
                if failure:
                    conflicting_evidence.append(dict(**evidence, cik=observation['cik'],
                        issuer_name=observation['issuer_name'], context_ref=observation['context_ref'],
                        reasons=sorted(failure)))
            if failure:
                mismatch.update(failure)
            else:
                matches.append(dict(**evidence, cik=observation['cik'],
                    reported_symbol=observation['reported_symbol'], context_ref=observation['context_ref'],
                    security_title=observation['security_title'], exchange=observation['exchange']))
        ciks = sorted({match['cik'] for match in matches})
        if len(related_issuers) > 1:
            reasons.add('MULTIPLE_SEC_ISSUERS')
        if not matches:
            reasons.update(mismatch)
        status = 'corroborated_source_observation' if len(ciks) == 1 and not reasons else 'review_required'
        records.append(dict(**candidate, candidate_ciks=json.dumps(ciks),
            observed_ciks=json.dumps(sorted(related_issuers)),
            conflicting_evidence=json.dumps(conflicting_evidence, ensure_ascii=False),
            evidence=json.dumps(matches, ensure_ascii=False), link_status=status,
            link_review_reasons=json.dumps(sorted(reasons)), listing_interval_verified=False))
    linked = pd.DataFrame(records)
    result = dict(schema_version=1, as_of=cutoff, generation=generation, code_sha256=version,
        listing_generation=history['generation'], reports=reports,
        candidate_links=export_frame(folder / 'candidate_links.parquet', linked),
        provider_listing_keys=len(linked), sec_reports=len(reports),
        corroborated_candidates=int(linked.link_status.eq('corroborated_source_observation').sum()),
        review_reason_counts=dict(Counter(reason for value in linked.link_review_reasons for reason in json.loads(value))),
        registered_identities=0, coverage_complete=False,
        policy='Corroborated source observations are not verified security lifetimes or price/settlement authorization.')
    result['silver_manifest'] = export_json(manifest_path, result)
    result['generation_reused'] = False
    return result
