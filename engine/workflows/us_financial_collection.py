"""Plan financial downloads independently of Alpha listing membership."""
from collections import Counter
from datetime import date
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_csv, export_frame, export_json
from engine.transformers.listing_history import normalize_alpha_listing_history
from engine.transformers.alpha_listing_population import build_alpha_listing_population
from engine.transformers.listing_identity import _name


def plan_us_financial_collection(*, as_of, symbols=None, listing_root=None,
                                ticker_map_path=None, ticker_aliases_path=None,
                                output_dir=None, gold_dir=None, submissions_manifest=None):
    """Keep every requested listing while resolving source collection targets.

    A mapping selects SEC source downloads; it does not approve historical
    financial periods or merge separate listings that reused a symbol.
    """
    cutoff = date.fromisoformat(as_of).isoformat()
    output = Path(output_dir or DATA_LAKE.silver('survivorship', 'us', 'financial_collection'))
    gold = Path(gold_dir or DATA_LAKE.gold('survivorship', 'us', 'financial_collection'))
    history = normalize_alpha_listing_history(
        root=listing_root or DATA_LAKE.bronze('alpha-vantage', 'listings'), end_date=cutoff,
        output_dir=output.parent / 'listing_history')
    if history is None:
        raise ValueError('Financial history collection requires retained Alpha listing snapshots')
    population = build_alpha_listing_population(history=history, output_dir=output.parent / 'alpha_listing_population')
    listings = pd.read_parquet(population['listing_population']['path'])
    symbol_counts = listings.groupby('symbol').provider_listing_id.nunique()
    listings = listings.loc[listings.assetType.eq('Stock')].copy()
    if symbols is not None:
        requested = {str(symbol).strip().upper() for symbol in symbols}
        listings = listings.loc[listings.symbol.isin(requested)].copy()
        absent = requested - set(listings.symbol)
        if absent:
            raise ValueError('Requested symbols absent from Alpha Stock history: ' + ', '.join(sorted(absent)))
    maps = []
    inputs = []
    for kind, path in [('current', Path(ticker_map_path or DATA_LAKE.silver('sec', 'company_tickers.csv'))),
                       ('historical_alias', Path(ticker_aliases_path or DATA_LAKE.meta('sec_ticker_aliases.csv')))]:
        if not path.exists():
            continue
        raw = path.read_bytes()
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        if not {'cik', 'ticker', 'title'}.issubset(frame.columns):
            raise ValueError('SEC collection mapping requires cik, ticker and title')
        frame = frame[['cik', 'ticker', 'title']].copy()
        frame['ticker'] = frame.ticker.str.strip().str.upper()
        if not frame.cik.str.fullmatch(r'[0-9]{1,10}').all() or frame.cik.astype('int64').eq(0).any():
            raise ValueError('Invalid CIK in financial collection mapping')
        frame['cik'] = frame.cik.map(lambda value: str(int(value)))
        frame['mapping_kind'] = kind
        frame['mapping_path'] = str(path.resolve())
        frame['mapping_sha256'] = sha256(raw).hexdigest()
        maps.append(frame)
        inputs.append(dict(kind=kind, path=str(path.resolve()), sha256=sha256(raw).hexdigest()))
    mappings = pd.concat(maps, ignore_index=True) if maps else pd.DataFrame(
        columns=['cik', 'ticker', 'title', 'mapping_kind', 'mapping_path', 'mapping_sha256'])
    by_symbol = {symbol: group.to_dict('records') for symbol, group in mappings.groupby('ticker')}
    ledger, selected = [], {}
    for row in listings.to_dict('records'):
        evidence = by_symbol.get(row['symbol'], [])
        ciks = sorted({item['cik'] for item in evidence})
        if symbol_counts[row['symbol']] != 1:
            status = 'ambiguous_listing_identity'
        elif not ciks:
            status = 'missing_cik'
        elif len(ciks) != 1:
            status = 'conflicting_cik'
        elif (row['state'] == 'delisted'
                and not any(item['mapping_kind'] == 'historical_alias' for item in evidence)
                and _name(row['name']) not in {_name(item['title']) for item in evidence}):
            status = 'issuer_name_mismatch'
        else:
            status = 'ready'
            selected[row['symbol']] = dict(cik=ciks[0], ticker=row['symbol'], title=evidence[0]['title'])
        ledger.append(dict(row, collection_status=status,
            collection_cik=ciks[0] if status == 'ready' else None,
            cik_candidates=json.dumps(ciks), mapping_evidence=json.dumps(evidence, ensure_ascii=False)))
    discovery, source_rows = None, []
    if submissions_manifest is not None:
        # A local issuer index supplies financial download leads. It must never
        # replace Alpha membership dates. A name-only lead is not a symbol
        # association or a stock class; require an explicit reported ticker
        # before adding the unique issuer to the normalization mapping.
        from engine.transformers.sec_issuer_discovery import discover_sec_issuers
        discovery = discover_sec_issuers(history=history, submissions_manifest=submissions_manifest,
            output_dir=output / 'issuer_discovery')
        candidates = pd.read_parquet(discovery['candidate_discovery']['path']).set_index('provider_listing_id')
        for row in ledger:
            if row['collection_status'] != 'missing_cik':
                continue
            candidate = candidates.loc[row['provider_listing_id']]
            if candidate.discovery_status == 'unique_name_candidate':
                matched = [edge for edge in json.loads(candidate.discovery_evidence)
                    if _name(edge['provider_name']) == _name(row['name'])
                    and row['symbol'] in {ticker.strip().upper() for ticker in edge['reported_tickers']}]
                if matched:
                    cik = str(int(json.loads(candidate.discovery_ciks)[0]))
                    evidence = [dict(edge, mapping_kind='sec_submissions_ticker_and_name',
                        mapping_path=discovery['inputs']['submissions_manifest'],
                        mapping_sha256=discovery['inputs']['submissions_manifest_sha256']) for edge in matched]
                    row.update(collection_status='ready', collection_cik=cik,
                        cik_candidates=json.dumps([cik]), mapping_evidence=json.dumps(evidence, ensure_ascii=False))
                    selected[row['symbol']] = dict(cik=cik, ticker=row['symbol'], title=row['name'])
                    continue
                source_rows.append(dict(provider_listing_id=row['provider_listing_id'], symbol=row['symbol'],
                    issuer_cik=json.loads(candidate.discovery_ciks)[0],
                    discovery_evidence=candidate.discovery_evidence, identity_verified=False))
    frame = pd.DataFrame(ledger, columns=[*listings.columns, 'collection_status', 'collection_cik',
        'cik_candidates', 'mapping_evidence'])
    source_frame = pd.DataFrame(source_rows, columns=['provider_listing_id', 'symbol', 'issuer_cik',
        'discovery_evidence', 'identity_verified'])
    generation = sha256(json.dumps(dict(as_of=cutoff, population=population['generation'], inputs=inputs,
        issuer_discovery=discovery['generation'] if discovery else None,
        symbols=sorted(set(listings.symbol)), code=sha256(Path(__file__).read_bytes()).hexdigest(),
        name_rule=sha256((Path(__file__).parents[1] / 'transformers/listing_identity.py').read_bytes()).hexdigest()), sort_keys=True).encode()).hexdigest()
    folder = output / 'generations' / generation
    unresolved = sum(row['collection_status'] != 'ready' for row in ledger)
    summary = dict(schema_version=1, market='us', as_of=cutoff, generation=generation,
        listing_keys=len(frame), requested_symbols=len(set(listings.symbol)),
        collection_symbols=sorted(selected), collection_ciks=len({row['cik'] for row in selected.values()}),
        unresolved_listing_keys=unresolved, status_counts=dict(Counter(row['collection_status'] for row in ledger)),
        coverage_complete=unresolved == 0 and bool(len(frame)), financial_history_verified=False,
        issuer_discovery=discovery,
        source_candidate_ciks=sorted(set(source_frame.issuer_cik)),
        source_candidates=export_frame(folder / 'source_candidates.parquet', source_frame),
        input_mappings=inputs, listing_history=history['silver_manifest'],
        listing_collection_ledger=export_frame(folder / 'listing_collection_ledger.parquet', frame),
        ticker_map=export_csv(folder / 'collection_ticker_map.csv', pd.DataFrame(
            [selected[symbol] for symbol in sorted(selected)], columns=['cik', 'ticker', 'title'])))
    summary['silver_manifest'] = export_json(folder / 'manifest.json', summary)
    export_json(gold / 'collection_plan.json', summary)
    return summary


