"""Build a standalone, analysis-ready database from one extraction run.

`data/eo.db` is the working store: it holds every run, the model's raw responses,
and the machinery of the pipeline. This builds the other thing -- a single file
holding one run plus its source text, normalised and keyed so that everything
joins.

Four decisions shape the schema:

**Every claim table gets a surrogate primary key.** The working store has none:
rows are identified by (document_number, run_id) plus their content, which is not
unique -- an order can task the same agency twice. Without a key there is nothing
for a raw quote to point at.

**`raw_quote` moves to its own table.** It is present on only 540 of 8,989 claims
(the ones whose quote drifted and was trimmed), so inlining it puts a mostly-null
column on every row. `quote_trimmed` stays on the claim itself, so finding the
affected rows never needs a join -- only reading the original does.

**`secondary_topics` is exploded.** It is a JSON array in the working store,
which SQLite cannot join against. The raw JSON is kept on `orders` as well, so
nothing is lost.

**Relationship targets are resolved to a document_number where possible.** Only
~79% of target EO numbers exist in this corpus; the rest point to pre-1994 orders
outside the Federal Register's full-text coverage. `target_document_number` is
NULL for those, which makes an unresolvable target visibly unresolvable instead
of a join that silently drops rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from eo import agencies, corrections

SCHEMA = """
PRAGMA foreign_keys = ON;

-- Provenance. One row. A database with no record of which model produced it is
-- the failure this project exists to prevent.
CREATE TABLE run_metadata (
  run_id          INTEGER PRIMARY KEY,
  model           TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,
  started_at      TIMESTAMP,
  finished_at     TIMESTAMP,
  input_tokens    INTEGER,
  output_tokens   INTEGER,
  cost_usd        REAL,
  source_database TEXT,
  built_at        TIMESTAMP,
  coverage        TEXT,
  quote_contract  TEXT
);

-- One row per executive order: Federal Register ground truth and the model's
-- order-level fields side by side.
CREATE TABLE orders (
  document_number         TEXT PRIMARY KEY,
  eo_number               INTEGER NOT NULL UNIQUE,
  title                   TEXT,
  president               TEXT,
  signing_date            DATE,
  publication_date        DATE,
  citation                TEXT,
  pdf_url                 TEXT,
  raw_text_url            TEXT,
  disposition_notes       TEXT,
  fr_agencies_json        TEXT,
  body_char_count         INTEGER,
  summary                 TEXT,
  primary_topic           TEXT,
  -- The model's label where a hand correction replaced it (corrections/
  -- primary_topic.json); NULL everywhere else. The reason lives in that file.
  primary_topic_as_extracted TEXT,
  topic_other_reason      TEXT,
  secondary_topics_json   TEXT,
  instrument              TEXT,
  instrument_other_reason TEXT,
  finish_reason           TEXT
);

-- Source text, separated because it is 15 MB and almost never wanted in a
-- SELECT *. Joins to orders on document_number.
CREATE TABLE order_text (
  document_number TEXT PRIMARY KEY REFERENCES orders (document_number),
  body_text       TEXT NOT NULL,
  body_char_count INTEGER
);

CREATE TABLE order_secondary_topics (
  document_number TEXT NOT NULL REFERENCES orders (document_number),
  topic           TEXT NOT NULL,
  PRIMARY KEY (document_number, topic)
);

