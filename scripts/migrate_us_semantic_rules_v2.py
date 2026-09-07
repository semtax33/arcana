from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.core.paths import DATA_LAKE
from engine.semantic.us_dsl import parse_us_semantic_rules, render_us_semantic_v2


DEFAULT_SOURCE = DATA_LAKE.rules("us_mapping.yaml")
DEFAULT_OUTPUT = DATA_LAKE.rules("semantic_us_v2.arcana")
DEFAULT_MANIFEST = DATA_LAKE.rules("semantic_us_rule_manifest.json")


def migrate_us_semantic_rules_v2(
    source: str | Path = DEFAULT_SOURCE,
    output: str | Path = DEFAULT_OUTPUT,
    manifest_path: str | Path = DEFAULT_MANIFEST,
) -> Path:
    source_path = Path(source)
    output_path = Path(output)
    manifest_path = Path(manifest_path)
    legacy = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
    rendered = render_us_semantic_v2(legacy)
    migrated = parse_us_semantic_rules(rendered).to_legacy_rule_groups()
    if migrated != legacy:
        raise ValueError("US semantic v2 migration is not lossless")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8", newline="\n")
    temporary.replace(output_path)
    bundle_hash = sha256(output_path.read_bytes()).hexdigest()
    source_hash = sha256(source_path.read_bytes()).hexdigest()
    tests_path = PROJECT_ROOT / "tests" / "test_us_semantic_dsl.py"
    tests_hash = sha256(tests_path.read_bytes()).hexdigest()
    manifest = {
        "manifest_version": 2,
        "bundle_id": "arcana.semantic.us.v2",
        "schema": "arcana.sec-semantic/v2",
        "engine": "arcana-sec-semantic-v2",
        "version": 2,
        "hash": f"sha256:{bundle_hash}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "parent": {
            "bundle_id": "arcana.semantic.us.v1",
            "hash": f"sha256:{source_hash}",
        },
        "golden_corpus_hash": f"sha256:{source_hash}",
        "test_suite_hash": f"sha256:{tests_hash}",
        "active_bundle": output_path.name,
        "sha256": bundle_hash,
        "rule_count": sum(len(legacy.get(group, [])) for group in legacy),
        "rule_group_counts": {
            group: len(legacy.get(group, [])) for group in legacy
        },
        "immutability_policy": (
            "A published US bundle is never edited in place; create a new version "
            "and update this manifest after migration and tests pass."
        ),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    manifest_temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest_temporary.replace(manifest_path)
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate US SEC YAML rules to Arcana DSL v2")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    print(migrate_us_semantic_rules_v2(args.source, args.output, args.manifest))


if __name__ == "__main__":
    main()
