"""Tests for the standalone analysis database.

Two failure modes are worth pinning. The first is the one every join in this
file depends on: a claim, its order, its source text and the model's original
quote must all reach each other by key. The second is subtler and was found by
running the build -- `source_quote` meant two different things depending on
`source`, so a groundedness check over the joined table read 70% instead of
100%. A column whose meaning depends on a sibling column is a trap, and the
schema now makes it structurally impossible.
"""

from __future__ import annotations

import sqlite3

import pytest

from eo import analysis_db, db

BODY = (
    "By the authority vested in me as President, the Secretary of Commerce\n"
    "shall submit a report within 30 days. Executive Order 100 is revoked."
)


@pytest.fixture
def source() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    db.migrate(con)
    for eo_number in (100, 101):
        con.execute(
            "INSERT INTO documents (document_number, eo_number, title, president,"
            " signing_date, body_text, body_char_count, disposition_notes)"
            " VALUES (?, ?, ?, 'P', '2020-01-01', ?, ?, 'Revokes: EO 100')",
            (f"doc-{eo_number}", eo_number, f"Order {eo_number}", BODY, len(BODY)),
        )
    for run_id in (1, 2):
        con.execute(
            "INSERT INTO extraction_runs (run_id, model, prompt_version, cost_usd)"
            " VALUES (?, ?, 'v7', 0.5)",
            (run_id, f"model-{run_id}"),
        )
    con.execute(
        "INSERT INTO extractions (document_number, run_id, summary, primary_topic,"
        " instrument, secondary_topics, finish_reason) VALUES ('doc-101', 1,"
        " 'a summary', 'health', 'creates_body', '[\"trade\", \"health\"]', 'stop')"
    )
    con.execute(
        "INSERT INTO extractions (document_number, run_id, summary, primary_topic,"
        " instrument, secondary_topics, finish_reason) VALUES ('doc-101', 2,"
        " 'other run', 'trade', 'other', '[]', 'stop')"
    )
    con.execute(
        "INSERT INTO agencies_tasked (document_number, run_id, agency_name, task,"
        " source_quote, raw_quote, quote_trimmed) VALUES ('doc-101', 1, 'Commerce',"
        " 'report', 'the Secretary of Commerce', 'the Secretary of Fiction', 1)"
    )
    con.execute(
        "INSERT INTO agencies_tasked (document_number, run_id, agency_name, task,"
        " source_quote) VALUES ('doc-101', 2, 'Other', 't', 'q')"
    )
    con.execute(
        "INSERT INTO relationships (document_number, run_id, relation,"
        " target_eo_number, source, source_quote) VALUES ('doc-101', 1, 'revokes',"
        " 100, 'model', 'Executive Order 100 is revoked')"
    )
    con.execute(
        "INSERT INTO relationships (document_number, run_id, relation,"
        " target_eo_number, source, source_quote) VALUES ('doc-101', NULL,"
        " 'revokes', 100, 'fr_disposition_notes', 'Revokes: EO 100')"
    )
    con.execute(
        "INSERT INTO relationships (document_number, run_id, relation,"
        " target_eo_number, source, source_quote) VALUES ('doc-101', 1, 'amends',"
        " 9999, 'model', 'the Secretary of Commerce')"
    )
    con.execute(
        "INSERT INTO review_queue (run_id, document_number, kind, detail, created_at)"
        " VALUES (1, 'doc-101', 'ungrounded_deadlines', 'drifted', '2020-01-01')"
    )
    con.commit()
    yield con
    con.close()


@pytest.fixture
def built(source, tmp_path):
    path = tmp_path / "analysis.db"
    counts = analysis_db.build(source, path, 1)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    yield con, counts, path
    con.close()


def test_only_the_named_run_is_included(built):
    con, counts, _ = built
    assert counts["orders"] == 1
    assert con.execute("SELECT summary FROM orders").fetchone()[0] == "a summary"
    names = [r[0] for r in con.execute("SELECT agency_name FROM agencies_tasked")]
    assert names == ["Commerce"]


def test_foreign_keys_hold(built):
    con, _, _ = built
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_source_text_joins_to_its_order(built):
    con, _, _ = built
    row = con.execute(
        "SELECT o.eo_number, t.body_text FROM orders o"
        " JOIN order_text t USING (document_number)"
    ).fetchone()
    assert row["eo_number"] == 101
    assert row["body_text"] == BODY


