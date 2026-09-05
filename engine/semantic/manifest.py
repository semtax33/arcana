from __future__ import annotations

from hashlib import sha256
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


class RuleBundleIntegrityError(ValueError):
    pass


_V2_REQUIRED_FIELDS = {
    "bundle_id",
    "schema",
    "engine",
    "hash",
    "created_at",
    "parent",
    "golden_corpus_hash",
    "test_suite_hash",
}


def validate_rule_manifest(manifest: dict[str, Any], *, path: Path | None = None) -> None:
    if int(manifest.get("manifest_version", 1)) < 2:
        return
    missing = sorted(_V2_REQUIRED_FIELDS - set(manifest))
    if missing:
        raise RuleBundleIntegrityError(
            f"semantic rule manifest v2 missing fields {missing}: {path or '<memory>'}"
        )
    if str(manifest["hash"]) != f"sha256:{manifest.get('sha256', '')}":
        raise RuleBundleIntegrityError("manifest hash and sha256 compatibility field disagree")
    for field in ("golden_corpus_hash", "test_suite_hash"):
        value = str(manifest.get(field) or "")
        if not value.startswith("sha256:") or len(value) != 71:
            raise RuleBundleIntegrityError(f"invalid {field} in semantic rule manifest")
    parent = manifest.get("parent")
    if not isinstance(parent, dict) or not parent.get("bundle_id") or not parent.get("hash"):
        raise RuleBundleIntegrityError("manifest parent must contain bundle_id and hash")
    try:
        datetime.fromisoformat(str(manifest["created_at"]))
    except ValueError as exc:
        raise RuleBundleIntegrityError("manifest created_at must be ISO-8601") from exc


def _manifest_bundle(path: Path, manifest: dict[str, Any]) -> Path:
    validate_rule_manifest(manifest, path=path)
    relative = str(manifest.get("active_bundle") or "")
    expected_hash = str(manifest.get("sha256") or "")
    if not relative or not expected_hash:
        raise RuleBundleIntegrityError(f"incomplete semantic rule manifest: {path}")
    bundle = (path.parent / relative).resolve()
    if not bundle.is_file():
        raise RuleBundleIntegrityError(f"semantic rule bundle does not exist: {bundle}")
    actual_hash = sha256(bundle.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        raise RuleBundleIntegrityError(
            f"semantic rule bundle checksum mismatch: expected={expected_hash} actual={actual_hash}"
        )
    data = yaml.safe_load(bundle.read_text(encoding="utf-8")) or {}
    expected_schema = manifest.get("schema_version")
    if expected_schema is not None and int(data.get("schema_version", 0)) != int(expected_schema):
        raise RuleBundleIntegrityError(
            f"semantic rule schema mismatch: expected={expected_schema} actual={data.get('schema_version')}"
        )
    return bundle


def resolve_rule_bundle(path: str | Path) -> Path:
    """Resolve a bundle, manifest, or YAML alias and verify immutable manifests."""

    current = Path(path).resolve()
    visited: set[Path] = set()
    for _ in range(8):
        if current in visited:
            raise RuleBundleIntegrityError(f"cyclic semantic rule alias: {current}")
        visited.add(current)
        if not current.is_file():
            raise FileNotFoundError(current)
        if current.suffix.lower() == ".json":
            manifest = json.loads(current.read_text(encoding="utf-8"))
            return _manifest_bundle(current, manifest)
        data = yaml.safe_load(current.read_text(encoding="utf-8")) or {}
        alias = data.get("alias_of")
        if not alias:
            return current
        current = (current.parent / str(alias)).resolve()
    raise RuleBundleIntegrityError(f"semantic rule alias depth exceeded: {path}")
