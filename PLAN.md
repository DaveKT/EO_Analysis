# Executive Order Analysis — Development Plan

**Status:** Phases 0-5 complete. The full corpus is extracted (run 11, 1,534
orders, $1.05). Seven of eight gates pass; the `other` rate gate fails at 7.3%
and that is a recorded finding, not an open defect. Notebook analysis and
`eo export` are next.
Written 2026-09-03; updated 2026-09-04 after the full sweep closed.
**Read this first if you are a fresh session picking up the work.**

---

## 0. Where the work actually stands (read before anything else)

### Done

| Phase | State | Evidence |
|---|---|---|
| 0 Reset | done | v1 in `archive/`, `my_secrets.py` deleted, package scaffolded |
| 1 Ingest | done | 1,556 documents, EO 12890 (1993-12-30) -> EO 14423 (2026-08-28) |
| 2 Contract | done | `models.py` + 3,924 FR relationship edges seeded |
| 3 Runner | done | `providers.py` + `extract.py`, resumable, bounded async |
| 4 Gates | done | all 8 gates pass on a 100-order sample (run 9) |
| 4b Frontier comparison | done | run 10, `openai/gpt-5.4`, 36 orders, $0.85 |
| 5 Full sweep | done | run 11, 1,534 orders, $1.05, ~2h50m, 7/8 gates |
| 5b Export | done | `eo export` (CSV) and `eo analysis-db` (SQLite) |
| 5c Notebook | deferred | see decision below; findings are in the README |
| 6 Optional | not started | taxonomy v8, pre-1994 backfill, `--since`, dashboard |

### The state in the database (`data/eo.db`, gitignored, rebuildable)

- `documents`: 1,556 rows. `extractable_documents` view: **1,534** -- one canonical
  row per EO number, excluding C1-/Z9- corrections and preferring R1- reprints.
- `relationships` with `run_id IS NULL`: 3,924 edges seeded from FR disposition notes.
- `extraction_runs`: 11 runs. **Run 11 is the shipped dataset** -- the full corpus.
  Run 9 (prompt v7, 100 orders) was the pilot and the sweep's baseline. Runs 1-8 are kept deliberately: they are the
  evidence for the prompt decisions below, and diffing them is the point of
  versioning by (model, prompt_version). **Run 10 is the frontier comparison**:
  `openai/gpt-5.4`, prompt v7, the 36-order comparison set.
- Run 11 holds all 1,534 orders: 3,195 agencies tasked, 2,240 deadlines, 1,707
  authorities, 1,847 model-found relationships, 866 review-queue items.

### Run 9 results, the baseline to beat

```
severe quote drift      1.7%   (<= 2%)     PASS
stored quotes verified  100%   (100%)      PASS
null rate               0.0%   (<= 2%)     PASS
truncation              0      (0)         PASS
other rate              3.0%   (<= 3%)     PASS
gold: primary_topic     80%    (>= 80%)    PASS
gold: instrument        85%    (>= 75%)    PASS
groundedness            94.8%  (reported)
```
100/100 extracted, 0 failures, $0.063, `openai/gpt-oss-120b`.

**Three of those pass at or within a point of their threshold.** The model is
nondeterministic; a re-run may not clear all eight. Do not present "all gates pass"
as a stable property of the pipeline.

### Run 10, the frontier comparison — what it settled

`openai/gpt-5.4`, prompt v7, the gold 20 plus run 9's 23 flagged documents (36
after overlap), $0.85. Reproduce with `eo compare --baseline 9 --candidate 10`.

| Over the same 36 documents | run 9 `gpt-oss-120b` | run 10 `gpt-5.4` |
|---|---|---|
| gold: primary_topic | 80% | **100%** |
| gold: instrument | 85% | 80% |
| groundedness | 88.6% | **99.8%** |
| severe drift | 3.7% | **0.0%** |
| quotes / agencies / authorities | 245 / 74 / 37 | 531 / 248 / 145 |

- **Topic agreement was the cheap model's ceiling, not the task's.** All four
  misses were the same failure: defaulting to `government_administration` or
  `foreign_policy` where a specific domain applied. The frontier model missed none.
- It extracts 2x the claims *and* is more grounded, so it is not scoring well by
  saying less. Relationships are the exception (46 -> 30).
- **The 88.6% baseline figure is not comparable to run 9's 94.8%**: this set is
  deliberately enriched with run 9's failures. Only the head-to-head columns are
  comparable. Any future comparison must be restricted to shared documents --
  `compare.py` enforces this, and its tests pin it.
- Cost of sweeping the full corpus on `gpt-5.4` would be ~$36 against ~$1.00.
  The cheap sweep remains the plan of record, with the gap recorded in the README.

