"""Collect a bounded batch from a retained SEC disclosure inventory."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine.core.serving_storage import export_json
from engine.extractors.sec_notice_documents import download_sec_notice_documents
from engine.transformers.sec_notice_observations import normalize_sec_notice_documents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--source-dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--gold', type=Path, required=True)
    parser.add_argument('--max-requests', type=int, required=True)
    parser.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args()
    result = download_sec_notice_documents(inventory=json.loads(args.inventory.read_bytes()),
        source_dir=args.source_dir, output_dir=args.output, max_requests=args.max_requests,
        retry_failed=args.retry_failed)
    export_json(args.gold/'notice_documents.json', result)
    observations = normalize_sec_notice_documents(collection=result, output_dir=args.output/'observations')
    export_json(args.gold/'notice_observations.json', observations)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
