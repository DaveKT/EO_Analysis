-- Two layers, deliberately separated:
--   Layer 1 (documents) is ground truth from the Federal Register. Deterministic,
--   re-fetchable, never written by a model.
--   Layer 2 (everything keyed by run_id) is model output. Versioned by
--   (model, prompt_version) so a better model later adds a run rather than
--   overwriting this one -- the runs can then be diffed.

PRAGMA foreign_keys = ON;

-- Layer 1 --------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS documents (
  document_number   TEXT PRIMARY KEY,   -- '2025-07835'
  eo_number         INTEGER,            -- 14289
  title             TEXT,
  president         TEXT,
  signing_date      DATE,
  publication_date  DATE,
  citation          TEXT,
  pdf_url           TEXT,
  raw_text_url      TEXT,
  disposition_notes TEXT,
  fr_agencies_json  TEXT,               -- FR's own agency tagging, verbatim
  body_text         TEXT,               -- unwrapped from <pre>, boilerplate stripped
  body_char_count   INTEGER,
  fetched_at        TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_documents_eo_number ON documents (eo_number);
CREATE INDEX IF NOT EXISTS idx_documents_president ON documents (president);
CREATE INDEX IF NOT EXISTS idx_documents_signing_date ON documents (signing_date);

-- Layer 2 --------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS extraction_runs (
  run_id         INTEGER PRIMARY KEY,
  model          TEXT NOT NULL,         -- 'openai/gpt-oss-120b'
  prompt_version TEXT NOT NULL,         -- 'v1'
  started_at     TIMESTAMP,
  finished_at    TIMESTAMP,
  input_tokens   INTEGER DEFAULT 0,
  output_tokens  INTEGER DEFAULT 0,
  cost_usd       REAL DEFAULT 0.0,
  notes          TEXT
);

CREATE TABLE IF NOT EXISTS extractions (
  document_number  TEXT NOT NULL REFERENCES documents (document_number),
  run_id           INTEGER NOT NULL REFERENCES extraction_runs (run_id),
  summary          TEXT,
  primary_topic    TEXT,                -- controlled vocabulary; see models.py
  secondary_topics TEXT,                -- JSON array
  significance     TEXT,                -- 'routine' | 'substantive' | 'major'
  finish_reason    TEXT,                -- 'length' means truncated -> invalid
  raw_response     TEXT,                -- kept for debugging
  PRIMARY KEY (document_number, run_id)
);

-- Every extracted claim below carries source_quote: the span of body_text it
-- came from. Phase 4 checks the quote actually appears there. This is the
-- anti-hallucination mechanism, not documentation.

CREATE TABLE IF NOT EXISTS agencies_tasked (
  document_number TEXT NOT NULL REFERENCES documents (document_number),
  run_id          INTEGER NOT NULL REFERENCES extraction_runs (run_id),
  agency_name     TEXT NOT NULL,
  task            TEXT,
  source_quote    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deadlines (
  document_number   TEXT NOT NULL REFERENCES documents (document_number),
  run_id            INTEGER NOT NULL REFERENCES extraction_runs (run_id),
  due_description   TEXT,
  due_date          DATE,
  responsible_party TEXT,
  source_quote      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS authorities (
  document_number TEXT NOT NULL REFERENCES documents (document_number),
  run_id          INTEGER NOT NULL REFERENCES extraction_runs (run_id),
  authority       TEXT NOT NULL,        -- statute or constitutional clause invoked
  source_quote    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relationships (
  document_number  TEXT NOT NULL REFERENCES documents (document_number),
  run_id           INTEGER REFERENCES extraction_runs (run_id),
  relation         TEXT NOT NULL,       -- revokes|amends|supersedes|continues|references
  target_eo_number INTEGER,
  source           TEXT NOT NULL,       -- 'fr_disposition_notes' | 'model'
  source_quote     TEXT
);

CREATE INDEX IF NOT EXISTS idx_agencies_tasked_doc ON agencies_tasked (document_number, run_id);
CREATE INDEX IF NOT EXISTS idx_deadlines_doc ON deadlines (document_number, run_id);
CREATE INDEX IF NOT EXISTS idx_authorities_doc ON authorities (document_number, run_id);
CREATE INDEX IF NOT EXISTS idx_relationships_doc ON relationships (document_number, run_id);
CREATE INDEX IF NOT EXISTS idx_relationships_target ON relationships (target_eo_number);

-- The set of documents Phase 3 actually extracts from.
--
-- The Federal Register's "executive_order" filter returns more than executive
-- orders, and it publishes some orders more than once:
--   * C1-/Z9- prefixes are short "change X to Y" correction notices.
--   * R1- prefixes are reprints, which supersede the original publication.
--   * Some documents (annexes, notices, military orders, CFIUS orders) get no
--     EO number from FR at all.
-- Ground truth in `documents` is left intact; this view picks one canonical row
-- per EO number.
CREATE VIEW IF NOT EXISTS extractable_documents AS
SELECT d.*
FROM documents d
WHERE d.eo_number IS NOT NULL
  AND d.document_number NOT LIKE 'C1-%'
  AND d.document_number NOT LIKE 'Z9-%'
  AND d.document_number = (
    SELECT d2.document_number
    FROM documents d2
    WHERE d2.eo_number = d.eo_number
      AND d2.document_number NOT LIKE 'C1-%'
      AND d2.document_number NOT LIKE 'Z9-%'
    ORDER BY d2.publication_date DESC, d2.body_char_count DESC
    LIMIT 1
  );
