"""Financial amendments survive the real SEC downloader and normalizer."""
import json
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


def write_sec_amendment_responses(lake, form='10-K/A'):
    fixtures = lake / 'bronze' / 'synthetic_sec_responses'
    fixtures.mkdir(parents=True)
    address = {key: '' for key in ['street1', 'street2', 'city', 'stateOrCountryDescription', 'stateOrCountry', 'zipCode']}
    fields = dict(accessionNumber='0000999990-20-000001', filingDate='2020-04-30', reportDate='2019-12-31',
        acceptanceDateTime='2020-04-30T16:00:00.000Z', act='34', form=form, fileNumber='001-00000', items='',
        size=2000, isXBRL=1, isInlineXBRL=0, primaryDocument='amendment.htm', primaryDocDescription='Amendment')
    if form == '10-Q/A':
        fields['reportDate'] = '2019-06-30'
    profile = {key: '' for key in ['sic', 'sicDescription', 'category', 'entityType', 'phone', 'ein',
        'description', 'website', 'investorWebsite', 'stateOfIncorporation', 'stateOfIncorporationDescription']}
    profile.update(cik='999990', name='Synthetic financial amendment', tickers=['999990'], exchanges=['NASDAQ'],
        fiscalYearEnd='1231', flags='', insiderTransactionForOwnerExists=0, insiderTransactionForIssuerExists=0,
        addresses=dict(mailing=address, business=address),
        filings=dict(recent={key: [value] for key, value in fields.items()}, files=[]))
    (fixtures / 'submissions.json').write_text(json.dumps(profile), 'utf-8')
    instance = '''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:dei="http://xbrl.sec.gov/dei/2019"
      xmlns:us-gaap="http://fasb.org/us-gaap/2019">
      <xbrli:context id="year"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">999990</xbrli:identifier></xbrli:entity>
      <xbrli:period><xbrli:startDate>2019-01-01</xbrli:startDate><xbrli:endDate>2019-12-31</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      <dei:EntityRegistrantName contextRef="year">Synthetic financial amendment</dei:EntityRegistrantName>
      <dei:DocumentFiscalYearFocus contextRef="year">2019</dei:DocumentFiscalYearFocus>
      <dei:DocumentFiscalPeriodFocus contextRef="year">FY</dei:DocumentFiscalPeriodFocus>
      <dei:DocumentPeriodEndDate contextRef="year">2019-12-31</dei:DocumentPeriodEndDate>
      <us-gaap:NetIncomeLoss contextRef="year" unitRef="USD" decimals="0">42</us-gaap:NetIncomeLoss>
      </xbrli:xbrl>'''
    if form == '10-Q/A':
        instance = instance.replace('2019-12-31', '2019-06-30').replace('>FY<', '>Q2<')
    submission = f'''<SEC-DOCUMENT>0000999990-20-000001.txt : 20200430
<SEC-HEADER>0000999990-20-000001.hdr.sgml : 20200430
<ACCEPTANCE-DATETIME>20200430160000
ACCESSION NUMBER: 0000999990-20-000001
CONFORMED SUBMISSION TYPE: {form}
PUBLIC DOCUMENT COUNT: 2
CONFORMED PERIOD OF REPORT: {fields['reportDate'].replace('-', '')}
FILED AS OF DATE: 20200430
FILER:
    COMPANY DATA:
        COMPANY CONFORMED NAME: Synthetic financial amendment
        CENTRAL INDEX KEY: 0000999990
</SEC-HEADER>
<DOCUMENT>
<TYPE>{form}
<SEQUENCE>1
<FILENAME>amendment.htm
<DESCRIPTION>Amendment
<TEXT>
<html><body><p>Amended financial statement. Net income 42 dollars.</p></body></html>
</TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>EX-101.INS
<SEQUENCE>2
<FILENAME>amendment.xml
<DESCRIPTION>XBRL INSTANCE
<TEXT>
{instance}
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>'''
    (fixtures / 'submission.txt').write_text(submission, 'utf-8')
    (lake / 'meta').mkdir()
    pd.DataFrame([dict(cik=999990, ticker='999990', title='Synthetic financial amendment')]).to_csv(
        lake / 'meta/sec_company_tickers.csv', index=False)
    pd.DataFrame([dict(canonical_id='NET_INCOME', canonical_nm='Net income', fs_type='IS')]).to_csv(
        lake / 'meta/CanonicalAccount.csv', index=False)


