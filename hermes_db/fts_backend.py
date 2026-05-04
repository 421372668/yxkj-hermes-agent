"""Full-text search backend abstraction.

Provides a uniform interface for full-text search that works across
SQLite (FTS5) and PostgreSQL (tsvector).  Each backend implements the
same :class:`FTSBackend` ABC.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from hermes_db.interface import DatabaseBackend

logger = logging.getLogger(__name__)


class FTSBackend(ABC):
    """Abstract full-text search backend.

    Each method receives a :class:`DatabaseBackend` instance to execute
    queries against.
    """

    @abstractmethod
    def create_fts_tables(self, backend: DatabaseBackend) -> None:
        """Create FTS virtual tables and triggers.

        Called during schema initialization.  Implementations should be
        idempotent (safe to call on every startup).
        """

    @abstractmethod
    def search(
        self,
        backend: DatabaseBackend,
        query: str,
        *,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Execute a full-text search and return matching message rows.

        Returns a list of dicts with keys:
            id, session_id, role, snippet, content,
            timestamp, tool_name, source, model, session_started
        """

    @abstractmethod
    def search_trigram(
        self,
        backend: DatabaseBackend,
        query: str,
        *,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Execute substring / CJK-aware search.

        SQLite: uses FTS5 trigram tokenizer.
        PostgreSQL: uses ``pg_trgm`` extension.
        """

    @abstractmethod
    def search_like(
        self,
        backend: DatabaseBackend,
        query: str,
        *,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """LIKE-based substring search fallback (short CJK queries, 1-2 chars)."""

    @abstractmethod
    def reindex(  # noqa: PLR0913
        self,
        backend: DatabaseBackend,
        cursor,
        old_schema: bool = False,
    ) -> None:
        """Rebuild the FTS index from all message rows.

        Used during schema migrations to repopulate the index after
        schema changes.

        Args:
            backend: Database backend.
            cursor: Database cursor from a write transaction.
            old_schema: When True, don't include tool_name/tool_calls
                        in the index (legacy format).
        """

    @abstractmethod
    def ensure_trigram_exists(self, backend: DatabaseBackend, cursor) -> bool:
        """Return True when the trigram FTS table already exists."""


# ── SQLite FTS5 implementation ────────────────────────────────────────

_SANITIZE_CHARS_RE = re.compile(r'[+{}()\"^]')
_MULTI_STAR_RE = re.compile(r"\*+")
_LEADING_STAR_RE = re.compile(r"(^|\s)\*")
_BOOLEAN_OP_RE = re.compile(r"(?i)^(AND|OR|NOT)\b\s*")
_TRAILING_OP_RE = re.compile(r"(?i)\s+(AND|OR|NOT)\s*$")
_DOTTED_TERM_RE = re.compile(r"\b(\w+(?:[._-]\w+)+)\b")


class SQLiteFTSBackend(FTSBackend):
    """SQLite FTS5 full-text search backend.

    Creates two FTS5 virtual tables:
    - ``messages_fts`` (unicode61 tokenizer)
    - ``messages_fts_trigram`` (trigram tokenizer for CJK)
    """

    FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""

    FTS_TRIGRAM_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""

    # ── Helpers (moved from hermes_state.SessionDB) ───────────────────

    @staticmethod
    def sanitize_fts5_query(query: str) -> str:
        """Sanitize user input for safe use in FTS5 MATCH queries."""
        quoted_parts: list = []

        def _preserve_quoted(m: re.Match) -> str:
            quoted_parts.append(m.group(0))
            return f"\x00Q{len(quoted_parts) - 1}\x00"

        sanitized = re.sub(r'"[^"]*"', _preserve_quoted, query)
        sanitized = _SANITIZE_CHARS_RE.sub(" ", sanitized)
        sanitized = _MULTI_STAR_RE.sub("*", sanitized)
        sanitized = _LEADING_STAR_RE.sub(r"\1", sanitized)
        sanitized = _BOOLEAN_OP_RE.sub("", sanitized.strip())
        sanitized = _TRAILING_OP_RE.sub("", sanitized.strip())
        sanitized = _DOTTED_TERM_RE.sub(r'"\1"', sanitized)

        for i, quoted in enumerate(quoted_parts):
            sanitized = sanitized.replace(f"\x00Q{i}\x00", quoted)

        return sanitized.strip()

    @staticmethod
    def _is_cjk_codepoint(cp: int) -> bool:
        return (
            0x4E00 <= cp <= 0x9FFF
            or 0x3400 <= cp <= 0x4DBF
            or 0x20000 <= cp <= 0x2A6DF
            or 0x3000 <= cp <= 0x303F
            or 0x3040 <= cp <= 0x309F
            or 0x30A0 <= cp <= 0x30FF
            or 0xAC00 <= cp <= 0xD7AF
        )

    @staticmethod
    def contains_cjk(text: str) -> bool:
        for ch in text:
            if SQLiteFTSBackend._is_cjk_codepoint(ord(ch)):
                return True
        return False

    @staticmethod
    def count_cjk(text: str) -> int:
        return sum(1 for ch in text if SQLiteFTSBackend._is_cjk_codepoint(ord(ch)))

    # ── FTSBackend interface ──────────────────────────────────────────

    def create_fts_tables(self, backend: DatabaseBackend) -> None:
        raw = backend.raw_connection
        raw.executescript(self.FTS_SQL)
        try:
            raw.execute("SELECT * FROM messages_fts_trigram LIMIT 0")
        except Exception:
            raw.executescript(self.FTS_TRIGRAM_SQL)

    def search(
        self,
        backend: DatabaseBackend,
        query: str,
        *,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        query = self.sanitize_fts5_query(query)
        if not query:
            return []

        where_clauses = ["messages_fts MATCH ?"]
        params: list = [query]

        if source_filter is not None:
            sp = ",".join("?" for _ in source_filter)
            where_clauses.append(f"s.source IN ({sp})")
            params.extend(source_filter)
        if exclude_sources is not None:
            ep = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({ep})")
            params.extend(exclude_sources)
        if role_filter:
            rp = ",".join("?" for _ in role_filter)
            where_clauses.append(f"m.role IN ({rp})")
            params.extend(role_filter)

        params.extend([limit, offset])
        sql = f"""
            SELECT m.id, m.session_id, m.role,
                   snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                   m.content, m.timestamp, m.tool_name,
                   s.source, s.model, s.started_at AS session_started
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {' AND '.join(where_clauses)}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """
        try:
            rows = backend.execute(sql, tuple(params)).fetchall()
        except Exception as exc:
            logger.debug("FTS5 search failed: %s", exc)
            return []
        return [dict(r) for r in rows]

    def search_trigram(
        self,
        backend: DatabaseBackend,
        query: str,
        *,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        tokens = query.split()
        parts = []
        for tok in tokens:
            if tok.upper() in ("AND", "OR", "NOT"):
                parts.append(tok)
            else:
                parts.append('"' + tok.replace('"', '""') + '"')
        trigram_query = " ".join(parts)

        tri_where = ["messages_fts_trigram MATCH ?"]
        tri_params: list = [trigram_query]
        if source_filter is not None:
            sp = ",".join("?" for _ in source_filter)
            tri_where.append(f"s.source IN ({sp})")
            tri_params.extend(source_filter)
        if exclude_sources is not None:
            ep = ",".join("?" for _ in exclude_sources)
            tri_where.append(f"s.source NOT IN ({ep})")
            tri_params.extend(exclude_sources)
        if role_filter:
            rp = ",".join("?" for _ in role_filter)
            tri_where.append(f"m.role IN ({rp})")
            tri_params.extend(role_filter)

        tri_params.extend([limit, offset])
        sql = f"""
            SELECT m.id, m.session_id, m.role,
                   snippet(messages_fts_trigram, 0, '>>>', '<<<', '...', 40) AS snippet,
                   m.content, m.timestamp, m.tool_name,
                   s.source, s.model, s.started_at AS session_started
            FROM messages_fts_trigram
            JOIN messages m ON m.id = messages_fts_trigram.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {' AND '.join(tri_where)}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """
        try:
            rows = backend.execute(sql, tuple(tri_params)).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    def search_like(
        self,
        backend: DatabaseBackend,
        query: str,
        *,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        escaped = (
            query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        like_where = [
            "(m.content LIKE ? ESCAPE '\\' "
            "OR m.tool_name LIKE ? ESCAPE '\\' "
            "OR m.tool_calls LIKE ? ESCAPE '\\')"
        ]
        like_params: list = [f"%{escaped}%", f"%{escaped}%", f"%{escaped}%"]
        if source_filter is not None:
            sp = ",".join("?" for _ in source_filter)
            like_where.append(f"s.source IN ({sp})")
            like_params.extend(source_filter)
        if exclude_sources is not None:
            ep = ",".join("?" for _ in exclude_sources)
            like_where.append(f"s.source NOT IN ({ep})")
            like_params.extend(exclude_sources)
        if role_filter:
            rp = ",".join("?" for _ in role_filter)
            like_where.append(f"m.role IN ({rp})")
            like_params.extend(role_filter)

        like_params.extend([limit, offset])
        sql = f"""
            SELECT m.id, m.session_id, m.role,
                   substr(m.content,
                          max(1, instr(m.content, ?)),
                          120) AS snippet,
                   m.content, m.timestamp, m.tool_name,
                   s.source, s.model, s.started_at AS session_started
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE {' AND '.join(like_where)}
            ORDER BY m.timestamp DESC
            LIMIT ? OFFSET ?
        """
        like_params.insert(0, query)
        rows = backend.execute(sql, tuple(like_params)).fetchall()
        return [dict(r) for r in rows]

    def reindex(
        self,
        backend: DatabaseBackend,
        cursor,
        old_schema: bool = False,
    ) -> None:
        if old_schema:
            content_expr = "COALESCE(content, '')"
        else:
            content_expr = (
                "COALESCE(content, '') || ' ' || "
                "COALESCE(tool_name, '') || ' ' || "
                "COALESCE(tool_calls, '')"
            )
        cursor.execute(
            f"INSERT INTO messages_fts(rowid, content) "
            f"SELECT id, {content_expr} FROM messages"
        )
        cursor.execute(
            f"INSERT INTO messages_fts_trigram(rowid, content) "
            f"SELECT id, {content_expr} FROM messages"
        )

    def ensure_trigram_exists(self, backend: DatabaseBackend, cursor) -> bool:
        try:
            cursor.execute("SELECT * FROM messages_fts_trigram LIMIT 0")
            return True
        except Exception:
            return False


# ── PostgreSQL tsvector implementation ────────────────────────────────


class PostgreSQLFTSBackend(FTSBackend):
    """PostgreSQL full-text search backend using ``tsvector`` / ``tsquery``.

    Requires the ``pg_trgm`` extension for trigram/CJK searches.
    """

    def create_fts_tables(self, backend: DatabaseBackend) -> None:
        """Create FTS indexes (rather than virtual tables).

        PostgreSQL uses functional indexes on ``to_tsvector()`` instead
        of virtual tables.
        """
        # Create the FTS index on the messages table
        try:
            backend.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_fts
                ON messages
                USING GIN (to_tsvector('simple',
                    COALESCE(content, '') || ' ' ||
                    COALESCE(tool_name, '') || ' ' ||
                    COALESCE(tool_calls, '')
                ))
            """)
        except Exception as exc:
            logger.debug("Could not create GIN index: %s", exc)

        # Create pg_trgm extension for trigram-like searches if available
        try:
            backend.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        except Exception:
            logger.debug("pg_trgm not available; trigram search will use ILIKE")

    def search(
        self,
        backend: DatabaseBackend,
        query: str,
        *,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        where_clauses = [
            "to_tsvector('simple', COALESCE(m.content, '') || ' ' || "
            "COALESCE(m.tool_name, '') || ' ' || COALESCE(m.tool_calls, '')) "
            "@@ plainto_tsquery('simple', ?)"
        ]
        params: list = [query]

        if source_filter is not None:
            sp = ",".join("%s" for _ in source_filter)
            where_clauses.append(f"s.source IN ({sp})")
            params.extend(source_filter)
        if exclude_sources is not None:
            ep = ",".join("%s" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({ep})")
            params.extend(exclude_sources)
        if role_filter:
            rp = ",".join("%s" for _ in role_filter)
            where_clauses.append(f"m.role IN ({rp})")
            params.extend(role_filter)

        params.extend([limit, offset])
        sql = f"""
            SELECT m.id, m.session_id, m.role,
                   ts_headline('simple', m.content,
                       plainto_tsquery('simple', %s),
                       'StartSel=>>>, StopSel=<<<, MaxWords=40, MinWords=20'
                   ) AS snippet,
                   m.content, m.timestamp, m.tool_name,
                   s.source, s.model, s.started_at AS session_started
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE {' AND '.join(where_clauses)}
            ORDER BY ts_rank(
                to_tsvector('simple',
                    COALESCE(m.content, '') || ' ' ||
                    COALESCE(m.tool_name, '') || ' ' ||
                    COALESCE(m.tool_calls, '')
                ),
                plainto_tsquery('simple', %s)
            ) DESC
            LIMIT %s OFFSET %s
        """
        params_with_second_query = params[:1] + [query] + params[1:]
        params_with_second_query[-2] = limit
        params_with_second_query[-1] = offset
        params_with_second_query[1] = query  # second occurrence for ts_headline
        # Rebuild with proper parameter count
        try:
            rows = backend.execute(sql, tuple(params_with_second_query)).fetchall()
        except Exception as exc:
            logger.debug("PostgreSQL FTS search failed: %s", exc)
            return []
        return [dict(r) for r in rows]

    def search_trigram(
        self,
        backend: DatabaseBackend,
        query: str,
        *,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Trigram search via ``ILIKE`` or ``similarity()``."""
        where_clauses = ["m.content ILIKE ?"]
        params: list = [f"%{query}%"]

        if source_filter is not None:
            sp = ",".join("?" for _ in source_filter)
            where_clauses.append(f"s.source IN ({sp})")
            params.extend(source_filter)
        if exclude_sources is not None:
            ep = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({ep})")
            params.extend(exclude_sources)
        if role_filter:
            rp = ",".join("?" for _ in role_filter)
            where_clauses.append(f"m.role IN ({rp})")
            params.extend(role_filter)

        params.extend([limit, offset])
        sql = f"""
            SELECT m.id, m.session_id, m.role,
                   SUBSTR(m.content, GREATEST(1, POSITION(? IN m.content) - 40), 120) AS snippet,
                   m.content, m.timestamp, m.tool_name,
                   s.source, s.model, s.started_at AS session_started
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE {' AND '.join(where_clauses)}
            ORDER BY m.timestamp DESC
            LIMIT ? OFFSET ?
        """
        params.insert(0, query)  # for POSITION() parameter
        rows = backend.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def search_like(
        self,
        backend: DatabaseBackend,
        query: str,
        *,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """LIKE-based fallback (same as SQLite path)."""
        escaped = (
            query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        like_where = [
            "(m.content LIKE ? "
            "OR m.tool_name LIKE ? "
            "OR m.tool_calls LIKE ?)"
        ]
        like_params: list = [f"%{escaped}%", f"%{escaped}%", f"%{escaped}%"]
        if source_filter is not None:
            sp = ",".join("?" for _ in source_filter)
            like_where.append(f"s.source IN ({sp})")
            like_params.extend(source_filter)
        if exclude_sources is not None:
            ep = ",".join("?" for _ in exclude_sources)
            like_where.append(f"s.source NOT IN ({ep})")
            like_params.extend(exclude_sources)
        if role_filter:
            rp = ",".join("?" for _ in role_filter)
            like_where.append(f"m.role IN ({rp})")
            like_params.extend(role_filter)

        like_params.extend([limit, offset])
        sql = f"""
            SELECT m.id, m.session_id, m.role,
                   SUBSTR(m.content, GREATEST(1, POSITION(? IN m.content)), 120) AS snippet,
                   m.content, m.timestamp, m.tool_name,
                   s.source, s.model, s.started_at AS session_started
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE {' AND '.join(like_where)}
            ORDER BY m.timestamp DESC
            LIMIT ? OFFSET ?
        """
        like_params.insert(0, query)
        rows = backend.execute(sql, tuple(like_params)).fetchall()
        return [dict(r) for r in rows]

    def reindex(
        self,
        backend: DatabaseBackend,
        cursor,
        old_schema: bool = False,
    ) -> None:
        # PostgreSQL GIN indexes are auto-maintained; no manual reindex needed.
        pass

    def ensure_trigram_exists(self, backend: DatabaseBackend, cursor) -> bool:
        # pg_trgm extension - check availability
        try:
            cursor.execute("SELECT * FROM pg_extension WHERE extname = 'pg_trgm'")
            return bool(cursor.fetchone())
        except Exception:
            return False
