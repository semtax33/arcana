"""Read-only source research; writes only this research evidence directory."""
from pathlib import Path
import datetime, hashlib, json, re, sys
from lxml import html

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
sys.path.insert(0, str(REPO))
from engine.extractors.stock_splits import OfficialSession
from engine.transformers._internal.edgar_identity import resolve_edgar_identity

CASES = {'KEEL': ('1812477', ['0001213900-21-023256_ea139842ex99-101_bitfarms.htm', '0001213900-21-023256_ea139842ex99-123_bitfarms.htm', '0001812477-26-000023_keel-20260630.htm']), 'SKYX': ('1598981', ['0001493152-22-003810_form-424b4.htm', '0001493152-22-004370_ex99-1.htm', '0000721748-17-000616_sqlposam083117.htm']), 'CAPS': ('887151', ['0001171843-19-005659_f8k_082619.htm', '0001171843-19-005659_exh_31.htm', '0001171843-19-005659_exh_101.htm', '0001213900-25-026436_ea0235933-10k_capstone.htm', '0001213900-25-053782_ea0245522-424b4_capstone.htm']), 'ASTI': ('1350102', ['0001350102-18-000024_asti-form8xkxreversestocks.htm', '0001350102-18-000024_finalasticharteramendment-.htm', '0001350102-19-000030_asti-20181231x10k.htm'])}

def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')

def compact(raw):
    return ' '.join(html.fromstring(raw).text_content().split())

def main():
    session = OfficialSession()
    session.session.headers.update({'User-Agent': resolve_edgar_identity(), 'Accept': 'text/html'})
    indexes, manifest, errors = {}, [], []
    for symbol, (cik, names) in CASES.items():
        target = ROOT / symbol
        target.mkdir(exist_ok=True)
        for name in names:
            accession, filename = name.split('_', 1)
            url = f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace("-", "")}/{filename}'
            path = target / name
            old = REPO / 'data-lake/bronze/sec/stock_splits/disclosures' / cik / name
            meta = {}
            try:
                if path.exists():
                    raw = path.read_bytes()
                    sidecar = path.with_suffix(path.suffix + '.metadata.json')
                    if sidecar.exists(): meta = json.loads(sidecar.read_text('utf-8'))
                elif old.exists():
                    raw = old.read_bytes()
                    sidecar = old.with_suffix(old.suffix + '.metadata.json')
                    if sidecar.exists(): meta = json.loads(sidecar.read_text('utf-8'))
                    meta['source_local_path'] = str(old)
                    path.write_bytes(raw)
                else:
                    response = session.request('GET', url)
                    raw = response.content
                    path.write_bytes(raw)
                    meta.update(retrieved_at=stamp(), content_type=response.headers.get('Content-Type'))
                meta.update(provider='EDGAR', symbol=symbol, symbols=[symbol], security_id='SEC_US_' + symbol,
                    cik=cik, source_id=accession, document_id=accession + ':' + filename,
                    source_url=url, source_sha256=hashlib.sha256(raw).hexdigest(),
                    path=str(path), research_copy_at=stamp())
                if accession not in indexes:
                    index_url = url.rsplit('/', 1)[0] + '/' + accession + '-index.html'
                    index_root = ROOT / 'filing_index_evidence'
                    index_root.mkdir(exist_ok=True)
                    index_path = index_root / (accession + '-index.html')
                    if index_path.exists(): index_raw = index_path.read_bytes()
                    else:
                        index_raw = session.request('GET', index_url).content
                        index_path.write_bytes(index_raw)
                    text = compact(index_raw)
                    date = re.search(r'Filing Date\s+(\d{4}-\d{2}-\d{2})', text)
                    accepted = re.search(r'Accepted\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', text)
                    assert date, 'No Filing Date in SEC index'
                    form = re.search(r'Form (\S+) -', text)
                    indexes[accession] = {'source_url': index_url, 'path': str(index_path),
                        'sha256': hashlib.sha256(index_raw).hexdigest(), 'filing_date': date[1],
                        'accepted_at': accepted[1] if accepted else None, 'form': form[1] if form else None}
                index = indexes[accession]
                meta.update(published_date=index['filing_date'], accepted_at_as_disclosed=index['accepted_at'],
                    form=index['form'], published_date_basis='SEC filing index Filing Date; distinct from release date and event date',
                    filing_date_evidence={k:index[k] for k in ['source_url','path','sha256']})
                dump(path.with_suffix(path.suffix + '.metadata.json'), meta)
                manifest.append(meta)
                print(symbol, name, meta['published_date'], flush=True)
            except Exception as exc:
                errors.append({'symbol':symbol, 'url':url, 'error':str(exc)})
                print('ERROR', symbol, name, type(exc).__name__, flush=True)
    for path in ROOT.glob('*/finra_*.json.metadata.json'):
        manifest.append(json.loads(path.read_text('utf-8')))
    dump(ROOT / 'manifest.json', manifest)
    dump(ROOT / 'filing_metadata_provenance.json', indexes)
    dump(ROOT / 'download_errors.json', errors)
    print('DONE',len(manifest),'sources',len(indexes),'indexes',len(errors),'errors', flush=True)

if __name__ == '__main__': main()
