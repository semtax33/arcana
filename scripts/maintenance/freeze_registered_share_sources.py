"""Pin registered source bytes in bronze, independently of mutable provider caches."""
import argparse
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import SourceRefreshLock, sha256_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Source registration audit must be in silver")
    args.output.mkdir(parents=True, exist_ok=False)
    manifest_path = DATA_LAKE.silver("krx", "shares", "historical_sources.json")
    current_input = DATA_LAKE.silver("krx", "shares", "kr_normalized_shares.csv")
    with SourceRefreshLock("kr"):
        if sha256_file(manifest_path) != args.expected_manifest_sha256:
            raise ValueError("Source registration changed")
        input_hash = sha256_file(current_input)
        shutil.copy2(manifest_path, args.output / "registration_before.json")
        manifest = json.loads(manifest_path.read_text("utf-8"))
        sources = []
        for source in manifest["sources"]:
            path = (DATA_LAKE.root / source["path"]).resolve()
            if not path.is_relative_to((DATA_LAKE.root / "bronze").resolve()) or sha256_file(path) != source["sha256"]:
                raise ValueError("Registered original is outside bronze or its bytes changed")
            target = DATA_LAKE.bronze("registered-market-sources", f"sha256={source['sha256']}", path.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
                try:
                    shutil.copyfile(path, temporary)
                    if sha256_file(temporary) != source["sha256"]:
                        raise ValueError("Source changed while archiving")
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
            if sha256_file(target) != source["sha256"]:
                raise ValueError("Existing archived source has different bytes")
            updated = dict(source, path=target.relative_to(DATA_LAKE.root).as_posix())
            updated.setdefault("original_path", source["path"])
            sources.append(updated)
        if sha256_file(manifest_path) != args.expected_manifest_sha256 or sha256_file(current_input) != input_hash:
            raise ValueError("Registration or normalized input changed while archiving")
        manifest["sources"] = sources
        export_json(manifest_path, manifest)
        shutil.copy2(manifest_path, args.output / "registration_after.json")
        report = dict(status="raw_bytes_archived_registration_verified", source_count=len(sources),
            implementation_sha256=sha256_file(__file__), prior_registration_sha256=args.expected_manifest_sha256,
            registration_sha256=sha256_file(manifest_path), default_input_sha256=input_hash,
            default_input_changed=False, source_bytes_changed=False, sources=sources)
        shutil.copy2(__file__, args.output / Path(__file__).name)
        export_json(args.output / "summary.json", report)
        print({k:report[k] for k in ["status", "source_count", "registration_sha256"]}, flush=True)


if __name__ == "__main__":
    main()
