"""Validate source proofs for the eight researched cases, without changing a ledger."""
from pathlib import Path
import datetime, hashlib, json, re, sys
ROOT = Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parents[2]))
from engine.transformers.stock_splits import text_of_html

manifest = json.loads((ROOT/'manifest.json').read_text('utf-8'))
by_name = {(m['symbol'],Path(m['path']).name):m for m in manifest}

def norm(s): return re.sub(r'\s+','',s).casefold()
def proof(symbol,name,roles,markers,location):
    m = by_name[(symbol,name)]
    value = {k:m[k] for k in ['provider','cik','source_id','document_id','published_date','source_url','source_sha256','security_id']}
    value.update(filename=name.split('_',1)[1],local_path=m['path'],roles=roles,markers=markers,location=location)
    return value

def finra(symbol,list_id):
    m=next(x for x in manifest if x['symbol']==symbol and x['provider']=='FINRA' and x['record_ids']==[list_id])
    row=next(x for x in json.loads(Path(m['path']).read_text('utf-8')) if x['OTCDailyListID']==list_id)
    value={k:m[k] for k in ['provider','cik','source_id','document_id','source_url','source_sha256','security_id','request_body']}
    value.update(local_path=m['path'],published_date=row['dailyListDatetime'][:10],
        roles=['trading_date','market_effective_date_confirmation'],finra_list_id=list_id,
        markers=[f'"OTCDailyListID":{list_id}',f'"exDate":"{row["exDate"]}"',f'"reverseSplitRate":"{row["reverseSplitRate"]}"'],
        location='JSON record selected by OTCDailyListID; exDate is actual market effective date, calendarDay is partition',
        json_record_assertions={k:row.get(k) for k in ['OTCDailyListID','dividendMasterID','dailyListDatetime','calendarDay','exDate','reverseSplitRate','dailyListEventCode','commentText','oldSymbolCode','newSymbolCode']})
    return value

def event(symbol,cik,new,old,market,legal,time,venue,historical=None):
    return {'symbol':symbol,'security_id':'SEC_US_'+symbol,'cik':cik,'share_class':'common',
        'new_shares':str(new),'old_shares':str(old),'new_to_old_exact':f'{new}/{old}',
        'first_split_adjusted_trading_date':market,'legal_effective_date':legal,
        'legal_effective_local_time':time,'venue_at_action':venue,'historical_symbol':historical or symbol,
        'status':'official_actual_market_date_confirmed','price_inference_used':False,
        'sources':[],'superseded_candidates':[],'limitations':[]}

def prior(symbol,name,date,proof_source,marker,kind):
    m=by_name[(symbol,name)]
    return {'effective_date':date,**{k:m[k] for k in ['source_id','source_url','source_sha256','published_date']},
        'retain_in_active_adjustment_ledger':False,'supersession_kind':kind,
        'proof_source_url':proof_source['source_url'],'proof_marker':marker}

events=[]
e=event('JXG','1546383',1,15,'2017-02-09',None,None,'Nasdaq','KBSF')
p=proof('JXG','0001213900-21-026934_f20f2020_kbsfashion.htm',
    ['exact_ratio','actual_execution','trading_date','supersedes_prior_schedule'],
    ['one-for-fifteen (1-for-15) reverse stock split','when the market opened on February 9, 2017'],
    'Introduction and Item 9.A; past-tense commencement plus February 2017 board/actual split discussion')
e['ratio_marker']=p['markers'][0]
e['sources']=[p,proof('JXG','0001144204-17-005951_v458403_ex99-2.htm',['prior_schedule'],
    ['when the market opens on February 8, 2017'],'Original February 3 press release'),
    proof('JXG','0001213900-25-043744_ea0239227-20f_jxluxven.htm',['historical_security_identity'],
    ['Since December 20, 2024, our Common Stock is trading on the Nasdaq Capital Market under the symbol “JXG.”'],
    'Item 9.A ticker chronology, KBSF to LLL to JXJT to JXG')]
e['superseded_candidates']=[prior('JXG','0001144204-17-005951_v458403_ex99-2.htm','2017-02-08',p,p['markers'][1],'announced_date_replaced_by_later_explicit_actual_trading_date')]
e['limitations']=['The later report confirms actual trading on February 9, but a separate explicit cancellation announcement for February 8 was not located. Legal effective date is not resolved here.']
events.append(e)

e=event('PMI','2030617',1,50,None,'2026-07-31','17:00 Eastern Daylight Time','NYSE American')
e.update(status='actual_share_unit_execution_confirmed_market_date_bridge_needed',announced_first_split_adjusted_trading_date='2026-08-03',
    date_bridge_basis='Executed July 31 contract plus explicit 17:00 EDT operative clause plus announced August 3 open; caller must separately verify first positive-volume quote and series identity')
