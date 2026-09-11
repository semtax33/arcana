"""Inventory a selected financial restoration without publishing financial values.

This checks output integrity and availability, not accounting correctness or
whether a common-share metric applies to every security class of an issuer.
"""
import argparse
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from engine.core.serving_storage import export_json, export_csv
from engine.core.paths import DATA_LAKE


def verify_direct_companyfacts(history, source_cache):
    """Match disclosed values and contexts to retained bytes, without rerunning rules."""
    selected = history.loc[history.source.isin([
        'companyfacts_primary', 'companyfacts_alternate', 'companyfacts_label'])]
    checked, failures = 0, []
    for row in selected.to_dict('records'):
        path = Path(row.get('source_path', '')).resolve()
        reason = None
        if not path.is_relative_to(DATA_LAKE.bronze('sec').resolve()) or not path.is_file():
            reason = 'missing_bronze_source'
        else:
            if str(path) not in source_cache:
                raw = path.read_bytes()
                payload = json.loads(raw)
                observations = set()
                for namespace, tags in payload.get('facts', {}).items():
                    for tag, fact in tags.items():
                        for unit, values in fact.get('units', {}).items():
                            for value in values:
                                if value.get('val') is not None:
                                    observations.add((namespace, tag, unit, str(value.get('accn', '')),
                                        str(value.get('filed', '')), str(value.get('start', '')),
                                        str(value.get('end', '')), str(value.get('form', '')),
                                        Decimal(str(value['val']))))
                source_cache[str(path)] = (sha256(raw).hexdigest(),
                    str(payload.get('cik', '')).lstrip('0'), observations)
            digest, cik, observations = source_cache[str(path)]
            if digest != row.get('source_sha256') or cik != row['cik'].lstrip('0'):
                reason = 'source_hash_or_issuer_mismatch'
            else:
                tag_parts = row['rule_id'].split(':')
                expected = (tag_parts[-2], tag_parts[-1], row['unit'], row['accn'], row['filed'],
                    row['period_start'], row['period_end'], row['form'], Decimal(row['raw_amount']))
                if expected not in observations:
                    reason = 'disclosed_value_or_context_not_found'
        if reason:
            failures.append(dict(accn=row['accn'], canonical_account_id=row['canonical_account_id'],
                                 rule_id=row['rule_id'], reason=reason))
        else:
            checked += 1
    return dict(eligible_rows=len(selected), matched_rows=checked, failures=failures,
                excluded_rows=len(history) - len(selected))


