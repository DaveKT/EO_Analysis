"""SQLite connection and migration.

Per-record commits are the point: v1 held every result in a Python list and
wrote the CSV only after the whole loop, so any interruption lost the run.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: Path, *, create_parents: bool = True) -> sqlite3.Connection:
    if create_parents:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def migrate(con: sqlite3.Connection) -> None:
    """Apply schema.sql. Every statement is IF NOT EXISTS, so this is idempotent."""
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    con.commit()


@contextmanager
def session(db_path: Path) -> Iterator[sqlite3.Connection]:
    con = connect(db_path)
    try:
        migrate(con)
        yield con
    finally:
        con.close()


TABLES = (
    "documents",
    "extraction_runs",
    "extractions",
    "agencies_tasked",
    "deadlines",
    "authorities",
    "relationships",
)


def table_counts(con: sqlite3.Connection) -> dict[str, int]:
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}

