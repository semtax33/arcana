from __future__ import annotations

import os
from typing import Any


DEFAULT_CLICKHOUSE_CONFIG = {
    "host": "127.0.0.1",
    "port": 8123,
    "username": "default",
    "password": "",
    "database": "arcana",
}


def get_clickhouse_client(**overrides: Any) -> Any:
    import clickhouse_connect

    config = {
        "host": os.getenv("CLICKHOUSE_HOST", DEFAULT_CLICKHOUSE_CONFIG["host"]),
        "port": int(os.getenv("CLICKHOUSE_PORT", str(DEFAULT_CLICKHOUSE_CONFIG["port"]))),
        "username": os.getenv("CLICKHOUSE_USERNAME", DEFAULT_CLICKHOUSE_CONFIG["username"]),
        "password": os.getenv("CLICKHOUSE_PASSWORD", DEFAULT_CLICKHOUSE_CONFIG["password"]),
        "database": os.getenv("CLICKHOUSE_DATABASE", DEFAULT_CLICKHOUSE_CONFIG["database"]),
    }
    config.update(overrides)
    return clickhouse_connect.get_client(**config)