CREATE TABLE agencies_tasked (
  id              INTEGER PRIMARY KEY,
  document_number TEXT NOT NULL REFERENCES orders (document_number),
  agency_name     TEXT NOT NULL,
  task            TEXT,
  source_quote    TEXT NOT NULL,
  quote_trimmed   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE deadlines (
  id                INTEGER PRIMARY KEY,
  document_number   TEXT NOT NULL REFERENCES orders (document_number),
  due_description   TEXT,
  due_date          DATE,
  responsible_party TEXT,
  source_quote      TEXT NOT NULL,
  quote_trimmed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE authorities (
  id              INTEGER PRIMARY KEY,
  document_number TEXT NOT NULL REFERENCES orders (document_number),
  authority       TEXT NOT NULL,
  source_quote    TEXT NOT NULL,
  quote_trimmed   INTEGER NOT NULL DEFAULT 0
);

-- `source` separates the model's findings from the Federal Register's own
-- disposition notes, which are authoritative and were seeded before any model
-- ran. target_document_number is NULL when the target is outside this corpus.
--
-- The two sources carry different evidence, in deliberately different columns.
-- `source_quote` is always verbatim text from this order's body, verified to
-- appear in it -- so it is NULL on Federal Register rows, whose evidence is an
-- editorial disposition note ("Revokes: EO 12088, October 13, 1978 (in part)")
-- that is *about* the order and appears nowhere inside it. Storing both in one
-- column, as the working database does, makes any groundedness check over the
-- joined table read ~70% instead of 100%.
CREATE TABLE relationships (
  id                     INTEGER PRIMARY KEY,
  document_number        TEXT NOT NULL REFERENCES orders (document_number),
  relation               TEXT NOT NULL,
  target_eo_number       INTEGER,
  target_document_number TEXT REFERENCES orders (document_number),
  target_type            TEXT,
  target_label           TEXT,
  in_part                INTEGER DEFAULT 0,
  source                 TEXT NOT NULL,
  source_quote           TEXT,          -- model rows only; verbatim from body_text
  fr_disposition_note    TEXT,          -- Federal Register rows only
  quote_trimmed          INTEGER NOT NULL DEFAULT 0,
  -- The Federal Register is authoritative. Where FR and the model both speak to
  -- the same (order, target) pair, the FR row is the one to count and the model
  -- row is marked superseded -- whether the two agree or contradict. Counting
  -- both inflated the revocation network by 27%, because 123 revocation pairs
  -- are asserted by both. Nothing is deleted: the model's row stays, with
  -- superseded_by naming the FR row that outranks it.
  authoritative          INTEGER NOT NULL DEFAULT 1,
  superseded_by          INTEGER REFERENCES relationships (id),
  contradicts_fr         INTEGER NOT NULL DEFAULT 0
);

-- What the model originally wrote, where the stored quote had to be trimmed to
-- its verifiable span. (claim_table, claim_id) points at the row it belongs to.
CREATE TABLE raw_quotes (
  id            INTEGER PRIMARY KEY,
  claim_table   TEXT NOT NULL,
  claim_id      INTEGER NOT NULL,
  source_quote  TEXT NOT NULL,
  raw_quote     TEXT NOT NULL,
  UNIQUE (claim_table, claim_id)
);

-- Canonical agencies. `agency_name` on the claim stays exactly as extracted,
-- because it is what the source quote supports; this is the resolved identity
-- beside it. `matched` records whether the alias table recognised the name or
-- the cleaned surface form became its own entity -- so the mapping's coverage
-- is queryable rather than assumed.
CREATE TABLE agencies (
  agency_id      INTEGER PRIMARY KEY,
  canonical_name TEXT NOT NULL UNIQUE,
  kind           TEXT NOT NULL,   -- department|office|official|collective|body
  matched        INTEGER NOT NULL,
  note           TEXT
);

-- A claim can name several agencies ("Attorney General and Secretary of
-- Homeland Security"), so this is many-to-many rather than a column on the
-- claim. Counting taskings per agency means counting rows here.
CREATE TABLE agency_mentions (
  id          INTEGER PRIMARY KEY,
  claim_table TEXT NOT NULL,     -- 'agencies_tasked' | 'deadlines'
  claim_id    INTEGER NOT NULL,
  agency_id   INTEGER NOT NULL REFERENCES agencies (agency_id),
  raw_name    TEXT NOT NULL,
  UNIQUE (claim_table, claim_id, agency_id)
);

CREATE TABLE review_queue (
  id              INTEGER PRIMARY KEY,
  document_number TEXT NOT NULL REFERENCES orders (document_number),
  kind            TEXT NOT NULL,
  detail          TEXT,
  created_at      TIMESTAMP
);

CREATE INDEX idx_orders_president ON orders (president);
CREATE INDEX idx_orders_topic ON orders (primary_topic);
CREATE INDEX idx_orders_instrument ON orders (instrument);
CREATE INDEX idx_orders_signing ON orders (signing_date);
CREATE INDEX idx_agencies_doc ON agencies_tasked (document_number);
CREATE INDEX idx_agencies_name ON agencies_tasked (agency_name);
CREATE INDEX idx_deadlines_doc ON deadlines (document_number);
CREATE INDEX idx_authorities_doc ON authorities (document_number);
CREATE INDEX idx_rel_doc ON relationships (document_number);
CREATE INDEX idx_rel_target ON relationships (target_document_number);
CREATE INDEX idx_rel_relation ON relationships (relation);
CREATE INDEX idx_review_doc ON review_queue (document_number);
CREATE INDEX idx_mentions_agency ON agency_mentions (agency_id);
CREATE INDEX idx_mentions_claim ON agency_mentions (claim_table, claim_id);

-- Convenience views. The revocation network is the join most likely to be got
-- subtly wrong by hand: it needs both endpoints resolved to real orders.
CREATE VIEW revocation_network AS
SELECT r.relation,
       src.eo_number AS source_eo, src.president AS source_president,
       src.signing_date AS source_date,
       tgt.eo_number AS target_eo, tgt.president AS target_president,
       r.source
FROM relationships r
JOIN orders src ON src.document_number = r.document_number
JOIN orders tgt ON tgt.document_number = r.target_document_number
WHERE r.relation IN ('revokes', 'amends', 'supersedes', 'continues')
  AND r.authoritative = 1;

-- Taskings per canonical agency, already joined to the order. This is the view
-- to count with: doing it from agencies_tasked.agency_name splits the Treasury
-- across "Secretary of the Treasury" and "Department of the Treasury".
CREATE VIEW agency_taskings AS
SELECT ag.agency_id, ag.canonical_name, ag.kind, ag.matched,
       m.claim_table, m.claim_id, m.raw_name,
       o.document_number, o.eo_number, o.president, o.signing_date,
       o.primary_topic, o.instrument
FROM agency_mentions m
JOIN agencies ag USING (agency_id)
JOIN (
    SELECT 'agencies_tasked' AS claim_table, id, document_number FROM agencies_tasked
    UNION ALL
    SELECT 'deadlines', id, document_number FROM deadlines
) c ON c.claim_table = m.claim_table AND c.id = m.claim_id
JOIN orders o ON o.document_number = c.document_number;

-- Every claim in one shape, for counting or for finding an order's whole
-- evidence trail without four separate queries.
CREATE VIEW all_claims AS
  SELECT 'agencies_tasked' AS claim_table, id, document_number,
         agency_name AS claim, source_quote, quote_trimmed FROM agencies_tasked
  UNION ALL
  SELECT 'deadlines', id, document_number, due_description, source_quote,
         quote_trimmed FROM deadlines
  UNION ALL
  SELECT 'authorities', id, document_number, authority, source_quote,
         quote_trimmed FROM authorities
  UNION ALL
  SELECT 'relationships', id, document_number,
         relation || ' ' || COALESCE(target_label, ''), source_quote,
         quote_trimmed FROM relationships WHERE source_quote IS NOT NULL;
-- Federal Register rows are absent by construction: they have no source_quote,
-- because their evidence is a note about the order rather than text from it.
"""

CLAIM_TABLES = ("agencies_tasked", "deadlines", "authorities", "relationships")


def _rows(con: sqlite3.Connection, sql: str, params: dict) -> list[sqlite3.Row]:
    return con.execute(sql, params).fetchall()


def _apply_fr_precedence(out: sqlite3.Connection) -> dict[str, int]:
    """Let the Federal Register win wherever it has an opinion.

    FR disposition notes are the authoritative record of what an order does to
    earlier orders; the model's job is to *add* to them, not to restate or
    contest them. So for any (order, target) pair FR speaks to, the FR row is
    authoritative and the model's row is marked superseded -- including when the
    two agree, which is the common case and the one that quietly inflates
    counts. 123 revocation pairs are asserted by both sources; counting both
    overstated the revocation network by 27%.

    Model rows on pairs FR says nothing about stay authoritative: finding edges
    FR missed is the point of extracting them.

    Nothing is deleted. `superseded_by` names the winning FR row, and
    `contradicts_fr` marks the subset where the model asserted a *different*
    relation -- the disagreements worth a human's attention.
    """
    out.execute(
        """
        UPDATE relationships AS m
           SET authoritative = 0,
               superseded_by = (
                 SELECT fr.id FROM relationships fr
                  WHERE fr.source = 'fr_disposition_notes'
                    AND fr.document_number = m.document_number
                    AND fr.target_eo_number = m.target_eo_number
                  ORDER BY fr.id LIMIT 1
               )
         WHERE m.source = 'model'
           AND m.target_eo_number IS NOT NULL
           AND EXISTS (
                 SELECT 1 FROM relationships fr
                  WHERE fr.source = 'fr_disposition_notes'
                    AND fr.document_number = m.document_number
                    AND fr.target_eo_number = m.target_eo_number
               )
        """
    )
    out.execute(
        """
        UPDATE relationships AS m
           SET contradicts_fr = 1
         WHERE m.source = 'model' AND m.superseded_by IS NOT NULL
           AND m.relation <> (
                 SELECT fr.relation FROM relationships fr
                  WHERE fr.id = m.superseded_by
               )
        """
    )
    out.commit()
    return {
        "relationships superseded by FR": out.execute(
            "SELECT COUNT(*) FROM relationships WHERE authoritative = 0"
        ).fetchone()[0],
        "  of which contradict FR": out.execute(
            "SELECT COUNT(*) FROM relationships WHERE contradicts_fr = 1"
        ).fetchone()[0],
    }


def _resolve_agencies(out: sqlite3.Connection) -> dict[str, int]:
    """Populate `agencies` and `agency_mentions` from the names as extracted.

    Runs over both `agencies_tasked.agency_name` and
    `deadlines.responsible_party`: they carry the same names and the same
    Secretary/Department split, and normalising only one of them would leave the
    other quietly wrong.
    """
    sources = (
        ("agencies_tasked", "SELECT id, agency_name AS raw FROM agencies_tasked"),
        (
            "deadlines",
            (
                "SELECT id, responsible_party AS raw FROM deadlines"
                " WHERE responsible_party IS NOT NULL"
                " AND TRIM(responsible_party) <> ''"
            ),
        ),
    )
    ids: dict[str, int] = {}
    mentions: list[tuple[str, int, int, str]] = []
    rows: list[tuple[int, str, str, int, str | None]] = []

    for claim_table, sql in sources:
        for row in out.execute(sql).fetchall():
            names, matched = agencies.resolve(row["raw"])
            for name in names:
                if name not in ids:
                    ids[name] = len(ids) + 1
                    note = (
                        agencies.DEFENSE_NOTE
                        if name == "Department of Defense"
                        else None
                    )
                    rows.append(
                        (ids[name], name, agencies.kind_of(name), int(matched), note)
                    )
                mentions.append((claim_table, row["id"], ids[name], row["raw"]))

    out.executemany(
        "INSERT INTO agencies (agency_id, canonical_name, kind, matched, note)"
        " VALUES (?,?,?,?,?)",
        rows,
    )
    out.executemany(
        "INSERT OR IGNORE INTO agency_mentions (claim_table, claim_id, agency_id,"
        " raw_name) VALUES (?,?,?,?)",
        mentions,
    )
    return {
        "agencies": len(rows),
        "agency_mentions": out.execute(
            "SELECT COUNT(*) FROM agency_mentions"
        ).fetchone()[0],
    }


def build(
    source: sqlite3.Connection,
    target_path: Path,
    run_id: int,
    corrections_path: Path | None = None,
) -> dict[str, int]:
    """Write a fresh analysis database for `run_id`. Returns row counts.

    The target is replaced, not merged: a half-updated analysis database that
    still looks complete is worse than no database.
    """
    if target_path.exists():
        target_path.unlink()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    out = sqlite3.connect(target_path)
    out.row_factory = sqlite3.Row
    out.executescript(SCHEMA)
    counts: dict[str, int] = {}
    params = {"run_id": run_id}

    run = source.execute(
        "SELECT * FROM extraction_runs WHERE run_id = :run_id", params
    ).fetchone()
    if run is None:
        raise ValueError(f"no such run: {run_id}")
    fixes = corrections.load_for_run(
        corrections_path, run_id, run["model"], run["prompt_version"]
    )

    # --- orders + text + secondary topics ------------------------------------
    orders = _rows(
        source,
        """
        SELECT d.document_number, d.eo_number, d.title, d.president,
               d.signing_date, d.publication_date, d.citation, d.pdf_url,
               d.raw_text_url, d.disposition_notes, d.fr_agencies_json,
               d.body_char_count, d.body_text,
               e.summary, e.primary_topic, e.topic_other_reason,
               e.secondary_topics, e.instrument, e.instrument_other_reason,
               e.finish_reason
        FROM extractions e JOIN documents d USING (document_number)
        WHERE e.run_id = :run_id AND d.eo_number IS NOT NULL
        ORDER BY d.eo_number
        """,
        params,
    )
    fixed = corrections.verified(fixes, {r["eo_number"]: r["primary_topic"] for r in orders})

    def topic(r: sqlite3.Row) -> tuple[str, str | None]:
        fix = fixed.get(r["eo_number"])
        return (fix.to_value, r["primary_topic"]) if fix else (r["primary_topic"], None)

    out.executemany(
        "INSERT INTO orders (document_number, eo_number, title, president,"
        " signing_date, publication_date, citation, pdf_url, raw_text_url,"
        " disposition_notes, fr_agencies_json, body_char_count, summary,"
        " primary_topic, primary_topic_as_extracted, topic_other_reason,"
        " secondary_topics_json, instrument, instrument_other_reason, finish_reason)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r["document_number"], r["eo_number"], r["title"], r["president"],
                r["signing_date"], r["publication_date"], r["citation"],
                r["pdf_url"], r["raw_text_url"], r["disposition_notes"],
                r["fr_agencies_json"], r["body_char_count"], r["summary"],
                *topic(r), r["topic_other_reason"], r["secondary_topics"],
                r["instrument"], r["instrument_other_reason"], r["finish_reason"],
            )
            for r in orders
        ],
    )
    counts["orders"] = len(orders)
    counts["topic_corrections"] = len(fixed)

    out.executemany(
        "INSERT INTO order_text (document_number, body_text, body_char_count)"
        " VALUES (?,?,?)",
        [
            (r["document_number"], r["body_text"], r["body_char_count"])
            for r in orders
            if r["body_text"]
        ],
    )
    counts["order_text"] = out.execute("SELECT COUNT(*) FROM order_text").fetchone()[0]

    topics = []
    for r in orders:
        for topic in json.loads(r["secondary_topics"] or "[]"):
            topics.append((r["document_number"], topic))
    out.executemany(
        "INSERT OR IGNORE INTO order_secondary_topics (document_number, topic)"
        " VALUES (?,?)",
        topics,
    )
    counts["order_secondary_topics"] = out.execute(
        "SELECT COUNT(*) FROM order_secondary_topics"
    ).fetchone()[0]

    known = {r["document_number"] for r in orders}
    by_eo = {r["eo_number"]: r["document_number"] for r in orders}

    # --- claim tables --------------------------------------------------------
    raw_quotes: list[tuple[str, int, str, str]] = []

    def carry_raw(table: str, claim_id: int, row: sqlite3.Row) -> None:
        if row["raw_quote"]:
            raw_quotes.append((table, claim_id, row["source_quote"], row["raw_quote"]))

    specs = {
        "agencies_tasked": (
            (
                "SELECT * FROM agencies_tasked WHERE run_id = :run_id"
                " ORDER BY document_number, rowid"
            ),
            (
                "INSERT INTO agencies_tasked (id, document_number, agency_name,"
                " task, source_quote, quote_trimmed) VALUES (?,?,?,?,?,?)"
            ),
            lambda i, r: (
                i, r["document_number"], r["agency_name"], r["task"],
                r["source_quote"], r["quote_trimmed"] or 0,
            ),
        ),
        "deadlines": (
            (
                "SELECT * FROM deadlines WHERE run_id = :run_id"
                " ORDER BY document_number, rowid"
            ),
            (
                "INSERT INTO deadlines (id, document_number, due_description,"
                " due_date, responsible_party, source_quote, quote_trimmed)"
                " VALUES (?,?,?,?,?,?,?)"
            ),
            lambda i, r: (
                i, r["document_number"], r["due_description"], r["due_date"],
                r["responsible_party"], r["source_quote"], r["quote_trimmed"] or 0,
            ),
        ),
        "authorities": (
            (
                "SELECT * FROM authorities WHERE run_id = :run_id"
                " ORDER BY document_number, rowid"
            ),
            (
                "INSERT INTO authorities (id, document_number, authority,"
                " source_quote, quote_trimmed) VALUES (?,?,?,?,?)"
            ),
            lambda i, r: (
                i, r["document_number"], r["authority"], r["source_quote"],
                r["quote_trimmed"] or 0,
            ),
        ),
    }
    for table, (select, insert, shape) in specs.items():
        rows = [r for r in _rows(source, select, params) if r["document_number"] in known]
        payload = []
        for index, row in enumerate(rows, start=1):
            payload.append(shape(index, row))
            carry_raw(table, index, row)
        out.executemany(insert, payload)
        counts[table] = len(payload)

    # Relationships take the model's edges for this run *and* the Federal
    # Register's seeded edges, which belong to no run and are authoritative.
    rel_rows = [
        r
        for r in _rows(
            source,
            "SELECT * FROM relationships WHERE run_id = :run_id OR run_id IS NULL"
            " ORDER BY document_number, rowid",
            params,
        )
        if r["document_number"] in known
    ]
    payload = []
    for index, row in enumerate(rel_rows, start=1):
        from_model = row["source"] == "model"
        payload.append(
            (
                index, row["document_number"], row["relation"],
                row["target_eo_number"], by_eo.get(row["target_eo_number"]),
                row["target_type"], row["target_label"], row["in_part"] or 0,
                row["source"],
                row["source_quote"] if from_model else None,
                None if from_model else row["source_quote"],
                row["quote_trimmed"] or 0,
            )
        )
        if from_model:
            carry_raw("relationships", index, row)
    out.executemany(
        "INSERT INTO relationships (id, document_number, relation, target_eo_number,"
        " target_document_number, target_type, target_label, in_part, source,"
        " source_quote, fr_disposition_note, quote_trimmed)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        payload,
    )
    counts["relationships"] = len(payload)

    out.executemany(
        "INSERT INTO raw_quotes (claim_table, claim_id, source_quote, raw_quote)"
        " VALUES (?,?,?,?)",
        raw_quotes,
    )
    counts["raw_quotes"] = len(raw_quotes)

    review = [
        r
        for r in _rows(
            source,
            "SELECT * FROM review_queue WHERE run_id = :run_id ORDER BY rowid",
            params,
        )
        if r["document_number"] in known
    ]
    out.executemany(
        "INSERT INTO review_queue (id, document_number, kind, detail, created_at)"
        " VALUES (?,?,?,?,?)",
        [
            (i, r["document_number"], r["kind"], r["detail"], r["created_at"])
            for i, r in enumerate(review, start=1)
        ],
    )
    counts["review_queue"] = len(review)

    counts.update(_apply_fr_precedence(out))
    counts.update(_resolve_agencies(out))

    out.execute(
        "INSERT INTO run_metadata (run_id, model, prompt_version, started_at,"
        " finished_at, input_tokens, output_tokens, cost_usd, source_database,"
        " built_at, coverage, quote_contract) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run["run_id"], run["model"], run["prompt_version"], run["started_at"],
            run["finished_at"], run["input_tokens"], run["output_tokens"],
            run["cost_usd"], "data/eo.db",
            datetime.now(UTC).isoformat(timespec="seconds"),
            (
                "Executive Orders with full text in the Federal Register API,"
                " which begins ~1994. Orders below EO 12890 are not included."
            ),
            (
                "Every source_quote is verbatim text from that order's body_text,"
                " verified to appear in it. quote_trimmed=1 means the model's"
                " original drifted and was trimmed to its verifiable span;"
                " raw_quotes holds what it originally wrote. Federal Register"
                " relationship rows carry no source_quote: their evidence is"
                " fr_disposition_note, an editorial note about the order that"
                " does not appear inside it."
            ),
        ),
    )
    out.commit()

    # Foreign keys are declared, so ask SQLite whether they actually hold.
    violations = out.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        out.close()
        raise RuntimeError(f"foreign key violations in {target_path}: {violations[:5]}")
    out.execute("VACUUM")
    out.close()
    return counts
