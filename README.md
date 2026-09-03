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
eo extract    # Phase 3: the LLM pass                      (not yet implemented)
eo validate   # Phase 4: quality gates                     (not yet implemented)
eo export     # Phase 5: CSV/Parquet                       (not yet implemented)
```

`eo fetch` is idempotent and resumable at two levels: documents already stored
are skipped, and bodies already in `data/raw/` are reused without a network
round trip. An interrupted run is resumed by running the command again.

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
