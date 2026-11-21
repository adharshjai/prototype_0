"""The store: one schema on one of two engines (specification section 4).

    from pcldb.store import open_store
    store = open_store(settings)          # SqliteStore or PostgresStore
    with store.app() as db:               # one read-write transaction
        rows = db.query("SELECT path FROM {pcl}.sessions WHERE instrument = ?", [slug])

The engine modules are imported only when they are asked for, and the PostgreSQL driver only
when a PostgreSQL connection is made, so the SQLite engine runs on a bare Python.
"""

from __future__ import annotations

from ..errors import PclError
from .base import DatabaseError, IntegrityError, SqlTextError

__all__ = ["open_store", "DatabaseError", "IntegrityError", "SqlTextError"]


def open_store(settings):
    """The store the settings name: database.engine is "sqlite" or "postgresql"."""
    engine = settings.database.engine
    if engine == "sqlite":
        from .sqlite_engine import SqliteStore
        return SqliteStore(settings)
    if engine == "postgresql":
        from .postgres_engine import PostgresStore
        return PostgresStore(settings)
    raise PclError('The setting database.engine is %r; it must be "postgresql" or "sqlite".'
                   % (engine,), code="bad_setting")
