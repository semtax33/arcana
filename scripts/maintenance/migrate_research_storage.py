"""Relocate the inspected docs/tests evidence bundles without changing sources.

Run without --apply to record an inventory; --apply consumes that inventory.
The journal retains original hashes and preimages of rebased metadata.
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "docs_tests_20260910"
STATE = ROOT / "data-lake/silver/storage_migrations" / MIGRATION
PREIMAGES = ROOT / "data-lake/bronze/storage_migrations" / MIGRATION / "preimages"
RAW_EXTENSIONS = {".html", ".htm", ".xml", ".zip", ".pdf", ".xls", ".jpg", ".png"}
PROVENANCE_NAMES = {"download_errors.json"}


def digest(raw):
    return sha256(raw).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def source_layer(path):
    if path.suffix.lower() in RAW_EXTENSIONS:
        return "bronze", "source_document"
    if path.name.endswith(".metadata.json") or path.name.endswith("_request.json"):
        return "bronze", "collection_metadata"
    if path.with_suffix(path.suffix + ".metadata.json").exists():
        return "bronze", "source_response"
    if path.suffix == ".json" and (
        "manifest" in path.stem and not path.stem.startswith(("local_", "code_"))
        or path.name in PROVENANCE_NAMES
    ):
        return "bronze", "collection_manifest"
    return "silver", "research_intermediate"


def inventory():
    entries = []
    groups = [p for p in (ROOT / "docs/research").iterdir() if p.is_dir()]
    groups.append(ROOT / "tests/fixtures/stock_splits")
    for group in sorted(groups):
        assert group.resolve().is_relative_to(ROOT), "Source outside workspace"
        if group.parts[-3:] == ("tests", "fixtures", "stock_splits"):
            suffix = Path("fixtures/stock_splits")
        elif "eps" in group.name:
            suffix = Path("research/financial_statements/eps") / group.name
        else:
            market = "us" if group.name.startswith("us_") else "kr"
            suffix = Path("research/stock_splits") / market / group.name
        for path in sorted(group.rglob("*")):
            if not path.is_file() or path.suffix in {".py", ".pyc", ".md"}:
                continue
            layer, role = source_layer(path)
            target = ROOT / "data-lake" / layer / suffix / path.relative_to(group)
            assert target.resolve().is_relative_to((ROOT / "data-lake" / layer).resolve())
            assert not target.exists(), f"Destination already exists: {target}"
            raw = path.read_bytes()
            entries.append(dict(source=path.relative_to(ROOT).as_posix(),
                destination=target.relative_to(ROOT).as_posix(), layer=layer, role=role,
                source_bytes=len(raw), original_sha256=digest(raw)))
    return entries


def path_rebaser(entries):
    mapping = {(ROOT / r["source"]).resolve(): (ROOT / r["destination"]).resolve() for r in entries}
    replacements = {}
    for old, new in mapping.items():
        for a, b in [(str(old), str(new)), (old.as_posix(), new.as_posix()),
                     (old.relative_to(ROOT).as_posix(), new.relative_to(ROOT).as_posix())]:
            replacements[a] = b
    pattern = re.compile("|".join(re.escape(k) for k in sorted(replacements, key=len, reverse=True)))

    def convert(value, old_parent, new_parent):
        if isinstance(value, dict):
            return {k: convert(v, old_parent, new_parent) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v, old_parent, new_parent) for v in value]
        if not isinstance(value, str) or value.startswith(("https://", "http://")):
            return value
        changed = pattern.sub(lambda m: replacements[m.group()], value)
        if changed != value:
            return changed
        if len(value) < 500 and "\n" not in value and not re.search(r"[<>|]", value):
            try:
                candidate = (old_parent / value).resolve()
                if candidate in mapping:
                    return Path(os.path.relpath(mapping[candidate], new_parent)).as_posix()
            except (OSError, ValueError):
                pass
        return value
    return mapping, convert


def apply(entries):
    mapping, convert = path_rebaser(entries)
    results = []
    for record in entries:
        source, target = ROOT / record["source"], ROOT / record["destination"]
        assert source.resolve().is_relative_to(ROOT / source.relative_to(ROOT).parts[0])
        assert source.relative_to(ROOT).parts[0] in {"docs", "tests"}
        assert target.resolve().is_relative_to(ROOT / "data-lake" / record["layer"])
        raw = source.read_bytes()
        assert digest(raw) == record["original_sha256"], f"Source changed since inventory: {source}"
        updated = raw
        if source.suffix == ".json" and record["role"] != "source_response":
            value = json.loads(raw.decode("utf-8-sig"))
            changed = convert(value, source.parent, target.parent)
            if changed != value:
                updated = (json.dumps(changed, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if updated != raw:
            backup = PREIMAGES / record["source"]
            backup.parent.mkdir(parents=True, exist_ok=True)
            assert not backup.exists()
            backup.write_bytes(raw)
        assert not target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        # Both absolute paths were checked inside the named workspace above.
        source.rename(target)
        if updated != raw:
            temporary = target.with_suffix(target.suffix + ".relocating")
            temporary.write_bytes(updated)
            temporary.replace(target)
        actual = digest(target.read_bytes())
        assert actual == digest(updated)
        if record["role"] in {"source_document", "source_response"}:
            assert actual == record["original_sha256"], "Original provider bytes changed"
        results.append(dict(record, relocated_sha256=actual, metadata_paths_rebased=updated != raw))
        if len(results) % 100 == 0:
            write_json(STATE / "journal.json", dict(status="running", files=results))
    write_json(STATE / "journal.json", dict(status="moved", files=results))
    return results


def verify():
    journal = json.loads((STATE / "journal.json").read_text(encoding="utf-8"))
    entries = journal["files"]
    destinations = {(ROOT / r["destination"]).resolve() for r in entries}
    checks, errors = Counter(), []

    def inspect(value, parent, owner):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and (key.endswith("path") or key in {"file", "filename"}):
                    if not item.startswith(("http://", "https://")) and len(item) < 500:
                        try:
                            candidates = [(parent / item).resolve(), (ROOT / item).resolve()]
                            target = next((p for p in candidates if p in destinations), None)
                        except (ValueError, OSError):
                            target = None
                        if target is not None:
                            checks["moved_file_references"] += 1
                            expected = value.get(key.removesuffix("path") + "sha256")
                            if expected is None and key in {"path", "file", "filename", "local_path"}:
                                expected = value.get("source_sha256") or value.get("sha256")
                            if expected is not None:
                                checks["source_digest_references"] += 1
                                if digest(target.read_bytes()) != expected:
                                    errors.append(dict(owner=owner, field=key, reason="referenced_digest_differs"))
                inspect(item, parent, owner)
        elif isinstance(value, list):
            for item in value:
                inspect(item, parent, owner)

    for record in entries:
        path = ROOT / record["destination"]
        assert path.resolve().is_relative_to(ROOT / "data-lake" / record["layer"])
        assert not (ROOT / record["source"]).exists(), record["source"]
        raw = path.read_bytes()
        assert digest(raw) == record["relocated_sha256"], record["destination"]
        if record["role"] in {"source_document", "source_response"}:
            assert digest(raw) == record["original_sha256"]
            checks["unchanged_source_files"] += 1
        elif path.suffix == ".json":
            inspect(json.loads(raw.decode("utf-8-sig")), path.parent, record["destination"])
        if record["metadata_paths_rebased"]:
            assert digest((PREIMAGES / record["source"]).read_bytes()) == record["original_sha256"]
            checks["preserved_metadata_preimages"] += 1
    remaining = [p.relative_to(ROOT).as_posix() for folder in ("docs", "tests")
                 for p in (ROOT / folder).rglob("*")
                 if p.is_file() and p.suffix.lower() in {".html", ".htm", ".json", ".csv"}]
    report = dict(status="passed" if not errors and not remaining else "failed", files=len(entries),
                  bytes=sum(r["source_bytes"] for r in entries),
                  by_layer=dict(Counter(r["layer"] for r in entries)), checks=dict(checks),
                  errors=errors, remaining_docs_tests_data=remaining)
    write_json(STATE / "verification.json", report)
    assert report["status"] == "passed", "Inspect verification.json"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--verify", action="store_true", help="Verify the existing relocation journal without moving files.")
    args = parser.parse_args()
    plan_path = STATE / "inventory.json"
    if args.verify:
        print(verify())
        return
    if not args.apply:
        assert not plan_path.exists(), "Preserve the existing inventory"
        entries = inventory()
        write_json(plan_path, dict(schema_version=1, workspace=str(ROOT), files=entries))
    else:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        assert plan["workspace"] == str(ROOT)
        assert not (STATE / "journal.json").exists(), "Inspect an existing migration journal before resuming"
        entries = apply(plan["files"])
    print(dict(files=len(entries), by_layer=dict(Counter(r["layer"] for r in entries)),
               by_role=dict(Counter(r["role"] for r in entries)), bytes=sum(r["source_bytes"] for r in entries)))


if __name__ == "__main__":
    main()
