"""Hermes Database Abstraction Layer.

Provides a configurable backend-agnostic database interface that supports
SQLite and PostgreSQL with minimal source code changes to the existing
codebase.  Each backend wraps the native driver and exposes a uniform
``execute()`` / ``executescript()`` / ``commit()`` / ``rollback()`` /
``close()`` surface.

Usage::

    from hermes_db import create_backend, DBConfig

    # From config
    backend = create_backend()

    # Explicit
    backend = create_backend(provider="sqlite", db_path="/path/to/db.sqlite")
    backend = create_backend(provider="postgresql", connection_string="...")

    backend.execute("CREATE TABLE ...")
    backend.execute("INSERT INTO t (c) VALUES (?)", (val,))
    backend.commit()
    backend.close()
"""

from hermes_db.config import DBConfig, get_db_config
from hermes_db.interface import DatabaseBackend, CursorResult
from hermes_db.sqlite_backend import SQLiteBackend
from hermes_db.postgres_backend import PostgreSQLBackend
from hermes_db.fts_backend import FTSBackend, SQLiteFTSBackend, PostgreSQLFTSBackend


def create_backend(
    provider: str = None,
    db_path: str = None,
    db_config: DBConfig = None,
    db_name: str = "state",
    **kwargs,
) -> DatabaseBackend:
    """Create a database backend from config or explicit parameters.

    Resolution order:
      1. *provider* / *db_path* / *kwargs* (explicit overrides)
      2. *db_config* (pre-loaded config object)
      3. ``get_db_config(db_name)`` (global config from ``config.yaml``)

    Returns:
        A :class:`DatabaseBackend` instance (``SQLiteBackend`` or
        ``PostgreSQLBackend``).
    """
    if db_config is None:
        db_config = get_db_config(db_name)

    resolved_provider = provider or db_config.provider or "sqlite"
    resolved_provider = resolved_provider.lower().replace("-", "_")

    if resolved_provider in ("sqlite",):
        path = db_path or db_config.sqlite.get(db_name + "_db")
        if path:
            from pathlib import Path
            path = str(Path(path).expanduser())
        return SQLiteBackend(path, db_config=db_config, **kwargs)

    elif resolved_provider in ("postgresql", "postgres", "pg"):
        return PostgreSQLBackend(
            connection_string=kwargs.get("connection_string"),
            db_config=db_config,
            **kwargs,
        )

    raise ValueError(
        f"Unknown database provider: {resolved_provider!r}. "
        f"Supported: sqlite, postgresql"
    )


def create_fts_backend(provider: str = None, db_config: DBConfig = None) -> FTSBackend:
    """Create a full-text-search backend matching the database provider."""
    if db_config is None:
        db_config = get_db_config()

    resolved_provider = provider or db_config.provider or "sqlite"
    resolved_provider = resolved_provider.lower().replace("-", "_")

    if resolved_provider in ("sqlite",):
        return SQLiteFTSBackend()
    elif resolved_provider in ("postgresql", "postgres", "pg"):
        return PostgreSQLFTSBackend()

    raise ValueError(f"Unknown provider for FTS: {resolved_provider!r}")


__all__ = [
    "create_backend",
    "create_fts_backend",
    "DBConfig",
    "get_db_config",
    "DatabaseBackend",
    "CursorResult",
    "SQLiteBackend",
    "PostgreSQLBackend",
    "FTSBackend",
    "SQLiteFTSBackend",
    "PostgreSQLFTSBackend",
]
