from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path
import sys

from bs4 import BeautifulSoup


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.semantic import NarrativeAccountScanner, load_semantic_mapping_rules
from engine.transformers._internal.dart_filings import normalize_account_name


RULES = PROJECT_ROOT / "data-lake" / "meta" / "rules" / "semantic_kr_v2.yaml"
COMMENT_ROOT = PROJECT_ROOT / "data-lake" / "bronze" / "dart" / "finance-comment"
OUTPUT = PROJECT_ROOT / "deliverables" / "narrative_account_candidate_audit.json"


def evenly_spaced(paths: list[Path], limit: int | None) -> list[Path]:
    if limit is None or limit >= len(paths):
        return paths
    if limit <= 0:
        return []
    if limit == 1:
        return [paths[len(paths) // 2]]
    return [paths[round(index * (len(paths) - 1) / (limit - 1))] for index in range(limit)]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover account/value candidates embedded in DART narrative text"
    )
    parser.add_argument("--rules", type=Path, default=RULES)
    parser.add_argument("--comment-root", type=Path, default=COMMENT_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--max-files", type=int, default=200)
    parser.add_argument("--min-bytes", type=int, default=1_000)
    parser.add_argument("--max-examples", type=int, default=100)
    args = parser.parse_args()

    ruleset = load_semantic_mapping_rules(
        [args.rules], text_normalizer=normalize_account_name
    )
    scanner = NarrativeAccountScanner.from_ruleset(ruleset)
    population = sorted(
        path
        for path in args.comment_root.rglob("*.html")
        if path.stat().st_size >= args.min_bytes
    )
    selected = evenly_spaced(population, args.max_files)
    canonical_counts: Counter[str] = Counter()
    files_with_candidates = 0
    candidate_count = 0
    confirmed_count = 0
    examples: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []

    for path in selected:
        try:
            html = path.read_text(encoding="utf-8", errors="ignore")
            candidates = scanner.scan_soup(BeautifulSoup(html, "lxml"))
        except Exception as exc:
            failures.append({"file": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        if candidates:
            files_with_candidates += 1
        candidate_count += len(candidates)
        confirmed_count += len(scanner.confirmed(candidates))
        for candidate in candidates:
            for canonical_id in candidate.canonical_ids:
                canonical_counts[canonical_id] += 1
            if len(examples) < args.max_examples:
                examples.append({"file": str(path), **asdict(candidate)})

    report = {
        "mode": "discovery_only_no_production_emit",
        "population_file_count": len(population),
        "sample_method": "evenly_spaced_over_sorted_paths",
        "sample_file_count": len(selected),
        "files_with_candidates": files_with_candidates,
        "candidate_count": candidate_count,
        "confirmed_candidate_count": confirmed_count,
        "review_required_candidate_count": candidate_count - confirmed_count,
        "parse_failure_count": len(failures),
        "top_canonical_candidates": [
            {"canonical_id": canonical_id, "candidate_count": count}
            for canonical_id, count in canonical_counts.most_common(30)
        ],
        "examples": examples,
        "failures": failures[:30],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "population_file_count": report["population_file_count"],
                "sample_file_count": report["sample_file_count"],
                "files_with_candidates": report["files_with_candidates"],
                "candidate_count": report["candidate_count"],
                "confirmed_candidate_count": report["confirmed_candidate_count"],
                "review_required_candidate_count": report[
                    "review_required_candidate_count"
                ],
                "parse_failure_count": report["parse_failure_count"],
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
