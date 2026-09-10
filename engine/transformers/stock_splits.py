"""Evidence-backed forward/reverse split events and split-only price adjustment.

Dates are the first trading dates on the new share basis, never board/record dates.
Uniform unpaid consolidations also change share units. Cash-return capital
reductions, mergers, rights issues and spin-offs require separate accounting.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Iterable

from bs4 import BeautifulSoup
from lxml import html as lxml_html
import numpy as np
import pandas as pd

from engine.core.paths import DATA_LAKE

SCHEMA_VERSION = 1
PARSER_VERSION = 'official-stock-splits-v3'


@dataclass(frozen=True)
class SplitEvent:
    security_id: str
    effective_date: str
    new_shares: str
    old_shares: str
    source: str
    source_id: str
    source_url: str
    source_sha256: str
    published_date: str
    status: str = 'confirmed'
    action_type: str = 'split'
    share_class: str = 'common'
    announcement_date: str = ''
    evidence: str = ''
    effective_date_basis: str = 'first_split_adjusted_trading_day'
    supersedes: str = ''
    capital_action_kind: str = 'share_unit_change'

    @property
    def ratio(self) -> Decimal:
        new, old = Decimal(self.new_shares), Decimal(self.old_shares)
        if not new.is_finite() or not old.is_finite() or new <= 0 or old <= 0:
            raise ValueError('share counts must be positive finite decimals')
        return new/old

    @property
    def event_id(self) -> str:
        key = '|'.join([self.security_id,self.share_class,self.effective_date,str(self.ratio.normalize())])
        return hashlib.sha256(key.encode()).hexdigest()[:32]

    def to_dict(self):
        return {**asdict(self),'event_id':self.event_id,'ratio':str(self.ratio),'parser_version':PARSER_VERSION}


def resolve_events(events: Iterable[SplitEvent], *, as_of: str | date | None = None,
                   knowledge_date: str | date | None = None) -> list[SplitEvent]:
    """Resolve explicit corrections, deduplicate echoes and fail on conflicts."""
    cutoff = date.fromisoformat(str(as_of)) if as_of else date.max
    known = date.fromisoformat(str(knowledge_date)) if knowledge_date else date.max
    available = [e for e in events if date.fromisoformat(e.published_date) <= known]
    superseded = {e.supersedes for e in available if e.supersedes}
    records = {}
    conflicts = {}
    for event in sorted(available,key=lambda e:(e.published_date,e.source_id)):
        if event.source_id in superseded or event.status != 'confirmed':
            continue
        if event.action_type not in {'split','reverse_split'}:
            continue
        if date.fromisoformat(event.effective_date) > cutoff:
            continue
        if event.effective_date_basis != 'first_split_adjusted_trading_day':
            raise ValueError('an explicit split-adjusted trading date is required')
        ratio=event.ratio
        if ratio == 1:
            raise ValueError('a 1:1 event is not a split')
        if not event.source_url.startswith(('https://dart.fss.or.kr/','https://opendart.fss.or.kr/','https://kind.krx.co.kr/','https://www.sec.gov/','https://data.sec.gov/','https://www.alphavantage.co/')):
            raise ValueError('split events require DART, KIND, EDGAR or Alpha Vantage provenance')
        key=(event.security_id,event.share_class,event.effective_date)
        if key in conflicts and conflicts[key] != ratio:
            raise ValueError(f'conflicting official split ratios for {key}')
        conflicts[key]=ratio
        records[event.event_id]=event
    return sorted(records.values(),key=lambda e:(e.security_id,e.effective_date,e.source_id))


def adjust_prices(prices: pd.DataFrame, events: Iterable[SplitEvent], *,
                  as_of: str | date | None = None, price_basis: str = 'raw',
                  knowledge_date: str | date | None = None) -> pd.DataFrame:
    """Return an idempotent split-only series while preserving all source columns.

    Yahoo Close is already split adjusted. A caller using that basis must supply
    a single coherent vendor history; multiplying it by split factors is forbidden.
    The raw branch always starts from close, never an existing adjusted result.
    """
    if price_basis not in {'raw','split_adjusted'}:
        raise ValueError('price_basis must be raw or split_adjusted')
    required={'security_id','trade_date','close'}
    if not required.issubset(prices):
        raise ValueError(f'missing price columns: {sorted(required-set(prices))}')
    result=prices.copy()
    days=pd.to_datetime(result.trade_date,errors='raise')
    cutoff=pd.Timestamp(as_of) if as_of else days.max()
    if result.empty:
        return result.assign(split_adj_close=pd.Series(dtype=float), split_adjustment_factor=pd.Series(dtype=float))
    if days.gt(cutoff).any():
        raise ValueError('price rows after the adjustment as_of are not allowed')
    resolved=resolve_events(events,as_of=min(cutoff,days.max()).date(),knowledge_date=knowledge_date)
    factors=np.ones(len(result),dtype=float)
    event_counts=np.zeros(len(result),dtype=int)
    for event in resolved:
        mask=result.security_id.eq(event.security_id).to_numpy() & (days<pd.Timestamp(event.effective_date)).to_numpy()
        factors[mask] /= float(event.ratio)
        event_counts[mask] += 1
    if not np.all(np.isfinite(factors)&(factors>0)):
        raise ValueError('invalid cumulative split adjustment factor')
    close=pd.to_numeric(result.close,errors='coerce').to_numpy(dtype=float)
    close=np.where(np.isfinite(close)&(close>0),close,np.nan)
    result['split_adjustment_factor']=factors
    result['split_adj_close']=close*factors if price_basis=='raw' else close
    for column in ['open','high','low']:
        if column in result:
            values=pd.to_numeric(result[column],errors='coerce')
            result[f'split_adj_{column}']=values*factors if price_basis=='raw' else values
    if 'volume' in result:
        values=pd.to_numeric(result.volume,errors='coerce')
        result['split_adj_volume']=values/factors if price_basis=='raw' else values
    result['split_event_count']=event_counts
    result['split_price_basis']=price_basis
    result['split_adjustment_as_of']=str(cutoff.date())
    ledger_json=json.dumps([e.to_dict() for e in resolved],sort_keys=True,separators=(',',':'))
    result['split_ledger_sha256']=hashlib.sha256(ledger_json.encode()).hexdigest()
    return result


def krx_split_reference_price(prior, ratio, effective_date, exchange):
    """Won truncation then quote-grid ceiling for documented 2010+ equities.

    KRX KOSPI rules 30/32 and KOSDAQ rules 17/18. The finer grid took effect
    on 2023-01-25; the old KOSDAQ grid caps its quote unit at 100 won.
    This validates a disclosed ratio; it never supplies an adjustment ratio.
    """
    if exchange not in {'KOSPI','KOSDAQ'} or effective_date<'2010-01-01':return None
    won=int(Decimal(str(prior))/Decimal(str(ratio)))
    if effective_date>='2023-01-25':
        grid=[(2000,1),(5000,5),(20000,10),(50000,50),(200000,100),(500000,500),(float('inf'),1000)]
    elif exchange=='KOSDAQ':
        grid=[(1000,1),(5000,5),(10000,10),(50000,50),(float('inf'),100)]
    else:
        grid=[(1000,1),(5000,5),(10000,10),(50000,50),(100000,100),(500000,500),(float('inf'),1000)]
    step=next(step for bound,step in grid if won<bound)
    return Decimal(max(step,((won+step-1)//step)*step))


def confirm_dart_with_prices(event: SplitEvent, prices: pd.DataFrame, *, reference_price=None,reference_exchange=None) -> tuple[SplitEvent,list[str]]:
    """Validate an official planned ratio/date against the exchange's daily change.

    This verifies execution; it never estimates a split ratio from a price jump.
    Only an exact new-basis trading date with positive volume is accepted.
    """
    df=prices.sort_values('trade_date')
    if 'security_id' in df:
        df=df[df.security_id.eq(event.security_id)]
    days=pd.to_datetime(df.trade_date)
    before=df.loc[days<pd.Timestamp(event.effective_date)]
    after=df.loc[days.eq(pd.Timestamp(event.effective_date))]
    if before.empty or len(after)!=1 or 'change_rate' not in after:
        return event,['missing_exchange_execution_price_evidence']
    prior=float(before.iloc[-1]['close']);row=after.iloc[0]
    if not (prior>0 and float(row['close'])>0 and float(row.get('volume',0))>0):
        return event,['no_positive_volume_on_planned_listing_date']
    reported=float(row['change_rate'])/100
    actual=float(row['close'])*float(event.ratio)/prior-1
    if reference_price is not None:
        reference=Decimal(str(reference_price))
        if not reference.is_finite() or reference<=0:return event,['invalid_reference_price']
        expected=krx_split_reference_price(prior,event.ratio,event.effective_date,reference_exchange)
        if expected is None:return event,['unverified_reference_quote_grid']
        if expected!=reference:return event,['official_ratio_conflicts_with_reference_price']
    comparable=actual if reference_price is None else float(row['close'])/float(reference_price)-1
    if not math.isfinite(reported) or not math.isfinite(comparable) or abs(reported-comparable)>.00015:
        return event,['official_ratio_date_conflicts_with_exchange_change']
    evidence=event.evidence+f' | KRX execution check: previous_close={prior}; close={row["close"]}; reference_price={reference_price}; reported_change={reported}; split_only_return={actual}'
    return replace(event,status='confirmed',evidence=evidence),[]


def parse_dart_resumption(html: str | bytes):
    """Return an explicitly announced exchange resumption of split common shares."""
    soup=soup_of_html(html)
    compact=re.sub(r'\s+','',soup.get_text(' ',strip=True))
    if '매매거래정지해제' not in compact:return None
    fields={}
    for row in soup.find_all('tr'):
        cells=[re.sub(r'\s+','',c.get_text(' ',strip=True)) for c in row.find_all(['td','th'],recursive=False)]
        if len(cells)>=2:fields[cells[0]]=' '.join(cells[1:])
    target=next((v for k,v in fields.items() if '대상' in k),'')
    reason=next((v for k,v in fields.items() if '사유' in k),'')
    timing=next((v for k,v in fields.items() if '해제일' in k),'')
    dates=re.findall(r'\d{4}-\d{2}-\d{2}',timing)
    if '보통주' not in target or not any(s in reason for s in ['액면분할','액면병합','주식분할','주식병합']) or len(dates)!=1:return None
    return {'effective_date':dates[0],'share_class':'common','reason':reason,'target':target,
            'action_type':'reverse_split' if '병합' in reason else 'split'}


def parse_kind_capital_listing(html, *, security_id, source_title=''):
    """Execution date of a capital-reduction listing, without an inferred ratio."""
    text=text_of_html(html)
    compact=re.sub(r'\s+','',text)
    if '변경상장' not in compact or not any(w in compact+source_title for w in ('감자','자본감소')):
        return None
    code=security_id.removeprefix('SEC_KR_')
    if not re.search(r'A'+re.escape(code)+r'(?![A-Z0-9])',compact) or '보통주' not in compact:
        return None
    if any(w in source_title for w in ('유상증자','무상증자','회사합병')):
        return None
    dates=re.findall(r'(?:변경상장일:|5\.상장일)(\d{4})(?:년|[-.])(\d{1,2})(?:월|[-.])(\d{1,2})',compact)
    unique={date(*map(int,d)).isoformat() for d in dates}
    if len(unique)!=1:return None
    return {'effective_date':unique.pop(),'share_class':'common','action_type':'reverse_split',
            'capital_action_kind':'capital_reduction_unpaid','target':security_id,
            'reason':'Official capital-reduction change listing; ratio requires the final decision'}


def parse_dart_capital_consolidation(html, *, security_id, source_id, source_url, published_date):
    """Exact, uniform common-share conversion in an unpaid reduction decision."""
    soup=soup_of_html(html);tables=[]
    for table in soup.find_all('table'):
        text=re.sub(r'\s+','',table.get_text(' ',strip=True))
        if '감자방법' in text and '감자전후발행주식수' in text and '정정전' not in text[:200]:
            tables.append(table)
    if len(tables)!=1:return [],['missing_or_ambiguous_capital_reduction_decision']
    table=tables[0];fields={}
    compact=re.sub(r'\s+','',table.get_text(' ',strip=True))
    for row in table.find_all('tr'):
        cells=[re.sub(r'\s+','',c.get_text(' ',strip=True)) for c in row.find_all(['td','th'],recursive=False)]
        if len(cells)>=2:fields[cells[0]]=' '.join(cells[1:])
    method=next((v for k,v in fields.items() if '감자방법' in k),'')
    if ('무상' not in method or any(w in compact for w in
            ('차등감자','유상감자','전액소각','전량소각','감자결정취소','자본금환급'))
            or any(w in method for w in ('대주주','최대주주','소각','현금'))):
        return [],['nonuniform_or_cash_capital_reduction_requires_separate_accounting']
    matches=re.findall(r'보통주(?:식)?([0-9,]+)주를.{0,40}?([0-9,]+)주로(?:무상)?(?:병합|합병)',method)
    if len(matches)!=1:return [],['missing_explicit_common_share_consolidation_ratio']
    old,new=[Decimal(v.replace(',','')) for v in matches[0]]
    if not 0<new<old:return [],['invalid_consolidation_ratio']
    def field_date(label):
        value=next((v for k,v in fields.items() if label in k),'')
        found=re.findall(r'(\d{4})(?:년|[-./])(\d{1,2})(?:월|[-./])(\d{1,2})',value)
        return date(*map(int,found[0])).isoformat() if len(found)==1 else None
    listing=field_date('신주상장');board=field_date('이사회결의')
    if not listing or not board:return [],['missing_capital_reduction_listing_or_board_date']
    raw=html if isinstance(html,bytes) else html.encode()
    event=SplitEvent(security_id=security_id,effective_date=listing,new_shares=str(new),old_shares=str(old),
        source='KIND' if source_url.startswith('https://kind.krx.co.kr/') else 'DART',
        source_id=source_id,source_url=source_url,source_sha256=hashlib.sha256(raw).hexdigest(),
        published_date=published_date,status='announced',action_type='reverse_split',
        announcement_date=board,capital_action_kind='capital_reduction_unpaid',
        evidence='Explicit unpaid common-share consolidation: '+method)
    return [event],[]


def parse_kr_share_unit_change(html, *, source_title='', **kwargs):
    text = text_of_html(html)
    title = re.sub(r'\s+', '', source_title)
    header = re.sub(r'\s+', '', text[:400])
    # A parent's filing can describe a subsidiary's shares. Do not merge it
    # with the parent's own same-day board decision or assign its unit change
    # to the parent security without an independently verified issuer mapping.
    if (re.search(r'(?:자회사|종속회사|종속기업)의주요경영사항', title)
            or re.search(r'(?:자회사|종속회사|종속기업)인.{1,100}?의주요경영사항신고', header)):
        return [], ['subsidiary_action_requires_affected_security_mapping']
    if '감자방법' in re.sub(r'\s+','',text):
        return parse_dart_capital_consolidation(html,**kwargs)
    return parse_dart_split(html,**kwargs)


def parse_kind_listing(html: str | bytes, *, security_id: str):
    """Actual exchange listing date with explicit common-share code and unit ratio."""
    text=text_of_html(html)
    if '변경상장' not in text or not re.search(r'액면\s*(?:분할|병합)',text):return None
    code=security_id.removeprefix('SEC_KR_')
    if not re.search(r'보통주\s*표준코드\s*:[^()]{0,80}\(\s*단축코드\s*:\s*A'+re.escape(code)+r'\s*\)',text):return None
    dates=re.findall(r'변경상장일\s*:\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일',text)
    par=re.findall(r'1주의\s*금액\s*:\s*([\d,]+)원\s*→\s*([\d,]+)원',text)
    if len(dates)!=1 or len(par)!=1:return None
    old,new=[Decimal(v.replace(',','')) for v in par[0]]
    if old<=0 or new<=0 or old==new:return None
    y,m,d=map(int,dates[0])
    return {'effective_date':date(y,m,d).isoformat(),'share_class':'common','ratio':str(old/new),
            'reason':'Official exchange change listing with explicit common-share code and par-value ratio',
            'target':security_id}


def parse_kind_reference_price(html: str | bytes, *, security_id=None):
    """Read an actual exchange reference price; never derive a split ratio."""
    soup=soup_of_html(html)
    text=re.sub(r'\s+','',soup.get_text(' ',strip=True))
    if '기준가격안내' not in text:return None
    fields={};prices=[];stock_code=None;reason_primary='';reason_detail=''
    for row in soup.find_all('tr'):
        cells=[re.sub(r'\s+','',c.get_text(' ',strip=True)) for c in row.find_all(['td','th'],recursive=False)]
        if len(cells)>=2:fields[cells[0]]=' '.join(cells[1:])
        if len(cells) in {2,3} and cells[0] in {'보통주','보통주식'} and re.fullmatch(r'[0-9,]+(?:\.[0-9]+)?',cells[-1]):
            if len(cells)==3:
                if not re.fullmatch(r'A[A-Z0-9]{6}',cells[1]):return None
                stock_code=cells[1][1:]
                if security_id is not None and security_id!=f'SEC_KR_{stock_code}':return None
            prices.append(Decimal(cells[-1].replace(',','')))
        if len(cells)>=2 and '사유' in cells[0]:
            reason_primary=cells[1];reason_detail=''.join(cells[2:])
    reason=next((v for k,v in fields.items() if '사유' in k),'')
    timing=next((v for k,v in fields.items() if '적용일' in k),'')
    dates=re.findall(r'\d{4}-\d{2}-\d{2}',timing)
    if len(dates)!=1 or len(prices)!=1 or prices[0]<=0 or reason_primary not in {'액면분할','액면병합','주식분할','주식병합'}:return None
    if any(word in reason_detail for word in ['감자','무상증자','회사합병','유상증자']):return None
    result={'effective_date':dates[0],'reference_price':str(prices[0]),'share_class':'common',
            'action_type':'reverse_split' if '병합' in reason_primary else 'split','reason':reason,'target':'보통주식'}
    result['exchange']='KOSDAQ' if '코스닥시장' in text else ('KOSPI' if '유가증권시장' in text else None)
    if stock_code:result['stock_code']=stock_code
    par=re.fullmatch(r'액면가([0-9,]+)원에서([0-9,]+)원으로액면(?:분할|병합)',reason_detail)
    if par:
        old,new=[Decimal(v.replace(',','')) for v in par.groups()]
        if old>0 and new>0 and old!=new:result['ratio']=str(old/new)
    explicit=re.fullmatch(r'([0-9,]+):([0-9,]+)비율로주식병합',reason_detail)
    if explicit and reason_primary=='주식병합':
        old,new=[Decimal(v.replace(',','')) for v in explicit.groups()]
        if 0<new<old:result['ratio']=str(new/old)
    return result


def ledger_path(market: str) -> Path:
    return DATA_LAKE.silver('corporate_actions',f'{market.lower()}_stock_splits.json')


def load_events(market: str, path: str | Path | None = None) -> list[SplitEvent]:
    path=Path(path or ledger_path(market))
    if not path.exists():
        return []
    payload=json.loads(path.read_text(encoding='utf-8'))
    fields=set(SplitEvent.__dataclass_fields__)
    return [SplitEvent(**{k:v for k,v in row.items() if k in fields}) for row in payload.get('events',[])]


def soup_of_html(html: str | bytes):
    # OpenDART ZIP members can be transcoded to UTF-8 while retaining an
    # obsolete euc-kr HTML meta tag. Strict UTF-8 decoding identifies that case.
    # Public DART viewer MS949 bytes fail this check and retain their declaration.
    if isinstance(html,bytes):
        try:html=html.decode('utf-8-sig',errors='strict')
        except UnicodeDecodeError:pass
    return BeautifulSoup(html,'lxml')


def text_of_html(html: str | bytes) -> str:
    if isinstance(html,bytes):
        try:html=html.decode('utf-8-sig',errors='strict')
        except UnicodeDecodeError:pass
    if isinstance(html,str):
        # Strict decoding above resolves stale headers in transcoded sources;
        # lxml cannot accept an XML encoding declaration on a Unicode string.
        html=re.sub(r'^\s*<\?xml\b[^>]*\?>','',html,count=1,flags=re.I)
    if not html or not html.strip():return ''
    document=lxml_html.fromstring(html)
    for element in document.xpath('//script|//style'):
        element.drop_tree()
    return re.sub(r'\s+',' ',' '.join(document.itertext()).strip()).replace('\u2011','-').replace('\u2013','-')


def parse_dart_split(html: str | bytes, *, security_id: str, source_id: str,
                     source_url: str, published_date: str) -> tuple[list[SplitEvent],list[str]]:
    """Read the operative decision table, excluding correction-comparison tables.

    Par-value ratios avoid rounding errors from fractional-share cash-outs.
    A listing-schedule observation is announced until the collector corroborates
    it with an exchange resumption disclosure or execution-date price evidence.
    """
    soup=soup_of_html(html)
    compact_document=re.sub(r'\s+','',soup.get_text(' ',strip=True))
    no_par='무액면' in compact_document or '1주당자본금' in compact_document
    tables=[]
    for table in soup.find_all('table'):
        compact=re.sub(r'\s+','',table.get_text(' ',strip=True))
        if ('분할전' in compact and '분할후' in compact or '병합전' in compact and '병합후' in compact) and '1주당' in compact:
            if '정정전' not in compact[:200]:
                tables.append(table)
    if len(tables)!=1:
        return [],['missing_or_ambiguous_operative_decision_table']
    table=tables[0]
    operative=re.sub(r'\s+','',table.get_text(' ',strip=True))
    explicit=re.findall(r'본주식병합건의병합비율은([0-9,]+):([0-9,]+)입니다',operative)
    if no_par and len(explicit)!=1:
        return [],['no_par_share_requires_explicit_ratio_source']
    if '환율' in operative and '반올림' in operative and len(explicit)!=1:
        return [],['rounded_fx_par_requires_explicit_ratio_source']
    rows=[[re.sub(r'\s+','',c.get_text(' ',strip=True)) for c in r.find_all(['td','th'],recursive=False)] for r in table.find_all('tr')]
    par=None; counts=None; listing=None; board=''
    evidence=[]
    for cells in rows:
        joined=' '.join(cells)
        if '1주당' in joined and ('가액' in joined or '액면' in joined):
            nums=[re.sub(r'[^\d.]','',v) for v in cells[-2:]]
            if len(nums)==2 and all(nums):
                par=nums
                evidence.append(joined)
        if any('보통주식' in v for v in cells) and len(cells)>=3:
            nums=[re.sub(r'[^\d.]','',v) for v in cells[-2:]]
            if all(nums):counts=nums
        if any(re.search(r'(?:신주권|신주|변경)상장(?:예정)?일',v) for v in cells[:-1]):
            found=re.findall(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}',cells[-1])
            if len(found)==1:
                listing=str(pd.Timestamp(found[0]).date());evidence.append(joined)
        if '이사회결의일' in joined:
            found=re.findall(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}',cells[-1])
            if len(found)==1:board=str(pd.Timestamp(found[0]).date())
    if len(explicit)==1:
        old,new=explicit[0]
        par=[new.replace(',',''),old.replace(',','')]
        evidence.append('Explicit common-share consolidation old:new '+old+':'+new)
    if not par or not listing:
        return [],['missing_par_ratio_or_listing_date']
    try:
        ratio=Decimal(par[0])/Decimal(par[1])
    except (InvalidOperation,ZeroDivisionError):
        return [],['invalid_par_ratio']
    if ratio<=0 or ratio==1:
        return [],['invalid_or_no_split_ratio']
    if counts:
        expected=Decimal(counts[0])*ratio
        if abs(expected-Decimal(counts[1]))>max(Decimal(1),abs(expected)*Decimal('0.0001')):
            return [],['par_ratio_conflicts_with_share_counts']
    raw=html if isinstance(html,bytes) else html.encode()
    event=SplitEvent(security_id=security_id,effective_date=listing,new_shares=par[0],old_shares=par[1],
                     source='KIND' if source_url.startswith('https://kind.krx.co.kr/') else 'DART',source_id=source_id,source_url=source_url,source_sha256=hashlib.sha256(raw).hexdigest(),
                     published_date=published_date,action_type='split' if ratio>1 else 'reverse_split',
                     status='announced',announcement_date=board,evidence=' | '.join(evidence))
    return [event],[]


MONTHS='January|February|March|April|May|June|July|August|September|October|November|December'
US_DATE=rf'(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?\s*,?\s*\d{{4}}'


def parse_us_date(text: str) -> str:
    clean=re.sub(r'(?<=\d)(st|nd|rd|th)\b','',text,flags=re.I)
    return pd.Timestamp(clean).date().isoformat()


def parse_edgar_split(html: str | bytes, *, security_id: str, source_id: str,
                      source_url: str, published_date: str) -> tuple[list[SplitEvent],list[str]]:
    """Precision-first event parser: explicit numeric ratio plus trading date.

    Unchosen board authorizations, ratio ranges, legal-effective dates without a
    trading date, multiple ambiguous events and mixed capital actions abstain.
    """
    text=text_of_html(html)
    small={'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,
           'ten':10,'eleven':11,'twelve':12,'thirteen':13,'fourteen':14,'fifteen':15,
           'sixteen':16,'seventeen':17,'eighteen':18,'nineteen':19}
    tens={'twenty':20,'thirty':30,'forty':40,'fifty':50,'sixty':60,'seventy':70,'eighty':80,'ninety':90}
    units='|'.join(list(small)[:9])
    below100=rf'(?:(?:{"|".join(tens)})(?:[- ](?:{units}))?|(?:{"|".join(small)}))'
    cardinal=rf'(?:(?:{units})[- ]hundred(?:[- ](?:and[- ])?{below100})?|{below100}|hundred)'
    def cardinal_value(value):
        total=0
        for word in re.split(r'[- ]+',value.lower()):
            if word=='hundred':total=max(total,1)*100
            elif word!='and':total+=small.get(word,tens.get(word,0))
        return total
    # Consume the complete cardinal, including twenty-five. A prefix match
    # would silently turn a 25:1 consolidation into a 20:1 consolidation.
    text=re.sub(rf'\b({cardinal})[- ]for[- ]({cardinal})\b(?![- ](?:{units}|hundred|thousand)\b)',
                lambda m:f'{cardinal_value(m[1])}-for-{cardinal_value(m[2])}',text,flags=re.I)
    if not re.search(r'(?:stock|share)\s+(?:split|consolidation)|reverse\s+split',text,re.I):
        return [],['no_split_language']
    trading=[]
    # Trading language and its date must stay in one sentence. In particular,
    # a following "As discussed below, on [meeting date]" is not a trading date.
    # Preserve the common a.m./p.m. abbreviations within a trading schedule.
    gap=r'(?:(?!(?<!a\.m)(?<!p\.m)\.\s+(?-i:[A-Z])|[!?;]).)'
    patterns=[
        rf'(?:begin|commence|start|resume)(?:\s+\w+){{0,4}}\s+trad(?:e|ing){gap}{{0,160}}?(?:split[- ]adjusted|post[- ](?:split|consolidation)){gap}{{0,120}}?({US_DATE})',
        rf'(?:begin|commence|start|resume)(?:\s+\w+){{0,4}}\s+trad(?:e|ing){gap}{{0,100}}?({US_DATE}){gap}{{0,120}}?(?:split[- ]adjusted|post[- ](?:split|consolidation))',
        rf'(?:reflected with|effective on){gap}{{0,80}}?(?:Nasdaq|NYSE|marketplace){gap}{{0,90}}?(?:open of business|opening of (?:the )?market){gap}{{0,30}}?({US_DATE})',
        rf'(?:split[- ]adjusted|post[- ]split)\s+basis\s+(?:on|beginning|starting|at (?:the )?(?:market )?open on)\s+({US_DATE})',
        rf'(?:opening|commencement) of trading on\s+({US_DATE}){gap}{{0,160}}?(?:split[- ]adjusted|post[- ](?:split|consolidation))',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern,text,re.I):
            trading.append((parse_us_date(match.group(1)),match.start(),match.end()))
    dates=sorted({d for d,_,_ in trading})
    if len(dates)!=1:
        return [],['missing_or_ambiguous_split_adjusted_trading_date']
    # A release can contain a mistyped year. Never repair it by assuming the
    # vendor is right, or accept trading a year before the stated legal change.
    legal_pattern=rf'will\s+(?:become|be)\s+effective{gap}{{0,100}}?({US_DATE})'
    for match in re.finditer(legal_pattern,text,re.I):
        if not any(min(abs(match.start()-a),abs(match.end()-b))<=1200 for _,a,b in trading):
            continue
        if (pd.Timestamp(parse_us_date(match.group(1)))-pd.Timestamp(dates[0])).days>7:
            return [],['inconsistent_legal_and_trading_dates']
    # A reverse split always reduces share count. Issuers use both 1-for-N
    # and N-for-1 for it; direction comes from the explicit action wording.
    # Ordinary reductions in authorized/outstanding shares are not ratios.
    ratios=[]
    for match in re.finditer(r'\b(\d[\d,]*(?:\.\d+)?)\s*[- ](?:for|to)\s*[- ](\d[\d,]*(?:\.\d+)?)\b',text,re.I):
        before=text[max(0,match.start()-180):match.start()]
        after=text[match.end():match.end()+150]
        action=r'(?:reverse\s+(?:(?:stock|share)\s+)?split|(?:stock|share)\s+(?:split|consolidation))'
        direct=re.match(r'\s*[)\"\u201d]?\s*('+action+r')\b',after,re.I)
        prior=list(re.finditer(action,before,re.I))
        # Only accept a ratio adjacent to its action or explicitly labelled as
        # a ratio/basis after that action. "reduced from X to Y" must abstain.
        labelled=re.search(r'(?:ratio|basis)(?:\s+(?:of|at))?\s*$',before,re.I)
        if not direct and not (prior and labelled):
            continue
        if re.search(r'between|range|up to|not less than|not more than|no less than|no more than',before[-100:],re.I):
            continue
        if re.match(r'\s+(?:to|through|and)\s+\d[\d,]*(?:\.\d+)?\s*[- ]for\s*[- ]\d',after,re.I):
            continue
        if re.match(r'-[A-Za-z]',after):
            # Do not parse the numeric prefix of malformed "1-for-20-five".
            continue
        if re.search(r'\bfrom\s*$',before,re.I):
            continue
        new,old=match.group(1).replace(',',''),match.group(2).replace(',','')
        if Decimal(new)<=0 or Decimal(old)<=0 or Decimal(new)==Decimal(old):
            continue
        wording=direct.group(1) if direct else prior[-1].group(0)
        if re.search(r'reverse|consolidation',wording,re.I) and Decimal(new)>Decimal(old):
            new,old=old,new
        ratios.append((new,old,match.start()))
    # Independently validate an explicit conversion even when a headline ratio
    # exists. Conflicting operative ratios must never be resolved by ordering.
    pat=r'every\s+(\d[\d,]*)\s+(?:issued.{0,45}?)?shares?.{0,140}?(?:combined|converted|consolidated).{0,40}?into\s+(one|\d[\d,]*)\s+(?:ordinary\s+|common\s+)?shares?'
    for match in re.finditer(pat,text,re.I):
        ratios.append(('1' if match.group(2).lower()=='one' else match.group(2).replace(',',''),match.group(1).replace(',',''),match.start()))
    # A distant historical ratio and an unrelated trading date in the same
    # financial report must never become one invented event.
    ratios=[r for r in ratios if any(min(abs(r[2]-a),abs(r[2]-b))<=1200 for _,a,b in trading)]
    if abs((pd.Timestamp(published_date)-pd.Timestamp(dates[0])).days)>370:
        return [],['historical_trading_date_requires_event_specific_confirmation']
    unique={Decimal(a)/Decimal(b) for a,b,_ in ratios}
    if len(unique)!=1:
        return [],['missing_or_ambiguous_final_split_ratio']
    new,old,pos=ratios[0]
    ratio=Decimal(new)/Decimal(old)
    if re.search(r'(?:cancelled|canceled|rescinded|abandoned)\s+(?:the\s+)?(?:proposed\s+)?(?:reverse\s+)?(?:stock|share)\s+(?:split|consolidation)',text,re.I):
        return [],['cancelled_split']
    # Mixed transactions require a richer corporate-action cash/share ledger.
    if re.search(r'(?:special cash distribution|going.private transaction|cash.in.lieu.{0,40}all.*shares)',text,re.I):
        return [],['mixed_cash_or_going_private_action']
    evidence=text[max(0,pos-120):pos+240]+' | '+' | '.join(text[a:b] for _,a,b in trading[:2])
    raw=html if isinstance(html,bytes) else html.encode()
    event=SplitEvent(security_id=security_id,effective_date=dates[0],new_shares=new,old_shares=old,
                     source='EDGAR',source_id=source_id,source_url=source_url,source_sha256=hashlib.sha256(raw).hexdigest(),
                     published_date=published_date,action_type='split' if ratio>1 else 'reverse_split',evidence=evidence)
    return [event],[]