def inspect_symbol(symbol, cik, root, source_cache):
    path = root / f'us_normalized_{symbol}.csv'
    result = dict(symbol=symbol, cik=cik, exists=path.exists(), rows=0,
                  accession_rows=0, issues=[], source_counts={})
    if not path.exists():
        result['issues'].append('missing_normalized_file')
        return result
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    result.update(rows=len(frame), normalized_sha256=sha256(path.read_bytes()).hexdigest())
    keys = ['fiscal_year', 'fiscal_month', 'canonical_account_id']
    result['periods'] = len(frame[['fiscal_year', 'fiscal_month']].drop_duplicates())
    result['first_year'] = frame.fiscal_year.min() if len(frame) else None
    result['last_year'] = frame.fiscal_year.max() if len(frame) else None
    if frame.duplicated(keys).any():
        result['issues'].append('duplicate_period_account')
    amounts = pd.to_numeric(frame.normalized_amount, errors='coerce')
    if (amounts.isna() | amounts.abs().eq(float('inf'))).any():
        result['issues'].append('nonfinite_amount')
    manifest_path = root / 'accessions' / symbol / 'manifest.json'
    if not manifest_path.exists():
        result['issues'].append('missing_accession_history')
        return result
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get('market') != 'us' or manifest.get('symbol') != symbol:
        raise ValueError(f'Accession manifest identity mismatch: {symbol}')
    history_path = (manifest_path.parent / manifest['normalized_path']).resolve()
    if not history_path.is_relative_to(manifest_path.parent.resolve()):
        raise ValueError(f'Accession history outside symbol directory: {symbol}')
    raw = history_path.read_bytes()
    if sha256(raw).hexdigest() != manifest['normalized_sha256']:
        raise ValueError(f'Accession history hash mismatch: {symbol}')
    history = pd.read_csv(history_path, dtype=str, keep_default_na=False)
    result.update(accession_rows=len(history), accession_sha256=manifest['normalized_sha256'])
    if history.empty:
        result['issues'].append('empty_accession_history')
        return result
    if not history.symbol.eq(symbol).all() or not history.cik.str.lstrip('0').eq(cik.lstrip('0')).all():
        result['issues'].append('issuer_or_symbol_mismatch')
    result['source_counts'] = history.source.value_counts().to_dict()
    result['accessions'] = history.accn.nunique()
    filed = pd.to_datetime(history.filed, errors='coerce')
    period = pd.to_datetime(history.period_end, errors='coerce')
    result['missing_filed_rows'] = int(filed.isna().sum())
    result['missing_period_rows'] = int(period.isna().sum())
    result['filed_before_period_rows'] = int((filed < period).sum())
    for key in ('missing_filed_rows', 'missing_period_rows', 'filed_before_period_rows'):
        if result[key]:
            result['issues'].append(key)
    if history.accn.eq('').any():
        result['issues'].append('missing_accession_id')
    result['rows_with_source_digest'] = int(history.get('source_sha256', pd.Series('', index=history.index)).ne('').sum())
    result['direct_companyfacts'] = verify_direct_companyfacts(history, source_cache)
    if result['direct_companyfacts']['failures']:
        result['issues'].append('direct_companyfacts_source_mismatch')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticker-map', type=Path, required=True)
    parser.add_argument('--normalized', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--gold', type=Path)
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args()
    mapping = pd.read_csv(args.ticker_map, dtype=str, keep_default_na=False)
    if (mapping.empty or mapping.ticker.str.strip().eq('').any()
            or not mapping.cik.str.fullmatch(r'[0-9]+').all()
            or mapping.ticker.duplicated().any()):
        raise ValueError('Selected ticker map must have one numeric issuer per nonempty symbol')
    source_cache = {}
    records = [inspect_symbol(row.ticker, row.cik, args.normalized, source_cache) for row in mapping.itertuples()]
    if args.baseline:
        prior = {row['symbol']: row for row in json.loads(args.baseline.read_bytes())['symbols']}
        for row in records:
            before = prior[row['symbol']]
            row['previous_rows'] = before['rows']
            row['row_delta'] = row['rows'] - before['rows']
            row['previously_missing'] = not before['exists']
            row['previous_issues'] = before['issues']
    report = dict(status='structural_inventory_only', ticker_map_sha256=sha256(args.ticker_map.read_bytes()).hexdigest(),
        normalized_root=str(args.normalized.resolve()), target_symbols=len(records),
        normalized_symbols=sum(row['exists'] for row in records),
        normalized_rows=sum(row['rows'] for row in records),
        accession_rows=sum(row['accession_rows'] for row in records),
        symbols_with_issues=sum(bool(row['issues']) for row in records), symbols=records,
        source_values_verified=False, share_class_verified=False, native_financial_rows_published=0)
    export_json(args.output / 'restoration_audit.json', report)
    table = pd.DataFrame(records)
    for column in ('issues', 'source_counts', 'previous_issues', 'direct_companyfacts'):
        if column in table:
            table[column] = table[column].map(json.dumps)
    export_csv(args.output / 'symbol_inventory.csv', table)
    if args.gold:
        export_json(args.gold / 'restoration_audit.json', report)
    print(json.dumps({key: report[key] for key in ('target_symbols', 'normalized_symbols',
        'normalized_rows', 'accession_rows', 'symbols_with_issues')}))


if __name__ == '__main__':
    main()