p=proof('PMI','0001437749-26-028565_pmi20260630_10q.htm',['exact_ratio','actual_execution'],
    ['at a ratio of 1-for-50','The Reverse Stock Split became effective commencing July 31, 2026.'],
    'Note 1, Basis of presentation; Note 14 also states completed July 31')
e['ratio_marker']=p['markers'][0]
e['sources']=[p,proof('PMI','0001437749-26-024025_ex_991235.htm',['operative_legal_time'],
    ['Effective at 5:00 p.m. Eastern Daylight Time on July 31, 2026'],
    'Certificate Section 2, share-unit conversion operative time; Section 4 amendment filing effectiveness is distinct'),
    proof('PMI','0001437749-26-024025_pmi20260721_8k.htm',['announced_trading_date','same_venue_and_symbol'],
    ['commencing at market open on August 3, 2026','under the existing trading symbol “PMI”'],
    'Item 8.01 prospective market opening on existing NYSE American ticker'),
    proof('PMI','0001829126-25-006957_picardmedical_424b4.htm',['historical_security_identity'],
    ['This is the initial public offering of common stock of Picard Medical, Inc','The date of this prospectus is August 29, 2025.'],
    'IPO prospectus cover, SEC filing date September 2, 2025')]
e['limitations']=['No contemporaneous or subsequent official document stating that actual market trading commenced on August 3 was located. No trading-resumption claim is made. The later execution confirmation states July 31 but does not repeat the intraday time; the time comes from the operative filed charter clause. The first-traded quote bridge and Alpha price units remain caller validations.']
events.append(e)

e=event('POCI','867840',1,3,'2022-11-02','2022-11-01','23:59 Eastern Time','OTCQB','PEYE')
p=proof('POCI','0001683168-22-007213_poci_ex9903.htm',['exact_ratio','actual_execution'],
    ['planned 1-for-3 reverse split of its common stock became effective after the close of business on Tuesday, November 1, 2022'],
    'November 2 completed-event press release opening paragraph')
e['ratio_marker']=p['markers'][0]
s=proof('POCI','0001683168-22-007213_poci_ex9902.htm',['supersedes_prior_schedule'],
    ['amending the date for its reverse stock split','due to an unanticipated delay in obtaining necessary regulatory clearances'],
    'October 27 announcement explicitly delays earlier October 26 legal/October 27 market plan')
e['sources']=[p,proof('POCI','0001683168-22-007213_poci_8k.htm',['actual_execution','trading_date'],
    ['Common Stock began trading on a reverse stock split-adjusted basis on the OTCQB on November 2, 2022'],
    'Item 5.03 actual trading paragraph; preceding paragraphs explain withdrawn and refiled amendments'),s,finra('POCI',247068),
    proof('POCI','0001683168-22-007213_poci_ex9901.htm',['prior_schedule','historical_security_identity'],
    ['when the market opens on October 27, 2022','the trading symbol for the shares will change to “POCI.”'],
    'Original October 26 plan and planned Nasdaq symbol after PEYE/PEYED')]
e['superseded_candidates']=[prior('POCI','0001683168-22-007213_poci_ex9901.htm','2022-10-27',s,s['markers'][0],'explicitly_delayed_prior_schedule')]
e['limitations']=['Alpha reported split coefficient on November 3, one day after both official actual trading confirmation and FINRA exDate. Parent also observes a positive-volume November 2 quote apparently still in earlier units. Moving only the coefficient can create a false one-day loss/recovery; independently verify source price units before price adjustment. No second split on November 3 is supported by these documents.']
events.append(e)

e=event('RDGL','1449349',1,8,'2019-06-28','2019-06-25','23:59 Eastern Time','OTC PINK')
p=proof('RDGL','0001493152-19-010114_form8-k.htm',['exact_ratio','actual_execution','trading_date'],
    ['1-for-8','at the opening of trading on June 28, 2019 under the symbol “RDGLD.”'],
    'Item 5.03: completed reverse split and FINRA approval June 27')
e['ratio_marker']='1-for-8'
s=proof('RDGL','0001493152-19-010114_ex99-2.htm',['supersedes_prior_schedule'],
    ['to correct its press release dated June 26, 2019','when the markets open on June 28, 2019'],
    'Corrective release dated June 27')
e['sources']=[p,s,finra('RDGL',156947),proof('RDGL','0001493152-19-010114_ex99-1.htm',['prior_schedule'],
    ['will begin trading on a post-split basis on June 26, 2019'],'Original June 26 release')]
e['superseded_candidates']=[prior('RDGL','0001493152-19-010114_ex99-1.htm','2019-06-26',s,s['markers'][0],'explicit_corrective_release')]
events.append(e)

