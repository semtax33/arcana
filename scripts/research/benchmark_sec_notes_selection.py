"""Measure public notes normalization with many unrelated accession rows."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pandas as pd
import yaml
from engine.core.serving_storage import export_json
from engine.transformers.sec_filings import normalize_us_sec_filings


def prepare(bronze, config, rows):
    quarter = bronze / '2026q1_notes'
    quarter.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)
    (quarter / 'sub.tsv').write_text('adsh\tcik\tname\tform\tperiod\tfy\tfp\tfiled\n'
        '0000000123-26-000001\t123\tSynthetic Old Issuer\t10-K\t20251231\t2025\tFY\t20260215\n', 'utf-8')
    num = quarter / 'num.tsv'
    marker = bronze / 'synthetic_fixture.json'
    if not marker.exists() or json.loads(marker.read_bytes())['unrelated_rows'] != rows:
        with num.open('w', encoding='utf-8', newline='') as stream:
            stream.write('adsh\ttag\tversion\tddate\tuom\tdimh\tvalue\n')
            block = 'OTHER\tUnrelatedAccountingTag\tus-gaap/2025\t20251231\tUSD\t0\t999\n'
            for offset in range(0, rows, 10000):
                stream.write(block * min(10000, rows - offset))
            for currency, dimension, value in [('USD', '1', '999'), ('EUR', '0', '555'), ('USD', '0', '77')]:
                stream.write(f'0000000123-26-000001\tResearchAndDevelopmentExpense\tus-gaap/2025\t20251231\t{currency}\t{dimension}\t{value}\n')
        marker.write_text(json.dumps(dict(synthetic=True, unrelated_rows=rows,
            num_sha256=sha256(num.read_bytes()).hexdigest())), 'utf-8')
    (config / 'tickers.csv').write_text('cik,ticker,title\n123,OLD,Synthetic Old Issuer\n', 'utf-8')
    (config / 'canonical.csv').write_text('canonical_id,canonical_nm,fs_type,is_derived,formula,description\nRND,Research and development,IS,FALSE,,\n', 'utf-8')
    (config / 'rules.yaml').write_text(yaml.safe_dump(dict(companyfacts_rules=[], notes_rules=[dict(
        id='synthetic_rnd', canonical_id='RND', fs_type='IS', tags=['ResearchAndDevelopmentExpense'],
        tag_patterns=['research.*development'], amount_policy='as_reported')])), 'utf-8')
    return json.loads(marker.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bronze', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--rows', type=int, default=2000000)
    parser.add_argument('--max-cpu-seconds', type=float)
    args = parser.parse_args()
    fixture = prepare(args.bronze, args.output / 'config', args.rows)
    config, output = args.output / 'config', args.output / args.label
    cpu, wall = time.process_time(), time.perf_counter()
    files = normalize_us_sec_filings(symbols=['OLD'], start_year=2025, end_year=2025,
        filings_dir=args.bronze / 'no-filings', companyfacts_dir=args.bronze / 'no-companyfacts',
        notes_root=args.bronze, output_dir=output / 'normalized', report_metadata_path=output / 'reports.csv',
        mapping_rule_path=config / 'rules.yaml', canonical_csv_path=config / 'canonical.csv',
        ticker_map_path=config / 'tickers.csv', use_filings=False, use_notes=True,
        use_edgartools=False, workers=1, log_progress=False)
    elapsed_cpu, elapsed_wall = time.process_time()-cpu, time.perf_counter()-wall
    assert len(files) == 1
    statement = pd.read_csv(files[0])
    assert statement.canonical_account_id.tolist() == ['RND']
    assert statement.normalized_amount.tolist() == [77]
    result = dict(cpu_seconds=elapsed_cpu, wall_seconds=elapsed_wall, fixture=fixture,
        normalized_sha256=sha256(files[0].read_bytes()).hexdigest(), value=77,
        implementation_sha256=sha256((ROOT / 'engine/transformers/_internal/sec_filings.py').read_bytes()).hexdigest())
    export_json(output / 'benchmark.json', result)
    print(json.dumps(result), flush=True)
    if args.max_cpu_seconds is not None:
        assert elapsed_cpu <= args.max_cpu_seconds, 'Notes selection exceeds the CPU budget'


if __name__ == '__main__':
    main()
