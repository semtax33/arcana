from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ScreenerStrategyRepository:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def list(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, name, created_at, updated_at
                FROM screener_strategies
                ORDER BY updated_at DESC, id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, strategy_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, name, strategy_json, created_at, updated_at
                FROM screener_strategies
                WHERE id = ?
                """,
                (int(strategy_id),),
            ).fetchone()

        if row is None:
            return None
        return self._decode_row(row)

    def save(self, name: str, strategy: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(strategy, ensure_ascii=False, sort_keys=True)
        now = _utc_now()

        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT id, created_at
                FROM screener_strategies
                WHERE name = ?
                """,
                (name,),
            ).fetchone()

            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO screener_strategies (name, strategy_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (name, payload, now, now),
                )
                strategy_id = int(cursor.lastrowid)
            else:
                strategy_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE screener_strategies
                    SET strategy_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (payload, now, strategy_id),
                )

        saved = self.get(strategy_id)
        if saved is None:  # pragma: no cover - defensive guard
            raise RuntimeError("saved screener strategy could not be reloaded")
        return saved

    def delete(self, strategy_id: int) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM screener_strategies WHERE id = ?",
                (int(strategy_id),),
            )
            deleted = cursor.rowcount > 0
        return deleted

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        self._ensure_schema(connection)
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS screener_strategies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                strategy_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_screener_strategies_name
            ON screener_strategies(name)
            """
        )

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["strategy"] = json.loads(str(data.pop("strategy_json")))
        return data


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")