### Run 11, the full sweep -- what shipped

1,534 orders, `openai/gpt-oss-120b`, prompt v7, $1.0464, 4.56M in + 2.59M out
tokens, ~2h50m at concurrency 8.

```
severe quote drift      1.8%   (<= 2%)     PASS
stored quotes verified  100%   (100%)      PASS
null rate               0.0%   (<= 2%)     PASS
truncation              0      (0)         PASS
other rate              7.3%   (<= 3%)     FAIL
gold: primary_topic     90%    (>= 80%)    PASS
gold: instrument        90%    (>= 75%)    PASS
groundedness            94.0%  (reported)
```

- **Zero truncations across all 1,534 orders**, including the 154,440-char
  maximum. The 8,000-token cap is adequate for the whole corpus.
- Three documents needed a second pass (two `TimeoutError`, one malformed
  response). `eo extract --run-id 11` recovered all three for $0.0007. Timeouts
  ran at ~226 retries over the run; none exhausted 4/4 except those two.
- **The gold gates went UP on the same model and prompt** (topic 80->90,
  instrument 85->90). Nondeterminism cuts both ways; this is why "all gates pass"
  is not claimed as a property.
- **The `other` gate failure is a finding, decomposed in the README.** ~40 of the
  112 rows had a correct existing category available and did not use it (15
  honours despite `confers_status_or_honor`, 9 agency-closure orders, succession
  despite `adjusts_pay_or_admin`, tariffs despite `imposes_sanctions`). Only ~16
  are a genuine gap (`continues_body`). **Adding categories is the wrong fix for
  the dominant cause** -- see the v6 lesson below. Topic axis is healthy at 1.8%;
  instrument carries 5.5%.
- **The 100-order pilot understated `other` at 3.0%.** A spread sample is
  representative for per-quote properties like grounding, but not for vocabulary
  coverage, because gaps cluster in order types a spread thins out. Do not size a
  taxonomy from a pilot.

**Decision 2026-09-04: ship run 11 and document the 7.3%** rather than spend
another $1.05 on a v8 re-sweep. The dataset is complete and every `other` row
carries its reason, so the gap is traceable rather than hidden.

### How to run it

```sh
PYTHONPATH=src .venv/bin/python -m eo.cli status
PYTHONPATH=src .venv/bin/python -m eo.cli extract --limit 100
PYTHONPATH=src .venv/bin/python -m eo.cli validate --run-id 9
PYTHONPATH=src .venv/bin/python -m eo.cli review --run-id 9
```

`PYTHONPATH=src` is required on this machine: files in the venv carry the macOS
`UF_HIDDEN` flag, CPython >= 3.11.4 skips hidden `.pth` files, and that silently
voids the editable install. `chflags nohidden` does not persist. See README.

The OpenRouter key is read from **`eo_openrouterkey` and no other name**.

### What to do next, in order

1. ~~Frontier-model comparison~~ **Done 2026-09-04, run 10.** See above. It
   answered the question it was for: the topic ceiling is the model's, not the
   task's, and the cheap sweep is still the right call at 1/36th the cost.
2. **Decide the three contested instrument labels** (below) before the sweep
   treats the gold set as ground truth. Costs nothing, needs a human.
3. ~~Phase 5 full sweep~~ **Done 2026-09-04, run 11.** See above.
4. ~~`eo export`~~ **Done 2026-09-04.** One run to flat files plus a provenance
   manifest, scoped to a single `run_id`.
5. ~~Notebook analysis~~ **Deferred 2026-09-04, by decision.** `jupyter`,
   `ipykernel`, `nbformat` and `matplotlib` are none of them installed, and an
   `.ipynb` written without executing it has unverified outputs -- the exact
   "looks finished, isn't" shape this project exists to prevent. The analyses the
   notebook was to contain were run directly against SQLite and their **verified**
   results are recorded in the README under "What the dataset shows". Anyone
   picking this up: install the four deps and execute the notebook, or do not ship
   one.
6. Optional Phase 6, in rough priority order:
   - **Normalise agency names** before any agency ranking is published --
     `Secretary of the Treasury` and `Department of the Treasury` are currently
     distinct entities.
   - **Improve deadline parsing** -- only 886 of 2,240 descriptions (40%) yield a
     duration, so median-deadline figures cover the parseable subset only.
   - Resolve the three contested gold `instrument` labels (EO 14081, 13489, 14287).
   - Instrument taxonomy v8: `continues_body` plus precedence clarity, *not* a pile
     of new categories.
   - Pre-1994 backfill from NARA disposition tables; `eo fetch --since`.

### Open risks a fresh session should not rediscover the hard way