def audit_us_companyfacts_collection(plan, *, companyfacts_dir=None, gold_dir=None):
    """Verify source CIKs and bodies without approving financial values or periods."""
    for name in ('ticker_map', 'source_candidates'):
        if name in plan and sha256(Path(plan[name]['path']).read_bytes()).hexdigest() != plan[name]['sha256']:
            raise ValueError('Financial collection plan artifact hash mismatch')
    mapping = pd.read_csv(plan['ticker_map']['path'], dtype=str, keep_default_na=False)
    ciks = {str(int(cik)).zfill(10) for cik in mapping.cik}
    if 'source_candidates' in plan:
        ciks.update(pd.read_parquet(plan['source_candidates']['path']).issuer_cik)
    root = Path(companyfacts_dir or DATA_LAKE.bronze('sec', 'companyfacts')).resolve()
    audit_code = sha256(Path(__file__).read_bytes()).hexdigest()
    cache_path = Path(plan['ticker_map']['path']).parents[2] / 'source_audit_cache' / 'latest.json'
    cached, reused = {}, 0
    if cache_path.exists():
        try:
            previous = json.loads(cache_path.read_bytes())
            artifact = previous['inventory']
            if (previous['audit_code'] == audit_code
                    and sha256(Path(artifact['path']).read_bytes()).hexdigest() == artifact['sha256']):
                cached = {row['issuer_cik']: row for row in pd.read_parquet(artifact['path']).to_dict('records')}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    rows = []
    for cik in sorted(ciks):
        path = root / f'CIK{cik}.json'
        url = f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json'
        row = dict(issuer_cik=cik, source_url=url, source_status='missing',
            source_path=None, source_sha256=None, entity_name=None)
        if path.exists():
            raw = path.read_bytes()
            row.update(source_path=str(path), source_sha256=sha256(raw).hexdigest(), source_status='invalid_payload')
            old = cached.get(cik)
            if (old and old['source_path'] == row['source_path']
                    and old['source_sha256'] == row['source_sha256']
                    and old['source_status'] in {'retained', 'empty_facts', 'invalid_payload'}):
                rows.append(old)
                reused += 1
                continue
            try:
                payload = json.loads(raw)
                if (isinstance(payload, dict) and str(payload.get('cik', '')).zfill(10) == cik
                        and isinstance(payload.get('facts'), dict)):
                    row.update(source_status='retained' if payload['facts'] else 'empty_facts',
                        entity_name=payload.get('entityName'))
            except (ValueError, UnicodeError):
                pass
        else:
            unavailable = root / 'unavailable' / f'CIK{cik}' / 'latest.json'
            if unavailable.exists():
                row['source_status'] = 'invalid_unavailable_record'
                try:
                    saved = json.loads(unavailable.read_bytes())
                    body = Path(saved['source_path']).resolve()
                    if (saved['http_status'] == 404 and saved['source_url'] == url
                            and body.is_relative_to(root)
                            and sha256(body.read_bytes()).hexdigest() == saved['source_sha256']):
                        row.update(source_status='unavailable_http_404', source_path=str(body),
                            source_sha256=saved['source_sha256'])
                except (OSError, ValueError, KeyError, TypeError):
                    pass
        rows.append(row)
    generation = sha256(json.dumps(dict(plan=plan['generation'], rows=rows,
        audit_code=audit_code), sort_keys=True).encode()).hexdigest()
    folder = Path(plan['ticker_map']['path']).parent / 'source_inventory' / generation
    counts = dict(Counter(row['source_status'] for row in rows))
    result = dict(plan_generation=plan['generation'], generation=generation,
        audit_code=audit_code,
        requested_ciks=len(ciks), status_counts=counts,
        all_sources_available=bool(rows) and counts.get('retained', 0) == len(rows),
        financial_history_verified=False,
        inventory=export_frame(folder / 'companyfacts.parquet', pd.DataFrame(rows)))
    result['silver_manifest'] = export_json(folder / 'manifest.json', result)
    export_json(cache_path, result)
    result['reused_payload_verifications'] = reused
    if gold_dir is not None:
        export_json(Path(gold_dir) / 'companyfacts_source_inventory.json', result)
    return result


