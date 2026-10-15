"""SQLite persistence for the queue and the download history."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import DownloadItem, State
from .util import data_dir, expand

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS downloads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT    NOT NULL,
    title          TEXT    DEFAULT '',
    state          TEXT    NOT NULL DEFAULT 'queued',
    backend        TEXT    DEFAULT '',
    progress       REAL    DEFAULT 0,
    downloaded     INTEGER DEFAULT 0,
    total          INTEGER,
    error          TEXT    DEFAULT '',
    output_path    TEXT    DEFAULT '',
    extractor      TEXT    DEFAULT '',
    playlist_title TEXT    DEFAULT '',
    playlist_index INTEGER,
    duration       REAL,
    attempts       INTEGER DEFAULT 0,
    priority       INTEGER DEFAULT 1,
    options        TEXT    DEFAULT '{}',
    created_at     REAL    DEFAULT 0,
    started_at     REAL,
    finished_at    REAL,
    meta           TEXT    DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_downloads_state    ON downloads(state);
CREATE INDEX IF NOT EXISTS idx_downloads_priority ON downloads(priority, created_at);
CREATE INDEX IF NOT EXISTS idx_downloads_url      ON downloads(url);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER,
    at      REAL,
    kind    TEXT,
    payload TEXT
);
"""

_COLUMNS = (
    "url", "title", "state", "backend", "progress", "downloaded", "total", "error",
    "output_path", "extractor", "playlist_title", "playlist_index", "duration",
    "attempts", "priority", "options", "created_at", "started_at", "finished_at", "meta",
)


class Store:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: Path | str | None = None) -> None:
        if path is None:
            path = data_dir() / "omadown.db"
        self.path = expand(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ plumbing
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    # --------------------------------------------------------------------- write
    def add(self, item: DownloadItem) -> int:
        cur = self._execute(
            f"INSERT INTO downloads ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(_COLUMNS))})",
            item.to_row(),
        )
        item.id = int(cur.lastrowid or 0)
        return item.id

    def update(self, item: DownloadItem) -> None:
        if item.id is None:
            raise ValueError("cannot update an item without an id")
        assignments = ", ".join(f"{c} = ?" for c in _COLUMNS)
        self._execute(
            f"UPDATE downloads SET {assignments} WHERE id = ?",
            (*item.to_row(), item.id),
        )

    def set_state(self, item_id: int, state: str, **kwargs: Any) -> None:
        fields = {"state": state, **kwargs}
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self._execute(
            f"UPDATE downloads SET {assignments} WHERE id = ?",
            (*fields.values(), item_id),
        )

    def remove(self, ids: Iterable[int]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        marks = ", ".join("?" * len(ids))
        cur = self._execute(f"DELETE FROM downloads WHERE id IN ({marks})", ids)
        return cur.rowcount

    def clear(self, *, states: Sequence[str] | None = None, everything: bool = False) -> int:
        if everything:
            return self._execute("DELETE FROM downloads").rowcount
        if not states:
            states = [State.DONE.value, State.FAILED.value, State.CANCELLED.value]
        marks = ", ".join("?" * len(states))
        return self._execute(f"DELETE FROM downloads WHERE state IN ({marks})", list(states)).rowcount

    def log_event(self, kind: str, item_id: int | None = None, payload: str = "") -> None:
        self._execute(
            "INSERT INTO events (item_id, at, kind, payload) VALUES (?, ?, ?, ?)",
            (item_id, time.time(), kind, payload),
        )

    def reset_interrupted(self) -> int:
        """On startup, requeue anything that was mid-flight (Downie's queue reset)."""
        cur = self._execute(
            "UPDATE downloads SET state = ?, progress = 0, downloaded = 0, error = '' "
            "WHERE state IN (?, ?, ?)",
            (State.QUEUED.value, State.PREPARING.value, State.DOWNLOADING.value,
             State.POSTPROCESSING.value),
        )
        return cur.rowcount

    def requeue(self, ids: Iterable[int]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        marks = ", ".join("?" * len(ids))
        cur = self._execute(
            f"UPDATE downloads SET state = ?, progress = 0, downloaded = 0, error = '', "
            f"started_at = NULL, finished_at = NULL WHERE id IN ({marks})",
            [State.QUEUED.value, *ids],
        )
        return cur.rowcount

    # ---------------------------------------------------------------------- read
    def get(self, item_id: int) -> DownloadItem | None:
        rows = self._query("SELECT * FROM downloads WHERE id = ?", (item_id,))
        return DownloadItem.from_row(rows[0]) if rows else None

    def list(
        self,
        *,
        states: Sequence[str] | None = None,
        limit: int = 0,
        order: str = "priority ASC, created_at ASC",
    ) -> list[DownloadItem]:
        sql = "SELECT * FROM downloads"
        params: list[Any] = []
        if states:
            marks = ", ".join("?" * len(states))
            sql += f" WHERE state IN ({marks})"
            params.extend(states)
        sql += f" ORDER BY {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [DownloadItem.from_row(r) for r in self._query(sql, params)]

    def queue(self) -> list[DownloadItem]:
        return self.list(states=[State.QUEUED.value, State.PAUSED.value,
                                 State.PREPARING.value, State.DOWNLOADING.value,
                                 State.POSTPROCESSING.value])

    def history(self, limit: int = 100) -> list[DownloadItem]:
        return self.list(
            states=[State.DONE.value, State.FAILED.value, State.CANCELLED.value],
            limit=limit,
            order="finished_at DESC, created_at DESC",
        )

    def has_url(self, url: str, states: Sequence[str] | None = None) -> bool:
        sql = "SELECT 1 FROM downloads WHERE url = ?"
        params: list[Any] = [url]
        if states:
            marks = ", ".join("?" * len(states))
            sql += f" AND state IN ({marks})"
            params.extend(states)
        sql += " LIMIT 1"
        return bool(self._query(sql, params))

    def stats(self) -> dict[str, Any]:
        rows = self._query("SELECT state, COUNT(*) AS n, SUM(total) AS bytes FROM downloads GROUP BY state")
        counts = {r["state"]: r["n"] for r in rows}
        total_bytes = sum(int(r["bytes"] or 0) for r in rows)
        return {
            "counts": counts,
            "total": sum(counts.values()),
            "bytes": total_bytes,
            "active": sum(counts.get(s.value, 0) for s in
                          (State.PREPARING, State.DOWNLOADING, State.POSTPROCESSING)),
            "queued": counts.get(State.QUEUED.value, 0),
            "done": counts.get(State.DONE.value, 0),
            "failed": counts.get(State.FAILED.value, 0),
        }

    def prune_history(self, keep: int) -> int:
        if keep <= 0:
            return 0
        rows = self._query(
            "SELECT id FROM downloads WHERE state IN (?, ?, ?) "
            "ORDER BY finished_at DESC, created_at DESC LIMIT -1 OFFSET ?",
            (State.DONE.value, State.FAILED.value, State.CANCELLED.value, keep),
        )
        return self.remove([r["id"] for r in rows])
