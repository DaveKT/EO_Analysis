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


# Columns added after the first databases were created. CREATE TABLE IF NOT
# EXISTS will not add them to an existing table, so they are applied explicitly.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "extractions": {
        "topic_other_reason": "TEXT",
        "instrument": "TEXT",
        "instrument_other_reason": "TEXT",
    },
    "relationships": {
        "target_type": "TEXT",
        "target_label": "TEXT",
        "in_part": "INTEGER DEFAULT 0",
        "raw_quote": "TEXT",
        "quote_trimmed": "INTEGER DEFAULT 0",
    },
    "agencies_tasked": {"raw_quote": "TEXT", "quote_trimmed": "INTEGER DEFAULT 0"},
    "deadlines": {"raw_quote": "TEXT", "quote_trimmed": "INTEGER DEFAULT 0"},
    "authorities": {"raw_quote": "TEXT", "quote_trimmed": "INTEGER DEFAULT 0"},
}


def _add_missing_columns(con: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        present = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in present:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# Indexes over columns from _ADDED_COLUMNS: they must be created after those
# columns exist, so they cannot live in schema.sql.
_POST_MIGRATION = (
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_relationships_fr_unique"
        " ON relationships (document_number, relation, target_label)"
        " WHERE run_id IS NULL"
    ),
)


def migrate(con: sqlite3.Connection) -> None:
    """Apply schema.sql, then any columns added to existing tables. Idempotent."""
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _add_missing_columns(con)
    for statement in _POST_MIGRATION:
        con.execute(statement)
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
    "review_queue",
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
    relationships = [
        (row[0], row[1])
        for row in con.execute(
            "SELECT relation, COUNT(*) FROM relationships WHERE run_id IS NULL"
            " GROUP BY relation ORDER BY COUNT(*) DESC"
        )
    ]
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
        "relationships": relationships,
        "by_president": by_president,
    }
