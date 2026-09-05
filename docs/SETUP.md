# Setup and operation

How to install the pipeline, rebuild the published artefacts, and run the
analysis notebook. What the project *is* and what it found is in the
[README](../README.md); what to trust is in [DATA_QUALITY.md](DATA_QUALITY.md).

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"          # pipeline + tests
.venv/bin/pip install -e ".[analysis]"     # + jupyterlab, matplotlib, scikit-learn
cp .env.example .env                       # then add your OpenRouter key
```

Configuration is read from the environment or `.env`. The OpenRouter key is read
from **`eo_openrouterkey` and nothing else**: there is deliberately no fallback to
other variable names, because a stale key under a different name would otherwise
be picked up silently. Nothing here requires a key except `eo extract`.

### If `import eo` fails after install

On macOS, files in the venv can carry the `UF_HIDDEN` flag, and CPython ≥ 3.11.4
deliberately skips hidden `.pth` files, which silently voids the editable
install. `chflags nohidden` on the `.pth` fixes it; if the flag returns, run
everything as

```sh
PYTHONPATH=src .venv/bin/python -m eo.cli <command>
```

The commands below are written that way so they work either way.

## The published artefacts

Everything in `data/analysis.db` and `data/export/` derives from the Federal
Register and is committed. The working store `data/eo.db` (every run, every raw
model response) and the raw-text cache are local and rebuildable.

```sh
PYTHONPATH=src .venv/bin/python -m eo.cli analysis-db --run-id 11   # data/analysis.db
PYTHONPATH=src .venv/bin/python -m eo.cli export      --run-id 11   # data/export/*.csv + manifest.json
```

Both apply the hand corrections in `corrections/primary_topic.json` at build
time and keep the model's original value beside the corrected one
(`primary_topic_as_extracted`). A correction whose expected value no longer
matches the model's fails the build. See DATA_QUALITY §6.8.

`export --format parquet` needs `pyarrow`, which is not a pinned dependency.
`--include-text` adds `body_text` and `raw_response`, which are large.

## The pipeline, end to end

```sh
eo status       # configuration, row counts, ingest health
eo fetch        # ingest from the Federal Register API (free, no model calls)
eo extract      # the LLM pass, one run per (model, prompt_version)
eo validate     # the eight quality gates for a run
eo review       # what the gates parked for human review
eo compare      # two runs, head to head over the documents both cover
eo analysis-db  # one run + its source text as a standalone database
eo export       # one run to flat files, with a provenance manifest
```

**Fetch** is idempotent and resumable: documents already stored are skipped and
cached bodies are reused. It also seeds `relationships` from the Federal
Register's own disposition notes, so extraction adds to a known-good base.

**Extract** samples evenly across presidencies, commits each document as it
completes, and resumes with `--run-id N`, re-offering only rows that failed or
came back truncated. `--dry-run` shows what would be sent and how large it is,
at no cost.

```sh
PYTHONPATH=src .venv/bin/python -m eo.cli extract --limit 25 --dry-run
PYTHONPATH=src .venv/bin/python -m eo.cli extract --limit 100                 # pilot
PYTHONPATH=src .venv/bin/python -m eo.cli extract --run-id 11                 # resume the full sweep
PYTHONPATH=src .venv/bin/python -m eo.cli extract --compare-run 9 --model openai/gpt-5.4
PYTHONPATH=src .venv/bin/python -m eo.cli compare --baseline 9 --candidate 10
```

A run is identified by `(model, prompt_version)` and records its own token
counts and cost. A better model later adds a run rather than overwriting this
one, so runs can be diffed. The prompt is versioned source in
[`src/eo/prompts.py`](../src/eo/prompts.py); bump `PROMPT_VERSION` whenever its
wording changes.

**Validate** runs at the end of every extraction and exits non-zero on failure.
Run it on its own to regenerate the review queue after re-seeding relationships.

```sh
PYTHONPATH=src .venv/bin/python -m eo.cli validate --run-id 11
PYTHONPATH=src .venv/bin/python -m eo.cli review   --run-id 11
```

### Re-seeding Federal Register relationships

If `src/eo/dispositions.py` changes, the seeded edges must be rebuilt. There is
no unique constraint on `relationships`, so delete before re-seeding:

```python
from eo import config, db, dispositions
with db.session(config.load_settings().db_path) as con:
    con.execute("DELETE FROM relationships WHERE run_id IS NULL AND source = 'fr_disposition_notes'")
    con.commit()
    dispositions.seed_relationships(con)
```

Then `validate`, `analysis-db` and `export` for the shipped run.

## The analysis notebook

`notebooks/analysis.ipynb` reads `data/analysis.db` read-only and contains no
pipeline code. It is committed **only after being executed**, so every output
in it was produced by the cell above it.

```sh
PYTHONPATH=src .venv/bin/jupyter lab notebooks/analysis.ipynb
PYTHONPATH=src .venv/bin/jupyter nbconvert --to notebook --execute --inplace notebooks/analysis.ipynb
```

In VS Code, select the `.venv` interpreter as the kernel. The sections build on
one another (the survival and reversal sections reuse the cancellation section's
edge set), so run it top to bottom.

## Tests

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q     # 194 tests, no network
.venv/bin/ruff check src tests
```

Tests run against saved fixtures in `tests/fixtures/` and pin the specific
failure modes the project was built around, including the HTTP-200 bot
interstitial that silently starved version 1.

## Provider notes

OpenRouter routes to providers that ignore `response_format` unless
`provider.require_parameters` is set, and that flag filters in both directions: a
parameter the model does not advertise (for example `temperature` on
`openai/gpt-5.4`) eliminates every endpoint and the request fails in routing at
zero cost. The runner asks the model's endpoint list for `supported_parameters`
once per run and omits what is unsupported. httpx timeouts are per socket
operation, so each attempt also carries an absolute deadline. Details and history
in [PLAN.md](PLAN.md), "Open risks".
