# Executive Order Analysis

A queryable structured dataset of U.S. Executive Orders. Each order carries
machine-extracted fields — topics, agencies tasked, deadlines, legal authorities,
and revocation/amendment relationships — every one of which is traceable back to
a quoted span of the source document.

The development plan is [PLAN.md](PLAN.md). The v1 attempt is preserved in
[archive/](archive/) as post-mortem evidence, not as working code.

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
eo export     # Phase 5: CSV/Parquet                       (not yet implemented)
```

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

Each order is described on two axes: `primary_topic` (13 domains — what the
order is about) and `instrument` (6 kinds — what it does).

`instrument` is decided by an explicit **precedence rule** rather than by
judgment about emphasis, because "the action the order is mostly devoted to"
is not reproducible: two careful readers split on EO 13985, which both
establishes a working group and directs government-wide equity assessments.
The rule takes the first that matches — creates a body, imposes sanctions,
revokes or amends, delegates authority, directs a report, adjusts pay or
administration — and is applied to what the text says, not to what seems most
important.

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
