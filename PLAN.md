# Executive Order Analysis — Development Plan

**Status:** approved plan, not yet implemented. Written 2026-09-03.
**Read this first if you are a fresh session picking up the work.**

---

## 1. Intent

Turn the full corpus of published Executive Orders into a **queryable structured dataset**,
where each EO carries machine-extracted fields (topics, agencies tasked, deadlines, legal
authorities, revocation/amendment relationships) that are traceable back to source text.

The v1 attempt (see `archive/`) aimed at "sentiment + summary" and failed. This plan replaces
that goal with structured extraction, which is *verifiable against the source document* —
the single most important change.

### Decisions locked in

| Question | Decision |
|---|---|
| Analysis output | Structured extraction (summary, topics, agencies, deadlines, authorities, EO relationships) |
| Corpus scope | Multi-administration history — all EOs available from the Federal Register API |
| Model provider | Provider-agnostic via **OpenRouter** (`OPENROUTER_API_KEY`, already in env) |
| Deliverable | Python CLI stages writing to **SQLite**; Jupyter reserved for analysis, not pipeline |

---

## 2. Corpus reality (verified 2026-09-03, not assumed)

Probes against `federalregister.gov/api/v1` returned:

- **1,559 executive orders** available via the API.
- Range: **EO 12890** (1993-12-30, Clinton) → **EO 14423** (2026-08-28, Trump).
  That is six presidencies: Clinton, G.W. Bush, Obama, Trump 45, Biden, Trump 47.
- Your existing CSV stops at **EO 14289 (May 2025)** — roughly **134 EOs are missing**, plus
  any revocations/amendments to EOs you already have.
- Full text is at `raw_text_url` per document and **fetches fine unauthenticated**.
- The API returns `disposition_notes` / `executive_order_notes` — FR's own cross-reference
  chain (e.g. `"See: Proc. 9704, March 8, 2018; ..."`). This is a **free, authoritative seed**
  for relationship extraction; do not make the model guess what FR already states.
- Useful fields confirmed present: `executive_order_number`, `signing_date`, `publication_date`,
  `president`, `title`, `citation`, `agencies[]`, `pdf_url`, `raw_text_url`, `full_text_xml_url`,
  `disposition_notes`, `start_page`/`end_page`.

### Two constraints that must shape the code

1. **HTML document pages are bot-blocked.** A plain `requests.get` on a
   `federalregister.gov/documents/...` page returns a `Request Access` interstitial **with
   HTTP 200**, so `raise_for_status()` does not catch it. This is what silently starved v1.
   → Use the JSON API and `raw_text_url` exclusively. Never scrape the HTML pages.
2. **`raw_text_url` returns text wrapped in `<html><head>...<body><pre>`.** It is not clean
   plain text. The fetcher must unwrap the `<pre>` block and strip the FR page-header
   boilerplate (`[Federal Register Volume 91, Number 170 ...]`) before storing.

### Coverage boundary (state this in the README)

The FR API's full-text coverage begins ~1994. EOs numbered below ~12890 (back to EO 7532 in
1937, and unnumbered orders before that) are **not** reachable this way. Options if you later
want them: the National Archives EO Disposition Tables, or the American Presidency Project.
**Out of scope for Phases 1–5.** Do not silently present the dataset as "all executive orders."

---

## 3. Target architecture

```
EO_Analysis/
├── PLAN.md                    ← this file
├── README.md                  ← what the dataset is, coverage boundary, how to run
├── pyproject.toml             ← deps + pinned versions (replaces bare .venv)
├── .env.example               ← names only, no values
├── src/eo/
│   ├── config.py              ← env-driven settings; NO my_secrets.py
│   ├── fr_client.py           ← Federal Register API + raw-text fetch/unwrap
│   ├── db.py                  ← SQLite connection, migrations
│   ├── schema.sql             ← tables below
│   ├── models.py              ← Pydantic extraction contract (the JSON schema)
│   ├── providers.py           ← OpenRouter client, model registry, cost accounting
│   ├── extract.py             ← the LLM pass, resumable
│   ├── validate.py            ← QA gates (§6)
│   └── cli.py                 ← `eo fetch | extract | validate | export | status`
├── notebooks/
│   └── exploration.ipynb      ← analysis only; imports from src/, no pipeline logic
├── data/
│   ├── eo.db                  ← SQLite (gitignored)
│   └── raw/{document_number}.txt   ← cached source text (gitignored)
├── tests/
│   ├── fixtures/              ← 3 saved API responses + raw texts
│   └── test_*.py
└── archive/                   ← v1 code, moved not deleted
```

