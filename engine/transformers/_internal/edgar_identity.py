from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from engine.core.paths import DATA_LAKE


DEFAULT_EDGAR_IDENTITY = "Arcana contact@example.com"
DEFAULT_EDGAR_LOCAL_DATA_DIR = DATA_LAKE.root / "cache" / "edgar"
EDGAR_CACHE_MIGRATION_MARKERS = (
    ".locale_fix_457_applied",
    ".empty_response_fix_672_applied",
)
EDGAR_IDENTITY_ENV_NAMES = (
    "EDGAR_IDENTITY",
    "SEC_IDENTITY",
    "SEC_USER_AGENT",
    "SEC_USERAGENT",
)


def configure_edgar_data_directory(
    data_dir: str | Path | None = None,
) -> Path:
    """Keep edgartools data and HTTP caches inside the project data lake."""
    resolved = Path(data_dir or DEFAULT_EDGAR_LOCAL_DATA_DIR).resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    os.environ["EDGAR_LOCAL_DATA_DIR"] = str(resolved)
    cache_dir = resolved / "_tcache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for marker_name in EDGAR_CACHE_MIGRATION_MARKERS:
        (cache_dir / marker_name).touch(exist_ok=True)
    return resolved


def resolve_edgar_identity() -> str:
    for env_name in EDGAR_IDENTITY_ENV_NAMES:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return DEFAULT_EDGAR_IDENTITY


def configure_edgar_identity(set_identity: Callable[[str], object]) -> str:
    identity = resolve_edgar_identity()
    set_identity(identity)
    return identity


# Every Arcana module that imports edgartools imports this module first. Set the
# location here so edgartools' import-time HTTP cache initialization never falls
# back to the user profile on C:.
configure_edgar_data_directory()
