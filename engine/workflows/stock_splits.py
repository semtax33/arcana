"""Official split download -> event ledger -> reproducible split-only price panels.

The panels are separate from source prices and include their adjustment basis.
Unparsed/conflicting disclosures remain visible in the review report. They are
never converted into a synthetic split from a large price move.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

import numpy as np
import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.core.source_storage import (
    SourceArchiveSession, SourceRefreshLock, new_source_run_id,
    write_source_text, write_source_dataframe, sha256_file,
)
from engine.extractors.stock_splits import download_stock_splits, root_for
from engine.transformers.stock_splits import (
    PARSER_VERSION, SCHEMA_VERSION, SplitEvent, adjust_prices, confirm_dart_with_prices,
    ledger_path, parse_dart_split, parse_kr_share_unit_change, parse_dart_resumption, parse_kind_listing,
    parse_kind_capital_listing, parse_kind_reference_price, parse_edgar_split, resolve_events,
)


def save_json(path, payload):
    write_source_text(path,json.dumps(payload,ensure_ascii=False,indent=2,default=str),source='stock-split-pipeline')


def read_kr_price(path, symbol):
    raw=pd.read_csv(path)
    mapping={'날짜':'trade_date','시가':'open','고가':'high','저가':'low','종가':'close','거래량':'volume','등락률':'change_rate'}
    frame=raw.rename(columns=mapping)
    frame['security_id']=f'SEC_KR_{symbol}'
    frame['currency']='KRW'
    frame['trade_date']=pd.to_datetime(frame.trade_date,format='mixed')
    return frame.sort_values('trade_date').drop_duplicates('trade_date',keep='last')


def load_alpha_split_candidates(*,as_of=None,root=None):
    """Keep the reported decimal verbatim; rounded reverse ratios are not guessed.

    Missing/empty/error payloads are coverage gaps, not a proof of no splits.
    Later snapshots supersede an entire symbol history, including deletions.
    """
    root=Path(root or DATA_LAKE.bronze('consensus','alpha-vantage','splits'))
    latest={};issues=[];rows=[]
    for snapshot in sorted(root.glob('snapshot_date=*')):
        snapshot_date=snapshot.name.split('=',1)[1]
        if as_of and snapshot_date>str(pd.Timestamp(as_of).date()):continue
        for path in snapshot.glob('ticker=*.json'):
            latest[path.stem.removeprefix('ticker=')]=(snapshot_date,path)
    for ticker,(snapshot_date,path) in sorted(latest.items()):
        payload=json.loads(path.read_text(encoding='utf-8-sig'))
        if payload.get('symbol')!=ticker or not isinstance(payload.get('data'),list):
            issues.append({'symbol':ticker,'reason':'invalid_or_missing_alpha_payload','path':str(path)});continue
        digest=sha256_file(path)
        for record in payload['data']:
            try:
                ratio=Decimal(record['split_factor'])
                if not ratio.is_finite() or ratio<=0 or ratio==1:raise ValueError('invalid ratio')
                effective_date=str(pd.Timestamp(record['effective_date']).date())
            except (ValueError,KeyError,ArithmeticError) as exc:
                issues.append({'symbol':ticker,'reason':str(exc),'record':record});continue
            rows.append({'security_id':f'SEC_US_{ticker}','symbol':ticker,'effective_date':effective_date,
                         'reported_ratio':str(ratio),'snapshot_date':snapshot_date,'source_path':str(path.resolve()),
                         'source_sha256':digest,'future_at_snapshot':effective_date>snapshot_date})
    return rows,issues


def normalize_split_ledger(market,*,as_of=None,source_root=None,output_path=None):
    cutoff=str(pd.Timestamp(as_of or date.today()).date())
    root=Path(source_root or root_for(market))
    observations=[];reviews=[];events=[];selected={};families={};sources=[]
    def prepare_source(path):
        metadata=json.loads(path.read_text(encoding='utf-8'))
        if metadata.get('provider') not in {'DART','KIND','EDGAR'}:return None,None
        if path.is_relative_to(root/'public_documents') and metadata.get('source_validation') not in {'pending','failed'}:
            return None,None
        if metadata.get('published_date','9999')>cutoff:return None,None
        # The local sidecar remains relocatable; never trust a manifest path to read outside this source tree.
        content_path=path.with_name(path.name.removesuffix('.metadata.json'))
        if metadata.get('source_validation') in {'pending','failed'} and metadata.get('byte_count')==0:
            if metadata.get('source_sha256')!=hashlib.sha256(b'').hexdigest():
                raise ValueError(f'empty response hash mismatch: {path}')
            if content_path.exists() and content_path.stat().st_size:
                raise ValueError(f'empty response metadata conflicts with source: {path}')
            metadata['_path']=None
            return metadata,None
        if not content_path.exists():
            return None,{'source_id':metadata.get('source_id'),'reason':'missing_source_file'}
        raw=content_path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=metadata['source_sha256']:
            raise ValueError(f'source hash mismatch: {content_path}')
        metadata['_path']=content_path
        if market=='us' and metadata.get('security_id'):
            metadata['_parsed']=parse_edgar_split(raw,**{k:metadata[k] for k in ['security_id','source_id','source_url','published_date']})
        return metadata,None
    paths=list(root.glob('disclosures/**/*.metadata.json'))
    if market=='kr':paths.extend(root.glob('public_documents/**/*.metadata.json'))
    with ThreadPoolExecutor(max_workers=8) as pool:
        for count,(metadata,issue) in enumerate(pool.map(prepare_source,paths),1):
            if issue:reviews.append(issue)
            if metadata is not None:
                sources.append(metadata)
            if count%1000==0:print(f'[SPLITS] verified market={market} documents={count}/{len(paths)}',flush=True)
    # API archives initially know only their receipt; a public DART main page
    # supplies explicit links to the other versions of the same disclosure.
    # Join those identities before choosing the latest version, including a
    # withdrawal with no operative table. Sources beyond cutoff were excluded
    # above, and every relationship remains scoped to the same security.
    parents={}
    def find(key):
        parents.setdefault(key,key)
        while parents[key]!=key:
            parents[key]=parents[parents[key]]
            key=parents[key]
        return key
    def source_key(metadata):
        sid=metadata.get('security_id')
        family=metadata.get('family_id')
        if market=='kr' and metadata.get('provider')=='DART':
            receipt=str(family or '').removeprefix('api:')
            if re.fullmatch(r'\d{14}',receipt):return (sid,'dart_receipt',receipt)
        if market=='us':family=metadata.get('document_id',str(metadata['_path']))
        return (sid,'family',family)
    if market=='kr':
        for metadata in sources:
            if metadata.get('provider')!='DART':continue
            anchor=source_key(metadata)
            for receipt in [metadata['source_id'],*metadata.get('family',[])]:
                if not re.fullmatch(r'\d{14}',str(receipt)):continue
                related=(metadata.get('security_id'),'dart_receipt',str(receipt))
                parents[find(related)]=find(anchor)
    family_members={}
    def version_order(metadata):
        return (metadata['published_date'],metadata['source_id'],
                metadata.get('source_validation') not in {'pending','failed'})
    for metadata in sources:
        key=find(source_key(metadata))
        family_members.setdefault(key,set()).add(metadata['source_id'])
        if key not in families or version_order(metadata)>version_order(families[key]):
            families[key]=metadata
    # First keep the final document of each official correction family. A
    # cancellation with no operative table must remove its earlier proposal.
    for family_key,metadata in families.items():
        if metadata.get('source_validation') in {'pending','failed'}:
            item={key:metadata.get(key) for key in ['source_id','security_id','source_url','source_sha256','family_id']}
            item.update(reason=['official_document_unavailable'],
                        validation_error=metadata.get('validation_error','Document validation is pending'),
                        blocked_family_source_ids=sorted(family_members[family_key]))
            reviews.append(item)
            observations.append({**item,'events':[],'issues':item['reason']})
            continue
        family=metadata.get('family_id') if market=='kr' else metadata.get('document_id',str(metadata['_path']))
        if market=='kr':
            preliminary,_=parse_kr_share_unit_change(metadata['_path'].read_bytes(),source_title=metadata.get('title',''),**{k:metadata[k] for k in ['security_id','source_id','source_url','published_date']})
            if preliminary and preliminary[0].announcement_date:
                family=f'board:{preliminary[0].announcement_date}'
        key=(metadata.get('security_id'),family)
        if key not in selected or (metadata['published_date'],metadata['source_id'])>(selected[key]['published_date'],selected[key]['source_id']):
            selected[key]=metadata
    kr_prices={};resumptions={};capital_listings={}
    if market=='kr':
        for meta in selected.values():
            capital=parse_kind_capital_listing(meta['_path'].read_bytes(),security_id=meta['security_id'],source_title=meta.get('title',''))
            if capital:capital_listings[(meta['security_id'],capital['effective_date'])]=(capital,meta)
            actual_listing=parse_kind_listing(meta['_path'].read_bytes(),security_id=meta['security_id'])
            reference=parse_kind_reference_price(meta['_path'].read_bytes(),security_id=meta['security_id'])
            resumed=reference or parse_dart_resumption(meta['_path'].read_bytes()) or actual_listing
            if resumed:
                key=(meta['security_id'],resumed['effective_date'])
                if key not in resumptions or 'reference_price' in resumed:
                    resumptions[key]=(resumed,meta)
            executed=actual_listing or (reference if reference and 'ratio' in reference else None)
            if executed:
                ratio=Decimal(executed['ratio'])
                event=SplitEvent(security_id=meta['security_id'],effective_date=executed['effective_date'],
                                 new_shares=str(ratio),old_shares='1',source=meta['provider'],source_id=meta['source_id'],
                                 source_url=meta['source_url'],source_sha256=meta['source_sha256'],published_date=meta['published_date'],
                                 action_type='split' if ratio>1 else 'reverse_split',evidence=executed['reason'])
                events.append(event)
                observations.append({'source_id':meta['source_id'],'source_url':meta['source_url'],'source_sha256':meta['source_sha256'],
                                     'events':[event.to_dict()],'issues':[],'evidence_type':'actual_exchange_listing_or_reference_notice'})
    for meta in selected.values():
        item={'source_id':meta['source_id'],'source_url':meta['source_url'],'source_sha256':meta['source_sha256']}
        if not meta.get('security_id'):
            reviews.append(item|{'reason':'ambiguous_listed_share_class','symbols':meta.get('symbols')});continue
        parser=parse_kr_share_unit_change if market=='kr' else parse_edgar_split
        if market=='kr' and (parse_dart_resumption(meta['_path'].read_bytes()) or parse_kind_reference_price(meta['_path'].read_bytes()) or parse_kind_listing(meta['_path'].read_bytes(),security_id=meta['security_id'])):continue
        found,issues=meta['_parsed'] if '_parsed' in meta else parser(meta['_path'].read_bytes(),
            **({'source_title':meta.get('title','')} if market=='kr' else {}),
            **{k:meta[k] for k in ['security_id','source_id','source_url','published_date']})
        if market=='kr' and found and found[0].capital_action_kind=='capital_reduction_unpaid':
            listing=capital_listings.get((found[0].security_id,found[0].effective_date))
            if listing:
                source=listing[1]
                found=[replace(found[0],status='confirmed',evidence=found[0].evidence+' | Actual listing: '+source['source_url'])]
                item['execution_source_url']=source['source_url'];item['execution_source_sha256']=source['source_sha256']
            else:issues.append('capital_reduction_execution_not_confirmed')
            observations.append(item|{'events':[e.to_dict() for e in found],'issues':issues})
            events.extend(e for e in found if e.status=='confirmed' and e.effective_date<=cutoff)
            if issues:reviews.append(item|{'security_id':meta['security_id'],'reason':issues,'candidate_events':[e.to_dict() for e in found]})
            continue
        if market=='kr' and found:
            symbol=meta['stock_code']
            resumption=resumptions.get((found[0].security_id,found[0].effective_date))
            price_path=DATA_LAKE.bronze('krx','price',f'kr_{symbol}.csv')
            if price_path.exists():
                if symbol not in kr_prices:kr_prices[symbol]=read_kr_price(price_path,symbol)
                reference=resumption[0].get('reference_price') if resumption else None
                confirmed,issues=confirm_dart_with_prices(found[0],kr_prices[symbol],reference_price=reference,
                                    reference_exchange=resumption[0].get('exchange') if resumption else None)
                found=[confirmed]
                item['execution_price_source_sha256']=sha256_file(price_path)
                item['execution_price_source']=str(price_path.resolve())
            if resumption:
                resume,source=resumption
                if ('ratio' not in resume or Decimal(resume['ratio'])==found[0].ratio) and resume.get('action_type',found[0].action_type)==found[0].action_type:
                    # A reference price without an explicit ratio must also
                    # pass the candidate-ratio/quote-grid check above.
                    if 'reference_price' not in resume or 'ratio' in resume or found[0].status=='confirmed':
                        found=[replace(found[0],status='confirmed',evidence=found[0].evidence+' | Official exchange execution: '+source['source_url'])]
                    item['resumption_source_url']=source['source_url']
                    item['resumption_source_sha256']=source['source_sha256']
                    if 'reference_price' in resume:item['exchange_reference_price']=resume['reference_price']
                else:
                    found=[replace(found[0],status='announced')]
                    issues.append('official_listing_ratio_conflict')
        observations.append(item|{'events':[e.to_dict() for e in found],'issues':issues,'family_id':meta.get('family_id')})
        for event in found:
            if event.status=='confirmed' and event.effective_date<=cutoff:events.append(event)
        if issues or any(e.status!='confirmed' for e in found):
            reviews.append(item|{'security_id':meta['security_id'],'reason':issues or ['execution_not_confirmed'],
                                 'candidate_events':[e.to_dict() for e in found]})
    listing_episodes=[]
    if market=='kr':
        from engine.transformers.corporate_action_evidence import validate_evidence
        linked,listing_episodes,link_reviews=validate_evidence(root,as_of=cutoff)
        events.extend(linked);reviews.extend(link_reviews)
        observations.extend({'events':[e.to_dict()],'evidence_type':'reviewed_official_source_link'} for e in linked)
    # Fail closed for each conflicting security/date without discarding other issuers.
    grouped={}
    for event in events:grouped.setdefault((event.security_id,event.share_class,event.effective_date),[]).append(event)
    resolved=[]
    for key,group in grouped.items():
        try:resolved.extend(resolve_events(group,as_of=cutoff))
        except ValueError as exc:reviews.append({'security_id':key[0],'effective_date':key[2],'reason':str(exc),'events':[e.to_dict() for e in group]})
    alpha=[];alpha_issues=[]
    if market=='us':
        alpha,alpha_issues=load_alpha_split_candidates(as_of=cutoff)
        by_key={(e.security_id,e.effective_date):e for e in resolved}
        for candidate in alpha:
            official=by_key.get((candidate['security_id'],candidate['effective_date']))
            if official:
                # AV reports four decimal places. This is a comparison tolerance, not a new split ratio.
                agrees=abs(official.ratio-Decimal(candidate['reported_ratio']))<=Decimal('0.00005')
                candidate['validation']='official_match' if agrees else 'official_conflict'
                candidate['official_event_id']=official.event_id
                if not agrees:reviews.append(candidate|{'reason':'alpha_official_ratio_conflict'})
            else:candidate['validation']='awaiting_official_confirmation'
    payload={'schema_version':SCHEMA_VERSION,'parser_version':PARSER_VERSION,
             'parser_code_sha256':sha256_file(Path(parse_edgar_split.__code__.co_filename)),
             'market':market,'as_of':cutoff,
             'created_at':datetime.now(timezone.utc).isoformat(),'events':[e.to_dict() for e in sorted(resolved,key=lambda e:(e.security_id,e.effective_date))],
             'observations':observations,'review':reviews,'listing_episodes':listing_episodes,'alpha_vantage_candidates':alpha,'alpha_vantage_issues':alpha_issues,
             'coverage_complete':False,'coverage_note':'Event extraction is not proof of exhaustive corporate-action coverage. Unresolved sources and independent candidates remain in this ledger.'}
    if market=='us':
        from engine.transformers.us_split_evidence import supplement_ledger
        payload=supplement_ledger(payload,root)
    save_json(Path(output_path or ledger_path(market)),payload)
    print(f'[SPLITS] normalized market={market} confirmed={len(resolved)} review={len(reviews)} alpha_candidates={len(alpha)}',flush=True)
    return payload


def price_panel_dir(market):
    return DATA_LAKE.silver('corporate_actions','prices',market)


def rebuild_manifest_path(market,*,data_lake=None):
    return (data_lake or DATA_LAKE).silver('corporate_actions',f'{market}_price_rebuild_required.json')


def normalized_price_frame(market,*,symbols=None,output_path=None):
    """Adapt audited panels to the existing market loader's nine-column contract.

    close/OHLC/volume stay as traded; adj_close is split-only for both markets.
    Alpha Vantage's split+dividend close remains in the provenance-rich parquet.
    """
    columns=['security_id','trade_date','open','high','low','close','volume','adj_close','currency']
    wanted=set(symbols) if symbols else None
    frames=[]
    for path in sorted(price_panel_dir(market).glob('*.parquet')):
        if wanted is not None and path.stem.removeprefix(f'{market}_') not in wanted:continue
        meta_path=path.with_suffix('.metadata.json')
        if meta_path.exists():
            meta=json.loads(meta_path.read_text(encoding='utf-8'))
            if meta.get('status')=='failed':raise ValueError(f'price panel is quarantined: {path.name}')
        frame=pd.read_parquet(path)
        frame['adj_close']=frame.split_adj_close
        frames.append(frame[columns])
    if not frames:raise ValueError(f'no split-adjusted {market} price panels are available')
    result=pd.concat(frames,ignore_index=True)
    if output_path is not None:
        output=Path(output_path)
        combined=result
        if symbols is not None and output.exists():
            existing=pd.read_csv(output)
            existing['trade_date']=pd.to_datetime(existing.trade_date)
            # A scoped refresh must not remove the rest of the market's history.
            existing=existing[~existing.security_id.isin(result.security_id.unique())]
            combined=pd.concat([existing[columns],result],ignore_index=True)
        write_source_dataframe(output,combined,source=f'{market}-split-adjusted-normalized-prices',index=False)
    return result


def _atomic_parquet(frame,path):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    frame.to_parquet(temporary,index=False)
    temporary.replace(path)


def build_split_price_panels(market,ledger,*,symbols=None,as_of=None,refresh_us=True):
    from engine.extractors.alpha_vantage_prices import parse_daily_payload, price_root
    cutoff=str(pd.Timestamp(as_of or ledger['as_of']).date())
    fields=set(SplitEvent.__dataclass_fields__)
    events=[SplitEvent(**{k:v for k,v in e.items() if k in fields}) for e in ledger['events']]
    event_map={}
    for event in events:event_map.setdefault(event.security_id,[]).append(event)
    alpha_map={}
    for candidate in ledger.get('alpha_vantage_candidates',[]):alpha_map.setdefault(candidate['security_id'],[]).append(candidate)
    source_root=DATA_LAKE.bronze('krx','price') if market=='kr' else price_root()
    output=price_panel_dir(market);output.mkdir(parents=True,exist_ok=True)
    wanted=set(symbols) if symbols else None
    report={'market':market,'as_of':cutoff,'processed':[],'errors':[],'coverage_complete':False}
    attempted=set()
    for path in sorted(source_root.glob('*.csv' if market=='kr' else '*.json')):
        if market=='us' and (path.name.endswith('.metadata.json') or path.name=='download_report.json'):continue
        symbol=path.stem.removeprefix('kr_') if market=='kr' else path.stem.removeprefix('ticker=')
        if wanted is not None and symbol not in wanted:continue
        attempted.add(symbol)
        sid=f'SEC_{market.upper()}_{symbol}'
        selected=event_map.get(sid,[])
        source_digest=sha256_file(path)
        episodes=[e for e in ledger.get('listing_episodes',[]) if e['security_id']==sid]
        overrides=[e for e in ledger.get('official_vendor_overrides',[]) if e['security_id']==sid]
        return_reviews=[e for e in ledger.get('return_review_events',[]) if e['security_id']==sid]
        event_digest=hashlib.sha256(json.dumps({'events':[e.to_dict() for e in selected],
                                              'listing_episodes':episodes,
                                              **({'official_vendor_overrides':overrides,'return_review_events':return_reviews}
                                                 if overrides or return_reviews else {})},sort_keys=True).encode()).hexdigest()
        manifest_path=output/f'{market}_{symbol}.metadata.json'
        panel_path=output/f'{market}_{symbol}.parquet'
        try:
            if manifest_path.exists() and panel_path.exists():
                old=json.loads(manifest_path.read_text(encoding='utf-8'))
                if all(old.get(k)==v for k,v in {'source_sha256':source_digest,'event_sha256':event_digest,'as_of':cutoff,'parser_version':PARSER_VERSION,'panel_path':str(panel_path.resolve()),'status':'ready'}.items()):
                    report['processed'].append(old);continue
            if market=='kr':
                frame=read_kr_price(path,symbol)
                basis='raw';vendor_digest='';vendor_actions=[]
            else:
                payload=json.loads(path.read_text(encoding='utf-8'))
                frame=parse_daily_payload(payload,symbol=symbol)
                vendor_digest=source_digest
                vendor_actions=[]
                daily=payload['Time Series (Daily)']
                official={e.effective_date:e for e in selected}
                supplemental=[]
                replacements={}
                for proof in overrides:
                    prior=proof.get('vendor_action_replacement')
                    if not prior:continue
                    day=prior['effective_date'];target=official.get(proof['effective_date'])
                    if (prior['source_sha256']!=source_digest or target is None or proof['event_id']!=target.event_id
                            or day in official or day not in daily
                            or str(daily[day]['8. split coefficient'])!=prior['reported_ratio']):
                        raise ValueError('Reviewed vendor-date replacement no longer matches its source')
                    ratio=Decimal(prior['reported_ratio'])
                    if ratio<=0 or abs(target.ratio-ratio)/ratio>Decimal('0.0001'):
                        raise ValueError('Reviewed vendor-date replacement share ratio differs')
                    if abs((pd.Timestamp(day)-pd.Timestamp(target.effective_date)).days)>7:
                        raise ValueError('Vendor-date replacement exceeds reviewed event window')
                    replacements[day]=proof
                for day,row in daily.items():
                    ratio=Decimal(row['8. split coefficient'])
                    if ratio==1 or day>cutoff:continue
                    vendor_actions.append({'effective_date':day,'ratio':str(ratio)})
                    if day in replacements:continue
                    if day in official:
                        if abs(official[day].ratio-ratio)/ratio>Decimal('0.0001'):
                            proof=next((e for e in overrides if e['event_id']==official[day].event_id),None)
                            if proof is None:raise ValueError(f'EDGAR/Alpha Vantage split conflict on {day}')
                        continue
                    supplemental.append(SplitEvent(security_id=sid,effective_date=day,new_shares=str(ratio),old_shares='1',
                                      source='ALPHA_VANTAGE',source_id=f'{symbol}:{day}',
                                      source_url=f'https://www.alphavantage.co/query?function=TIME_SERIES_DAILY_ADJUSTED&symbol={symbol}',
                                      source_sha256=source_digest,published_date=cutoff,
                                      action_type='split' if ratio>1 else 'reverse_split',
                                      evidence='Executed daily split coefficient from Alpha Vantage; vendor precision retained.'))
                for event in selected:
                    if frame.trade_date.min()<=pd.Timestamp(event.effective_date)<=frame.trade_date.max() and event.effective_date not in {x['effective_date'] for x in vendor_actions}:
                        proof=next((e for e in overrides if e['event_id']==event.event_id
                                    and (e.get('allow_missing_vendor_action') or e.get('vendor_action_replacement'))),None)
                        actual=frame.loc[frame.trade_date.eq(pd.Timestamp(event.effective_date))]
                        executable=not actual.empty and actual.volume.gt(0).all()
                        if proof and proof.get('market_date_confirmation')=='FINRA_exDate':
                            # An official event can occur on a day absent from
                            # the vendor history. Keep that missing quote; the
                            # exact share units still apply to earlier prices.
                            executable=frame.loc[frame.trade_date.ge(pd.Timestamp(event.effective_date))].volume.gt(0).any()
                        if proof and proof.get('execution_bridge'):
                            bridge=proof['execution_bridge']
                            following=frame.loc[frame.trade_date.gt(pd.Timestamp(bridge['legal_effective_date']))&frame.volume.gt(0)]
                            executable=not following.empty and str(following.trade_date.min().date())==event.effective_date
                        if proof is None or not executable:
                            raise ValueError(f'EDGAR trading date absent from Alpha Vantage action history: {event.effective_date}')
                selected=selected+supplemental
                basis='raw'
            frame=frame[pd.to_datetime(frame.trade_date)<=pd.Timestamp(cutoff)].copy()
            if frame.empty:raise ValueError('no prices on or before as_of')
            adjusted=adjust_prices(frame,selected,as_of=cutoff,price_basis=basis)
            boundaries=pd.DatetimeIndex(sorted({e['effective_date'] for e in episodes if e['effective_date']<=cutoff}))
            adjusted['listing_episode']=np.searchsorted(boundaries.to_numpy(),adjusted.trade_date.to_numpy(),side='right')
            if return_reviews:
                adjusted['return_review_event']=adjusted.trade_date.isin(pd.to_datetime([
                    e['effective_date'] for e in return_reviews if e.get('kind')!='price_source_review']))
                adjusted['price_source_review_event']=False
                for review in return_reviews:
                    if review.get('kind')!='price_source_review':continue
                    observed=adjusted.loc[adjusted.trade_date.ge(pd.Timestamp(review['effective_date']))&adjusted.volume.gt(0)]
                    if not observed.empty:
                        adjusted.loc[adjusted.trade_date.eq(observed.trade_date.min()),'price_source_review_event']=True
            recompute_from=None
            if panel_path.exists():
                previous=pd.read_parquet(panel_path,columns=['trade_date','close','split_adj_close'])
                shared=previous.merge(adjusted[['trade_date','close','split_adj_close']],on='trade_date',suffixes=('_old','_new'))
                changed=np.zeros(len(shared),dtype=bool)
                for column in ['close','split_adj_close']:
                    old=pd.to_numeric(shared[f'{column}_old'],errors='coerce')
                    new=pd.to_numeric(shared[f'{column}_new'],errors='coerce')
                    changed |= ~np.isclose(old,new,rtol=1e-10,atol=1e-10,equal_nan=True)
                if changed.any():recompute_from=str(shared.loc[changed,'trade_date'].min().date())
            elif market=='us' or selected:
                recompute_from=str(adjusted.trade_date.min().date())
            if episodes:
                # Resetting technical history changes factors even if prices do not.
                recompute_from=min(recompute_from or '9999-12-31',str(boundaries.min().date()))
            discrepancies=[]
            if market=='us':
                va={v['effective_date']:Decimal(v['ratio']) for v in vendor_actions}
                for event in selected:
                    if frame.trade_date.min()<=pd.Timestamp(event.effective_date)<=frame.trade_date.max():
                        if event.effective_date not in va or abs(va[event.effective_date]-event.ratio)/event.ratio>Decimal('0.0001'):
                            discrepancies.append({'event_id':event.event_id,'reason':'official_vendor_action_mismatch'})
            changes=adjusted.split_adj_close.pct_change(fill_method=None)
            unresolved=int(((changes>1)|(changes<-.7)).sum())
            meta={'symbol':symbol,'security_id':sid,'as_of':cutoff,'source_sha256':source_digest,'status':'ready',
                  'price_provider':'KRX' if market=='kr' else 'ALPHA_VANTAGE','vendor_source_sha256':vendor_digest,'event_sha256':event_digest,'parser_version':PARSER_VERSION,
                  'price_basis':basis,'rows':len(adjusted),'events':len(selected),'unresolved_extreme_moves':unresolved,
                  'official_vendor_discrepancies':discrepancies,'vendor_actions':vendor_actions,
                  'official_vendor_overrides':overrides,'return_review_events':return_reviews,
                  'recompute_from':recompute_from,
                  'panel_path':str(panel_path.resolve()),'coverage_complete':False,
                  'price_semantics':'split-only price; excludes cash dividends and complex corporate-action wealth adjustments'}
            _atomic_parquet(adjusted,panel_path)
            save_json(manifest_path,meta)
            report['processed'].append(meta)
        except Exception as exc:
            report['errors'].append({'symbol':symbol,'error':f'{type(exc).__name__}: {exc}'})
            save_json(manifest_path,{'symbol':symbol,'as_of':cutoff,'status':'failed','error':str(exc)})
        count=len(report['processed'])+len(report['errors'])
        if count%25==0:print(f'[SPLITS] prices market={market} processed={len(report["processed"])} errors={len(report["errors"])}',flush=True)
    for symbol in sorted((wanted or set())-attempted):
        report['errors'].append({'symbol':symbol,'error':'requested source price history is missing'})
    dirty_path=rebuild_manifest_path(market)
    dirty=json.loads(dirty_path.read_text(encoding='utf-8')) if dirty_path.exists() else {'market':market,'items':{}}
    for item in report['processed']:
        if item.get('recompute_from'):
            sid=item['security_id'];previous=dirty['items'].get(sid,{})
            signature=item['source_sha256']+item['event_sha256']
            if previous.get('signature')!=signature:
                dirty['items'][sid]={'from_date':min(item['recompute_from'],previous.get('from_date','9999-12-31')),
                                     'signature':signature,'completed_bases':[],'snapshots_rebuilt':False}
    if dirty['items']:save_json(dirty_path,dirty)
    save_json(output/'build_report.json',report)
    return report


def run_stock_split_refresh(*,market,symbols=None,end_date=None,start_date='20100101',
                           download=True,build_prices=True,refresh_us=True,force=False,gold_dir=None):
    cutoff=str(pd.Timestamp(end_date or date.today()).date())
    if download:
        report=download_stock_splits(market=market,symbols=symbols,start_date=start_date,end_date=cutoff,force=force)
        if report.get('errors'):print(f'[SPLITS] download review items={len(report["errors"])}',flush=True)
        if market=='kr':
            from engine.extractors.kind_stock_splits import download_reviewed_kind_sources
            download_reviewed_kind_sources(as_of=cutoff,symbols=symbols,force=force)
        else:
            from engine.transformers.us_split_evidence import download_reviewed_sources
            reviewed=download_reviewed_sources(root=root_for('us'),as_of=cutoff,symbols=symbols)
            if reviewed['errors']:print(f'[SPLITS] reviewed EDGAR source errors={len(reviewed["errors"])}',flush=True)
    if market=='us' and refresh_us and build_prices:
        from engine.extractors.alpha_vantage_prices import download_alpha_vantage_prices
        download_alpha_vantage_prices(symbols=symbols,as_of=cutoff,force=force)
    ledger=normalize_split_ledger(market,as_of=cutoff)
    prices=build_split_price_panels(market,ledger,symbols=symbols,as_of=cutoff,refresh_us=refresh_us) if build_prices else None
    if market=='us' and build_prices:
        from engine.transformers.sec_shares import build_disclosed_shares
        build_disclosed_shares(symbols=symbols,as_of=cutoff)
    from engine.core.serving_storage import export_json, export_prices
    gold = Path(gold_dir or DATA_LAKE.gold('corporate_actions', market))
    artifacts = [export_json(gold / 'stock_splits.json', {
        'market': market, 'as_of': cutoff, 'events': ledger['events'],
        'review': ledger['review'], 'coverage_complete': False})]
    for metadata in prices['processed'] if prices else []:
        filename = f"{market}_{metadata['symbol']}.parquet"
        artifacts.append(export_prices(gold / 'prices' / filename,
                                       pd.read_parquet(price_panel_dir(market) / filename)))
    result = {'ledger_path':str(ledger_path(market)),'events':len(ledger['events']),'review':len(ledger['review']),
              'price_panels':len(prices['processed']) if prices else 0,'price_errors':prices['errors'] if prices else [],
              'market': market, 'as_of': cutoff, 'coverage_complete': False,
              'gold_dir': str(gold.resolve()), 'artifacts': artifacts}
    export_json(gold / 'summary.json', result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--market',required=True,choices=['kr','us'])
    parser.add_argument('--symbols')
    parser.add_argument('--start-date',default='20100101')
    parser.add_argument('--end-date',default=str(date.today()))
    parser.add_argument('--skip-download',action='store_true')
    parser.add_argument('--skip-prices',action='store_true')
    parser.add_argument('--no-us-price-refresh',action='store_true')
    parser.add_argument('--force',action='store_true')
    parser.add_argument('--gold-output',type=Path,help='User-facing split and price export directory.')
    args=parser.parse_args()
    with SourceRefreshLock(args.market),SourceArchiveSession(market=args.market,run_id=new_source_run_id()):
        result=run_stock_split_refresh(market=args.market,symbols=args.symbols.split(',') if args.symbols else None,
                                      end_date=args.end_date,start_date=args.start_date,download=not args.skip_download,
                                      build_prices=not args.skip_prices,refresh_us=not args.no_us_price_refresh,force=args.force,
                                      gold_dir=args.gold_output)
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
