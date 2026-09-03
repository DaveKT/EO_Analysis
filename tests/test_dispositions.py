"""Tests for parsing the Federal Register's disposition notes.

These notes are authoritative and free, so the model is never asked to
reconstruct them. That only holds if the parser is faithful -- including about
direction, which the relation names encode.
"""

from __future__ import annotations

import sqlite3

import pytest

from eo import db, dispositions


def parse(notes: str) -> list[dispositions.ParsedRelation]:
    return dispositions.parse_notes(notes)


def test_simple_outbound_relation() -> None:
    (rel,) = parse("Revokes: EO 13767, January 25, 2017")
    assert rel.relation == "revokes"
    assert rel.target_eo_number == 13767
    assert rel.target_type == "executive_order"
    assert rel.in_part is False


def test_inbound_relation_keeps_its_direction() -> None:
    """"Revoked by" is knowledge from the future: this order did not act, it
    was acted upon. Collapsing the two directions would invert the revocation
    network."""
    (rel,) = parse("Revoked by: EO 14148, January 20, 2025")
    assert rel.relation == "revoked_by"
    assert rel.target_eo_number == 14148


def test_multiple_labels_and_targets_in_one_note() -> None:
    notes = (
        "Revokes: EO 13767, January 25, 2017\n"
        "Revoked by: EO 14148, January 20, 2025; EO 14159, January 20, 2025"
    )
    parsed = parse(notes)
    assert {(r.relation, r.target_eo_number) for r in parsed} == {
        ("revokes", 13767),
        ("revoked_by", 14148),
        ("revoked_by", 14159),
    }


def test_in_part_is_recorded() -> None:
    (rel,) = parse("Supersedes in part: EO 13106, December 7, 1998")
    assert rel.relation == "supersedes"
    assert rel.in_part is True


def test_see_becomes_references() -> None:
    (rel,) = parse("See: EO 12958, April 17, 1995")
    assert rel.relation == "references"


@pytest.mark.parametrize(
    ("notes", "target_type"),
    [
        ("See: Proc. 9704, March 8, 2018", "proclamation"),
        ("Continued by: Notice of September 18, 1995", "notice"),
        ("See: Memorandum of January 20, 2021", "memorandum"),
    ],
)
def test_non_executive_order_targets_are_typed_not_dropped(
    notes: str, target_type: str
) -> None:
    """589 of ~4,000 edges point at proclamations, notices and memoranda.
    Storing only EO numbers would silently lose them."""
    (rel,) = parse(notes)
    assert rel.target_type == target_type
    assert rel.target_eo_number is None


def test_bare_number_target() -> None:
    """FR writes this one without the EO prefix (EO 12921)."""
    (rel,) = parse("Amends: 12864, September 15, 1993")
    assert rel.relation == "amends"
    assert rel.target_eo_number == 12864


def test_typo_in_federal_register_label_is_handled() -> None:
    """FR spells it 'Suspersedes' once in the corpus."""
    (rel,) = parse("Suspersedes: EO 12100, January 1, 1979")
    assert rel.relation == "supersedes"


def test_prose_notes_yield_nothing() -> None:
    assert parse("Note: In memoriam of President Nixon") == []
    assert parse("Federal Register page and date: 66 FR 57355, November 15, 2001;") == []
    assert parse("") == []
    assert parse(None) == []


def test_compound_label() -> None:
    parsed = parse("Revokes in part and supplements: EO 12345, June 1, 1982")
    assert {r.relation for r in parsed} == {"revokes", "supplements"}
    assert all(r.in_part for r in parsed)


def test_seeding_is_idempotent() -> None:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    db.migrate(con)
    con.execute(
        "INSERT INTO documents (document_number, eo_number, disposition_notes)"
        " VALUES ('2021-02563', 14010, 'Revokes: EO 13767, January 25, 2017')"
    )
    con.commit()

    first = dispositions.seed_relationships(con)
    second = dispositions.seed_relationships(con)

    assert first["edges_inserted"] == 1
    assert second["edges_inserted"] == 0
    assert con.execute("SELECT COUNT(*) FROM relationships").fetchone()[0] == 1
    row = con.execute("SELECT * FROM relationships").fetchone()
    assert row["source"] == dispositions.FR_SOURCE
    assert row["run_id"] is None  # seeded rows belong to no extraction run
    con.close()
