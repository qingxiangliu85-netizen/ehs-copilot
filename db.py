"""Central SQLite connection handling for the V5 prototype.

Responsibilities are deliberately small: resolve the database path, open a
connection with the pragmas the domain model relies on, and initialise the
schema.  The service layer owns transactions (``with connection:``), so this
module never commits on behalf of business code.

The prototype uses SQLite as a portfolio demo store only; it makes no
production-grade database claim.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from config import EHS_DB_PATH
from schema import initialize


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_FILENAME = "ehs_copilot.db"


def default_db_path() -> Path:
    """Return the configured database path, defaulting to ``data/ehs_copilot.db``."""
    configured = str(EHS_DB_PATH or "").strip()
    if configured:
        return Path(configured)
    return PROJECT_ROOT / "data" / DEFAULT_DB_FILENAME


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open one connection; ``:memory:`` is supported for isolated tests."""
    target = str(db_path) if db_path is not None else str(default_db_path())
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def open_database(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection and make sure the schema exists."""
    connection = connect(db_path)
    initialize(connection)
    return connection


@contextmanager
def session(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Yield an initialised connection and always close it afterwards."""
    connection = open_database(db_path)
    try:
        yield connection
    finally:
        connection.close()


__all__ = [
    "DEFAULT_DB_FILENAME",
    "PROJECT_ROOT",
    "connect",
    "default_db_path",
    "open_database",
    "session",
]
