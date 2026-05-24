from __future__ import annotations

from typing import Any

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client


def insert_dataframe(
    table_name: str,
    frame: pd.DataFrame,
    *,
    client: Any = None,
    column_names: list[str] | None = None,
) -> int:
    if frame.empty:
        return 0
    owns_client = client is None
    client = client or get_clickhouse_client()
    try:
        columns = column_names or list(frame.columns)
        client.insert_df(table_name, frame, column_names=columns)
    finally:
        if owns_client:
            client.close()
    return len(frame)

