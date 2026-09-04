# Executive Order Analysis

A queryable structured dataset of U.S. Executive Orders. Each order carries
machine-extracted fields — topics, agencies tasked, deadlines, legal authorities,
and revocation/amendment relationships — every one of which is traceable back to
a quoted span of the source document.

The development plan is [PLAN.md](PLAN.md). The v1 attempt is preserved in
[archive/](archive/) as post-mortem evidence, not as working code.

**Start here depending on what you want:**

| | |
|---|---|
| Start querying | **[INVESTIGATORS_CHEAT_SHEET.md](INVESTIGATORS_CHEAT_SHEET.md)** — one page of the traps that will bite you. *Read this first.* |
| Do analysis | [DATA_DICTIONARY.md](DATA_DICTIONARY.md) — schema, ERD, worked joins for `data/analysis.db` |
| Know what to trust | [DATA_QUALITY.md](DATA_QUALITY.md) — every control, finding and caveat |
| Understand the build | [PLAN.md](PLAN.md) — phase status, decisions, what is still open |

**The data is published in this repository.** `data/analysis.db` (23 MB) and
`data/export/` (4 MB of CSV) are committed — everything in them derives from the
Federal Register, which is public domain. The working store (`data/eo.db`, every
run and every raw model response) and the raw-text cache stay local; both are
rebuildable.

## Coverage boundary

**This dataset is not "all executive orders."** The Federal Register API's
full-text coverage begins in **1994**. The corpus therefore runs from
**EO 12890** (signed 1993-12-30, published 1994-01-05) to the present, spanning
six presidencies: Clinton, G.W. Bush, Obama, Trump 45, Biden, Trump 47.

Orders numbered below ~12890 — back to EO 7532 in 1937, plus the unnumbered
orders that precede those — are **not reachable this way** and are not included.
Reaching them would mean a different source: the National Archives EO Disposition
Tables, or the American Presidency Project.

A small number of documents returned by the API's `executive_order` filter are
not themselves executive orders — annexes published separately, and the odd
presidential notice. The Federal Register assigns them no EO number, they are
stored with `eo_number IS NULL`, and they are excluded from extraction.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env      # then add your OpenRouter key
```

Configuration is read from the environment (or `.env`). The OpenRouter key is
read from **`eo_openrouterkey` and nothing else** — there is deliberately no
fallback to other variable names, because a stale key under a different name
would otherwise be picked up silently. There is no `my_secrets.py`.

### If `import eo` fails after install

On macOS, files in this venv can carry the `UF_HIDDEN` flag, and CPython ≥3.11.4
deliberately skips hidden `.pth` files — which silently voids the editable
install's path injection. `chflags nohidden` on the `.pth` fixes it; if the flag
returns, run commands as `PYTHONPATH=src .venv/bin/python -m eo.cli ...`.

## Usage

```sh
eo status     # configuration, row counts, ingest health
eo fetch      # Phase 1: ingest from the Federal Register (free, no model calls)
eo extract    # Phase 3: the LLM pass
eo validate   # Phase 4: quality gates
eo review     # Phase 4: what the gates parked for human review
eo compare    # two runs, head to head over the documents both cover
eo analysis-db # one run + its source text as a standalone, joinable database
eo export     # one run to flat files, with a provenance manifest
```

### The analysis database

`eo analysis-db --run-id 11` builds **`data/analysis.db`** (23 MB): one run, its
source text, and nothing else — no prior runs, no raw model responses, no
pipeline machinery. Foreign keys are declared and checked at build time, so
everything joins.

```
run_metadata             1  which model, prompt, cost, coverage — read this first
orders               1,534  PK document_number, UNIQUE eo_number
order_text           1,534  body_text, split out because it is 15 MB
order_secondary_topics 313  the JSON array exploded into joinable rows
agencies_tasked      3,195  id PK -> orders
deadlines            2,240  id PK -> orders
authorities          1,707  id PK -> orders
relationships        5,765  id PK -> orders, target_document_number -> orders
raw_quotes             540  (claim_table, claim_id) -> the claim it belongs to
review_queue           866  id PK -> orders
agencies               558  canonical agencies
agency_mentions      5,863  (claim_table, claim_id) -> agencies, many-to-many
```

Three views come with it: `revocation_network` (both endpoints resolved to real
orders, and deduplicated — see Federal Register precedence below),
`agency_taskings` (canonical agencies joined to their orders) and `all_claims`
(every quoted claim in one shape).

### Federal Register precedence

The Federal Register's disposition notes are the authoritative record of what an
order does to earlier orders; the model's job is to *add* to them. So wherever FR
has an opinion about an (order, target) pair, **the FR row is authoritative and
the model's row is marked `authoritative = 0`** — including when the two agree,
which is the common case. 917 model edges are superseded this way, 174 of them
because the model asserted a *different* relation (`contradicts_fr = 1`).

This matters for counting: 123 revocation pairs are asserted by both sources, and
counting both inflated the revocation network by 27%. `revocation_network`
filters to `authoritative = 1`. Model edges on pairs FR says nothing about are
kept (930 of them) — finding those is the point of extracting relationships.
Nothing is deleted; `superseded_by` names the FR row that outranks each one.

```sql
-- who revokes whom, across administrations
SELECT source_president, target_president, COUNT(*)
FROM revocation_network WHERE relation = 'revokes' GROUP BY 1, 2 ORDER BY 3 DESC;