### Why SQLite and not CSV

v1 lost everything on any interruption because `results` was only written after the whole
loop. SQLite gives per-record commits, `--resume` for free, and lets extractions be
**versioned by (model, prompt_version)** so a better model next year does not overwrite this
year's results — you can diff them instead.

---

## 4. Data model

Two-layer separation is the core design idea: **facts from the Federal Register are immutable
and deterministic; LLM output is versioned and disposable.**

```sql
-- Layer 1: ground truth from FR. Re-fetchable, never model-touched.
documents (
  document_number      TEXT PRIMARY KEY,   -- '2025-07835'
  eo_number            INTEGER,            -- 14289
  title                TEXT,
  president            TEXT,
  signing_date         DATE,
  publication_date     DATE,
  citation             TEXT,
  pdf_url              TEXT,
  raw_text_url         TEXT,
  disposition_notes    TEXT,
  fr_agencies_json     TEXT,               -- FR's own agency tagging
  body_text            TEXT,               -- unwrapped, cleaned
  body_char_count      INTEGER,
  fetched_at           TIMESTAMP
)

-- Layer 2: model output. Many rows per document, one per run.
extraction_runs (
  run_id           INTEGER PRIMARY KEY,
  model            TEXT,        -- 'openai/gpt-oss-120b'
  prompt_version   TEXT,        -- 'v1'
  started_at       TIMESTAMP,
  finished_at      TIMESTAMP,
  input_tokens     INTEGER,
  output_tokens    INTEGER,
  cost_usd         REAL,
  notes            TEXT
)

extractions (
  document_number  TEXT REFERENCES documents,
  run_id           INTEGER REFERENCES extraction_runs,
  summary          TEXT,
  primary_topic    TEXT,        -- controlled vocabulary, see models.py
  secondary_topics TEXT,        -- JSON array
  significance     TEXT,        -- 'routine' | 'substantive' | 'major'
  finish_reason    TEXT,        -- MUST be stored; 'length' means truncated → invalid
  raw_response     TEXT,        -- keep for debugging
  PRIMARY KEY (document_number, run_id)
)

agencies_tasked   (document_number, run_id, agency_name, task, source_quote)
deadlines         (document_number, run_id, due_description, due_date, responsible_party, source_quote)
authorities       (document_number, run_id, authority)      -- statutes/constitutional clauses invoked
relationships     (document_number, run_id, relation, target_eo_number, source, source_quote)
                   -- relation: 'revokes'|'amends'|'supersedes'|'continues'|'references'
                   -- source:   'fr_disposition_notes' | 'model'
```

**`source_quote` is mandatory on every extracted claim.** It is the anti-hallucination
mechanism: validation (§6) checks the quote actually appears in `body_text`. v1's headline
failure — a tariff EO summarized as a COVID Defense Production Act order — would have been
caught automatically by this one column.

---

## 5. Phases

Each phase ends in something runnable and checkable. Do not start a phase before its
predecessor's exit criteria pass.

### Phase 0 — Reset (no network, no cost)
- `git mv` v1 code into `archive/`: `EO_Analysis.ipynb`, `Initial_EO_Research/`, the loose CSVs.
  Keep them — they are the post-mortem evidence.
- Delete `my_secrets.py` and its `__pycache__` entry; **rotate the OpenAI key that was in it**
  (it never reached git history — verified — but it has been sitting in plaintext on disk).
- `config.py` reads `OPENROUTER_API_KEY` from env. Add `.env.example` with names only.
- `pyproject.toml` with pinned deps: `httpx`, `pydantic`, `tenacity`, `typer`, `pandas`,
  `python-dotenv`. Note `openai` SDK is only needed if you use it as the OpenRouter transport.
- Extend `.gitignore`: `data/`, `.env`.

**Exit:** `eo status` runs and reports an empty database.