def test_raw_quote_links_back_to_its_claim(built):
    con, _, _ = built
    row = con.execute(
        "SELECT a.agency_name, q.source_quote, q.raw_quote"
        " FROM raw_quotes q JOIN agencies_tasked a ON a.id = q.claim_id"
        " WHERE q.claim_table = 'agencies_tasked'"
    ).fetchone()
    assert row["agency_name"] == "Commerce"
    assert row["raw_quote"] == "the Secretary of Fiction"
    assert row["source_quote"] == "the Secretary of Commerce"


def test_raw_quotes_match_the_trimmed_flag(built):
    con, counts, _ = built
    trimmed = con.execute(
        "SELECT COUNT(*) FROM all_claims WHERE quote_trimmed = 1"
    ).fetchone()[0]
    assert counts["raw_quotes"] == trimmed


def test_secondary_topics_are_exploded(built):
    con, counts, _ = built
    topics = sorted(r[0] for r in con.execute("SELECT topic FROM order_secondary_topics"))
    assert topics == ["health", "trade"]
    assert counts["order_secondary_topics"] == 2
    # The raw array is kept as well, so nothing is lost.
    assert '"trade"' in con.execute("SELECT secondary_topics_json FROM orders").fetchone()[0]


def test_federal_register_evidence_is_not_a_source_quote(built):
    """The trap this schema exists to remove: an FR disposition note is *about*
    the order and appears nowhere inside it, so filing it as `source_quote`
    makes any groundedness check over the join read far below 100%."""
    con, _, _ = built
    fr = con.execute(
        "SELECT source_quote, fr_disposition_note FROM relationships"
        " WHERE source = 'fr_disposition_notes'"
    ).fetchone()
    assert fr["source_quote"] is None
    assert fr["fr_disposition_note"] == "Revokes: EO 100"

    model = con.execute(
        "SELECT source_quote, fr_disposition_note FROM relationships"
        " WHERE source = 'model' AND relation = 'revokes'"
    ).fetchone()
    assert model["source_quote"] == "Executive Order 100 is revoked"
    assert model["fr_disposition_note"] is None


def test_every_all_claims_quote_is_in_its_order_text(built):
    """The dataset's central contract, checkable inside this database alone --
    which the CSV export could not support."""
    from eo import grounding

    con, _, _ = built
    rows = con.execute(
        "SELECT c.source_quote, t.body_text FROM all_claims c"
        " JOIN order_text t USING (document_number)"
    ).fetchall()
    assert rows
    assert all(grounding.is_grounded(r["source_quote"], r["body_text"]) for r in rows)


def test_unresolvable_relationship_targets_are_null_not_dropped(built):
    con, _, _ = built
    row = con.execute(
        "SELECT target_eo_number, target_document_number FROM relationships"
        " WHERE relation = 'amends'"
    ).fetchone()
    assert row["target_eo_number"] == 9999
    assert row["target_document_number"] is None

    resolved = con.execute(
        "SELECT target_document_number FROM relationships"
        " WHERE relation = 'revokes' AND source = 'model'"
    ).fetchone()[0]
    assert resolved is None  # EO 100 was never extracted, so it is not an order here


def test_run_metadata_records_provenance(built):
    con, _, _ = built
    row = con.execute("SELECT * FROM run_metadata").fetchone()
    assert row["run_id"] == 1
    assert row["model"] == "model-1"
    assert "1994" in row["coverage"]
    assert "fr_disposition_note" in row["quote_contract"]


def test_rebuild_replaces_rather_than_appends(source, tmp_path):
    path = tmp_path / "analysis.db"
    analysis_db.build(source, path, 1)
    counts = analysis_db.build(source, path, 1)
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == counts["orders"]
    assert con.execute("SELECT COUNT(*) FROM run_metadata").fetchone()[0] == 1
    con.close()


def test_unknown_run_is_an_error(source, tmp_path):
    with pytest.raises(ValueError, match="no such run"):
        analysis_db.build(source, tmp_path / "x.db", 99)


