from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from engine.core.paths import DATA_LAKE
from engine.extractors.pqci_inputs import (
    SUPPORTED_SOURCES,
    collect_sources,
    default_pqci_config,
    load_pqci_config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect BLS, Census, BEA, EIA, FDIC, NASS, and FHFA inputs for "
            "P/Q/C/I forecasting into data-lake/bronze/pqci/{source}."
        )
    )
    parser.add_argument(
        "--source",
        dest="sources",
        action="append",
        choices=["all", *SUPPORTED_SOURCES],
        help="source to collect; repeat the option for multiple sources (default: all)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="JSON file overriding non-secret source query settings",
    )
    parser.add_argument(
        "--data-lake-root",
        type=Path,
        default=DATA_LAKE.root,
        help="data lake root containing the bronze directory",
    )
    parser.add_argument(
        "--skip-missing-keys",
        action="store_true",
        help="skip sources whose required environment API key is not set",
    )
    parser.add_argument(
        "--print-default-config",
        action="store_true",
        help="print the default non-secret query config and exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.print_default_config:
        print(json.dumps(default_pqci_config(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    config = load_pqci_config(args.config)
    results = collect_sources(
        args.sources,
        config=config,
        data_lake_root=args.data_lake_root,
        skip_missing_keys=args.skip_missing_keys,
    )
    print(json.dumps([result.to_dict() for result in results], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
