"""PostgreSQL backend implementation.

Requires the ``psycopg`` package (``pip install psycopg[binary]``).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from hermes_db.config import DBConfig
from hermes_db.interface import CursorResult, DatabaseBackend

logger = logging.getLogger(__name__)

# Check if psycopg is available
try:
    import psycopg
    from psycopg import sql as pg_sql

    PSYCOPG_AVAILABLE = True
except ImportError:
    PSYCOPG_AVAILABLE = False
    psycopg = None  # type: ignore[assignment]
    pg_sql = None


# Regex to convert SQLite ?-style placeholders to PostgreSQL %s-style.
# Handles:
#   - Simple: "WHERE id = ?" -> "WHERE id = %s"
#   - Inside functions: "instr(m.content, ?)" -> "instr(m.content, %s)"
_SQLITE_PLACEHOLDER_RE = re.compile(r"(?<!\?)%(?!%)")


def _convert_placeholders(sql: str) -> str:
    """Convert SQLite ``?`` placeholders to PostgreSQL ``%s`` style.

    Handles the case where ``?`` appears inside SQLite-specific functions
    that PostgreSQL doesn't have — we let those through and they'll be
    translated separately.

    Note: ``INSERT OR REPLACE`` and ``INSERT OR IGNORE`` are translated
    to standard PostgreSQL syntax.
    """
    result = sql

    # Translate INSERT OR REPLACE
    result = re.sub(
        r"\bINSERT\s+OR\s+REPLACE\s+INTO\b",
        "INSERT INTO",
        result,
        flags=re.IGNORECASE,
    )

    # Translate INSERT OR IGNORE
    result = re.sub(
        r"\bINSERT\s+OR\s+IGNORE\s+INTO\b",
        "INSERT INTO",
        result,
        flags=re.IGNORECASE,
    )

    # Translate SQLite-specific functions
    result = re.sub(
        r"\binstr\(([^,]+),\s*([^)]+)\)",
        r"STRPOS(\1, \2)",
        result,
        flags=re.IGNORECASE,
    )

    # Translate SUBSTR to SUBSTRING (standard SQL)
    result = re.sub(
        r"\bsubstr\(([^(]+(?:\([^)]*\))?[^,]*),\s*([^,]+),\s*([^)]+)\)",
        r"SUBSTRING(\1 FROM \2 FOR \3)",
        result,
        flags=re.IGNORECASE,
    )

    # Convert ? to %s
    result = _SQLITE_PLACEHOLDER_RE.sub("%s", result)

    return result


class _PostgresCursorResult:
    """Converts psycopg row tuples to CursorResult."""

    def __init__(self, row: tuple, description):
        self._row = row
        self._description = description
        if description:
            self._col_names = [d.name for d in description]
        else:
            self._col_names = []

    def __getitem__(self, key):
        if isinstance(key, str):
            idx = self._col_names.index(key)
            return self._row[idx]
        return self._row[key]

    def keys(self):
        return self._col_names

    def __iter__(self):
        return iter(self._row)

    def __len__(self):
        return len(self._row)

    def __bool__(self):
        return True

    def get(self, key, default=None):
        try:
            return self[key]
        except (IndexError, ValueError):
            return default

    def __repr__(self):
        return repr(dict(zip(self._col_names, self._row)))


class PostgreSQLBackend(DatabaseBackend):
    """PostgreSQL database backend using ``psycopg``.

    Converts SQLite-style queries to PostgreSQL-compatible SQL
    automatically (``?`` → ``%s``, ``INSERT OR REPLACE`` → upsert, etc.).

    Raises ``RuntimeError`` at instantiation when ``psycopg`` is not
    installed.
    """

    def __init__(
        self,
        connection_string: str = None,
        *,
        db_config: DBConfig = None,
    ):
        if not PSYCOPG_AVAILABLE:
            raise RuntimeError(
                "PostgreSQL backend requires 'psycopg[binary]>=3.1'. "
                "Install with: pip install psycopg[binary]"
            )

        if db_config is not None:
            self._config = db_config
        else:
            from hermes_db.config import get_db_config

            self._config = get_db_config()

        self._connection_string = (
            connection_string or self._config.postgresql.build_connection_string()
        )
        self._conn = None
        self._cursor = None
        self._lastrowid: Optional[int] = None

        self.connect()

    # ── Connection lifecycle ──────────────────────────────────────────

    def connect(self) -> None:
        if self._conn is not None:
            return
        try:
            self._conn = psycopg.connect(self._connection_string)
        except Exception as exc:
            logger.error("Failed to connect to PostgreSQL: %s", exc)
            raise
        self._conn.autocommit = False

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._cursor = None

    # ── Transaction control ───────────────────────────────────────────

    def begin(self) -> None:
        self._assert_conn()
        # In psycopg, BEGIN is implicit on the first statement when
        # autocommit is False.  Explicit BEGIN is still fine.
        self._conn.execute("BEGIN")

    def begin_immediate(self) -> None:
        self._assert_conn()
        # PostgreSQL doesn't have BEGIN IMMEDIATE.  BEGIN + READ WRITE
        # transaction is equivalent in practice.
        self._conn.execute("BEGIN")

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

        # psycopg requires tuples for positional params but can also accept lists
        if isinstance(params, list):
            params = tuple(params)

        pg_sql = _convert_placeholders(sql)
        try:
            self._cursor = self._conn.execute(pg_sql, params)
            if (
                self._cursor
                and self._cursor.pgresult
                and self._cursor.pgresult.status == 1  # COMMAND_OK for INSERT
            ):
                # Try to get the last inserted OID
                try:
                    self._lastrowid = self._cursor.lastrowid
                except Exception:
                    self._lastrowid = None
        except Exception as exc:
            logger.debug("PostgreSQL execute error: %s | SQL: %s", exc, pg_sql[:200])
            raise
        return self

    def executescript(self, sql: str) -> None:
        self._assert_conn()
        # Split on semicolons and execute each statement
        statements = [
            s.strip() for s in sql.split(";") if s.strip()
        ]
        for stmt in statements:
            if stmt.upper().startswith("CREATE VIRTUAL TABLE"):
                # Skip FTS5 virtual table DDL (PostgreSQL handles FTS
                # via indexes, not virtual tables)
                continue
            if stmt.upper().startswith("PRAGMA "):
                # Skip PRAGMA statements
                continue
            if "TRIGGER" in stmt.upper() and ("messages_fts" in stmt or "messages_fts_trigram" in stmt):
                # Skip FTS triggers (handled by FTS backend)
                continue
            if stmt.upper().startswith("INSERT OR REPLACE"):
                # Handled by _convert_placeholders
                pass
            if stmt.upper().startswith("INSERT OR IGNORE"):
                pass

            try:
                pg_stmt = _convert_placeholders(stmt)
                self._conn.execute(pg_stmt)
            except Exception as exc:
                logger.debug("PostgreSQL executescript error: %s | SQL: %s", exc, stmt[:200])
                raise

    def executemany(
        self, sql: str, params_seq: List[Union[Tuple, List, Dict]]
    ) -> None:
        self._assert_conn()
        pg_sql = _convert_placeholders(sql)
        converted = []
        for p in params_seq:
            if isinstance(p, list):
                converted.append(tuple(p))
            else:
                converted.append(p)
        self._conn.executemany(pg_sql, converted)

    # ── Result retrieval ──────────────────────────────────────────────

    def fetchone(self) -> Optional[CursorResult]:
        if self._cursor is None:
            return None
        row = self._cursor.fetchone()
        if row is None:
            return None
        return CursorResult(dict(row))

    def fetchall(self) -> List[CursorResult]:
        if self._cursor is None:
            return []
        rows = self._cursor.fetchall()
        return [CursorResult(dict(r)) for r in rows]

    @property
    def rowcount(self) -> int:
        if self._cursor is None:
            return -1
        try:
            return self._cursor.rowcount
        except Exception:
            return -1

    @property
    def lastrowid(self) -> Optional[int]:
        return self._lastrowid

    # ── SQL dialect helpers ───────────────────────────────────────────

    def insert_or_replace(self, table: str, data: Dict[str, Any]) -> None:
        cols = list(data.keys())
        quoted_cols = [f'"{c}"' for c in cols]
        placeholders = ", ".join("%s" for _ in cols)

        # Build ON CONFLICT DO UPDATE SET for all columns except the
        # first one (assumed to be the PK)
        update_set = ", ".join(
            f'"{c}" = EXCLUDED."{c}"' for c in cols
        )

        sql = (
            f"INSERT INTO {table} ({', '.join(quoted_cols)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT DO UPDATE SET {update_set}"
        )
        self.execute(sql, tuple(data.values()))

    def insert_or_ignore(self, table: str, data: Dict[str, Any]) -> None:
        cols = list(data.keys())
        quoted_cols = [f'"{c}"' for c in cols]
        placeholders = ", ".join("%s" for _ in cols)
        sql = (
            f"INSERT INTO {table} ({', '.join(quoted_cols)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT DO NOTHING"
        )
        self.execute(sql, tuple(data.values()))

    # ── Schema inspection ─────────────────────────────────────────────

    def table_exists(self, table_name: str) -> bool:
        self._assert_conn()
        row = self._conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = %s AND table_schema = 'public'",
            (table_name,),
        ).fetchone()
        return row is not None

    def get_table_columns(self, table_name: str) -> Dict[str, str]:
        self._assert_conn()
        cols: Dict[str, str] = {}
        rows = self._conn.execute(
            """
            SELECT column_name, data_type, is_nullable,
                   column_default, character_maximum_length
            FROM information_schema.columns
            WHERE table_name = %s AND table_schema = 'public'
            ORDER BY ordinal_position
            """,
            (table_name,),
        ).fetchall()
        for row in rows:
            name = row[0]
            data_type = row[1]
            nullable = row[2] == "YES"
            default = row[3]
            # Reconstruct type expression
            parts = [data_type.upper()]
            if not nullable:
                parts.append("NOT NULL")
            if default is not None:
                parts.append(f"DEFAULT {default}")
            cols[name] = " ".join(parts)
        return cols

    def get_indexes(self, table_name: str) -> List[Dict[str, Any]]:
        self._assert_conn()
        rows = self._conn.execute(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE tablename = %s AND schemaname = 'public'
            """,
            (table_name,),
        ).fetchall()
        indexes = []
        for row in rows:
            indexes.append({
                "name": row[0],
                "definition": row[1],
            })
        return indexes

    # ── Compatibility ─────────────────────────────────────────────────

    @property
    def raw_connection(self):
        return self._conn

    def _assert_conn(self) -> None:
        if self._conn is None:
            raise RuntimeError("Database connection is closed")