### Phase 1 — Ingest, zero LLM
- Page the FR API for all `presidential_document_type=executive_order`, oldest→newest,
  ~50 pages at `per_page=100`. Use the URL-encoded bracket form
  (`conditions%5Btype%5D%5B%5D=PRESDOCU`) — the unencoded form failed in testing.
- For each: fetch `raw_text_url`, unwrap `<pre>`, strip FR header boilerplate, cache to
  `data/raw/`, insert into `documents`.
- Idempotent: skip anything already cached. Polite rate limiting + `tenacity` retry with
  backoff. Detect the `Request Access` interstitial explicitly and fail loudly on it.

**Exit:** `documents` holds ~1,559 rows; **zero** rows with `body_char_count < 500`; spot-check
5 EOs across different presidencies against the live site. This whole phase is free — get it
completely right before spending a cent on tokens.

### Phase 2 — Extraction contract
- `models.py`: Pydantic model → JSON schema, passed to the provider as a structured-output
  schema. **No regex line-parsing of prose.** v1 lost 47 of 143 rows to `**Sentiment:**`
  defeating `startswith("sentiment:")`.
- Fix the topic vocabulary as an `Enum` (~15–20 values: trade, immigration, national security,
  energy, civil service, health, education, technology/AI, federal land, …). A free-text topic
  field will produce 1,559 unique near-synonyms and be useless for aggregation.
- Seed `relationships` from `disposition_notes` with `source='fr_disposition_notes'` **before**
  the model runs, so the model's job is to *add* to a known-good base, not reconstruct it.
- Pin `prompt_version = 'v1'` and treat prompt text as versioned source.

**Exit:** schema validates against 3 hand-written fixture extractions; no API calls yet.

### Phase 3 — Extraction runner
- `providers.py`: OpenRouter is OpenAI-compatible — `base_url="https://openrouter.ai/api/v1"`.
  Model id is config, never hardcoded at a call site. Record per-call token usage and cost into
  `extraction_runs`.
- Concurrency: bounded async (start at 8) with retry/backoff. v1's 18-minute serial run was
  latency-bound, not rate-limited.
