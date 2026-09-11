"""Collect and index the official all-filer SEC history for identity discovery."""
from pathlib import Path

from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.extractors.sec_submissions import download_sec_submissions_bulk
from engine.transformers.sec_submissions import normalize_sec_submissions_bulk


def run_sec_submissions_refresh(*, source_dir=None, output_dir=None, gold_dir=None, force=False):
    source = download_sec_submissions_bulk(source_dir=source_dir, force=force)
    output = Path(output_dir or DATA_LAKE.silver('sec', 'submissions'))
    result = normalize_sec_submissions_bulk(source=source, output_dir=output)
    result['download_reused'] = source['cache_reused']
    export_json(output / 'latest.json', result)
    export_json(Path(gold_dir or DATA_LAKE.gold('survivorship', 'sec_submissions')) / 'summary.json', result)
    return result
