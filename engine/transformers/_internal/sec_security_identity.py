"""Filing-local DEI security observations, separate from listing lifetimes."""
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlparse, unquote

from engine.core.serving_storage import export_json
from engine.markets.us import US_MARKET_CONFIG


def reported_security_identity(descriptor, xbrl, facts):
    manifest = descriptor.manifest
    cik = descriptor.cik.zfill(10)
    report = dict(schema_version=1, requested_symbol=descriptor.symbol, cik=cik,
        accession=descriptor.accession, source_manifest=str((descriptor.path / 'filing.json').resolve()),
        manifest_sha256=sha256((descriptor.path / 'filing.json').read_bytes()).hexdigest(),
        observations=[], review_reasons=[])
    instances = [item for item in manifest.get('xbrl_documents', []) if item.get('role') == 'instance']
    if len(instances) != 1:
        report['review_reasons'].append('NO_UNIQUE_SOURCE_INSTANCE')
        return report
    source = instances[0]
    name = source.get('document_name', '')
    path = (descriptor.path / name).resolve()
    url = urlparse(source.get('source_url', ''))
    expected_url_path = f'/Archives/edgar/data/{int(descriptor.cik)}/{descriptor.accession.replace("-", "")}/{name}'
    if (Path(name).name != name or path.parent != descriptor.path.resolve()
            or not path.is_file() or sha256(path.read_bytes()).hexdigest() != source.get('sha256')
            or url.scheme != 'https' or url.hostname not in {'www.sec.gov', 'sec.gov'}
            or unquote(url.path) != expected_url_path):
        report['review_reasons'].append('INSTANCE_SOURCE_INTEGRITY_FAILED')
        return report
    report['instance_source'] = dict(path=str(path), sha256=source['sha256'], source_url=source['source_url'])
    try:
        published = date.fromisoformat(str(manifest.get('filing_date'))).isoformat()
    except (TypeError, ValueError):
        report['review_reasons'].append('MISSING_PUBLICATION_DATE')
        return report
    concepts = {'dei:TradingSymbol', 'dei:Security12bTitle', 'dei:SecurityExchangeName', 'dei:EntityRegistrantName'}
    rows = facts.loc[facts.concept.isin(concepts)].to_dict('records')
    def same_issuer(context_ref):
        context = xbrl.contexts.get(context_ref)
        identifier = str((getattr(context, 'entity', {}) or {}).get('identifier', '')).removeprefix('CIK')
        return identifier.isdigit() and identifier.zfill(10) == cik
    names = {str(row.get('value', '')).strip() for row in rows
        if row['concept'] == 'dei:EntityRegistrantName' and same_issuer(row.get('context_ref'))} - {''}
    if len(names) != 1:
        report['review_reasons'].append('AMBIGUOUS_ISSUER_NAME')
        return report
    report.update(issuer_name=next(iter(names)), published_date=published, accepted_at=manifest.get('accepted_at'))
    groups = {}
    for row in rows:
        if row['concept'] != 'dei:EntityRegistrantName':
            groups.setdefault(row.get('context_ref'), []).append(row)
    for context_ref, group in groups.items():
        context = xbrl.contexts.get(context_ref)
        if not same_issuer(context_ref):
            report['review_reasons'].append('CONTEXT_ISSUER_MISMATCH')
            continue
        fields = {}
        for concept, field in [('dei:TradingSymbol', 'reported_symbol'),
                ('dei:Security12bTitle', 'security_title'), ('dei:SecurityExchangeName', 'exchange')]:
            values = {str(row.get('value', '')).strip() for row in group if row['concept'] == concept} - {''}
            if len(values) != 1:
                break
            fields[field] = values.pop()
        if len(fields) != 3:
            report['review_reasons'].append('INCOMPLETE_OR_AMBIGUOUS_SECURITY_CONTEXT')
            continue
        title = fields['security_title']
        normalized_symbol = US_MARKET_CONFIG.normalize_symbol(fields['reported_symbol'])
        common = (re.match(r'(?i)^(?:class\s+[a-z0-9-]+\s+)?(?:common\s+stock|ordinary\s+shares)\b', title)
                  and not re.search(r'(?i)\b(warrants?|rights?|units?|preferred|depositary)\b', title))
        period_ends = {str(row.get('period_end') or row.get('period_instant') or '')[:10] for row in group}
        report['observations'].append(dict(cik=cik, issuer_name=next(iter(names)),
            **fields, security_type='common_stock' if common else 'unclassified',
            normalized_symbol=normalized_symbol,
            context_ref=context_ref, dimensions=dict(getattr(context, 'dimensions', {}) or {}),
            reported_period_end=next(iter(period_ends)) if len(period_ends) == 1 else None,
            published_date=published, accepted_at=manifest.get('accepted_at'),
            listing_interval_verified=False))
    if not groups:
        report['review_reasons'].append('NO_REPORTED_SECURITY_CLASS')
    if report['observations'] and not any(row['normalized_symbol'] == descriptor.symbol for row in report['observations']):
        report['review_reasons'].append('REQUESTED_SYMBOL_DIFFERS_FROM_FILING')
    report['review_reasons'] = sorted(set(report['review_reasons']))
    return report


def write_security_identity_reports(reports, output_dir):
    root = (Path(output_dir) / 'security_identity').resolve()
    for report in reports:
        symbol, accession = report['requested_symbol'], report['accession']
        if Path(symbol).name != symbol or not re.fullmatch(r'\d{10}-\d{2}-\d{6}', accession):
            raise ValueError('Invalid SEC identity output key')
        fingerprint = sha256(json.dumps(report, sort_keys=True, default=str).encode()).hexdigest()
        export_json(root / symbol / 'versions' / f'{fingerprint}.json', report)
        export_json(root / symbol / f'{accession}.json', report)
