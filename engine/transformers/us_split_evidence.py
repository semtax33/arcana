"""Reviewed official share-unit contracts that disagree with vendor actions."""
from __future__ import annotations

from decimal import Decimal, DecimalException
import hashlib
import json
from pathlib import Path
import re

from engine.transformers.stock_splits import SplitEvent, resolve_events, text_of_html, US_DATE, parse_us_date

MANIFEST=Path(__file__).with_suffix('.json')
FINRA_URL='https://api.finra.org/data/group/otcMarket/name/otcDailyList'


def contract_ratio(marker):
    words={'one thousand':1000,'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,'ten':10}
    number=rf'(?:\d[\d,]*(?:\.\d+)?|{"|".join(words)})'
    numeric_tail=r'(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|and)'
    end=rf'\b(?!-\w)(?!\s+{numeric_tail}\b)'
    def value(token):
        return Decimal(words[token.lower()]) if token.lower() in words else Decimal(token.replace(',',''))
    def divide(new,old):
        if not new.is_finite() or not old.is_finite() or min(new,old)<=0:
            raise ValueError('Share-unit counts must be finite and positive')
        return new/old
    pair=re.search(rf'\b({number})\s*[- ]for\s*[- ]({number}){end}',marker,re.I)
    if pair:return divide(value(pair[1]),value(pair[2]))
    pair=re.search(rf'\b(?:every|each)\s+({number}){end}.{{0,300}}?(?:combined|converted|consolidated).{{0,35}}?into\s+({number}){end}',marker,re.I)
    if pair:return divide(value(pair[2]),value(pair[1]))
    raise ValueError('No explicit share-unit conversion in reviewed ratio marker')


def records(path=None):
    return json.loads(Path(path or MANIFEST).read_text(encoding='utf-8'))['records']


def source_path(root, source):
    # All locations are validated components under the caller's source root.
    if source.get('provider')=='FINRA':
        if not re.fullmatch(r'FINRA-OTCDAILYLIST-[A-Z0-9-]+',source['source_id']):
            raise ValueError('Invalid FINRA evidence identifier')
        return Path(root)/'regulatory'/'finra'/(source['source_id']+'.json')
    if source.get('provider')=='issuer_IR':
        if source['cik']!='1880343' or not re.fullmatch(r'AMZE-IR-press-release-\d+',source['source_id']):
            raise ValueError('Unknown reviewed issuer source')
        return Path(root)/'issuer_ir'/(source['source_id']+'.html')
    if not re.fullmatch(r'\d+',source['cik']):raise ValueError('Invalid evidence CIK')
    if not re.fullmatch(r'\d{10}-\d{2}-\d{6}',source['source_id']):raise ValueError('Invalid accession')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',source['filename']):raise ValueError('Invalid evidence filename')
    return Path(root)/'disclosures'/source['cik']/(source['source_id']+'_'+source['filename'])


def verify_record(record, root, as_of):
    if record['effective_date']>as_of:raise ValueError('Future unit change')
    if not record.get('review_reference'):raise ValueError('Missing review reference')
    try:
        ratio=Decimal(record['new_shares'])/Decimal(record['old_shares'])
    except DecimalException as exc:
        raise ValueError('Invalid reviewed share-unit counts') from exc
    if not ratio.is_finite():raise ValueError('Invalid reviewed share-unit counts')
    if ratio<=0 or ratio==1:raise ValueError('Reviewed action must change positive share units')
    if len({s['source_url'] for s in record['sources']})<2:raise ValueError('Two official documents are required')
    roles=set()
    for source in record['sources']:
        finra=source.get('provider')=='FINRA'
        if source.get('provider')=='issuer_IR':
            if source['cik']!='1880343' or not re.fullmatch(r'https://ir\.amaze\.co/news-events/press-releases/detail/\d+/[a-z0-9-]+',source['source_url']):
                raise ValueError('Unexpected official issuer URL')
            expected_url=source['source_url']
        else:expected_url=FINRA_URL if finra else f"https://www.sec.gov/Archives/edgar/data/{source['cik']}/{source['source_id'].replace('-','')}/{source['filename']}"
        if source['source_url']!=expected_url:raise ValueError('Unexpected official source URL')
        if source['published_date']>as_of:raise ValueError('Future supporting evidence')
        path=source_path(root,source);raw=path.read_bytes()
        meta=json.loads(path.with_suffix(path.suffix+'.metadata.json').read_text(encoding='utf-8'))
        if hashlib.sha256(raw).hexdigest()!=source['source_sha256']:raise ValueError('Evidence source hash changed')
        if any(meta.get(k)!=source[k] for k in ['source_url','source_id','published_date','source_sha256']):
            raise ValueError('Evidence source metadata changed')
        if meta.get('security_id')!=record['security_id']:raise ValueError('Evidence share class differs')
        if finra:
            rows=json.loads(raw)
            selected=[row for row in rows if row.get('OTCDailyListID')==source['finra_list_id']]
            if len(selected)!=1:raise ValueError('FINRA action identifier is not unique')
            row=selected[0]
            if row.get('dailyListEventCode')!='DA' or 'cancel' in str(row.get('commentText') or '').lower():
                raise ValueError('FINRA record is not an active action announcement')
            if str(row.get('exDate'))[:10]!=record['effective_date']:
                raise ValueError('FINRA exDate differs from the trading date')
            if str(row.get('dailyListDatetime'))[:10]!=source['published_date']:
                raise ValueError('FINRA publication date differs')
            symbol=record.get('historical_symbol') or record['security_id'].removeprefix('SEC_US_')
            if row.get('oldSymbolCode')!=symbol:raise ValueError('FINRA historical symbol differs')
            rate=str(row.get('reverseSplitRate') or row.get('forwardSplitRate') or '')
            pair=re.fullmatch(r'(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)',rate)
            if not pair or Decimal(pair[2])<=0:
                raise ValueError('FINRA share ratio is missing')
            if Decimal(pair[1])/Decimal(pair[2])!=ratio:
                raise ValueError('FINRA and charter share-unit ratios disagree')
            if meta.get('request_body')!=source['request_body']:
                raise ValueError('FINRA query provenance differs')
        compact=re.sub(r'\s+','',text_of_html(raw)).casefold()
        if not source['markers'] or any(re.sub(r'\s+','',m).casefold() not in compact for m in source['markers']):
            raise ValueError('Reviewed operative text is missing')
        roles.update(source['roles'])
    if not {'exact_ratio','actual_execution'}<=roles or not ('trading_date' in roles or
            record.get('execution_bridge') and 'announced_trading_date' in roles):
        raise ValueError('Incomplete reviewed evidence')
    bridge=record.get('execution_bridge')
    if bridge:
        source=next((s for s in record['sources'] if s['source_url']==bridge['source_url']
                     and 'legal_effectiveness' in s['roles']),None)
        if not source or bridge['marker'] not in source['markers']:
            raise ValueError('Missing legal-effectiveness bridge evidence')
        dates={parse_us_date(m.group(0)) for m in re.finditer(US_DATE,bridge['marker'],re.I)}
        if dates!={bridge['legal_effective_date']} or not re.search(r'5:00\s*p\.?m\.?\s*Eastern',bridge['marker'],re.I):
            raise ValueError('Bridge requires an explicit 5 p.m. Eastern legal effective time')
        from datetime import date
        lag=(date.fromisoformat(record['effective_date'])-date.fromisoformat(bridge['legal_effective_date'])).days
        if not 0<lag<=7:raise ValueError('Legal/trading bridge dates are inconsistent')
    if record.get('allow_missing_vendor_action'):
        date_proof='market_resumption' in roles or any(s.get('provider')=='FINRA' for s in record['sources']) or bridge
        if not date_proof or not any('actual_execution' in s['roles']
                and s.get('provider','EDGAR')=='EDGAR' for s in record['sources']):
            raise ValueError('Missing execution and market-resumption proof for absent vendor action')
    main=next(s for s in record['sources'] if 'exact_ratio' in s['roles'])
    if record['ratio_marker'] not in main['markers'] or contract_ratio(record['ratio_marker'])!=ratio:
        raise ValueError('Reviewed ratio fields disagree with the operative text')
    return SplitEvent(security_id=record['security_id'],effective_date=record['effective_date'],
        new_shares=record['new_shares'],old_shares=record['old_shares'],source='EDGAR',
        source_id=main['source_id'],source_url=main['source_url'],source_sha256=main['source_sha256'],
        published_date=max(s['published_date'] for s in record['sources']),action_type='reverse_split' if ratio<1 else 'split',
        evidence=json.dumps({'review_reference':record['review_reference'],'sources':record['sources']},ensure_ascii=False))


def supplement_ledger(ledger, root, manifest_path=None):
    """A parser disagreement still fails closed; vendor disagreement may be reviewed."""
    manifest=json.loads(Path(manifest_path or MANIFEST).read_text(encoding='utf-8'))
    for exclusion in manifest.get('event_exclusions',[]):
        try:
            if exclusion['reason']!='different_issuer_in_acquisition_exhibit' or not exclusion['review_reference']:
                raise ValueError('Unsupported source-event exclusion')
            source=exclusion['source']
            expected_url=f"https://www.sec.gov/Archives/edgar/data/{source['cik']}/{source['source_id'].replace('-','')}/{source['filename']}"
            if source['source_url']!=expected_url or source['published_date']>ledger['as_of']:
                raise ValueError('Invalid exclusion source URL/date')
            path=source_path(root,source);raw=path.read_bytes()
            meta=json.loads(path.with_suffix(path.suffix+'.metadata.json').read_text(encoding='utf-8'))
            if hashlib.sha256(raw).hexdigest()!=source['source_sha256'] or any(meta.get(k)!=source[k]
                    for k in ['source_id','source_url','source_sha256','published_date']):
                raise ValueError('Exclusion source identity/hash changed')
            if meta.get('security_id')!=exclusion['security_id']:
                raise ValueError('Exclusion filing issuer differs')
            compact=re.sub(r'\s+','',text_of_html(raw)).casefold()
            if len(source['markers'])<2 or any(re.sub(r'\s+','',m).casefold() not in compact for m in source['markers']):
                raise ValueError('Explicit acquired-issuer evidence is missing')
            subject=exclusion['affected_issuer_symbol']
            if subject==exclusion['security_id'].removeprefix('SEC_US_') or not re.search(
                    r'symbol\s*[\"\u201c]?'+re.escape(subject)+r'\b',' '.join(source['markers']),re.I):
                raise ValueError('Different affected trading symbol is not evidenced')
            removed=[e for e in ledger['events'] if all(e.get(k)==exclusion[k]
                     for k in ['security_id','effective_date','new_shares','old_shares'])
                     and e['source_id']==source['source_id'] and e['source_sha256']==source['source_sha256']]
            for event in removed:
                ledger['events'].remove(event)
                ledger['review'].append({'security_id':event['security_id'],'reason':exclusion['reason'],
                    'excluded_event':event,'affected_issuer_symbol':subject,'review_reference':exclusion['review_reference']})
        except (OSError,ValueError,KeyError) as exc:
            ledger['review'].append({'security_id':exclusion.get('security_id'),
                'reason':'source_event_exclusion_unverified','detail':str(exc)})
    fields=set(SplitEvent.__dataclass_fields__)
    events=[SplitEvent(**{k:v for k,v in row.items() if k in fields}) for row in ledger['events']]
    existing={(e.security_id,e.effective_date):e for e in events}
    overrides=[];return_reviews=[]
    for record in records(manifest_path):
        if record['effective_date']>ledger['as_of']:continue
        try:
            event=verify_record(record,root,ledger['as_of'])
            superseded=[]
            for prior in record.get('supersedes_sources',[]):
                proof=next((s for s in record['sources'] if s['source_url']==prior['proof_source_url']
                            and 'supersedes_prior_schedule' in s['roles']),None)
                if not proof or prior['proof_marker'] not in proof['markers']:
                    raise ValueError('Missing verified supersession text')
                stale=[e for e in events if e.security_id==event.security_id
                    and e.effective_date==prior['effective_date'] and e.source_id==prior['source_id']
                    and e.source_sha256==prior['source_sha256']]
                if any(e.ratio!=event.ratio for e in stale):
                    raise ValueError('Superseded plan has a different share-unit ratio')
                superseded.extend(stale)
            other=existing.get((event.security_id,event.effective_date))
            if other and other.ratio!=event.ratio:raise ValueError('Reviewed contract conflicts with another official extraction')
            for stale in superseded:
                events.remove(stale)
                ledger['review'].append({'security_id':stale.security_id,'effective_date':stale.effective_date,
                    'reason':'superseded_announced_schedule','superseded_event':stale.to_dict(),
                    'confirmed_effective_date':event.effective_date,'review_reference':record['review_reference']})
            if other:events.remove(other)
            events.append(event)
            overrides.append({'security_id':event.security_id,'effective_date':event.effective_date,
                'new_shares':event.new_shares,'old_shares':event.old_shares,'event_id':event.event_id,
                'review_sha256':hashlib.sha256(json.dumps(record,sort_keys=True).encode()).hexdigest(),
                'review_reference':record['review_reference'],
                **({'allow_missing_vendor_action':True} if record.get('allow_missing_vendor_action') else {}),
                **({'market_date_confirmation':'FINRA_exDate'} if any(s.get('provider')=='FINRA' for s in record['sources']) else {}),
                **({'execution_bridge':record['execution_bridge']} if record.get('execution_bridge') else {}),
                **({'vendor_action_replacement':record['vendor_action_replacement']} if record.get('vendor_action_replacement') else {})})
            for action in record.get('other_actions',[]):
                return_reviews.append({'security_id':event.security_id,'effective_date':event.effective_date,**action})
        except (OSError,ValueError,KeyError) as exc:
            ledger['review'].append({'security_id':record['security_id'],'effective_date':record['effective_date'],
                                    'reason':'reviewed_us_evidence_unavailable','detail':str(exc)})
    ledger['events']=[e.to_dict() for e in resolve_events(events,as_of=ledger['as_of'])]
    ledger['official_vendor_overrides']=overrides
    ledger['return_review_events']=return_reviews
    return ledger


def download_reviewed_sources(*, root, as_of, symbols=None, manifest_path=None):
    from engine.extractors.stock_splits import OfficialSession, _save_response
    from engine.transformers._internal.edgar_identity import resolve_edgar_identity
    session=OfficialSession();session.session.headers.update({'User-Agent':resolve_edgar_identity()})
    report={'sources':[],'errors':[]}
    manifest=json.loads(Path(manifest_path or MANIFEST).read_text(encoding='utf-8'))
    downloads=manifest['records']+[r|{'sources':[r['source']]} for r in manifest.get('event_exclusions',[])]
    for record in downloads:
        if record['effective_date']>as_of or (symbols and record['security_id'].removeprefix('SEC_US_') not in symbols):continue
        for source in record['sources']:
            path=source_path(root,source)
            if path.exists() and path.with_suffix(path.suffix+'.metadata.json').exists():continue
            try:
                if source.get('provider')=='FINRA':
                    if source['source_url']!=FINRA_URL:raise ValueError('Unexpected FINRA endpoint')
                    response=session.request('POST',FINRA_URL,json=source['request_body'])
                else:response=session.request('GET',source['source_url'])
                if hashlib.sha256(response.content).hexdigest()!=source['source_sha256']:
                    raise ValueError('Official document changed; renewed review is required')
                meta={k:source[k] for k in ['source_id','published_date','cik']}
                meta.update(provider=source.get('provider','EDGAR'),security_id=record['security_id'],
                    document_id=source['source_id']+':'+source.get('filename','daily-list.json'))
                if source.get('provider')=='FINRA':meta.update(request_method='POST',request_body=source['request_body'])
                report['sources'].append(_save_response(path.parent,path.name,response,meta))
            except Exception as exc:
                report['errors'].append({'source_url':source['source_url'],'error':f'{type(exc).__name__}: {exc}'})
    return report
