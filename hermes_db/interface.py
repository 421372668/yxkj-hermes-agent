"""Abstract database interface and cursor result wrapper."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Union


class CursorResult:
    """Wrapper around a query result row.

    Provides both dict-style (``row["col"]``) and positional
    (``row[0]``) access for backward compatibility with code that
    expects ``sqlite3.Row`` behaviour.

    For PostgreSQL backends, each row is created from the ``psycopg``
    row data.  For SQLite, we wrap ``sqlite3.Row`` directly.
    """

    def __init__(self, data: Union[Dict[str, Any], Tuple, Any]):
        if isinstance(data, dict):
            self._dict = data
            self._tuple = tuple(data.values())
        elif isinstance(data, (tuple, list)):
            self._tuple = tuple(data)
            self._dict = {}
        else:
            # Single scalar (COUNT(*), etc.)
            self._tuple = (data,)
            self._dict = {}

    def __getitem__(self, key: Union[str, int]) -> Any:
        if isinstance(key, str):
            return self._dict[key]
        return self._tuple[key]

    def __getattr__(self, name: str) -> Any:
        try:
            return self._dict[name]
        except KeyError:
            raise AttributeError(name) from None

    def keys(self):
        return self._dict.keys()

    def values(self):
        return self._dict.values()

    def items(self):
        return self._dict.items()

    def __contains__(self, key: object) -> bool:
        return key in self._dict

    def __iter__(self):
        return iter(self._dict)

    def __len__(self) -> int:
        return len(self._tuple)

    def __bool__(self) -> bool:
        return True

    def get(self, key: str, default: Any = None) -> Any:
        return self._dict.get(key, default)

    def __repr__(self) -> str:
        return repr(self._dict or self._tuple)


class DatabaseBackend(ABC):
    """Abstract database backend interface.

    Every method that takes raw SQL uses ``?`` placeholders regardless
    of the backend — the concrete implementation translates to the
    native parameter style (``%s`` for PostgreSQL) internally.
    """

    # ── Connection lifecycle ──────────────────────────────────────────

    @abstractmethod
    def connect(self) -> None:
        """Open or re-establish the connection."""

    @abstractmethod
    def close(self) -> None:
        """Close the connection and release resources."""

    # ── Transaction control ───────────────────────────────────────────

    @abstractmethod
    def begin(self) -> None:
        """Start a transaction (equivalent to BEGIN)."""

    @abstractmethod
    def begin_immediate(self) -> None:
        """Start a write transaction immediately.

        SQLite: BEGIN IMMEDIATE (acquires WAL write lock).
        PostgreSQL: BEGIN with appropriate isolation level.
        """

    @abstractmethod
    def commit(self) -> None:
        """Commit the current transaction."""

    @abstractmethod
    def rollback(self) -> None:
        """Roll back the current transaction."""

    # ── Query execution ───────────────────────────────────────────────

    @abstractmethod
    def execute(
        self, sql: str, params: Union[Tuple, List, Dict, None] = None
    ) -> DatabaseBackend:
        """Execute a single SQL statement and return *self* for chaining.

        The result rows are available via :meth:`fetchone` /
        :meth:`fetchall` / :attr:`rowcount` on *self* afterwards.
        """

    @abstractmethod
    def executescript(self, sql: str) -> None:
        """Execute multiple SQL statements (no parameters).

        SQLite: maps to ``executescript()``.
        PostgreSQL: splits on ``;`` and executes each statement.
        """

    @abstractmethod
    def executemany(
        self, sql: str, params_seq: List[Union[Tuple, List, Dict]]
    ) -> None:
        """Execute the same SQL with each parameter set in *params_seq*."""

    # ── Result retrieval ──────────────────────────────────────────────

    @abstractmethod
    def fetchone(self) -> Optional[CursorResult]:
        """Fetch the next row, or None when exhausted."""

    @abstractmethod
    def fetchall(self) -> List[CursorResult]:
        """Fetch all remaining rows."""

    @property
    @abstractmethod
    def rowcount(self) -> int:
        """Number of rows affected by the last DML statement."""

    @property
    @abstractmethod
    def lastrowid(self) -> Optional[int]:
        """Row ID of the last INSERT, if applicable."""

    # ── SQL dialect helpers ───────────────────────────────────────────

    @abstractmethod
    def insert_or_replace(self, table: str, data: Dict[str, Any]) -> None:
        """INSERT or replace (upsert) a row.

        SQLite: ``INSERT OR REPLACE INTO table (...) VALUES (...)``
        PostgreSQL: ``INSERT INTO table (...) VALUES (...) ON CONFLICT DO UPDATE``
        """

    @abstractmethod
    def insert_or_ignore(self, table: str, data: Dict[str, Any]) -> None:
        """INSERT, silently skipping on conflict.

        SQLite: ``INSERT OR IGNORE INTO table (...) VALUES (...)``
        PostgreSQL: ``INSERT INTO table (...) VALUES (...) ON CONFLICT DO NOTHING``
        """

    # ── Schema inspection (used by migration helpers) ─────────────────

    @abstractmethod
    def table_exists(self, table_name: str) -> bool:
        """Return True when *table_name* exists in the database."""

    @abstractmethod
    def get_table_columns(self, table_name: str) -> Dict[str, str]:
        """Return a dict mapping column name → type expression for *table_name*.

        The type expression is the same format as what ``ALTER TABLE
        ... ADD COLUMN`` would accept (e.g. ``"TEXT"``, ``"INTEGER NOT
        NULL DEFAULT 0"``).
        """

    @abstractmethod
    def get_indexes(self, table_name: str) -> List[Dict[str, Any]]:
        """Return index metadata for *table_name*."""

    # ── Compatibility properties ──────────────────────────────────────

    @property
    @abstractmethod
    def raw_connection(self):
        """Expose the underlying native connection object.

        Use only for operations that can't be expressed through the
        abstract interface (e.g. ``sqlite3.backup()``).  Code that
        accesses ``raw_connection`` is NOT portable across backends.
        """