@pytest.mark.parametrize('changed_query', [False, True, 'enriched'])
@pytest.mark.parametrize('form,requested_form', [('10-K/A', '10-K'), ('10-Q/A', '10-Q'), ('10-K/A', '10-K/A')])
def test_financial_collection_includes_an_amendment_and_normalizes_its_facts(tmp_path, changed_query, form, requested_form):
    lake = tmp_path / 'lake'
    write_sec_amendment_responses(lake, form=form)
    program = '''
import json, sys
from pathlib import Path
from dataclasses import asdict
import httpx
from engine.core import paths
paths.DATA_LAKE=paths.DataLakePaths(Path(sys.argv[1]))
fixtures=paths.DATA_LAKE.bronze('synthetic_sec_responses')
def send(self, request, **kwargs):
    url=str(request.url)
    if url.endswith('/submissions/CIK0000999990.json'):
        raw=(fixtures/'submissions.json').read_bytes()
        content_type='application/json'
    elif url.endswith('/0000999990-20-000001.txt'):
        raw=(fixtures/'submission.txt').read_bytes()
        content_type='text/plain'
    else:
        raise AssertionError('Unexpected external request: '+url)
    return httpx.Response(200, content=raw, headers={'Content-Type':content_type}, request=request)
httpx.Client.send=send
from engine.extractors.sec_filings import download_us_filing_htmls
from engine.transformers.sec_filings import normalize_us_sec_filings
result=download_us_filing_htmls(symbols=['999990'], forms=[sys.argv[4]], start_date='2020-04-30', end_date='2020-04-30',
    retries=0, sleep_seconds=0, workers=1)
assert result.errors==0 and result.filings_seen==1, asdict(result)
assert result.filing_bundles_written==1 and result.xbrl_files_written==1, asdict(result)
if sys.argv[3] in ['True','enriched']:
    source_folder=paths.DATA_LAKE.bronze('sec','fillings',sys.argv[5].replace('/','_'),'999990')
    manifest=next(source_folder.rglob('filing.json'))
    if sys.argv[3]=='enriched':
        legacy=json.loads(manifest.read_bytes())
        assert legacy['accepted_at']
        legacy['accepted_at']=''
        manifest.write_text(json.dumps(legacy,indent=2),'utf-8')
    originals={str(p):p.read_bytes() for p in source_folder.rglob('*') if p.is_file()}
    assert len(originals)>=3
    repeated=download_us_filing_htmls(symbols=['999990'], forms=[sys.argv[4]], start_date='2020-04-29', end_date='2020-04-30',
        retries=0, sleep_seconds=0, workers=1)
    assert repeated.errors==0 and repeated.filings_seen==1, asdict(repeated)
    if sys.argv[3]=='enriched':
        assert json.loads(manifest.read_bytes())['accepted_at']
        assert all(Path(p).read_bytes()==raw for p,raw in originals.items() if Path(p)!=manifest)
        assert any(p!=manifest and p.read_bytes()==originals[str(manifest)] for p in source_folder.rglob('*.json')), 'Metadata enrichment must retain the previous original in Bronze'
    else:
        assert all(Path(p).read_bytes()==raw for p,raw in originals.items()), 'Expanded query must preserve an existing verified original and its metadata'
normalize_us_sec_filings(symbols=['999990'],start_year=2019,end_year=2019,
    mapping_rule_path=Path(sys.argv[2]), use_notes=False,use_edgartools=False,workers=1)
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-W', 'ignore', '-c', program, str(lake),
        str(ROOT / 'data-lake/meta/rules/semantic_us_rule_manifest.json'), str(changed_query), requested_form, form], cwd=ROOT,
        capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    rows = pd.read_csv(lake / 'silver/sec/normalized/us_normalized_999990.csv')
    assert rows.loc[rows.canonical_account_id.eq('NET_INCOME'), 'normalized_amount'].item() == 42