def test_agency_names_are_normalised_across_both_tables(source, tmp_path):
    """Secretary/Department variants must collapse, and `responsible_party` on
    deadlines must be normalised too -- doing only one leaves the other wrong."""
    source.execute(
        "INSERT INTO agencies_tasked (document_number, run_id, agency_name, task,"
        " source_quote) VALUES ('doc-101', 1, 'Department of Commerce', 't',"
        " 'the Secretary of Commerce')"
    )
    source.execute(
        "INSERT INTO deadlines (document_number, run_id, due_description,"
        " responsible_party, source_quote) VALUES ('doc-101', 1, 'report',"
        " 'Secretary of Commerce', 'the Secretary of Commerce')"
    )
    source.commit()
    path = tmp_path / "a.db"
    analysis_db.build(source, path, 1)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        "SELECT claim_table, raw_name FROM agency_taskings"
        " WHERE canonical_name = 'Department of Commerce' ORDER BY claim_table"
    ).fetchall()
    assert [r["claim_table"] for r in rows] == ["agencies_tasked", "deadlines"]
    # Two different raw spellings, one canonical agency.
    assert {r["raw_name"] for r in rows} == {
        "Department of Commerce", "Secretary of Commerce"
    }
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


def test_raw_agency_name_is_never_overwritten(built):
    con, _, _ = built
    assert con.execute(
        "SELECT agency_name FROM agencies_tasked"
    ).fetchone()[0] == "Commerce"


def test_a_compound_agency_row_credits_every_agency_named(source, tmp_path):
    source.execute(
        "INSERT INTO agencies_tasked (document_number, run_id, agency_name, task,"
        " source_quote) VALUES ('doc-101', 1,"
        " 'Attorney General and Secretary of Homeland Security', 't', 'q')"
    )
    source.commit()
    path = tmp_path / "b.db"
    analysis_db.build(source, path, 1)
    con = sqlite3.connect(path)
    names = {
        r[0]
        for r in con.execute(
            "SELECT canonical_name FROM agency_taskings WHERE raw_name LIKE 'Attorney%'"
        )
    }
    assert names == {"Department of Justice", "Department of Homeland Security"}
    con.close()


def test_the_war_rename_is_recoverable_from_raw_names(source, tmp_path):
    """Merging War into Defense must not erase which name an order used."""
    source.execute(
        "INSERT INTO agencies_tasked (document_number, run_id, agency_name, task,"
        " source_quote) VALUES ('doc-101', 1, 'Secretary of War', 't', 'q')"
    )
    source.execute(
        "INSERT INTO agencies_tasked (document_number, run_id, agency_name, task,"
        " source_quote) VALUES ('doc-101', 1, 'Secretary of Defense', 't2', 'q')"
    )
    source.commit()
    path = tmp_path / "war.db"
    analysis_db.build(source, path, 1)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        "SELECT raw_name FROM agency_taskings"
        " WHERE canonical_name = 'Department of Defense' ORDER BY raw_name"
    ).fetchall()
    assert [r["raw_name"] for r in rows] == ["Secretary of Defense", "Secretary of War"]
    assert con.execute(
        "SELECT COUNT(*) FROM agencies WHERE canonical_name = 'Department of War'"
    ).fetchone()[0] == 0
    note = con.execute(
        "SELECT note FROM agencies WHERE canonical_name = 'Department of Defense'"
    ).fetchone()[0]
    assert "14347" in note and "raw_name" in note
    con.close()


def test_federal_register_outranks_the_model_even_when_they_agree(source, tmp_path):
    """The common case, and the one that quietly inflates counts: FR and the
    model assert the same edge, and counting both double-counts it."""
    source.execute(
        "INSERT INTO relationships (document_number, run_id, relation,"
        " target_eo_number, source, source_quote) VALUES ('doc-101', 1, 'revokes',"
        " 100, 'model', 'Executive Order 100 is revoked')"
    )
    source.commit()  # doc-101 already has an identical FR 'revokes 100' edge
    path = tmp_path / "fr.db"
    analysis_db.build(source, path, 1)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        "SELECT source, authoritative, superseded_by, contradicts_fr"
        " FROM relationships WHERE relation = 'revokes' AND target_eo_number = 100"
        " ORDER BY source"
    ).fetchall()
    fr, model = rows[0], rows[-1]
    assert fr["source"] == "fr_disposition_notes" and fr["authoritative"] == 1
    assert model["source"] == "model" and model["authoritative"] == 0
    assert model["superseded_by"] == con.execute(
        "SELECT id FROM relationships WHERE source = 'fr_disposition_notes'"
        " AND target_eo_number = 100"
    ).fetchone()[0]
    # Agreement is not a contradiction.
    assert model["contradicts_fr"] == 0
    con.close()