-- an order's whole evidence trail, including what the model originally wrote
SELECT c.claim_table, c.claim, c.source_quote, q.raw_quote
FROM all_claims c
LEFT JOIN raw_quotes q ON q.claim_table = c.claim_table AND q.claim_id = c.id
WHERE c.document_number = (SELECT document_number FROM orders WHERE eo_number = 14081);
```

Three things to know before writing queries against it:

- **`source_quote` means exactly one thing: verbatim text from that order's
  `body_text`.** Federal Register relationship rows carry no `source_quote` —
  their evidence is an editorial disposition note ("Revokes: EO 12088…") that is
  *about* the order and appears nowhere inside it, so it lives in
  `fr_disposition_note` instead. The working database stores both in one column,
  which makes a groundedness check over the joined table read ~70% instead of
  100%. Splitting them is why this database can verify its own central claim:
  joining `all_claims` to `order_text` gives **8,989/8,989 = 100%**.
- **`target_document_number` is NULL for ~21% of relationship targets.** Those
  point at pre-1994 orders outside the Federal Register's full-text coverage.
  `target_eo_number` is still populated, so an unresolvable target is visibly
  unresolvable rather than a join that silently drops rows.
- **Count agencies through `agency_taskings`, not `agencies_tasked.agency_name`.**
  See below.

### Agency normalisation

Full rules and the judgment calls behind them: [DATA_QUALITY.md](DATA_QUALITY.md) §6.4.

The extraction records agencies as each order names them — which is correct, since
the `source_quote` has to match the text — but that left 1,146 distinct names over
3,195 taskings, with `Secretary of the Treasury` (91) and `Department of the
Treasury` (66) as separate entities. `agencies` + `agency_mentions` resolve them to
**558 canonical entities**, covering **83% of mentions** by the alias table.

```sql
SELECT canonical_name, COUNT(*) AS taskings, COUNT(DISTINCT document_number) AS orders
FROM agency_taskings WHERE kind <> 'collective'
GROUP BY agency_id ORDER BY taskings DESC LIMIT 10;
```

| Agency | Taskings | Orders |
|---|---|---|
| Department of Homeland Security | 357 | 115 |
| Department of Health and Human Services | 295 | 96 |
| Department of Justice | 293 | 119 |
| Department of Commerce | 267 | 101 |
| Department of the Treasury | 267 | 144 |
| Department of Defense | 259 | 103 |
| Department of State | 247 | 115 |
| Office of Management and Budget | 196 | 104 |

Four rules govern the mapping, each because the obvious approach is wrong:

- **`agency_name` is never overwritten.** It stays exactly as extracted, because
  it is what the source quote supports. Canonical names sit beside it, so a
  disagreement with this mapping is visible and fixable without re-extracting.
- **Resolution is alias lookup, not splitting on "and".** A row can name several
  agencies (`Attorney General and Secretary of Homeland Security` → both), but
  splitting on conjunctions would destroy `Health and Human Services`. Names are
  scanned longest-alias-first instead. `agency_mentions` is many-to-many, so a
  compound row credits every agency it names.
- **An unrecognised name becomes its own entity, never a bucket.** Most of the
  long tail is real — one-off commissions, task forces, boards — and should stay
  countable. `agencies.matched` records whether the alias table recognised a name,
  so coverage is queryable rather than assumed.
- **A qualifier is not noise.** `each federal agency` resolves to the collective;
  `all contracting agencies` does not, because it names a subset and merging it
  would claim the whole executive branch was tasked.
- **Bare references are flagged, not merged.** "Task Force", "the Commission" and
  "Parties to the Dispute" each refer to a body created inside their own order, so
  two orders' "Task Force" are different task forces. They carry
  `kind = 'generic'` (44 entities, 262 mentions) so you can exclude them in one
  predicate; collapsing them would invent a body spanning unrelated orders.

`Secretary of X` and `Department of X` are merged — one institution for the
purpose of counting. **`Department of War` resolves to `Department of Defense`**
for the same reason: EO 14347 (2025-09-05) renamed the department, and it is one
institution across the rename.

Nothing is lost by that merge. The name each order actually used is preserved on
every mention, so the Department of War period stays queryable:

```sql
SELECT raw_name, COUNT(*), MIN(signing_date), MAX(signing_date)
FROM agency_taskings
WHERE canonical_name = 'Department of Defense' AND raw_name LIKE '%War%'
GROUP BY raw_name;
-- 54 taskings, 2025-09-05 → 2026-07-20
```

The rename is recorded in `agencies.note` on the Department of Defense row.

`deadlines.responsible_party` is normalised by the same rules — it carries the
same names and the same split, and doing only one table would leave the other
quietly wrong.

### Flat files

`eo export --run-id 11` writes `orders`, `agencies_tasked`, `deadlines`,
`authorities`, `relationships` and `review_queue` to `data/export/`, plus a
`manifest.json` recording the run, model, prompt version, token counts, cost and
coverage boundary. Export is scoped to **one run**: extractions are versioned by
(model, prompt_version) and eleven runs share the tables, so an unscoped dump
would interleave runs of different quality. `body_text` and `raw_response` are
omitted unless you pass `--include-text`. `--format parquet` needs `pyarrow`,
which is not a pinned dependency.

Comparing two models is a first-class operation, not a one-off script, because
a single run's gate report cannot distinguish the model's ceiling from the
task's:

```sh
eo extract --compare-run 9 --model openai/gpt-5.4   # gold set + what run 9 flagged
eo compare --baseline 9 --candidate 10
```

`eo fetch` is idempotent and resumable at two levels: documents already stored
are skipped, and bodies already in `data/raw/` are reused without a network
round trip. An interrupted run is resumed by running the command again. It also
seeds `relationships` from the Federal Register's own disposition notes — about
3,900 edges across 1,111 orders — so extraction adds to a known-good base
instead of reconstructing what FR already states.

## The extraction contract

Each order is described on two axes: `primary_topic` (14 domains — what the
order is about) and `instrument` (7 kinds — what it does).

`instrument` is decided by an explicit **precedence rule** rather than by
judgment about emphasis, because "the action the order is mostly devoted to"
is not reproducible: two careful readers split on EO 13985, which both
establishes a working group and directs government-wide equity assessments.
The rule takes the first that matches — creates a body, imposes sanctions,
revokes or amends, confers status or honour, delegates authority, directs a
report, adjusts pay or administration — and is applied to what the text says,
not to what seems most important. The order of those seven steps is the rule;
the prompt in [prompts.py](src/eo/prompts.py) is authoritative.

A `significance` field was **removed**. Asked to rate 25 orders, the model
returned 19 `major`, 6 `substantive` and no `routine` — including a
one-sentence order raising a council's membership from 25 to 30 — and agreed
with hand labels 0% of the time. Alone among the fields it had no textual
referent, so it could not be checked against the source. The second axis
exists because this corpus is not shaped like a generic policy taxonomy:
roughly 200 of 1,534 orders establish councils or task forces, 78 block
property, and 49 set agency succession.

Both axes admit `other`, which requires a written reason, and Phase 4 fails any
run where `other` exceeds 3%. A forced choice would make a vocabulary gap
indistinguishable from a good fit — the class of silent failure this rebuild
exists to prevent.

## Extraction runs

```sh
eo extract --limit 25 --dry-run     # what would be sent, and how big. No cost.
eo extract --limit 25               # pilot: samples evenly across presidencies
eo extract --run-id 1 --limit 25    # resume: retries only what failed
```

A run is identified by `(model, prompt_version)` and records its own token
counts and cost. Documents are committed as each completes, so an interrupted
run keeps everything finished so far. Resuming re-offers rows that came back
truncated or unparsable — a failed row is work still to do, not work done —
and samples before filtering, so `--limit 25 --run-id N` means the same 25
orders every time.

## Design

Two layers, deliberately separated:

- **`documents`** is ground truth from the Federal Register — deterministic,
  re-fetchable, never written by a model.
- **Everything keyed by `run_id`** is model output, versioned by
  `(model, prompt_version)`. A better model later adds a run rather than
  overwriting this one, so runs can be diffed.

`source_quote` is mandatory on every extracted claim. Validation checks the
quote actually appears in `body_text`. This is the anti-hallucination mechanism,
not documentation.

Models sometimes quote a span correctly and then drift in its final words. Such
a quote is **trimmed to the part that is verifiably in the document**, the
model's original is kept in `raw_quote`, and `quote_trimmed` marks the row. The
groundedness gate scores `raw_quote` — what the model actually wrote — so the
metric measures the model and not the repair. A separate invariant asserts that
every stored quote appears in its source, which is 100% by construction; a
failure there means the write path is broken.

## Quality gates

Gates run at the end of every extraction, and a run that fails them exits
non-zero.

| Gate | Target | 100-order result |
|---|---|---|
| severe quote drift | ≤ 2% | 1.7% |
| stored quotes verified | 100% | 100% |
| null rate | ≤ 2% | 0.0% |
| truncation | 0 | 0 |
| `other` rate, either axis | ≤ 3% | 3.0% |
| gold: primary_topic | ≥ 80% | 80% |
| gold: instrument | ≥ 75% | 85% |
| groundedness (as the model wrote it) | reported | 94.8% |
| relationship agreement vs FR | advisory, to review queue | 2 flagged |

Measured on 100 orders spanning five presidencies, `openai/gpt-oss-120b` with
prompt v7, at $0.063. **Three gates pass at or within a point of their
threshold**, so a re-run may not clear all eight; these are thresholds the
process meets, not margins it enjoys.

Groundedness is split by severity because a quote that drifts in its last three
words is not the same defect as one that is largely invented, and a single
threshold scored them identically. The hard gate is on severe drift; overall
fidelity is reported, and every stored quote is verified regardless.

The gold set is [gold/gold_set.json](gold/gold_set.json): 20 orders hand-read
from source, spanning all six presidencies, including the shortest order in the
corpus and the longest in the sample. It is the only gate that can catch a model
that is fluent and confidently wrong about what an order *is*; groundedness
proves quotes are real, not that the reading is right.

## How good is the cheap model, really

A single run's gate report cannot tell whether 80% topic agreement is the
*model's* ceiling or the *task's*. Answering that takes a second run, so 36
orders — the 20 gold labels plus the 23 documents run 9 flagged for review, less
overlap — were re-extracted on `openai/gpt-5.4` under the identical prompt (v7).
`eo compare --baseline 9 --candidate 10` reports it.

| Measured over the same 36 documents | `gpt-oss-120b` | `gpt-5.4` |
|---|---|---|
| gold: primary_topic | 80% (16/20) | **100% (20/20)** |
| gold: instrument | 85% (17/20) | 80% (16/20) |
| groundedness, as the model wrote it | 88.6% | **99.8%** |
| severe quote drift | 3.7% | **0.0%** |
| quotes extracted | 245 | 531 |
| agencies tasked / authorities | 74 / 37 | 248 / 145 |
| cost | $0.02 | $0.85 |

**Topic agreement was the cheap model's ceiling, not the task's.** All four of
its topic misses were the same failure — defaulting to `government_administration`
or `foreign_policy` where a specific domain applied (EO 13985 civil rights,
EO 14105 economy, EO 13115 and EO 14167 security). The frontier model missed none.

It also extracts **more than twice as many claims while being more grounded**,
which rules out the obvious confound: it is not scoring well by saying less.
The one place it does less is relationships (46 → 30).

Two honest caveats:

- The 88.6% baseline groundedness here is **not** the 94.8% quoted above. This
  36-document set is deliberately enriched with run 9's own failures, so no rate
  measured over it is comparable to a rate over a random sample. Only the
  head-to-head columns are comparable, because both runs cover the same documents.
- **Instrument agreement did not improve, and that points at the labels.** Three
  orders are labelled differently by *both* models, independently: EO 13489,
  EO 14081, EO 14287. Two independent models reading the same text the same way
  is evidence about the label, which nothing else checks. EO 14081 is the clear
  case — it establishes a "Data for the Bioeconomy Initiative" and a national
  Initiative, both models call that `creates_body`, and the gold convention's
  precedence rule never says whether a *program* counts as a *body*. The rule is
  underspecified, not merely misapplied.

The cheap sweep remains the plan of record for the full corpus (~$1.00 against
~$36 for `gpt-5.4`), with the topic-agreement gap recorded here rather than
smoothed over.

## The dataset (run 11, the full sweep)

**1,534 orders, EO 12890 (1993-12-30) through EO 14423 (2026-08-28), $1.05.**
`openai/gpt-oss-120b`, prompt v7, 4.56M input + 2.59M output tokens, ~2h50m.

| Table | Rows |
|---|---|
| `extractions` | 1,534 |
| `agencies_tasked` | 3,195 |
| `relationships` (model-found) | 1,847 |
| `deadlines` | 2,240 |
| `authorities` | 1,707 |
| `review_queue` | 866 |

Gates over the whole corpus, against the 100-order pilot they were tuned on:

| Gate | Target | Full sweep | 100-order pilot |
|---|---|---|---|
| severe quote drift | ≤ 2% | 1.8% | 1.7% |
| stored quotes verified | 100% | 100% | 100% |
| null rate | ≤ 2% | 0.0% | 0.0% |
| truncation | 0 | 0 | 0 |
| **`other` rate** | **≤ 3%** | **7.3% — FAILS** | 3.0% |
| gold: primary_topic | ≥ 80% | 90% | 80% |
| gold: instrument | ≥ 75% | 95% | 85% |
| groundedness | reported | 94.0% | 94.8% |

Three documents needed a second pass (two connection timeouts, one malformed
response); `eo extract --run-id 11` recovered all three for $0.0007. Zero
truncations across all 1,534 orders, including the 154,440-character maximum.

### What the dataset shows

Verified against `data/eo.db` (run 11). These are sanity checks on the extraction,
not a finished analysis — the notebook is deferred, and the caveats below are real.

**Orders per active year.** Trump's two terms are non-contiguous, so a first-to-last
date span attributes 2021–24 to him and understates the rate. Counting only calendar
years in which each president actually signed:

| President | Orders | Per active year |
|---|---|---|
| Clinton | 308 | 34.2 |
| G.W. Bush | 291 | 32.3 |
| Obama | 276 | 30.7 |
| **Trump** | **497** | **71.0** |
| Biden | 162 | 32.4 |

Four presidents cluster at 30–34; Trump is more than double any of them.

**Instrument mix separates administrations.** Trump's orders are 37%
`delegates_authority` against 14–23% for everyone else, and 19% `creates_body`
against Obama's 41%. This is the second axis earning its place: a single topic
taxonomy would file both under vague domain buckets.

**The revocation network.** Trump→Biden (106) and Biden→Trump (67) dwarf every
earlier transition — Obama→G.W. Bush and G.W. Bush→Clinton are 37 each. (These
counts are deduplicated: the Federal Register and the model both assert many of
the same edges, and an earlier version of this figure double-counted them.)

**Deadlines.** 893 of 2,240 descriptions parse to a duration; medians run from 60
days (G.W. Bush) to 135 (Obama), with 30/60/90/120/180/365 the common windows.

**Most-tasked officials** are the Attorney General (92 tasks), Secretary of the
Treasury (91), Commerce (73) and State (69).

Two caveats before anyone quotes these. **Agency names are not normalised** —
`Secretary of the Treasury` and `Department of the Treasury` are counted as
different entities, so agency rankings are indicative only. And **only 40% of
deadline descriptions parse** to a duration, so the medians describe the parseable
subset, not all deadlines.

### The `other` gate fails, and that is a real finding

Full decomposition and the rest of the known limits: [DATA_QUALITY.md](DATA_QUALITY.md).

**7.3% of orders (112/1,534) answered `other` on one axis or the other**, against
a 3% threshold. This gate exists to make vocabulary gaps loud — it is how
`education` was found — so a failure here is information, not a defect. The
extraction itself is sound: every `other` row carries a recorded free-text reason,
and the other seven gates pass.

Reading all 112 reasons, the failure decomposes into three unequal causes:

| Cause | Rows | What would actually fix it |
|---|---|---|
| A category that already exists was not used | ~40 | prompt clarity, **not** new categories |
| A genuine vocabulary gap | ~16 | one new instrument, `continues_body` |
| Precedence unresolved on multi-action orders | ~15 | a clearer precedence rule |
| Long-tail one-offs | remainder | nothing; this is the tail |

The dominant cause is the *first*: roughly 40 rows had a correct answer available
and did not take it — 15 designations and honours despite `confers_status_or_honor`
existing, nine "Closing of Executive Departments and Agencies" orders, an order of
succession despite `adjusts_pay_or_admin` naming succession explicitly, and tariff
actions despite `imposes_sanctions` covering trade restriction. Adding categories
would not fix any of those, and would lengthen the prompt, which
[PLAN.md](PLAN.md) records as actively harmful: v6 added categories plus
explanatory prose and regressed every metric.

The axes behave differently and should not be treated as one number. **The topic
axis is healthy at 1.8%** — its uncategorised reasons are true one-offs
(millennium commemoration, the Army–Navy football broadcast, the National Garden
of American Heroes). **The instrument axis carries 5.5%** and is where any future
revision belongs.

Two further honest notes:

- **The 100-order pilot understated this at 3.0%.** A spread sample is
  representative for grounding, which is a per-quote property, but not for
  vocabulary coverage, because gaps cluster in order types the sample thins out.
  Do not size a taxonomy from a pilot.
- **The gold gates went up, not down** (topic 80% → 90%, instrument 85% → 95%) on
  the same model and the same prompt. That is run-to-run nondeterminism, and it is
  the same reason "all gates pass" was never claimed as a stable property. It cuts
  both ways. Both columns are scored against the same gold set (v2), so the rise
  is not an artefact of the EO 14287 relabel.

## What went wrong in v1, and what prevents it now

| v1 failure | Evidence | Structural fix |
|---|---|---|
| Empty scrape → hallucinated content | EO 14289 (tariffs) summarized as a COVID/DPA order | Ingest gate + ≥500-char precondition + `source_quote` grounding check |
| Bot-block invisible to error handling | `Request Access` page returns HTTP 200 | JSON API only; explicit interstitial detection |
| Prose parsing broke on markdown | 47/143 rows blank from `**Sentiment:**` | Structured outputs against a JSON schema |
| `max_tokens=200` truncation | 29 summaries end mid-clause | Generous cap + `finish_reason` check |
| No resume; write-at-end | Any crash lost the whole run | SQLite per-record commit + resumable fetch |
| Silent success on garbage | 0 rows said "Failed", 33% were empty | Validation gates fail the run |
| Unfalsifiable output | Same EO scored `Urgent` and `Assertive` in two runs | Extract facts with quotes, not vibes |
| Secret in a tracked-adjacent file | `my_secrets.py` plaintext on disk | Env vars + `.env.example` |

## Tests

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Tests run against saved fixtures in `tests/fixtures/` — no network — and pin the
specific failure modes above, including the HTTP-200 bot interstitial.