e=event('SHIP','1448397',1,15,'2011-06-27','2011-06-24',None,'Nasdaq Global Market')
p=proof('SHIP','0000919574-11-004401_d1218694_6-k.htm',['exact_ratio','actual_execution','trading_date'],
    ['1-for-15 reverse split','The shares commenced trading on a split-adjusted basis on June 27, 2011.'],
    'Exhibit earnings release, Reverse Stock Split section; past-tense actual commencement')
e['ratio_marker']='1-for-15 reverse split'
e['sources']=[p,proof('SHIP','0000919574-11-003908_d1207198_6-k.htm',['announced_trading_date','exact_ratio_correspondence'],
    ['1-for-15 reverse split','at the opening of trading on June 27, 2011'],
    'Original June 23 reverse-split press release'),
    proof('SHIP','0000919574-11-003954_d1207693_6-k.htm',['operative_legal_date'],
    ['Effective with the commencement of business on June 24, 2011','1 for 15 reverse stock split'],
    'Filed Articles of Amendment operative paragraph')]
e['limitations']=['Alpha coefficient date is June 28, one day after the actual date stated in the subsequent 6-K. Parent observes positive-volume June 27 daily raw quotes apparently still in earlier units, while independently downloaded Alpha INTRADAY adjusted=false 2011-06 has a June 27 14:00 quote at 6.0 (volume1119), directly conflicting with daily June27 0.3488 (volume18000). Parent source: deliverables/cross_market_top70_20260909/corrected/price_source_checks/SHIP_TIME_SERIES_INTRADAY_2011-06.json. Official date alone cannot repair vendor quote units; avoid mechanical coefficient-only relocation. No second June28 split is supported. The 2011 issue precedes the 2017+ strategy research period but prevents claiming complete historical price validation.']
events.append(e)

e=event('SMTK','1817760',1,35,'2023-09-21','2023-09-21','00:01 Eastern Time','OTCQB')
p=proof('SMTK','0001104659-23-102289_tm2326434d1_8k.htm',['exact_ratio'],
    ['a ratio of 1-for-35 (the “Reverse Stock Split”)'],'Item 5.03 final board ratio, distinct from prior permitted range')
e['ratio_marker']=p['markers'][0]
s=proof('SMTK','0001104659-23-102289_tm2326434d1_ex99-2.htm',['supersedes_prior_schedule','announced_trading_date'],
    ['September 21, 2023 rather than September 20, 2023','market open at 9:30 a.m. on September 21, 2023'],
    'Corrected press release after further discussion with FINRA')
e['sources']=[p,s,finra('SMTK',268874),proof('SMTK','0001558370-24-004098_smtk-20231231x10k.htm',['actual_execution'],
    ['a one-for-thirty-five (1:35) reverse stock split effected on September 21, 2023'],
    'Note 2 Reverse Stock Split and audited financial statement footnotes'),
    proof('SMTK','0001104659-23-102289_tm2326434d1_ex3-2.htm',['operative_legal_time','legal_date_correction'],
    ['Effective Time of 12:01 AM, Eastern Time, on September 21, 2023'],
    'Certificate of Correction Section 4 replaces Section SECOND of original charter'),
    proof('SMTK','0001104659-23-102289_tm2326434d1_ex99-1.htm',['prior_schedule'],
    ['September 20, 2023'],'Original press release superseded by Exhibit 99.2')]
e['superseded_candidates']=[prior('SMTK','0001104659-23-102289_tm2326434d1_ex99-1.htm','2023-09-20',s,s['markers'][0],'explicitly_corrected_announcement_and_charter_date')]
e['limitations']=['FINRA proves market effective date, not a claim that halted trading resumed. Alpha has no 2023 split coefficient and parent observes missing September 21/22 rows plus an unusual first later quote on September 25. Validate the first observed quote unit separately; neither missing quote dates nor a price jump establish an event ratio.']
events.append(e)

e=event('SPRB','1683553',1,75,'2025-08-07','2025-08-04','17:00 Eastern Time','OTCQB')
p=proof('SPRB','0000950170-25-108868_sprb-ex99_1.htm',['exact_ratio','actual_execution','trading_date','supersedes_prior_schedule'],
    ['implemented a 1-for-75 reverse stock split','common stock began trading on a split-adjusted basis on the OTCQB on August 7, 2025'],
    'August 14 earnings release, Corporate Updates')
e['ratio_marker']=p['markers'][0]
e['sources']=[p,finra('SPRB',310719),proof('SPRB','0000950170-25-098340_sprb-ex99_1.htm',['prior_schedule','announced_legal_time'],
    ['effective at 5:00 p.m. Eastern Time on August 4, 2025','when the markets open on August 5, 2025'],
    'Original July 24 press release'),
    proof('SPRB','0001193125-26-097558_sprb-20251231.htm',['actual_execution','trading_date'],
    ['common stock began trading on the OTCQB on a split-adjusted basis on August 7, 2025'],
    '2025 10-K Note 1 Reverse Stock Split; final actual date')]
