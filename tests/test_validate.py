"""Tests for the quality gates.

v1 could not tell success from failure: zero rows said "Failed" and a third
were empty. These tests pin that each gate actually fails on the condition it
exists to catch -- a gate that cannot fail is worse than no gate, because it
reads as reassurance.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from eo import db, validate

BODY = (
    "Executive Order 99999 of May 1, 2020\n\nTest Order\n\n"
    "By the authority vested in me as President, the Secretary of Commerce\n"
    "shall submit a report within 30 days. Executive Order 12345 is revoked."
)


@pytest.fixture
def con() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.migrate(connection)
    connection.execute(
        "INSERT INTO documents (document_number, eo_number, title, president,"
        " signing_date, body_text, body_char_count) VALUES"
        " ('2020-0001', 99999, 'Test Order', 'A President', '2020-05-01', ?, ?)",
        (BODY, len(BODY)),
    )
    connection.execute(
        "INSERT INTO extraction_runs (run_id, model, prompt_version)"
        " VALUES (1, 'test/model', 'v-test')"
    )
    connection.commit()
    yield connection
    connection.close()


def add_extraction(con: sqlite3.Connection, **overrides: object) -> None:
    row = {
        "summary": "A test order directing a report.",
        "primary_topic": "government_administration",
        "instrument": "directs_report_or_study",
        "finish_reason": "stop",
    }
    row.update(overrides)
    con.execute(
        "INSERT OR REPLACE INTO extractions (document_number, run_id, summary,"
        " primary_topic, instrument, finish_reason)"
        " VALUES ('2020-0001', 1, ?, ?, ?, ?)",
        (
            row["summary"], row["primary_topic"], row["instrument"],
            row["finish_reason"],
        ),
    )
    con.commit()


def add_quote(con: sqlite3.Connection, quote: str) -> None:
    con.execute(
        "INSERT INTO authorities (document_number, run_id, authority, source_quote)"
        " VALUES ('2020-0001', 1, 'test authority', ?)",
        (quote,),
    )
    con.commit()


def test_groundedness_passes_on_a_real_quote(con: sqlite3.Connection) -> None:
    add_extraction(con)
    add_quote(con, "the Secretary of Commerce shall submit a report")
    gates, failures = validate.gate_groundedness(con, 1)
    assert all(g.passed for g in gates)
    assert failures == []


def test_groundedness_fails_on_a_fabricated_quote(con: sqlite3.Connection) -> None:
    """The v1 headline failure: a tariff order described as a COVID order."""
    add_extraction(con)
    add_quote(con, "invokes the Defense Production Act to expand production")
    gates, failures = validate.gate_groundedness(con, 1)
    assert not gates[0].passed
    assert len(failures) == 1


def test_groundedness_scores_the_model_not_the_repair(con: sqlite3.Connection) -> None:
    """A trimmed quote must not count as if the model had written it.

    Scoring the repaired text would make the gate report its own success."""
    add_extraction(con)
    con.execute(
        "INSERT INTO authorities (document_number, run_id, authority,"
        " source_quote, raw_quote, quote_trimmed) VALUES ('2020-0001', 1, 'a', ?, ?, 1)",
        (
            "the Secretary of Commerce shall submit a report",
            "the Secretary of Commerce shall submit a report to the Moon",
        ),
    )
    con.commit()

    gates, failures = validate.gate_groundedness(con, 1)
    fidelity, stored = gates
    assert not fidelity.passed          # the model's own quote was wrong
    assert stored.passed                # what we stored is verifiable
    assert "1 quote(s) trimmed" in stored.detail
    assert len(failures) == 1


def test_null_rate_fails_on_empty_rows(con: sqlite3.Connection) -> None:
    add_extraction(con, summary=None, primary_topic=None)
    assert not validate.gate_null_rate(con, 1).passed


def test_truncation_gate_fails_on_a_length_finish(con: sqlite3.Connection) -> None:
    add_extraction(con, finish_reason="length")
    gate = validate.gate_truncation(con, 1)
    assert not gate.passed
    assert gate.value == "1"


def test_other_rate_fails_when_the_vocabulary_has_a_gap(con: sqlite3.Connection) -> None:
    """How the missing 'education' domain surfaced."""
    add_extraction(con, primary_topic="other")
    assert not validate.gate_other_rate(con, 1).passed


def test_relationship_disagreement_is_flagged_not_failed(con: sqlite3.Connection) -> None:
    """FR is authoritative, but a disagreement is for a human to judge, so it
    goes to the review queue rather than failing the run."""
    add_extraction(con)
    con.execute(
        "INSERT INTO relationships (document_number, run_id, relation,"
        " target_eo_number, target_label, source) VALUES"
        " ('2020-0001', NULL, 'revokes', 12345, 'EO 12345', 'fr_disposition_notes')"
    )
    con.execute(
        "INSERT INTO relationships (document_number, run_id, relation,"
        " target_eo_number, target_label, source) VALUES"
        " ('2020-0001', 1, 'amends', 12345, 'EO 12345', 'model')"
    )
    con.commit()

    gate, disagreements = validate.gate_relationship_agreement(con, 1)
    assert gate.advisory
    assert gate.passed  # advisory gates never fail a run
    assert len(disagreements) == 1
    assert "FR says revokes" in disagreements[0][2]


def test_agreeing_relationship_is_not_flagged(con: sqlite3.Connection) -> None:
    add_extraction(con)
    for run_id, source in ((None, "fr_disposition_notes"), (1, "model")):
        con.execute(
            "INSERT INTO relationships (document_number, run_id, relation,"
            " target_eo_number, target_label, source) VALUES"
            " ('2020-0001', ?, 'revokes', 12345, 'EO 12345', ?)",
            (run_id, source),
        )
    con.commit()
    _, disagreements = validate.gate_relationship_agreement(con, 1)
    assert disagreements == []


def test_gold_set_scores_agreement(con: sqlite3.Connection, tmp_path: Path) -> None:
    add_extraction(con, primary_topic="health", instrument="creates_body")
    gold_file = tmp_path / "gold.json"
    gold_file.write_text(
        json.dumps({"labels": [
            {"eo_number": 99999, "primary_topic": "health",
             "instrument": "imposes_sanctions"}
        ]})
    )
    gold = validate.load_gold_set(gold_file)

    gates = {g.name: g for g in validate.gate_gold_set(con, 1, gold)}
    assert gates["gold: primary_topic"].value == "100%"
    assert gates["gold: instrument"].value == "0%"
    assert not gates["gold: instrument"].passed


def test_missing_gold_set_is_advisory_not_a_pass(con: sqlite3.Connection) -> None:
    """An absent gold set must not read as a passing score."""
    add_extraction(con)
    (gate,) = validate.gate_gold_set(con, 1, {})
    assert gate.advisory
    assert gate.value == "absent"


def test_review_queue_is_rebuilt_per_validation(con: sqlite3.Connection) -> None:
    add_extraction(con)
    add_quote(con, "a quote that is not in the document at all, definitely not")
    first = validate.validate_run(con, 1)
    second = validate.validate_run(con, 1)
    assert first.review_items == second.review_items
    assert con.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == second.review_items


def test_shipped_gold_set_is_valid() -> None:
    """The real gold set must stay parseable and use only allowed values."""
    from eo.models import Domain, Instrument

    gold = validate.load_gold_set()
    assert len(gold) == 20
    for entry in gold.values():
        assert entry["primary_topic"] in {d.value for d in Domain}
        assert entry["instrument"] in {i.value for i in Instrument}
        assert "significance" not in entry  # dropped: uncalibrated, 0% agreement