def test_a_model_edge_fr_says_nothing_about_is_kept(source, tmp_path):
    """Finding edges FR missed is the point of extracting them."""
    source.execute(
        "INSERT INTO relationships (document_number, run_id, relation,"
        " target_eo_number, source, source_quote) VALUES ('doc-101', 1,"
        " 'references', 555, 'model', 'the Secretary of Commerce')"
    )
    source.commit()
    path = tmp_path / "fr2.db"
    analysis_db.build(source, path, 1)
    con = sqlite3.connect(path)
    row = con.execute(
        "SELECT authoritative, superseded_by FROM relationships"
        " WHERE target_eo_number = 555"
    ).fetchone()
    assert row == (1, None)
    con.close()


def test_a_contradiction_is_marked_not_deleted(source, tmp_path):
    source.execute(
        "INSERT INTO relationships (document_number, run_id, relation,"
        " target_eo_number, source, source_quote) VALUES ('doc-101', 1, 'amends',"
        " 100, 'model', 'Executive Order 100 is revoked')"
    )
    source.commit()  # FR says 'revokes 100'; the model says 'amends'
    path = tmp_path / "fr3.db"
    analysis_db.build(source, path, 1)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT authoritative, contradicts_fr FROM relationships"
        " WHERE source = 'model' AND relation = 'amends'"
        "   AND target_eo_number = 100"
    ).fetchone()
    assert row["authoritative"] == 0
    assert row["contradicts_fr"] == 1
    con.close()


def test_revocation_network_counts_each_edge_once(source, tmp_path):
    source.execute(
        "INSERT INTO relationships (document_number, run_id, relation,"
        " target_eo_number, source, source_quote) VALUES ('doc-101', 1, 'revokes',"
        " 100, 'model', 'Executive Order 100 is revoked')"
    )
    source.execute(
        "INSERT INTO extractions (document_number, run_id, summary, primary_topic,"
        " instrument, secondary_topics, finish_reason) VALUES ('doc-100', 1, 's',"
        " 'health', 'creates_body', '[]', 'stop')"
    )
    source.commit()
    path = tmp_path / "fr4.db"
    analysis_db.build(source, path, 1)
    con = sqlite3.connect(path)
    total = con.execute(
        "SELECT COUNT(*) FROM revocation_network WHERE relation = 'revokes'"
    ).fetchone()[0]
    assert total == 1, "FR and model both assert this edge; it must count once"
    con.close()


def _corrections_file(tmp_path, run_id=1, model="model-1", from_value="health"):
    import json
    path = tmp_path / "primary_topic.json"
    path.write_text(json.dumps({
        "field": "primary_topic",
        "applies_to": {"run_id": run_id, "model": model, "prompt_version": "v7"},
        "corrections": [{"eo_number": 101, "from": from_value, "to": "trade", "reason": "test"}],
    }))
    return path


def test_a_hand_correction_replaces_the_label_and_keeps_the_original(source, tmp_path):
    path = tmp_path / "a.db"
    counts = analysis_db.build(source, path, 1, corrections_path=_corrections_file(tmp_path))
    con = sqlite3.connect(path)
    topic, original = con.execute(
        "SELECT primary_topic, primary_topic_as_extracted FROM orders WHERE eo_number = 101"
    ).fetchone()
    assert (topic, original) == ("trade", "health")
    assert counts["topic_corrections"] == 1


def test_an_uncorrected_order_has_no_as_extracted_value(built):
    con, _, _ = built
    (n,) = con.execute(
        "SELECT COUNT(*) FROM orders WHERE primary_topic_as_extracted IS NOT NULL"
    ).fetchone()
    assert n == 0


def test_a_stale_correction_fails_the_build(source, tmp_path):
    """If a re-sweep changes the label, the correction must not silently
    overwrite the fresh answer."""
    with pytest.raises(ValueError, match="stale"):
        analysis_db.build(
            source, tmp_path / "b.db", 1,
            corrections_path=_corrections_file(tmp_path, from_value="civil_rights"),
        )


def test_corrections_are_pinned_to_their_run(source, tmp_path):
    path = tmp_path / "c.db"
    analysis_db.build(source, path, 2, corrections_path=_corrections_file(tmp_path, run_id=1))
    con = sqlite3.connect(path)
    (topic,) = con.execute("SELECT primary_topic FROM orders WHERE eo_number = 101").fetchone()
    assert topic == "trade"  # run 2's own label, untouched
    (n,) = con.execute("SELECT COUNT(*) FROM orders WHERE primary_topic_as_extracted IS NOT NULL").fetchone()
    assert n == 0
