from __future__ import annotations

import os
from collections.abc import Callable


DEFAULT_EDGAR_IDENTITY = "Arcana contact@example.com"
EDGAR_IDENTITY_ENV_NAMES = (
    "EDGAR_IDENTITY",
    "SEC_IDENTITY",
    "SEC_USER_AGENT",
    "SEC_USERAGENT",
)


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