def collect_us_financial_plan(plan, *, start_date, force=False, workers=1,
                             sleep_seconds=0.1, retries=3, retry_backoff_seconds=2.0,
                             filings_dir=None, companyfacts_dir=None, filings_provider=None):
    """Collect resolvable issuers and retain unresolved listings in the plan."""
    from engine.extractors.sec_filings import download_us_filing_htmls, download_us_companyfacts
    for name in ('ticker_map', 'listing_collection_ledger', 'source_candidates'):
        if name not in plan:
            continue
        artifact = plan[name]
        if sha256(Path(artifact['path']).read_bytes()).hexdigest() != artifact['sha256']:
            raise ValueError('Financial collection plan artifact hash mismatch')
    mapping = Path(plan['ticker_map']['path'])
    symbols = plan['collection_symbols']
    candidate_ciks = []
    if 'source_candidates' in plan:
        candidate_ciks = sorted(set(pd.read_parquet(plan['source_candidates']['path']).issuer_cik))
        if candidate_ciks != plan['source_candidate_ciks']:
            raise ValueError('Financial source candidate CIKs differ from verified plan artifact')
    if not symbols and not candidate_ciks:
        return dict(status='no_resolvable_targets', coverage_complete=False,
            financial_history_verified=False,
            unresolved_listing_keys=plan['unresolved_listing_keys'], filings=None, companyfacts_written=[])
    filings = download_us_filing_htmls(symbols=symbols, start_date=start_date, end_date=plan['as_of'],
        forms=['10-K', '10-Q'], force=force, workers=workers, sleep_seconds=sleep_seconds,
        retries=retries, retry_backoff_seconds=retry_backoff_seconds,
        ticker_map_path=mapping, ticker_aliases_path=mapping,
        output_dir=filings_dir or DATA_LAKE.bronze('sec', 'fillings'), filings_provider=filings_provider) if symbols else None
    # Explicit CIKs collect candidate source documents without creating symbol
    # aliases or allowing candidate-only issuers into financial normalization.
    facts = download_us_companyfacts(symbols=[*symbols, *candidate_ciks], force=force, sleep_seconds=sleep_seconds,
        ticker_map_path=mapping, output_dir=companyfacts_dir or DATA_LAKE.bronze('sec', 'companyfacts'))
    inventory = audit_us_companyfacts_collection(plan, companyfacts_dir=companyfacts_dir)
    report = dict(status='collected', unresolved_listing_keys=plan['unresolved_listing_keys'],
        coverage_complete=bool(plan['coverage_complete'] and filings is not None and filings.errors == 0
            and inventory['all_sources_available']),
        companyfacts_inventory=inventory,
        financial_history_verified=False, filings=filings.to_dict() if filings is not None else None,
        source_candidate_ciks=candidate_ciks,
        companyfacts_written=[str(path.resolve()) for path in facts])
    export_json(mapping.parent / 'collection_result.json', report)
    return report


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--end-date', required=True)
    parser.add_argument('--listing-root', type=Path)
    parser.add_argument('--ticker-map', type=Path)
    parser.add_argument('--ticker-aliases', type=Path)
    parser.add_argument('--submissions-manifest', type=Path,
        help='Retained SEC bulk issuer index for financial sources; never downloads delisting notices.')
    parser.add_argument('--audit-sources', action='store_true', help='Verify retained Company Facts bodies and CIKs.')
    parser.add_argument('--collect-missing-companyfacts', action='store_true',
        help='Collect missing CIK sources only; retained and unavailable responses are preserved.')
    parser.add_argument('--companyfacts-dir', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--gold', type=Path)
    args = parser.parse_args()
    retained_bulk = args.submissions_manifest or DATA_LAKE.silver('sec', 'submissions', 'latest.json')
    result = plan_us_financial_collection(as_of=args.end_date, listing_root=args.listing_root,
        ticker_map_path=args.ticker_map, ticker_aliases_path=args.ticker_aliases,
        output_dir=args.output, gold_dir=args.gold,
        submissions_manifest=retained_bulk if args.submissions_manifest or retained_bulk.exists() else None)
    print(json.dumps({key: result[key] for key in ('generation', 'listing_keys',
        'requested_symbols', 'collection_ciks', 'unresolved_listing_keys', 'status_counts', 'coverage_complete')}))
    if args.audit_sources or args.collect_missing_companyfacts:
        gold = args.gold or DATA_LAKE.gold('survivorship', 'us', 'financial_collection')
        audit = audit_us_companyfacts_collection(result, companyfacts_dir=args.companyfacts_dir, gold_dir=gold)
        print(json.dumps(dict(companyfacts_status_counts=audit['status_counts'])), flush=True)
        if args.collect_missing_companyfacts:
            from engine.extractors.sec_filings import download_us_companyfacts
            inventory = pd.read_parquet(audit['inventory']['path'])
            missing = inventory.loc[inventory.source_status.eq('missing'), 'issuer_cik'].tolist()
            written = download_us_companyfacts(symbols=missing, ticker_map_path=result['ticker_map']['path'],
                output_dir=args.companyfacts_dir or DATA_LAKE.bronze('sec', 'companyfacts'), sleep_seconds=0.25) if missing else []
            final = audit_us_companyfacts_collection(result, companyfacts_dir=args.companyfacts_dir, gold_dir=gold) if missing else audit
            report = dict(plan_generation=result['generation'], requested_ciks=missing,
                written=[str(path.resolve()) for path in written], before=audit, after=final,
                financial_history_verified=False)
            export_json(Path(result['ticker_map']['path']).parent / 'missing_companyfacts_collection.json', report)
            export_json(Path(gold) / 'missing_companyfacts_collection.json', report)
            print(json.dumps(dict(companyfacts_written=len(written), companyfacts_status_counts=final['status_counts'])), flush=True)


if __name__ == '__main__':
    main()
