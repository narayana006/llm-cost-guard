"""SQLite storage for recorded calls."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,      -- UTC, ISO 8601
    day           TEXT NOT NULL,      -- UTC date, YYYY-MM-DD
    user          TEXT NOT NULL,
    feature       TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'ok'   -- 'ok' or 'blocked'
);
CREATE INDEX IF NOT EXISTS calls_day_user ON calls (day, user);
CREATE INDEX IF NOT EXISTS calls_day_feature ON calls (day, feature);
"""

GROUP_COLUMNS = ("user", "feature", "model", "day")


class SpendStore:
    """A thin, thread-safe wrapper around one SQLite file (or ``:memory:``)."""

    def __init__(self, path: str | Path = "llm_costs.db") -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add(
        self,
        *,
        ts: str,
        day: str,
        user: str,
        feature: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        status: str = "ok",
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO calls (ts, day, user, feature, model, input_tokens,"
                " output_tokens, cost_usd, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, day, user, feature, model, input_tokens, output_tokens, cost_usd, status),
            )

    def spent(self, day: str, column: str | None = None, key: str | None = None) -> float:
        """Total cost on ``day``, optionally restricted to one user/feature/model."""
        query = "SELECT COALESCE(SUM(cost_usd), 0) FROM calls WHERE day = ?"
        params: list = [day]
        if column is not None:
            if column not in GROUP_COLUMNS:
                raise ValueError(f"unknown column '{column}'")
            query += f" AND {column} = ?"
            params.append(key)
        with self._lock:
            return float(self._conn.execute(query, params).fetchone()[0])

    def summary(
        self, by: str = "user", since: str | None = None, until: str | None = None
    ) -> list[dict]:
        """Aggregate rows grouped by user, feature, model or day, highest spend
        first (days are listed in date order). Dates are inclusive."""
        if by not in GROUP_COLUMNS:
            raise ValueError(f"cannot group by '{by}' (choose from {', '.join(GROUP_COLUMNS)})")
        where, params = [], []
        if since:
            where.append("day >= ?")
            params.append(since)
        if until:
            where.append("day <= ?")
            params.append(until)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        order = "key" if by == "day" else "cost_usd DESC, key"
        query = f"""
            SELECT {by} AS key,
                   SUM(status = 'ok') AS calls,
                   SUM(status = 'blocked') AS blocked,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cost_usd) AS cost_usd
            FROM calls {clause} GROUP BY {by} ORDER BY {order}
        """
        with self._lock:
            return [dict(row) for row in self._conn.execute(query, params)]