- **Hard preconditions before every call** (each maps to a v1 failure):
  - `body_text` present and ≥ 500 chars, else record `skipped`, never call the model.
    *(v1 sent empty strings to GPT-4 and got confident fiction back.)*
  - Generous `max_tokens`; on `finish_reason == 'length'`, mark the row invalid and retry once
    with a higher cap. *(v1's `max_tokens=200` truncated 29 summaries mid-sentence.)*
  - Full document text — no `text[:3000]`. Every candidate model has ≥128k context; the
    corpus averages ~3.5k tokens per EO. Truncation is now a self-inflicted wound.
- Write to SQLite per record. `--resume` skips `(document_number, run_id)` already present.
- `--limit N` and `--dry-run` for cheap iteration.

**Exit:** a 25-EO run across four presidencies completes with 0 nulls and a printed cost.

### Phase 4 — Validation gates
Run automatically at the end of every extraction run; the run **fails** if gates fail.

1. **Groundedness:** every `source_quote` must appear in `body_text` (normalized whitespace).
   Target ≥ 95%; investigate anything below.
2. **Null rate:** < 2% of rows with empty `summary` or `primary_topic`. v1 shipped 33% null.
3. **Truncation:** zero rows with `finish_reason = 'length'`.
4. **Relationship agreement:** where `disposition_notes` names a revoked EO, the model should
   not contradict it. Disagreements go to a review queue, not silently into the table.
5. **Gold set:** hand-label **20 EOs** (span all six presidencies, include one very short and
   one very long). Score each run against it. This is the only real defense against a model
   that is confidently wrong at scale — build it in Phase 4, not "later."

**Exit:** gates pass on a 100-EO sample.

### Phase 5 — Full run + analysis
- Full 1,559-EO extraction with the cheap model; gates must pass.
- Re-run the gold set + any low-confidence rows on a frontier model; compare, and record the
  disagreement rate in the README as an honest quality figure.
- `notebooks/exploration.ipynb`: EOs per president per year, topic mix over time, revocation
  network (who revokes whom across administrations), median deadline length, most-tasked
  agencies. Notebook reads from SQLite and **contains no pipeline code**.
- `eo export` → CSV/Parquet for anyone who wants flat files.

### Phase 6 — Optional, only if wanted
Pre-1994 backfill from NARA disposition tables; incremental `eo fetch --since`; a published
dashboard artifact.

---

## 6. Cost

Corpus ≈ 1,559 EOs × ~3.5k tokens ≈ **5–6M input tokens**, plus ~1.2M output.
Live OpenRouter pricing checked 2026-09-03:

| Model | In / Out per M | Full-corpus estimate |
|---|---|---|
| `openai/gpt-oss-120b` | $0.04 / $0.17 | **~$0.45** |
| `google/gemini-2.5-flash-lite:batch` | $0.05 / $0.20 | **~$0.55** |
| `openai/gpt-5.6-luna-pro:batch` | $0.10 / $0.60 | **~$1.30** |
| `anthropic/claude-opus-5` | $5.00 / $25.00 | **~$60** |

**Strategy:** sweep the full corpus with a cheap structured-output model (~$0.50), then spend
~$5 re-running the 20-EO gold set plus flagged rows on a frontier model to measure quality.
340 of the 424 OpenRouter models advertise `structured_outputs`; `:batch` variants are ~50% off
where latency does not matter. The v1 cost warning in the notebook is obsolete — at these
prices the full corpus is cheaper than v1's 143-EO GPT-4 run was.

---

## 7. Failure post-mortem → guardrail map

Carry this table into the README. Every v1 failure has exactly one structural fix.

| v1 failure | Evidence | Structural fix |
|---|---|---|
| Empty scrape → hallucinated content | EO 14289 (tariffs) summarized as a COVID/DPA order | Phase 1 exit gate + ≥500-char precondition + `source_quote` grounding check |
| Bot-block invisible to error handling | `Request Access` page returns HTTP 200 | JSON API only; explicit interstitial detection |
| Prose parsing broke on markdown | 47/143 rows blank from `**Sentiment:**` | Structured outputs against a JSON schema |
| `max_tokens=200` truncation | 29 summaries end mid-clause | Generous cap + `finish_reason` check |
| No resume; write-at-end | Any crash lost the whole run | SQLite per-record commit + `--resume` |
| Silent success on garbage | 0 rows said "Failed", 33% were empty | Validation gates fail the run |
| Unfalsifiable output | Same EO scored `Urgent` and `Assertive` in two runs | Extract facts with quotes, not vibes |
| Secret in a tracked-adjacent file | `my_secrets.py` plaintext on disk | Env vars + `.env.example` |

---

## 8. Open items for the implementing session

- ~~Confirm the topic controlled vocabulary before Phase 2~~ **Decided 2026-09-03: two axes.**
  `primary_topic` from 12 domains, plus a separate `instrument` from 6 values describing what
  the order *does* (creates_body, delegates_authority, imposes_sanctions, revokes_or_amends,
  directs_report_or_study, adjusts_pay_or_admin). Both accept `other` with a required free-text
  reason, and Phase 4 fails the run if `other` exceeds 3% on either axis, so vocabulary gaps
  surface instead of hiding.

  Chosen over a single generic policy list because the corpus does not look like a generic
  policy taxonomy: ~200 of 1,534 orders establish councils/commissions/task forces, 78 block
  property, 49 set agency succession, ~42 adjust federal pay. Domain alone files all of those
  under vague buckets; the second axis is what makes "how often does each president create a
  commission vs. impose sanctions" answerable.

  Implemented in schema.sql: `extractions` gained `instrument` and the two `*_other_reason`
  columns. `instrument` and `significance` are kept as separate fields.
- ~~The 12 domains have no `education` category~~ **Resolved 2026-09-03: `education` added as
  the 13th domain.** Found while hand-labelling the Phase 2 fixtures: 52 orders are
  education-related (the Educational Excellence series, Tribal Colleges, HBCUs, educational
  technology, the Space Academy) and had no home. Caught before any tokens were spent.
- ~~Decide whether `significance` survives alongside `instrument`~~ **Resolved 2026-09-03:
  both are kept as separate fields.**
- Pick the sweep model at Phase 3 (`gpt-oss-120b` is the default recommendation).
- Decide whether proclamations and presidential memoranda eventually join the corpus. The
  schema supports it; scope currently says EOs only.