e['superseded_candidates']=[prior('SPRB','0000950170-25-098340_sprb-ex99_1.htm','2025-08-05',p,p['markers'][1],'announced_date_replaced_by_FINRA_and_later_actual_trading_confirmation')]
e['limitations']=['The final date is confirmed by FINRA and later execution disclosures; a separate release explicitly cancelling the August 5 forecast was not found. Temporary SPRBD is the same common stock. Later Nasdaq relisting is not another reverse split.']
events.append(e)

e=event('WLFC','1018164',3,1,'2026-07-21','2026-07-17','16:05 Eastern Time','Nasdaq')
p=proof('WLFC','0001018164-26-000068_wlfc-20260630.htm',['exact_ratio','actual_execution','trading_date','supersedes_prior_schedule'],
    ['effected a three-for-one forward stock split','Trading on a split-adjusted basis commenced on July 21, 2026.'],
    'Note 1 and Note 14 Subsequent Events, actual execution and actual market commencement')
e['ratio_marker']=p['markers'][0]
e['sources']=[p,proof('WLFC','0001193125-26-300912_d142690d8k.htm',['updated_announced_trading_date'],
    ['Trading of the Common Stock on a split-adjusted basis is expected to commence on or about July 21, 2026'],
    'July 10 8-K updates earlier forecast'),
    proof('WLFC','0001193125-26-307870_d85905dex31.htm',['operative_legal_time','exact_ratio_correspondence'],
    ['subdivided and reclassified into three (3)','effective at 4:05 p.m. Eastern Time on July 17, 2026'],
    'Filed certificate, conversion paragraph and Section 5'),
    proof('WLFC','0001193125-26-279754_d129925dex991.htm',['prior_schedule'],
    ['split-adjusted basis at market open on July 20, 2026'],'Original June 23 stockholder approval release')]
e['superseded_candidates']=[prior('WLFC','0001193125-26-279754_d129925dex991.htm','2026-07-20',p,p['markers'][1],'announced_date_updated_and_replaced_by_subsequent_actual_trading_confirmation')]
e['limitations']=['July 17 after-close charter effectiveness and July 21 market commencement are different fields. July 20 is the superseded forecast, not a separate additional split.']
events.append(e)

failures=[]
checked=[]
for m in manifest:
    raw=Path(m['path']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=m['source_sha256']:failures.append('source hash: '+m['path'])
    for key in ['source_url','source_id','published_date','security_id','cik']:
        if not m.get(key):failures.append('missing '+key+': '+m['path'])
for e in events:
    for p in e['sources']:
        raw=Path(p['local_path']).read_bytes()
        text=raw.decode('utf-8') if p['provider']=='FINRA' else text_of_html(raw)
        for marker in p['markers']:
            ok=norm(marker) in norm(text)
            checked.append({'symbol':e['symbol'],'source_url':p['source_url'],'marker':marker,'present':ok})
            if not ok:failures.append(e['symbol']+' missing literal '+marker)
        if p['provider']=='FINRA':
            rows=json.loads(raw)
            row=next(x for x in rows if x['OTCDailyListID']==p['finra_list_id'])
            assert all(row.get(k)==v for k,v in p['json_record_assertions'].items())
            assert row['exDate'][:10]==e['first_split_adjusted_trading_date']
            assert row['dailyListEventCode']=='DA' and not row.get('commentText')
    if e['symbol']!='PMI':
        assert {'exact_ratio','actual_execution','trading_date'}<=set(role for p in e['sources'] for role in p['roles'])
    for old in e['superseded_candidates']:
        assert any(p['source_url']==old['proof_source_url'] and old['proof_marker'] in p['markers'] and 'supersedes_prior_schedule' in p['roles'] for p in e['sources'])

result={'scope':'JXG, PMI, POCI, RDGL, SHIP, SMTK, SPRB, WLFC only. Research evidence; no production ledger or price edits.',
    'researched_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'events':events,
    'vendor_price_warning':'A confirmed official action date does not prove the per-day share units of vendor prices. POCI/SHIP/SMTK/PMI quote continuity remains a separate caller verification.'}
(ROOT/'expected_parser_fields.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
validation={'source_count':len(manifest),'filing_index_count':len(json.loads((ROOT/'filing_metadata_provenance.json').read_text('utf-8'))),
    'literal_checks':checked,'finra_record_checks':4,'failures':failures,
    'status':'passed' if not failures else 'failed','price_adjustment_validated':False,'production_code_changed':False}
(ROOT/'validation.json').write_text(json.dumps(validation,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps({'sources':len(manifest),'markers':len(checked),'failures':failures},ensure_ascii=False))
if failures:raise SystemExit(1)
