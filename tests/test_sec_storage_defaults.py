"""The public SEC extractor initializes the external HTTP cache in Bronze."""
import json
from pathlib import Path
import subprocess
import sys


def test_public_sec_extractor_routes_edgartools_http_cache_to_bronze():
    root = Path(__file__).resolve().parents[1]
    program = ('import json; import engine.extractors.sec_filings; '
        'from edgar.httpclient import get_cache_directory; '
        'print(json.dumps({"cache":str(get_cache_directory())}))')
    result = subprocess.run([sys.executable, '-c', program], cwd=root,
        text=True, capture_output=True, check=True)
    cache = Path(json.loads(result.stdout.splitlines()[-1])['cache']).absolute()
    assert cache == root/'data-lake/bronze/sec/edgar-cache/_tcache'
