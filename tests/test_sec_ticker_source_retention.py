"""Official ticker downloads retain original bytes before Silver projection."""
from io import BytesIO
from pathlib import Path
from hashlib import sha256
import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
import pandas as pd

from engine.extractors.sec_filings import download_sec_company_tickers, download_us_companyfacts
from engine.transformers.sec_filings import load_sec_ticker_map


def test_download_retains_exact_response_and_all_share_class_rows(tmp_path):
    raw = b'{"0":{"cik_str":123,"ticker":"ABC","title":"Example Inc."}, "1":{"cik_str":123,"ticker":"ABC-B","title":"Example Inc."}}\n'
    output = tmp_path / 'silver/sec/company_tickers.csv'
    bronze = tmp_path / 'bronze/sec/company-tickers'
    # Mock only the external urllib HTTP boundary, not Arcana collaborators.
    with patch('engine.extractors._internal.sec_filings.urlopen', return_value=BytesIO(raw)):
        frame = download_sec_company_tickers(output, source_dir=bronze)
    assert frame.ticker.tolist() == ['ABC', 'ABC-B']
    responses = list(bronze.rglob('response.json'))
    assert len(responses) == 1
    assert responses[0].read_bytes() == raw
    metadata = json.loads(responses[0].with_name('metadata.json').read_bytes())
    assert metadata['source_url'] == 'https://www.sec.gov/files/company_tickers.json'
    assert metadata['source_sha256'] == sha256(raw).hexdigest()
    assert metadata['retrieved_at'].endswith('+00:00')
    assert metadata['historical_listing_intervals_verified'] is False
    provenance = json.loads(output.with_suffix('.metadata.json').read_bytes())
    assert provenance['source_path'] == str(responses[0].resolve())
    assert provenance['rows'] == 2


def test_changed_response_preserves_prior_original_and_projection(tmp_path):
    bronze = tmp_path / 'bronze/sec/company-tickers'
    output = tmp_path / 'silver/sec/company_tickers.csv'
    first = b'{"0":{"cik_str":123,"ticker":"ABC","title":"Old Issuer"}}'
    second = b'{"0":{"cik_str":456,"ticker":"ABC","title":"New Issuer"}}'
    with patch('engine.extractors._internal.sec_filings.urlopen', return_value=BytesIO(first)):
        download_sec_company_tickers(output, source_dir=bronze)
    previous = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with patch('engine.extractors._internal.sec_filings.urlopen', return_value=BytesIO(second)):
        frame = download_sec_company_tickers(output, source_dir=bronze)
    assert frame.cik.tolist() == ['456']
    assert {p.read_bytes() for p in bronze.rglob('response.json')} == {first, second}
    for path, raw in previous.items():
        if path not in {output, output.with_suffix('.metadata.json')}:
            assert path.read_bytes() == raw


@pytest.mark.parametrize('raw', [b'', b'<html>blocked</html>', b'{}',
    b'{"0":{"cik_str":true,"ticker":"ABC","title":"Invalid CIK"}}'])
def test_unusable_response_is_retained_without_replacing_silver(tmp_path, raw):
    output = tmp_path / 'silver/sec/company_tickers.csv'
    output.parent.mkdir(parents=True)
    output.write_bytes(b'previous verified projection')
    bronze = tmp_path / 'bronze/sec/company-tickers'
    with patch('engine.extractors._internal.sec_filings.urlopen', return_value=BytesIO(raw)):
        with pytest.raises(ValueError):
            download_sec_company_tickers(output, source_dir=bronze)
    assert output.read_bytes() == b'previous verified projection'
    response, = bronze.rglob('response.json')
    assert response.read_bytes() == raw


def test_http_failure_retains_body_and_status_without_a_projection(tmp_path):
    output = tmp_path / 'silver/sec/company_tickers.csv'
    bronze = tmp_path / 'bronze/sec/company-tickers'
    failure = HTTPError('https://www.sec.gov/files/company_tickers.json', 503,
        'Service Unavailable', {}, BytesIO(b'<html>temporary failure</html>'))
    with patch('engine.extractors._internal.sec_filings.urlopen', side_effect=failure):
        with pytest.raises(HTTPError):
            download_sec_company_tickers(output, source_dir=bronze)
    response, = bronze.rglob('response.bin')
    assert response.read_bytes() == b'<html>temporary failure</html>'
    assert json.loads(response.with_name('metadata.json').read_bytes())['http_status'] == 503
    assert not output.exists()


@pytest.mark.integration
def test_explicit_legacy_input_is_not_replaced_by_the_new_default_projection(tmp_path):
    legacy = Path(__file__).resolve().parents[1] / 'data-lake/meta/sec_company_tickers.csv'
    if not legacy.exists():
        pytest.skip('No retained legacy SEC projection on this installation')
    expected = pd.read_csv(legacy, dtype=str, keep_default_na=False).sort_values('ticker').reset_index(drop=True)
    actual = load_sec_ticker_map(path=legacy, aliases_path=tmp_path / 'no_aliases.csv')
    pd.testing.assert_frame_equal(actual, expected)


@pytest.mark.parametrize('symbols', [['ABC-B'], None])
def test_companyfacts_resolves_every_class_but_downloads_once_per_issuer(tmp_path, symbols):
    mapping = tmp_path / 'silver/company_tickers.csv'
    mapping.parent.mkdir()
    pd.DataFrame([dict(cik='123', ticker=code, title='Example Inc.')
        for code in ['ABC', 'ABC-B']]).to_csv(mapping, index=False)
    with patch('engine.extractors._internal.sec_filings.urlopen',
            return_value=BytesIO(b'{"cik":123,"facts":{}}')) as http:
        written = download_us_companyfacts(symbols=symbols, ticker_map_path=mapping,
            output_dir=tmp_path / 'bronze/companyfacts', sleep_seconds=0, force=True)
    assert [path.name for path in written] == ['CIK0000000123.json']
    assert http.call_count == 1


def test_literal_na_ticker_is_preserved_by_public_financial_readers(tmp_path):
    mapping = tmp_path / 'silver/company_tickers.csv'
    mapping.parent.mkdir()
    mapping.write_text('cik,ticker,title\n123,NA,Literal ticker issuer\n', encoding='utf-8')
    loaded = load_sec_ticker_map(path=mapping, aliases_path=tmp_path / 'no_aliases.csv')
    assert loaded.ticker.tolist() == ['NA']
    with patch('engine.extractors._internal.sec_filings.urlopen',
            return_value=BytesIO(b'{"cik":123,"facts":{}}')):
        written = download_us_companyfacts(symbols=['NA'], ticker_map_path=mapping,
            output_dir=tmp_path / 'bronze/companyfacts', sleep_seconds=0)
    assert len(written) == 1
