"""Flat-file export of one extraction run.

The database is the working store; this is the handoff format for anyone who
does not want to open SQLite. Two things shape the design:

**A run is the unit of export, not the database.** Extractions are versioned by
(model, prompt_version) and every run is retained, so exporting "the
extractions table" would silently interleave eleven runs of differing quality.
Every file here is scoped to one `run_id`, and the manifest records which.

**Source text and raw responses are excluded by default.** `body_text` is 15 MB
across the corpus and `raw_response` is larger; both are debugging material, and
putting them in a CSV that is meant to be opened in a spreadsheet helps nobody.
`--include-text` restores them for anyone who wants a single self-contained file.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

CSV = "csv"
PARQUET = "parquet"

# Columns dropped unless --include-text. Keyed by table.
BULK_COLUMNS = {
    "orders": ("body_text", "raw_response"),
}


@dataclass(frozen=True)
class Table:
    name: str
    sql: str


def tables(run_id: int) -> list[Table]:
    """One denormalised order-level table, plus the claim tables.

    `orders` is the join most people want: Federal Register ground truth and the
    model's per-order fields side by side, one row per executive order. The
    claim tables stay long rather than being flattened into columns, because an
    order has any number of agencies, deadlines and authorities.

    `relationships` deliberately includes the Federal Register's own seeded
    edges (`run_id IS NULL`) alongside the model's. Dropping them would export a
    weaker graph than the project actually has, and `source` distinguishes them.
    """
    return [
        Table(
            "orders",
            """
            SELECT d.eo_number, d.document_number, d.title, d.president,
                   d.signing_date, d.publication_date, d.citation,
                   d.body_char_count, d.pdf_url, d.raw_text_url,
                   e.summary, e.primary_topic, e.topic_other_reason,
                   e.secondary_topics, e.instrument, e.instrument_other_reason,
                   e.finish_reason, d.body_text, e.raw_response
            FROM extractions e
            JOIN documents d USING (document_number)
            WHERE e.run_id = :run_id
            ORDER BY d.eo_number
            """,
        ),
        Table(
            "agencies_tasked",
            """
            SELECT d.eo_number, t.document_number, t.agency_name, t.task,
                   t.source_quote, t.quote_trimmed
            FROM agencies_tasked t JOIN documents d USING (document_number)
            WHERE t.run_id = :run_id ORDER BY d.eo_number, t.agency_name
            """,
        ),
        Table(
            "deadlines",
            """
            SELECT d.eo_number, t.document_number, t.due_description, t.due_date,
                   t.responsible_party, t.source_quote, t.quote_trimmed
            FROM deadlines t JOIN documents d USING (document_number)
            WHERE t.run_id = :run_id ORDER BY d.eo_number
            """,
        ),
        Table(
            "authorities",
            """
            SELECT d.eo_number, t.document_number, t.authority, t.source_quote,
                   t.quote_trimmed
            FROM authorities t JOIN documents d USING (document_number)
            WHERE t.run_id = :run_id ORDER BY d.eo_number
            """,
        ),
        Table(
            "relationships",
            """
            SELECT d.eo_number, t.document_number, t.relation, t.target_eo_number,
                   t.target_type, t.target_label, t.in_part, t.source,
                   t.source_quote, t.quote_trimmed
            FROM relationships t JOIN documents d USING (document_number)
            WHERE t.run_id = :run_id OR (t.run_id IS NULL AND :include_fr)
            ORDER BY d.eo_number, t.relation
            """,
        ),
        Table(
            "review_queue",
            """
            SELECT d.eo_number, q.document_number, q.kind, q.detail, q.created_at
            FROM review_queue q JOIN documents d USING (document_number)
            WHERE q.run_id = :run_id ORDER BY q.kind, d.eo_number
            """,
        ),
    ]


def fetch(
    con: sqlite3.Connection, table: Table, run_id: int, include_text: bool
) -> tuple[list[str], list[tuple]]:
    cursor = con.execute(table.sql, {"run_id": run_id, "include_fr": 1})
    columns = [d[0] for d in cursor.description]
    drop = () if include_text else BULK_COLUMNS.get(table.name, ())
    keep = [i for i, name in enumerate(columns) if name not in drop]
    rows = [tuple(row[i] for i in keep) for row in cursor.fetchall()]
    return [columns[i] for i in keep], rows


def manifest(
    con: sqlite3.Connection, run_id: int, counts: dict[str, int], fmt: str
) -> dict:
    """What was exported, from which run, by which model.

    A flat file with no provenance is the thing this project exists not to
    produce: six months from now the difference between run 9 and run 11 is
    invisible from the CSV alone.
    """
    run = con.execute(
        "SELECT model, prompt_version, started_at, finished_at, input_tokens,"
        " output_tokens, cost_usd, notes FROM extraction_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return {
        "run_id": run_id,
        "model": run["model"],
        "prompt_version": run["prompt_version"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "input_tokens": run["input_tokens"],
        "output_tokens": run["output_tokens"],
        "cost_usd": run["cost_usd"],
        "notes": run["notes"],
        "format": fmt,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "row_counts": counts,
        "coverage": (
            "Executive Orders with full text in the Federal Register API,"
            " which begins ~1994. Orders below EO 12890 are not included."
        ),
        "source_quote": (
            "Every claim row carries a source_quote verified to appear in the"
            " order's text. quote_trimmed=1 means the model's original quote"
            " drifted and was trimmed to its verifiable span."
        ),
    }


def write_csv(path: Path, columns: list[str], rows: list[tuple]) -> None:
    import csv as csv_module

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv_module.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)


def write_parquet(path: Path, columns: list[str], rows: list[tuple]) -> None:
    import pandas as pd

    pd.DataFrame(rows, columns=columns).to_parquet(path, index=False)


def export_run(
    con: sqlite3.Connection,
    run_id: int,
    out_dir: Path,
    *,
    fmt: str = CSV,
    include_text: bool = False,
) -> dict[str, int]:
    """Write one run to `out_dir`. Returns row counts per table."""
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    for table in tables(run_id):
        columns, rows = fetch(con, table, run_id, include_text)
        path = out_dir / f"{table.name}.{fmt}"
        if fmt == PARQUET:
            write_parquet(path, columns, rows)
        else:
            write_csv(path, columns, rows)
        counts[table.name] = len(rows)

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest(con, run_id, counts, fmt), indent=2) + "\n",
        encoding="utf-8",
    )
    return counts
