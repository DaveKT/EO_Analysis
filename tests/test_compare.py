"""Tests for the run-vs-run comparison.

The comparison exists to separate "the model's ceiling" from "the task's
ceiling", and the way it could quietly lie is by comparing populations rather
than documents: each run's own gate report is computed over its own rows, and
the comparison set is deliberately enriched with the baseline's failures. Every
figure here must therefore be restricted to the documents both runs cover.
"""

from __future__ import annotations

import sqlite3

import pytest

from eo import compare, db

BODY = (
    "By the authority vested in me as President, the Secretary of Commerce\n"
    "shall submit a report within 30 days."
)
GOLD = {
    100: {"primary_topic": "health", "instrument": "creates_body"},
    101: {"primary_topic": "trade", "instrument": "delegates_authority"},
}


@pytest.fixture
def con() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.migrate(connection)
    for eo_number in (100, 101, 102):
        connection.execute(
            "INSERT INTO documents (document_number, eo_number, title, president,"
            " signing_date, body_text, body_char_count)"
            " VALUES (?, ?, ?, 'P', '2020-01-01', ?, ?)",
            (f"doc-{eo_number}", eo_number, f"Order {eo_number}", BODY, len(BODY)),
        )
    for run_id, model, cost in ((1, "cheap/model", 0.06), (2, "frontier/model", 0.85)):
        connection.execute(
            "INSERT INTO extraction_runs (run_id, model, prompt_version, cost_usd)"
            " VALUES (?, ?, 'v7', ?)",
            (run_id, model, cost),
        )
    connection.commit()
    yield connection
    connection.close()


def add(con, run_id, eo_number, topic, instrument, summary="s"):
    con.execute(
        "INSERT OR REPLACE INTO extractions (document_number, run_id, summary,"
        " primary_topic, instrument, finish_reason) VALUES (?, ?, ?, ?, ?, 'stop')",
        (f"doc-{eo_number}", run_id, summary, topic, instrument),
    )
    con.commit()


def add_quote(con, run_id, eo_number, quote, raw=None):
    con.execute(
        "INSERT INTO authorities (document_number, run_id, authority, source_quote,"
        " raw_quote, quote_trimmed) VALUES (?, ?, 'auth', ?, ?, ?)",
        (f"doc-{eo_number}", run_id, quote, raw, 1 if raw else 0),
    )
    con.commit()


@pytest.fixture(autouse=True)
def gold(monkeypatch):
    monkeypatch.setattr(compare, "load_gold_set", lambda: GOLD)


def test_only_documents_both_runs_extracted_are_compared(con):
    add(con, 1, 100, "health", "creates_body")
    add(con, 1, 101, "trade", "delegates_authority")
    add(con, 2, 100, "health", "creates_body")
    # run 2 never finished EO 101.

    report = compare.compare_runs(con, 1, 2)

    assert report.documents == ["doc-100"]
    assert report.axes[0].n == 1


def test_a_failed_row_is_not_counted_as_a_disagreement(con):
    """A row with no summary is work that failed, not an answer that differs."""
    add(con, 1, 100, "health", "creates_body")
    add(con, 2, 100, "health", "creates_body")
    con.execute(
        "INSERT INTO extractions (document_number, run_id, finish_reason)"
        " VALUES ('doc-101', 2, 'length')"
    )
    add(con, 1, 101, "trade", "delegates_authority")
    con.commit()

    report = compare.compare_runs(con, 1, 2)

    assert report.documents == ["doc-100"]


def test_both_wrong_is_separated_from_one_wrong(con):
    add(con, 1, 100, "energy_and_environment", "creates_body")   # topic wrong
    add(con, 2, 100, "energy_and_environment", "creates_body")   # topic wrong too
    add(con, 1, 101, "trade", "creates_body")                    # instrument wrong
    add(con, 2, 101, "trade", "delegates_authority")             # instrument right

    report = compare.compare_runs(con, 1, 2)
    topic, instrument = report.axes

    assert [entry[0] for entry in topic.both_wrong] == [100]
    assert topic.baseline_only_wrong == []
    assert [entry[0] for entry in instrument.baseline_only_wrong] == [101]
    assert instrument.both_wrong == []
    assert topic.baseline_hits == 1 and topic.candidate_hits == 1
    assert instrument.baseline_hits == 1 and instrument.candidate_hits == 2


def test_non_gold_documents_score_agreement_but_not_gold(con):
    add(con, 1, 102, "health", "creates_body")
    add(con, 2, 102, "trade", "creates_body")

    report = compare.compare_runs(con, 1, 2)

    assert report.gold_orders == 0
    assert report.axes[0].n == 0
    agree, total, disagreements = report.field_agreement["primary_topic"]
    assert (agree, total) == (0, 1)
    assert disagreements[0][0] == 102
    assert report.field_agreement["instrument"][:2] == (1, 1)


def test_grounding_scores_the_model_not_the_repair(con):
    """Where a quote was trimmed, `raw_quote` is what the model wrote."""
    add(con, 1, 100, "health", "creates_body")
    add(con, 2, 100, "health", "creates_body")
    add_quote(con, 1, 100, "the Secretary of Commerce", raw="the Secretary of Fiction")
    add_quote(con, 2, 100, "the Secretary of Commerce")

    report = compare.compare_runs(con, 1, 2)

    assert report.grounding[1].checked == 1
    assert report.grounding[1].grounded == 0   # scored on raw_quote, which is invented
    assert report.grounding[2].grounded == 1


def test_grounding_and_claims_exclude_unshared_documents(con):
    add(con, 1, 100, "health", "creates_body")
    add(con, 2, 100, "health", "creates_body")
    add(con, 1, 101, "trade", "delegates_authority")   # baseline only
    add_quote(con, 1, 100, "the Secretary of Commerce")
    add_quote(con, 1, 101, "the Secretary of Commerce")

    report = compare.compare_runs(con, 1, 2)

    assert report.documents == ["doc-100"]
    assert report.grounding[1].checked == 1
    assert report.claims[1]["authorities"] == 1


def test_unknown_run_is_an_error(con):
    with pytest.raises(ValueError, match="no such run"):
        compare.compare_runs(con, 1, 99)
