"""Tests for the flat-file export.

The failure mode worth guarding is quiet contamination: extractions are
versioned by (model, prompt_version) and eleven runs share the tables, so an
export that forgets to scope by run_id produces a plausible-looking file that
interleaves runs of different quality. That is v1's defining shape -- output
that looks finished and is not -- so it is pinned here.
"""

from __future__ import annotations

import csv
import json
import sqlite3

import pytest

from eo import db, export

BODY = (
    "By the authority vested in me as President, the Secretary of Commerce\n"
    "shall submit a report within 30 days."
)


@pytest.fixture
def con() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.migrate(connection)
    for eo_number in (100, 101):
        connection.execute(
            "INSERT INTO documents (document_number, eo_number, title, president,"
            " signing_date, body_text, body_char_count)"
            " VALUES (?, ?, ?, 'P', '2020-01-01', ?, ?)",
            (f"doc-{eo_number}", eo_number, f"Order {eo_number}", BODY, len(BODY)),
        )
    for run_id, model in ((1, "cheap/model"), (2, "frontier/model")):
        connection.execute(
            "INSERT INTO extraction_runs (run_id, model, prompt_version, cost_usd)"
            " VALUES (?, ?, 'v7', 0.5)",
            (run_id, model),
        )
        connection.execute(
            "INSERT INTO extractions (document_number, run_id, summary,"
            " primary_topic, instrument, finish_reason, raw_response)"
            " VALUES ('doc-100', ?, ?, 'health', 'creates_body', 'stop', '{\"a\":1}')",
            (run_id, f"summary from run {run_id}"),
        )
        connection.execute(
            "INSERT INTO agencies_tasked (document_number, run_id, agency_name,"
            " task, source_quote) VALUES ('doc-100', ?, 'Commerce', 't', 'q')",
            (run_id,),
        )
    # A Federal Register edge, belonging to no run.
    connection.execute(
        "INSERT INTO relationships (document_number, run_id, relation,"
        " target_eo_number, source) VALUES ('doc-100', NULL, 'revokes', 99,"
        " 'fr_disposition_notes')"
    )
    connection.execute(
        "INSERT INTO relationships (document_number, run_id, relation,"
        " target_eo_number, source) VALUES ('doc-100', 1, 'amends', 98, 'model')"
    )
    connection.commit()
    yield connection
    connection.close()


def read_csv(path):
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_export_is_scoped_to_one_run(con, tmp_path):
    counts = export.export_run(con, 1, tmp_path)

    assert counts["orders"] == 1
    rows = read_csv(tmp_path / "orders.csv")
    assert [r["summary"] for r in rows] == ["summary from run 1"]
    assert counts["agencies_tasked"] == 1


def test_bulk_columns_are_omitted_by_default(con, tmp_path):
    export.export_run(con, 1, tmp_path)
    columns = read_csv(tmp_path / "orders.csv")[0].keys()

    assert "body_text" not in columns
    assert "raw_response" not in columns
    assert "summary" in columns and "eo_number" in columns


def test_include_text_restores_them(con, tmp_path):
    export.export_run(con, 1, tmp_path, include_text=True)
    row = read_csv(tmp_path / "orders.csv")[0]

    assert row["body_text"] == BODY
    assert row["raw_response"] == '{"a":1}'


def test_relationships_keep_the_federal_register_edges(con, tmp_path):
    """FR's seeded edges belong to no run; dropping them would export a weaker
    graph than the project has."""
    export.export_run(con, 1, tmp_path)
    rows = read_csv(tmp_path / "relationships.csv")

    sources = sorted(r["source"] for r in rows)
    assert sources == ["fr_disposition_notes", "model"]


def test_relationships_exclude_another_runs_model_edges(con, tmp_path):
    con.execute(
        "INSERT INTO relationships (document_number, run_id, relation,"
        " target_eo_number, source) VALUES ('doc-100', 2, 'supersedes', 97, 'model')"
    )
    con.commit()
    export.export_run(con, 1, tmp_path)
    rows = read_csv(tmp_path / "relationships.csv")

    assert "supersedes" not in [r["relation"] for r in rows]


def test_manifest_records_provenance(con, tmp_path):
    counts = export.export_run(con, 2, tmp_path)
    payload = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))

    assert payload["run_id"] == 2
    assert payload["model"] == "frontier/model"
    assert payload["prompt_version"] == "v7"
    assert payload["row_counts"] == counts
    assert "1994" in payload["coverage"]


def test_orders_without_extractions_are_not_exported(con, tmp_path):
    """EO 101 was never extracted; it must not appear as a blank row."""
    export.export_run(con, 1, tmp_path)
    rows = read_csv(tmp_path / "orders.csv")

    assert [r["eo_number"] for r in rows] == ["100"]
