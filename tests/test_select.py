"""Tests for document selection, including the frontier-comparison set.

Selection is the one place where spending real money on the wrong rows is
silent: an off-by-one here costs tokens rather than raising. The comparison set
in particular has to hold two independent populations at once -- the gold
labels and a run's review queue -- and dropping either would still produce a
plausible-looking run.
"""

from __future__ import annotations

import sqlite3

import pytest

from eo import db, extract


@pytest.fixture
def con() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.migrate(connection)
    body = "x" * 1000
    for eo_number in range(100, 110):
        connection.execute(
            "INSERT INTO documents (document_number, eo_number, title, president,"
            " signing_date, body_text, body_char_count)"
            " VALUES (?, ?, 'T', 'P', '2020-01-01', ?, ?)",
            (f"doc-{eo_number}", eo_number, body, len(body)),
        )
    connection.execute(
        "INSERT INTO extraction_runs (run_id, model, prompt_version)"
        " VALUES (1, 'cheap/model', 'v7')"
    )
    connection.commit()
    yield connection
    connection.close()


def flag(con: sqlite3.Connection, document_number: str) -> None:
    con.execute(
        "INSERT INTO review_queue (run_id, document_number, kind, detail, created_at)"
        " VALUES (1, ?, 'ungrounded_deadlines', 'drifted', '2020-01-01')",
        (document_number,),
    )
    con.commit()


def test_comparison_set_is_gold_plus_flagged(con, monkeypatch):
    monkeypatch.setattr(extract, "load_gold_set", lambda: {101: {}, 102: {}})
    flag(con, "doc-107")
    flag(con, "doc-108")

    picked = {row["document_number"] for row in extract.comparison_set(con, 1)}

    assert picked == {"doc-101", "doc-102", "doc-107", "doc-108"}


def test_comparison_set_counts_a_document_once(con, monkeypatch):
    """A gold order the run also flagged must not be extracted twice."""
    monkeypatch.setattr(extract, "load_gold_set", lambda: {101: {}})
    flag(con, "doc-101")
    flag(con, "doc-101")

    picked = [row["document_number"] for row in extract.comparison_set(con, 1)]

    assert picked == ["doc-101"]


def test_comparison_set_ignores_other_runs_review_queue(con, monkeypatch):
    monkeypatch.setattr(extract, "load_gold_set", dict)
    con.execute(
        "INSERT INTO extraction_runs (run_id, model, prompt_version)"
        " VALUES (2, 'other/model', 'v7')"
    )
    con.execute(
        "INSERT INTO review_queue (run_id, document_number, kind, detail, created_at)"
        " VALUES (2, 'doc-109', 'ungrounded_deadlines', 'drifted', '2020-01-01')"
    )
    con.commit()
    flag(con, "doc-107")

    picked = {row["document_number"] for row in extract.comparison_set(con, 1)}

    assert picked == {"doc-107"}


def test_empty_gold_set_still_returns_flagged(con, monkeypatch):
    """A missing gold file must not silently select the whole corpus."""
    monkeypatch.setattr(extract, "load_gold_set", dict)
    flag(con, "doc-103")

    picked = {row["document_number"] for row in extract.comparison_set(con, 1)}

    assert picked == {"doc-103"}


def test_select_documents_uses_comparison_set_over_sampling(con, monkeypatch):
    monkeypatch.setattr(extract, "load_gold_set", lambda: {105: {}})

    picked = extract.select_documents(
        con, limit=None, run_id=None, spread=True, compare_run=1
    )

    assert [row["document_number"] for row in picked] == ["doc-105"]


def test_comparison_set_resume_skips_finished_rows(con, monkeypatch):
    """`--compare-run 9 --run-id 10` must retry only what run 10 still owes."""
    monkeypatch.setattr(extract, "load_gold_set", lambda: {101: {}, 102: {}})
    con.execute(
        "INSERT INTO extraction_runs (run_id, model, prompt_version)"
        " VALUES (10, 'frontier/model', 'v7')"
    )
    con.execute(
        "INSERT INTO extractions (document_number, run_id, summary, primary_topic,"
        " instrument, finish_reason) VALUES ('doc-101', 10, 's', 'health',"
        " 'creates_body', 'stop')"
    )
    con.commit()

    picked = extract.select_documents(
        con, limit=None, run_id=10, spread=True, compare_run=1
    )

    assert [row["document_number"] for row in picked] == ["doc-102"]
