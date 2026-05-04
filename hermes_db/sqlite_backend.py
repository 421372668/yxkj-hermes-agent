"""SQLite backend implementation."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from hermes_db.config import DBConfig
from hermes_db.interface import CursorResult, DatabaseBackend

logger = logging.getLogger(__name__)


class SQLiteBackend(DatabaseBackend):
    """SQLite database backend.

    Wraps ``sqlite3.Connection`` and exposes the
    :class:`DatabaseBackend` interface.  Thread-safe for the common
    gateway pattern (multiple readers, single writer via WAL mode).
    """

    def __init__(
        self,
        db_path: str = None,
        *,
        db_config: DBConfig = None,
        timeout: float = 1.0,
        check_same_thread: bool = False,
    ):
        self._db_path = db_path
        self._timeout = timeout
        self._check_same_thread = check_same_thread
        self._conn: Optional[sqlite3.Connection] = None
        self._cursor: Optional[sqlite3.Cursor] = None
        self._lock = threading.Lock()

        # Resolve default path from hermes home when none given
        if not self._db_path:
            try:
                from hermes_constants import get_hermes_home
                self._db_path = str(get_hermes_home() / "state.db")
            except ImportError:
                self._db_path = ":memory:"

        self.connect()

    # ── Connection lifecycle ──────────────────────────────────────────

    def connect(self) -> None:
        if self._conn is not None:
            return
        path = self._db_path
        if path and path != ":memory:":
            parent = Path(path).parent
            parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            path or ":memory:",
            check_same_thread=self._check_same_thread,
            timeout=self._timeout,
            isolation_level=None,  # We manage transactions explicitly
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._cursor = None

    def close(self) -> None:
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception:
                    pass
                self._conn.close()
                self._conn = None
                self._cursor = None

    # ── Transaction control ───────────────────────────────────────────

    def begin(self) -> None:
        self._assert_conn()
        self._conn.execute("BEGIN")

    def begin_immediate(self) -> None:
        self._assert_conn()
        self._conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self._assert_conn()
        self._conn.commit()

    def rollback(self) -> None:
        self._assert_conn()
        try:
            self._conn.rollback()
        except Exception:
            pass

    # ── Query execution ───────────────────────────────────────────────

    def execute(
        self, sql: str, params: Union[Tuple, List, Dict, None] = None
    ) -> DatabaseBackend:
        self._assert_conn()
        if params is not None:
            self._cursor = self._conn.execute(sql, params)
        else:
            self._cursor = self._conn.execute(sql)
        return self

    def executescript(self, sql: str) -> None:
        self._assert_conn()
        self._conn.executescript(sql)

    def executemany(
        self, sql: str, params_seq: List[Union[Tuple, List, Dict]]
    ) -> None:
        self._assert_conn()
        self._conn.executemany(sql, params_seq)

    # ── Result retrieval ──────────────────────────────────────────────

    def fetchone(self) -> Optional[CursorResult]:
        if self._cursor is None:
            return None
        row = self._cursor.fetchone()
        if row is None:
            return None
        return CursorResult(dict(row) if isinstance(row, sqlite3.Row) else row)

    def fetchall(self) -> List[CursorResult]:
        if self._cursor is None:
            return []
        rows = self._cursor.fetchall()
        return [
            CursorResult(dict(r) if isinstance(r, sqlite3.Row) else r)
            for r in rows
        ]

    @property
    def rowcount(self) -> int:
        if self._cursor is None:
            return -1
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> Optional[int]:
        if self._cursor is None:
            return None
        return self._cursor.lastrowid

    # ── SQL dialect helpers ───────────────────────────────────────────

    def insert_or_replace(self, table: str, data: Dict[str, Any]) -> None:
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        sql = f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})"
        self.execute(sql, tuple(data.values()))

    def insert_or_ignore(self, table: str, data: Dict[str, Any]) -> None:
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        sql = f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})"
        self.execute(sql, tuple(data.values()))

    # ── Schema inspection ─────────────────────────────────────────────

    def table_exists(self, table_name: str) -> bool:
        self._assert_conn()
        row = self._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None

    def get_table_columns(self, table_name: str) -> Dict[str, str]:
        self._assert_conn()
        cols: Dict[str, str] = {}
        for row in self._conn.execute(
            f'PRAGMA table_info("{table_name}")'
        ).fetchall():
            # row: (cid, name, type, notnull, dflt_value, pk)
            col_name = row[1]
            col_type = row[2] or ""
            notnull = row[3]
            default = row[4]
            pk = row[5]
            parts = [col_type] if col_type else []
            if notnull and not pk:
                parts.append("NOT NULL")
            if default is not None:
                parts.append(f"DEFAULT {default}")
            cols[col_name] = " ".join(parts)
        return cols

    def get_indexes(self, table_name: str) -> List[Dict[str, Any]]:
        self._assert_conn()
        indexes = []
        for row in self._conn.execute(
            f'PRAGMA index_list("{table_name}")'
        ).fetchall():
            indexes.append({
                "name": row[1],
                "unique": bool(row[2]),
            })
        return indexes

    # ── Compatibility ─────────────────────────────────────────────────

    @property
    def raw_connection(self) -> Optional[sqlite3.Connection]:
        return self._conn

    # ── SQLite-specific helpers (used by SessionDB) ───────────────────

    def pragma(self, pragma_stmt: str) -> CursorResult:
        """Execute a PRAGMA statement and return the result."""
        self._assert_conn()
        cursor = self._conn.execute(f"PRAGMA {pragma_stmt}")
        row = cursor.fetchone()
        if row is None:
            return CursorResult({})
        return CursorResult(dict(zip([c[0] for c in cursor.description], row)))

    def get_autoincrement_value(self, table: str) -> Optional[int]:
        """Get the current AUTOINCREMENT value for a table."""
        self._assert_conn()
        row = self._conn.execute(
            f"SELECT seq FROM sqlite_sequence WHERE name=?",
            (table,),
        ).fetchone()
        return row[0] if row else None

    # ── Internals ─────────────────────────────────────────────────────

    def _assert_conn(self) -> None:
        if self._conn is None:
            raise RuntimeError("Database connection is closed")
