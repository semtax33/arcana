"""Official KIND fallback documents and exchange split execution notices.

Identifiers and document URLs come from the published search/viewer responses;
DART receipt numbers are never transformed into guessed KIND identifiers.
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlencode

import pandas as pd

from engine.extractors.stock_splits import OfficialSession, _json, _save_response, root_for
from engine.transformers.stock_splits import soup_of_html

BASE = 'https://kind.krx.co.kr'


def parse_kind_search(content):
    soup = soup_of_html(content)
    total = re.search(r'전체\s*([\d,]+)\s*건', soup.get_text(' ', strip=True))
    if not total:
        raise ValueError('KIND search is missing its explicit result count')
    rows = []
    for anchor in soup.select('a[onclick]'):
        match = re.search(r"openDisclsViewer\('([0-9]{14})','([0-9]*)'\)", anchor['onclick'])
        if not match:
            continue
        row = anchor.find_parent('tr')
        company = row.select_one('a#companysum') if row else None
        dates=[cell.get_text(' ',strip=True) for cell in row.find_all('td',recursive=False)
               if re.fullmatch(r'\d{4}[-.]\d{2}[-.]\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?',cell.get_text(' ',strip=True))] if row else []
        if len(dates)!=1:
            raise ValueError('KIND search row has no unambiguous publication date')
        rows.append({'kind_acpt_no': match[1], 'search_doc_no': match[2],
                     'title': anchor.get_text(' ', strip=True),
                     'company_name': company.get('title', company.get_text(' ', strip=True)) if company else '',
                     'published_date': str(pd.Timestamp(dates[0][:10]).date()),
                     'publication_date_text': dates[0]})
    return rows, int(total[1].replace(',', ''))


def normalized_title(title):
    # Search labels include nested explanatory parentheses, while the viewer
    # often shows only the base report title. An unfinished parenthesis in the
    # published search label also belongs to the explanation.
    title = re.sub(r'\[[^]]*\]', '', title)
    depth = 0
    base = []
    for character in title:
        if character == '(':
            depth += 1
        elif character == ')':
            depth = max(0, depth - 1)
        elif not depth and not character.isspace():
            base.append(character)
    return ''.join(base)


def parse_kind_viewer(content, record):
    soup = soup_of_html(content)
    heading = soup.find('h1')
    codes = re.findall(r'\(([A-Z0-9]{6})\)', heading.get_text() if heading else '')
    if len(codes) != 1:
        raise ValueError('KIND viewer has no unambiguous six-digit stock code')
    options = []
    for option in soup.select('#mainDoc option[value]'):
        value = option['value']
        if not re.fullmatch(r'[0-9]{14}\|[YN]', value):
            continue
        label = option.get_text(' ', strip=True)
        dates = re.findall(r'\(([0-9]{4}\.[0-9]{2}\.[0-9]{2})\)', label)
        if len(dates) != 1:
            continue
        options.append({'kind_doc_no': value.split('|')[0], 'option_value': value,
                        'published_date': dates[0].replace('.', '-'),
                        'title': re.sub(r'\s*\([0-9]{4}\.[0-9]{2}\.[0-9]{2}\)', '', label),
                        'selected': option.has_attr('selected')})
    matches = [o for o in options if o['published_date'] == record['published_date']
               and normalized_title(o['title']) == normalized_title(record['title'])]
    if len(matches) > 1:
        matches = [o for o in matches if o['selected']]
    if len(matches) != 1:
        raise ValueError('KIND viewer does not identify the requested dated document uniquely')
    return codes[0], matches[0], options


def parse_kind_contents(content):
    text = content.decode('utf-8') if isinstance(content, bytes) else content
    urls = re.findall(r"parent\.setPath\('[^']*','([^']+)'", text)
    if len(urls) != 1 or not re.fullmatch(r'https://kind\.krx\.co\.kr/external/[0-9/]+\.[hH][tT][mM][lL]?', urls[0]):
        raise ValueError('KIND contents response has no permitted official document URL')
    return urls[0]


class KindClient:
    def __init__(self, root=None, force=False):
        self.root = Path(root or root_for('kr'))
        self.force = force
        self.session = OfficialSession(.5)
        self.session.session.headers.update({'Referer': BASE + '/', 'User-Agent': 'Mozilla/5.0 Arcana Research'})

    def response(self, url, *, data=None):
        digest = hashlib.sha256(json.dumps([url, data], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
        folder = self.root / 'kind_responses'
        path = folder / f'{digest}.html'
        if path.exists() and not self.force:
            return path.read_bytes()
        response = self.session.request('POST' if data is not None else 'GET', url, **({'data': data} if data is not None else {}))
        _save_response(folder, path.name, response, {'provider': 'KIND', 'request': data})
        return response.content

    def search(self, start, end, title):
        rows = []; page = 1; expected = None
        while True:
            data = {'method': 'searchDetailsSub', 'currentPageSize': '100', 'pageIndex': str(page),
                    'orderMode': '1', 'orderStat': 'D', 'forward': 'details_sub',
                    'fromDate': str(pd.Timestamp(start).date()), 'toDate': str(pd.Timestamp(end).date()),
                    'reportNm': title, 'reportNmTemp': title, 'searchCorpName': '', 'marketType': ''}
            # The official form posts empty disclosure categories as well; the
            # server rejects requests that omit these form fields.
            data.update({key: '' for key in ['searchCodeType','repIsuSrtCd','allRepIsuSrtCd',
                         'oldSearchCorpName','disclosureType','disTypevalue','reportCd',
                         'submitOblgNm','reportNmPop']})
            for category in ['01','02','03','04','05','06','07','08','09','10','11','13','14','20']:
                data['disclosureType'+category]=''
                data['pDisclosureType'+category]=''
            found, total = parse_kind_search(self.response(BASE + '/disclosure/details.do', data=data))
            if expected is not None and total != expected:
                raise ValueError('KIND search count changed during pagination')
            expected = total
            if not found and total:
                raise ValueError('KIND search unexpectedly returned an empty page')
            rows.extend(found)
            if page * 100 >= total:
                break
            page += 1
        unique = {r['kind_acpt_no']: r for r in rows}
        if len(unique) != expected:
            raise ValueError('KIND search result count/pagination mismatch')
        return list(unique.values())

    def document(self, record, *, expected_code=None, dart_record=None):
        acpt = record['kind_acpt_no']
        if not re.fullmatch(r'[0-9]{14}', acpt):
            raise ValueError('invalid KIND receipt')
        viewer_url = BASE + '/common/disclsviewer.do?' + urlencode({'method': 'search', 'acptno': acpt, 'docno': ''})
        code, chosen, family = parse_kind_viewer(self.response(viewer_url), record)
        if expected_code and code != expected_code:
            return None
        doc = chosen['kind_doc_no']
        folder = self.root / 'disclosures' / code
        path = folder / f'kind_{acpt}_{doc}.html'
        meta_path = path.with_suffix('.html.metadata.json')
        if path.exists() and meta_path.exists() and not self.force:
            metadata = json.loads(meta_path.read_text(encoding='utf-8'))
        else:
            contents = self.response(BASE + '/common/disclsviewer.do?' + urlencode({'method': 'searchContents', 'docNo': doc}))
            url = parse_kind_contents(contents)
            response = self.session.request('GET', url)
            metadata = _save_response(folder, path.name, response, {
                **record, 'provider': 'KIND', 'source_id': acpt, 'kind_doc_no': doc,
                'security_id': f'SEC_KR_{code}', 'stock_code': code,
                'family_id': 'kind:' + min(o['kind_doc_no'] for o in family),
                'viewer_url': viewer_url, 'document_options': family})
        if dart_record:
            metadata.update({'dart_rcept_no': dart_record['source_id'], 'opendart_document_status': '014',
                             'dart_title': dart_record['title'],
                             'link_evidence': 'Same disclosed stock code, publication day and normalized report title; identifiers independently obtained from official KIND search/viewer.'})
            _json(meta_path, metadata)
        return metadata


def download_kind_splits(*, unavailable=(), corporation_mapping=None, symbols=None,
                         start_date='20100101', end_date=None, output_dir=None, force=False,
                         notice_types=('변경상장','매매거래정지해제','기준가격','감자결정'),report_filename='kind_download_report.json',
                         search_titles=('액면분할','액면병합','주식분할','주식병합','감자','자본감소')):
    client = KindClient(output_dir, force)
    start = pd.Timestamp(str(start_date)); end = pd.Timestamp(str(end_date or date.today()))
    wanted = {str(s).zfill(6) for s in symbols} if symbols else None
    report = {'provider': 'KIND', 'fallback_recovered': [], 'fallback_unresolved': [],
              'exchange_documents': [], 'errors': [], 'search_windows': []}
    seen_exchange_documents=set()
    mapping = corporation_mapping or {}
    for record in unavailable:
        code = mapping.get(record['corp_code'], '')
        if not code or (wanted is not None and code not in wanted):
            continue
        title = '주식분할' if '분할' in record['title'] else '주식병합'
        try:
            if not record.get('published_date'):
                raise ValueError('Missing evidenced DART publication date for KIND fallback')
            day = pd.Timestamp(record['published_date'])
            if pd.isna(day):
                raise ValueError('Missing evidenced DART publication date for KIND fallback')
            found = client.search(day, day, title)
            matches = []
            for candidate in found:
                if normalized_title(candidate['title']) != normalized_title(record['title']):
                    continue
                result = client.document(candidate, expected_code=code)
                if result:
                    matches.append(result)
            target = 'fallback_recovered' if len(matches) == 1 else 'fallback_unresolved'
            if len(matches)==1:
                client.document(next(r for r in found if r['kind_acpt_no']==matches[0]['source_id']),
                                expected_code=code,dart_record=record)
            report[target].append({'dart_rcept_no': record['source_id'], 'matches': [m['source_id'] for m in matches]})
        except Exception as exc:
            report['errors'].append({'dart_rcept_no': record['source_id'], 'error': str(exc)})
            if len(report['errors']) >= 5:
                break
        count = len(report['fallback_recovered']) + len(report['fallback_unresolved']) + len(report['errors'])
        if count % 25 == 0:
            print(f'[SPLITS] KIND fallback recovered={len(report["fallback_recovered"])} unresolved={len(report["fallback_unresolved"])} errors={len(report["errors"])}', flush=True)
            _json(client.root / report_filename, report)
    # These narrow title searches find actual exchange actions, including older
    # notices whose split decision predates the requested history.
    if not report['errors']:
        # Fetch current events first; all older years still remain in scope.
        for year in range(end.year, start.year - 1, -1):
            a = max(start, pd.Timestamp(year=year, month=1, day=1))
            b = min(end, pd.Timestamp(year=year, month=12, day=31))
            for title in search_titles:
                try:
                    found = client.search(a, b, title)
                    report['search_windows'].append({'start': str(a.date()), 'end': str(b.date()), 'title': title, 'count': len(found)})
                    for record in found:
                        if not any(t in record['title'] for t in notice_types):
                            continue
                        try:
                            result = client.document(record)
                        except ValueError as exc:
                            report['errors'].append({'source_id':record['kind_acpt_no'],'title':record['title'],'error':str(exc)})
                            continue
                        if result and (wanted is None or result['stock_code'] in wanted):
                            if result['source_id'] in seen_exchange_documents:continue
                            seen_exchange_documents.add(result['source_id'])
                            report['exchange_documents'].append({'source_id': result['source_id'], 'security_id': result['security_id']})
                            if len(report['exchange_documents'])%25==0:
                                print(f'[SPLITS] KIND exchange documents={len(report["exchange_documents"])} errors={len(report["errors"])}',flush=True)
                                _json(client.root / report_filename,report)
                    print(f'[SPLITS] KIND exchange year={year} title={title} documents={len(report["exchange_documents"])}', flush=True)
                    _json(client.root / report_filename, report)
                except Exception as exc:
                    report['errors'].append({'year': year, 'title': title, 'error': str(exc)})
                    break
            if any('year' in e for e in report['errors']):
                break
    _json(client.root / report_filename, report)
    return report


def download_reviewed_kind_sources(*, output_dir=None, as_of, symbols=None, force=False):
    from engine.transformers.corporate_action_evidence import evidence_records
    client = KindClient(output_dir, force)
    report = {'downloaded_or_cached': [], 'errors': []}
    wanted = set(symbols) if symbols else None
    seen = set()
    for item in evidence_records():
        code = item['security_id'].removeprefix('SEC_KR_')
        if wanted is not None and code not in wanted:
            continue
        for source in item['sources']:
            if source['published_date'] > as_of or source['source_id'] in seen:
                continue
            seen.add(source['source_id'])
            try:
                meta = client.document({'kind_acpt_no': source['source_id'],
                                        'title': source['title'], 'published_date': source['published_date']},
                                       expected_code=code)
                if not meta or meta['source_sha256'] != source['source_sha256']:
                    raise ValueError('reviewed source hash or issuer changed')
                report['downloaded_or_cached'].append(source['source_id'])
            except Exception as exc:
                report['errors'].append({'source_id': source['source_id'], 'error': str(exc)})
    _json(client.root / 'reviewed_source_download_report.json', report)
    return report
