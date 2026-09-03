"""Ingest tests: storage shape, resumability, and refusal to store junk."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from eo import db, fr_client, ingest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def con(tmp_path: Path) -> sqlite3.Connection:
    connection = db.connect(tmp_path / "test.db")
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def record() -> dict:
    payload = json.loads((FIXTURES / "search_page.json").read_text())
    return payload["results"][0]


def _cache_body(raw_dir: Path, document_number: str, fixture: str) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    body = fr_client.clean_raw_text((FIXTURES / fixture).read_text())
    (raw_dir / f"{document_number}.txt").write_text(body)


def test_ingest_stores_a_document_from_cache_without_network(
    con: sqlite3.Connection, tmp_path: Path, record: dict
) -> None:
    raw_dir = tmp_path / "raw"
    _cache_body(raw_dir, record["document_number"], "raw_1994_94-290.txt")

    report = ingest.ingest(con, raw_dir, records=iter([record]))

    assert report.inserted == 1
    assert report.from_cache == 1
    assert report.failures == []

    row = con.execute("SELECT * FROM documents").fetchone()
    assert row["document_number"] == record["document_number"]
    assert row["eo_number"] == 12890
    assert row["president"] == "William J. Clinton"  # flattened from the object
    assert row["body_char_count"] >= fr_client.MIN_BODY_CHARS
    assert json.loads(row["fr_agencies_json"])  # FR's own tagging is preserved


def test_second_run_skips_what_is_already_stored(
    con: sqlite3.Connection, tmp_path: Path, record: dict
) -> None:
    raw_dir = tmp_path / "raw"
    _cache_body(raw_dir, record["document_number"], "raw_1994_94-290.txt")

    ingest.ingest(con, raw_dir, records=iter([record]))
    second = ingest.ingest(con, raw_dir, records=iter([record]))

    assert second.skipped == 1
    assert second.inserted == 0
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_document_with_no_raw_text_url_is_reported_not_stored(
    con: sqlite3.Connection, tmp_path: Path, record: dict
) -> None:
    """v1 stored empty bodies and sent them to the model, which invented
    content for them. Here the row is refused and surfaced instead."""
    broken = dict(record, raw_text_url=None)

    report = ingest.ingest(con, tmp_path / "raw", records=iter([broken]))

    assert report.inserted == 0
    assert len(report.failures) == 1
    assert report.failures[0][0] == broken["document_number"]
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_short_body_is_refused(con: sqlite3.Connection, tmp_path: Path, record: dict) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / f"{record['document_number']}.txt").write_text("too short")
    broken = dict(record, raw_text_url=None)

    report = ingest.ingest(con, raw_dir, records=iter([broken]))

    assert report.inserted == 0
    assert len(report.failures) == 1