- **The gold set is Claude's labels, not an expert's.** 20 orders read from source.
  80% agreement means the model matches *that* reading. Two labels were found wrong
  during Phase 4 (EO 13985, EO 14081), caught only because the precedence rule gave
  an objective test. Worth a human review before it anchors anything further.
- **Four separate times, a constraint lived only in the Pydantic validator where the
  model could not see it** -- `maxItems` on secondary topics, the repeated-topic rule,
  and the `other`-requires-a-reason rule on both axes. Each one silently destroyed
  otherwise-good extractions. Before adding any validation, ask whether the model is
  shown the rule; if JSON Schema cannot express it, normalise or flag, never reject.
- **Prompt length is not free.** v6 added two genuinely-missing categories *and*
  paragraphs explaining them, and regressed every metric (topic 80->75, instrument
  90->80, other 7->11). v7 kept the categories and deleted the prose: other fell to
  3%. Explaining what a category does *not* cover teaches the model to answer `other`.
- **`provider.require_parameters` filters in both directions.** It is what makes
  structured output a guarantee, but a parameter the model does not advertise is not
  ignored -- it eliminates every candidate endpoint and the request 404s in routing,
  before any model sees it. `openai/gpt-5.4` fixes its own sampling temperature and
  does not list `temperature`; sending `temperature=0` failed all 36 documents of the
  first run 10 attempt at zero cost. The runner now asks
  `client.supported_parameters(model)` once per run and omits what is unsupported.
  This will recur with any new model; it is not gpt-5.4-specific.
- **Provider hazards, all now handled but easy to reintroduce**: OpenRouter routes to
  providers that ignore `response_format` unless `provider.require_parameters` is set;
  httpx timeouts are per socket operation, so a trickling connection hangs forever
  without `asyncio.wait_for`; empty and malformed responses are transient and worth
  one retry.

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
| Model provider | Provider-agnostic via **OpenRouter** (`eo_openrouterkey`; the old `OPENROUTER_API_KEY` name is deliberately not read) |
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

### Phase 0 — Reset (no network, no cost) — **DONE**
- `git mv` v1 code into `archive/`: `EO_Analysis.ipynb`, `Initial_EO_Research/`, the loose CSVs.
  Keep them — they are the post-mortem evidence.
- Delete `my_secrets.py` and its `__pycache__` entry; **rotate the OpenAI key that was in it**
  (it never reached git history — verified — but it has been sitting in plaintext on disk).
- `config.py` reads `OPENROUTER_API_KEY` from env. Add `.env.example` with names only.
- `pyproject.toml` with pinned deps: `httpx`, `pydantic`, `tenacity`, `typer`, `pandas`,
  `python-dotenv`. Note `openai` SDK is only needed if you use it as the OpenRouter transport.
- Extend `.gitignore`: `data/`, `.env`.

**Exit:** `eo status` runs and reports an empty database.

### Phase 1 — Ingest, zero LLM — **DONE**
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

### Phase 2 — Extraction contract — **DONE**
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

### Phase 3 — Extraction runner — **DONE**
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

### Phase 4 — Validation gates — **DONE**
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

### Phase 5 — Full run + analysis — **NEXT**
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
- ~~Pick the sweep model at Phase 3~~ **Settled: `openai/gpt-oss-120b`**, which clears
  every gate at $0.063 per 100 orders (~$1.00 for the corpus). Revisit only if the
  frontier-model comparison shows the ceiling is the model rather than the task.
- ~~Confirm significance~~ **Dropped 2026-09-03.** The model rated 19 of 25 orders
  `major` and none `routine`, agreeing with hand labels 0% of the time. Alone among
  the fields it had no textual referent to check against.
- **Still open, now with evidence: three specific gold `instrument` labels need a
  human.** Run 10 showed instrument agreement did *not* improve on a far stronger
  model (85% -> 80%), and three orders are labelled differently by both models
  independently:
  - **EO 14081** — establishes a "Data for the Bioeconomy Initiative" and a national
    Initiative. Both models say `creates_body`; gold says `directs_report_or_study`.
    The convention's precedence rule never says whether a *program* counts as a
    *body*. This is an underspecified rule, not a misapplied one — fix the rule.
  - **EO 13489** (Presidential Records) — gold `delegates_authority`, run 9
    `revokes_or_amends`, run 10 `adjusts_pay_or_admin`. Three different readings.
  - **EO 14287** — gold `delegates_authority`, run 9 `imposes_sanctions`, run 10
    `other`.
  Two independent models agreeing against the label is evidence about the label,
  which is the one thing in this project nothing else checks.
- Decide whether proclamations and presidential memoranda eventually join the corpus. The
  schema supports it; scope currently says EOs only.
