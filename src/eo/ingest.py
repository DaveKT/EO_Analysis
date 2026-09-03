"""Phase 1 ingest: Federal Register metadata + document bodies into SQLite.

Nothing here calls a model. This stage is free, so it is worth getting
completely right before any tokens are spent -- every v1 failure downstream
began with a document body that was empty and not noticed.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from eo import fr_client

UPSERT = """
INSERT OR REPLACE INTO documents (
  document_number, eo_number, title, president, signing_date, publication_date,
  citation, pdf_url, raw_text_url, disposition_notes, fr_agencies_json,
  body_text, body_char_count, fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@dataclass
class Report:
    seen: int = 0
    inserted: int = 0
    skipped: int = 0
    from_cache: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"seen {self.seen}, inserted {self.inserted} "
            f"({self.from_cache} from cache), skipped {self.skipped}, "
            f"failed {len(self.failures)}"
        )


def _president_name(record: dict[str, Any]) -> str | None:
    value = record.get("president")
    if isinstance(value, dict):
        return value.get("name")
    return value


def _eo_number(record: dict[str, Any]) -> int | None:
    raw = record.get("executive_order_number")
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def existing_document_numbers(con: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT document_number FROM documents WHERE body_char_count >= ?",
            (fr_client.MIN_BODY_CHARS,),
        )
    }


def _body_for(
    client: httpx.Client,
    record: dict[str, Any],
    raw_dir: Path,
    *,
    refresh: bool,
) -> tuple[str, bool]:
    """Return (body, came_from_cache). Raises on anything that is not real text."""
    cache_path = raw_dir / f"{record['document_number']}.txt"
    if cache_path.exists() and not refresh:
        cached = cache_path.read_text(encoding="utf-8")
        if len(cached) >= fr_client.MIN_BODY_CHARS:
            return cached, True

    url = record.get("raw_text_url")
    if not url:
        raise fr_client.EmptyBodyError("no raw_text_url on this record")

    body = fr_client.fetch_body(client, url)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(body, encoding="utf-8")
    return body, False


def ingest(
    con: sqlite3.Connection,
    raw_dir: Path,
    *,
    limit: int | None = None,
    refresh: bool = False,
    per_page: int = 100,
    on_progress: Callable[[Report, dict[str, Any]], None] | None = None,
    records: Iterator[dict[str, Any]] | None = None,
) -> Report:
    report = Report()
    already = set() if refresh else existing_document_numbers(con)
    client = fr_client.make_client()

    try:
        source = records if records is not None else fr_client.iter_documents(
            client, per_page=per_page
        )
        for record in source:
            if limit is not None and report.seen >= limit:
                break
            report.seen += 1
            doc_num = record["document_number"]

            if doc_num in already:
                report.skipped += 1
                continue

            try:
                body, cached = _body_for(client, record, raw_dir, refresh=refresh)
            except (fr_client.BlockedError, fr_client.EmptyBodyError, httpx.HTTPError) as exc:
                # Recorded, never swallowed: these become the run's failure list
                # and the document is not inserted with a junk body.
                report.failures.append((doc_num, f"{type(exc).__name__}: {exc}"))
                continue

            con.execute(
                UPSERT,
                (
                    doc_num,
                    _eo_number(record),
                    record.get("title"),
                    _president_name(record),
                    record.get("signing_date"),
                    record.get("publication_date"),
                    record.get("citation"),
                    record.get("pdf_url"),
                    record.get("raw_text_url"),
                    record.get("disposition_notes"),
                    json.dumps(record.get("agencies") or []),
                    body,
                    len(body),
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )
            con.commit()  # per record: an interrupted run keeps everything so far

            report.inserted += 1
            if cached:
                report.from_cache += 1
            if on_progress:
                on_progress(report, record)
    finally:
        client.close()

    return report
