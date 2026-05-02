"""SQLite connection helper with WAL mode + sane defaults.

Four daemons share `data/paper_trades.db`. Default rollback-journal mode
serializes all writes against all reads — under load this manifests as
`database is locked` errors and, on crash, can corrupt the journal.
WAL mode permits concurrent readers alongside a single writer and survives
crashes more gracefully. journal_mode is persisted on the DB file, so a
single successful PRAGMA is enough, but re-applying on every connect is
idempotent and costs one round-trip.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]


def connect(path: PathLike, **kwargs) -> sqlite3.Connection:
    """Open a SQLite connection with WAL journaling enabled."""
    conn = sqlite3.connect(str(path), **kwargs)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
