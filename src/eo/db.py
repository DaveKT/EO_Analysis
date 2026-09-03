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


def ingest_health(con: sqlite3.Connection) -> dict[str, object]:
    """Facts the Phase 1 exit gate is judged on."""
    row = con.execute(
        "SELECT COUNT(*) n, MIN(eo_number) lo, MAX(eo_number) hi,"
        " MIN(signing_date) first_date, MAX(signing_date) last_date,"
        " AVG(body_char_count) avg_chars"
        " FROM documents"
    ).fetchone()
    short = con.execute(
        "SELECT COUNT(*) FROM documents WHERE body_char_count < 500"
    ).fetchone()[0]
    missing_eo = con.execute(
        "SELECT COUNT(*) FROM documents WHERE eo_number IS NULL"
    ).fetchone()[0]
    with_notes = con.execute(
        "SELECT COUNT(*) FROM documents"
        " WHERE disposition_notes IS NOT NULL AND disposition_notes != ''"
    ).fetchone()[0]
    extractable = con.execute("SELECT COUNT(*) FROM extractable_documents").fetchone()[0]
    by_president = con.execute(
        "SELECT president, COUNT(*) n, MIN(signing_date) lo, MAX(signing_date) hi"
        " FROM documents GROUP BY president ORDER BY lo"
    ).fetchall()
    return {
        "count": row["n"],
        "eo_range": (row["lo"], row["hi"]),
        "date_range": (row["first_date"], row["last_date"]),
        "avg_chars": row["avg_chars"] or 0,
        "short_bodies": short,
        "missing_eo_number": missing_eo,
        "with_disposition_notes": with_notes,
        "extractable": extractable,
        "by_president": by_president,
    }
