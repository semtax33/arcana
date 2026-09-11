"""Compare all prepared factor events, retaining source and calculation changes."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', required=True, type=Path)
    parser.add_argument('--after', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    assert output.is_relative_to((DATA_LAKE.root / 'silver').resolve()) and not output.exists()
    output.mkdir(parents=True)
    paths = [folder / 'summary.json' for folder in (args.before, args.after)]
    reports = [json.loads(path.read_bytes()) for path in paths]
    assert all(report['status'] == 'prepared_not_independently_validated' for report in reports)
    cases = [{(case['symbol'], case['basis']): case for case in report['cases']} for report in reports]
    assert cases[0].keys() == cases[1].keys() and len(cases[0]) == 12
    pins = {str(path.resolve()): digest(path) for path in paths}
    results = []
    keys = ['security_id', 'trade_date', 'financial_basis', 'factor_id']
    for key in cases[0]:
        frames = []
        for case in [cases[0][key], cases[1][key]]:
            assert digest(case['path']) == case['sha256']
            pins[str(Path(case['path']).resolve())] = case['sha256']
            frame = pd.read_parquet(case['path'])
            assert not frame.duplicated(keys).any()
            frames.append(frame[keys + ['factor_value']])
        compared = frames[0].merge(frames[1], on=keys, how='outer', suffixes=('_before', '_after'), indicator=True)
        compared['value_changed'] = ~np.isclose(compared.factor_value_before,
            compared.factor_value_after, rtol=1e-12, atol=1e-12, equal_nan=True)
        changed = compared[compared.value_changed | compared._merge.ne('both')].copy()
        folder = output / key[0] / key[1]
        folder.mkdir(parents=True)
        changed.to_parquet(folder / 'changed.parquet', index=False)
        results.append(dict(symbol=key[0], basis=key[1], compared_events=len(compared),
            changed_events=len(changed), changed_factors=changed.groupby('factor_id').size().to_dict(),
            changed_from=str(changed.trade_date.min()) if len(changed) else None,
            changed_through=str(changed.trade_date.max()) if len(changed) else None,
            difference_path=str(folder / 'changed.parquet'), difference_sha256=digest(folder / 'changed.parquet')))
        print(key[0], key[1], 'compared', len(compared), 'changed', len(changed), flush=True)
    assert all(digest(path) == value for path, value in pins.items())
    report = dict(status='all_prepared_event_changes_enumerated', cases=results,
        input_pins=pins, runner_sha256=digest(__file__), independent_account_validation=False,
        native_published=False, snapshots_published=False, coverage_complete=False)
    (output / 'summary.json').write_text(json.dumps(report, indent=2), 'utf-8')


if __name__ == '__main__':
    main()
