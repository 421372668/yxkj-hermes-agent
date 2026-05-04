"""Database configuration model and config loader.

Reads from ``config.yaml`` under the ``database:`` key, or falls back to
sensible defaults (SQLite with profile-aware paths).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class SQLiteConfig:
    """SQLite-specific configuration."""

    state_db: str = ""         # Path for state.db (empty = use default)
    kanban_db: str = ""        # Path for kanban.db
    response_store: str = ""   # Path for response_store.db


@dataclass
class PostgreSQLConfig:
    """PostgreSQL-specific configuration.

    Either provide individual fields (host, port, etc.) OR a complete
    ``connection_string``.  When *connection_string* is set it takes
    precedence over individual fields.
    """

    host: str = "localhost"
    port: int = 5432
    database: str = "hermes"
    user: str = "hermes"
    password: str = ""
    connection_string: str = ""
    pool_min_size: int = 2
    pool_max_size: int = 10

    def build_connection_string(self) -> str:
        """Build a PostgreSQL connection URI from individual fields."""
        if self.connection_string:
            return self.connection_string
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )


@dataclass
class DBConfig:
    """Top-level database configuration.

    The ``provider`` field selects the backend:
    - ``"sqlite"`` (default) — local file-based storage
    - ``"postgresql"`` — remote PostgreSQL server

    Each database (state, kanban, response_store) shares the same provider
    but can have independent file paths (SQLite) or connect to the same
    PostgreSQL server.
    """

    provider: str = "sqlite"
    sqlite: SQLiteConfig = field(default_factory=SQLiteConfig)
    postgresql: PostgreSQLConfig = field(default_factory=PostgreSQLConfig)


# ── Config cache ──────────────────────────────────────────────────────

_DB_CONFIG_CACHE: Optional[DBConfig] = None


def get_db_config(db_name: str = "state") -> DBConfig:
    """Load database configuration from config.yaml or return defaults.

    The config is cached after the first load; subsequent calls return the
    same object.  Call ``clear_db_config_cache()`` to force a reload (e.g.
    after config file changes).

    Args:
        db_name:  Which database name to scope config lookups for
                  (``"state"``, ``"kanban"``, ``"response_store"``).
                  Currently all databases share the same provider config.
    """
    global _DB_CONFIG_CACHE
    if _DB_CONFIG_CACHE is not None:
        return _DB_CONFIG_CACHE

    cfg = DBConfig()  # Start with defaults

    # Try to load from hermes config.yaml
    try:
        from hermes_cli.config import load_config

        raw = load_config()
        db_raw = raw.get("database", {}) if isinstance(raw, dict) else {}
        _apply_raw_config(cfg, db_raw)
    except ImportError:
        pass
    except Exception:
        pass

    # Environment variable overrides (highest precedence)
    _apply_env_overrides(cfg)

    _DB_CONFIG_CACHE = cfg
    return cfg


def clear_db_config_cache() -> None:
    """Force a fresh load on the next ``get_db_config()`` call."""
    global _DB_CONFIG_CACHE
    _DB_CONFIG_CACHE = None


def _apply_raw_config(cfg: DBConfig, raw: Dict[str, Any]) -> None:
    """Apply values from a parsed YAML dict to *cfg*."""
    provider = raw.get("provider", "").strip().lower()
    if provider:
        cfg.provider = provider

    sqlite_raw = raw.get("sqlite", {})
    if isinstance(sqlite_raw, dict):
        for key in ("state_db", "kanban_db", "response_store"):
            val = sqlite_raw.get(key, "")
            if val:
                setattr(cfg.sqlite, key, str(val))

    pg_raw = raw.get("postgresql", {})
    if isinstance(pg_raw, dict):
        for key in ("host", "port", "database", "user", "password", "connection_string", "pool_min_size", "pool_max_size"):
            val = pg_raw.get(key)
            if val is not None:
                setattr(cfg.postgresql, key, val)


def _apply_env_overrides(cfg: DBConfig) -> None:
    """Check for environment variable overrides (highest precedence)."""
    env_provider = os.environ.get("HERMES_DB_PROVIDER", "").strip().lower()
    if env_provider:
        cfg.provider = env_provider

    env_state_db = os.environ.get("HERMES_STATE_DB", "").strip()
    if env_state_db:
        cfg.sqlite.state_db = env_state_db

    env_kanban_db = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if env_kanban_db:
        cfg.sqlite.kanban_db = env_kanban_db

    env_pg_conn = os.environ.get("HERMES_PG_CONNECTION_STRING", "").strip()
    if env_pg_conn:
        cfg.postgresql.connection_string = env_pg_conn
        cfg.provider = "postgresql"
