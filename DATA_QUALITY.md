# Data Quality

What was done to make this dataset trustworthy, what is known to be wrong with
it, and what you must not claim from it.

**Read the "Before you publish anything" section before quoting a number.**

The dataset is `data/analysis.db` — 1,534 Executive Orders, extracted by
`openai/gpt-oss-120b` under prompt v7 (run 11, 2026-09-04, $1.05). Provenance is
in its `run_metadata` table.

---

## 1. Why this project distrusts its own output

Version 1 of this project produced 143 rows that looked finished and were not.
An empty scrape returned an HTTP 200 "Request Access" page, so a tariff order was
summarised as a COVID Defense Production Act order. A third of rows were blank.
Nothing reported a failure. **Every control below exists because of a specific
way that went wrong**, and the whole design assumption is that a fluent model
will produce confident, wrong output at scale unless something mechanical catches
it.

| v1 failure | What it produced | Structural fix now in place |
|---|---|---|
| Empty scrape → hallucination | EO 14289 (tariffs) described as a COVID/DPA order | JSON API only; ≥500-char precondition before any model call |
| Bot block invisible to error handling | `Request Access` page returned HTTP 200 | Explicit interstitial detection; `raise_for_status` is not trusted |
| Prose parsing broke on markdown | 47/143 rows blank from `**Sentiment:**` | Structured outputs against a JSON schema |
| `max_tokens=200` | 29 summaries ended mid-clause | Generous cap + `finish_reason` stored and gated |
| No resume, write-at-end | Any crash lost the whole run | SQLite per-record commit; `--run-id N` resumes |
| Silent success on garbage | 0 rows said "Failed"; 33% were empty | Gates fail the run and exit non-zero |
| Unfalsifiable output | Same order scored `Urgent` and `Assertive` in two runs | Extract facts with quotes, not vibes |

---

## 2. The central control: every claim carries a verified quote

Each extracted agency tasking, deadline, authority and relationship stores a
`source_quote`, and **the quote is checked against the order's own text**. This
is the mechanism that would have caught v1's headline failure automatically.

- **8,989 / 8,989 stored quotes (100%)** appear in their order's `body_text`.
- The check is reproducible inside `analysis.db` alone: join `all_claims` to
  `order_text`. This is deliberate — a dataset that asserts its quotes are
  verified but does not ship the means to re-verify them is asking for trust it
  has not earned.
- Matching is forgiving about typography and strict about words. Federal Register
  text renders quotation marks as `` and ``, and uses several Unicode dashes; on
  an early run, 30 of 41 apparent failures were punctuation, not drift.

**What "100%" does and does not mean.** It means every *stored* quote is real
text from that order. It does **not** mean the model quoted perfectly: 540 quotes
(6%) drifted from the source and were **trimmed to their verifiable span**. The
model's original wording is preserved in `raw_quotes` — nothing was silently
improved.

The honest figure for the model's own quoting is **groundedness: 94.0%** — the
share of quotes that were exactly right as written, scored on `raw_quote` where a
repair happened. A metric that scored the repaired text would be reporting the
repair's success and calling it the model's.

Of the 6% that drifted, most trail off at the tail rather than being invented.
**Severe drift — quotes keeping under 40% of their words — is 1.8%.**

---

## 3. The eight quality gates

Gates run automatically at the end of every extraction; a run that fails them
exits non-zero. Run 11's results, against the 100-order pilot they were tuned on:

| Gate | Target | Run 11 | Pilot (run 9) |
|---|---|---|---|
| severe quote drift | ≤ 2% | 1.8% | 1.7% |
| stored quotes verified | 100% | 100% | 100% |
| null rate | ≤ 2% | 0.0% | 0.0% |
| truncation | 0 | 0 | 0 |
| **`other` rate** | **≤ 3%** | **7.3% — FAILS** | 3.0% |
| gold: primary_topic | ≥ 80% | 90% | 80% |
| gold: instrument | ≥ 75% | 95% | 85% |
| groundedness | reported | 94.0% | 94.8% |

**Zero truncations across all 1,534 orders**, including the 154,440-character
maximum — so no summary or claim list was cut off mid-generation.

**Never present "all gates pass" as a property of this pipeline.** The model is
nondeterministic. The pilot scored 80% on gold topic against an 80% threshold;
the full sweep scored 90% on the *same model and the same prompt*. That is
run-to-run variance, and it cuts both ways.

---

## 4. The gold set, and how far it can be trusted

Twenty orders were read from source by hand and labelled, spanning all six
presidencies and including both the shortest order in the corpus (760 chars) and
one of the longest (54,068). It is the only control that can catch a model that
is fluent and confidently wrong about *what an order is* — groundedness proves
quotes are real, not that the reading is right.

**The gold labels are Claude's, not a domain expert's.** Two were found wrong
during development (EO 13985, EO 14081), caught only because the labelling
convention gave an objective precedence test.

**The contested labels were reviewed by hand on 2026-09-04** (gold set version 2).
Three had been disputed; the review resolved all three and, more usefully, fixed
two gaps in the *convention* that had caused them.

| Order | Was | Now | Resolution |
|---|---|---|---|
| EO 14081 | `directs_report_or_study` | unchanged | **Gold confirmed.** Its only "shall establish" clauses create an *Initiative* and a *program*, neither of which is a body |
| EO 14287 | `delegates_authority` | **`directs_report_or_study`** | **Relabelled.** The operative act is publishing a list on a 30-day deadline |
| EO 13489 | `delegates_authority` | unchanged | No longer disputed — run 11 agrees |

Two clarifications were added to the labelling convention, because the
disagreements were rule gaps rather than misreadings:

- **A "body" has membership** — a council, commission, task force, board or
  working group. A program, initiative or fund is not a body, however
  substantial. Three separate extraction runs read EO 14081's "Data for the
  Bioeconomy Initiative" as `creates_body`; the rule had never said.
- **`delegates_authority` means conferring the President's own statutory or
  constitutional functions** on an official. Merely directing an official to act
  is not delegation — otherwise nearly every order qualifies.

**A caution the review exposed.** The model's *summary* of EO 14081 cites an
"Interagency Technical Working Group" and a "Biosafety and Biosecurity
Innovation Initiative" as bodies the order establishes. Neither phrase appears
anywhere in the order's text. This is §6.6 in action: `summary` is unverified
free text, and it named entities that do not exist. Only `source_quote` is
checked.

**Consequence for you:** instrument agreement is now 95% (19/20) and topic 90%
(18/20) on run 11. Both still rest on 20 labels written by a model and reviewed
by a non-expert. Treat them as a sanity check, not a precision measurement.

---

## 5. Is the cheap model good enough? — the frontier comparison

A single run's gate report cannot distinguish the *model's* ceiling from the
*task's*. To separate them, 36 orders (the gold 20 plus the 23 the pilot flagged)
were re-extracted on `openai/gpt-5.4` under the identical prompt.

| Over the same 36 documents | `gpt-oss-120b` | `gpt-5.4` |
|---|---|---|
| gold: primary_topic | 80% | **100%** |
| gold: instrument | 85% | 80% |
| groundedness | 88.6% | **99.8%** |
| severe drift | 3.7% | **0.0%** |
| quotes extracted | 245 | 531 |
| cost | $0.02 | $0.85 |

**Topic agreement was the cheap model's ceiling, not the task's.** All four of its
misses were the same failure — defaulting to `government_administration` or
`foreign_policy` where a specific domain applied. The frontier model missed none.
It also extracted **more than twice the claims while being more grounded**, which
rules out the obvious confound: it is not scoring well by saying less.

**What this means for the shipped data:** `primary_topic` in `analysis.db` is
produced by the weaker model, and a frontier model would assign a more specific
domain in roughly one case in five. The full corpus on `gpt-5.4` would have cost
~$36 against ~$1.00; the cheap sweep was shipped deliberately, with this gap
recorded rather than smoothed over.

**Do not compare the 88.6% above to the 94.0% in §2.** That 36-order set is
deliberately enriched with the pilot's own failures. Only the head-to-head
columns are comparable, because both runs cover the same documents.

---

## 6. Known defects and limits

### 6.1 The `other` rate gate fails at 7.3% — a real finding

112 of 1,534 orders answered `other` on one axis (topic 28, instrument 84). The
gate exists to make vocabulary gaps loud; this is it working. Reading all 112
recorded reasons, the failure decomposes:

| Cause | Rows | What would fix it |
|---|---|---|
| A category that already exists was not used | ~40 | prompt clarity, **not** new categories |
| A genuine vocabulary gap | ~16 | one new instrument, `continues_body` |
| Precedence unresolved on multi-action orders | ~15 | a clearer precedence rule |
| Long-tail one-offs | remainder | nothing; this is the tail |

The dominant cause is the first: 15 designations and honours despite
`confers_status_or_honor` existing, nine "Closing of Executive Departments and
Agencies" orders, an order of succession despite `adjusts_pay_or_admin` naming
succession, tariff actions despite `imposes_sanctions` covering trade restriction.

**The two axes differ and should not be read as one number.** Topic is healthy at
1.8% with a genuine long tail (millennium commemoration, the Army–Navy football
broadcast). Instrument carries 5.5% and is where any revision belongs.

### 6.2 Absence of a claim is not absence in the order

This is the most likely way to misuse this dataset.

| | Orders with ≥1 | Orders with none |
|---|---|---|
| tasked agency | 1,007 (66%) | 527 |
| deadline | 837 (55%) | 697 |
| authority | 1,069 (70%) | 465 |
| model-found relationship | 898 (59%) | 636 |

Many of those orders genuinely contain no deadline. Others contain one the model
did not extract. **Nothing in this dataset distinguishes the two**, because
recall was never measured — there is no exhaustive human-annotated set to measure
it against. Every gate here measures **precision** (is what was extracted
correct?), never **recall** (was everything extracted?).

So: "34% of orders task no agency" is **not** a supportable finding. "The model
extracted at least one tasked agency from 66% of orders" is.

### 6.3 Relationship targets resolve only ~79% of the time

| | Edges | Name an EO number | Resolve to an order here |
|---|---|---|---|
| Federal Register | 3,918 | 3,343 | 2,778 |
| Model | 1,847 | 1,089 | 704 |

`target_document_number` is NULL for the rest, mostly because the target is a
pre-1994 order outside this corpus, or a proclamation/memorandum rather than an
EO. **A revocation-network analysis silently drops those.** `target_eo_number`
is still populated, so an unresolvable target is visibly unresolvable rather than
missing.

**The Federal Register wins wherever it has an opinion.** Its disposition notes
are the authoritative record of what an order does to earlier orders. Where FR
and the model both speak to an (order, target) pair, the FR row carries
`authoritative = 1` and the model's carries `0` — *including when they agree*.
This is not fussiness: 123 revocation pairs are asserted by both sources, and
counting both inflated the revocation network by **27%**. An early version of the
figures in the README made exactly that mistake (Trump→Biden read 124; the
deduplicated figure is 106).

- 3,918 FR edges: always authoritative.
- 930 model edges on pairs FR is silent about: authoritative. Finding these is
  the point of extraction.
- 917 model edges superseded by FR, of which **174 contradict** it
  (`contradicts_fr = 1`) — the model asserted a different relation.

`revocation_network` already filters to `authoritative = 1`. If you query
`relationships` directly, **add that predicate yourself** or you will
double-count.

### 6.4 Agency names: 83% resolved, and the residual is explainable

1,146 distinct names covered 3,195 taskings before normalisation, with
`Secretary of the Treasury` and `Department of the Treasury` as separate
entities. `agencies` + `agency_mentions` resolve these to 558 canonical entities
and **83% of mentions match a known agency**.

The residual is not one problem, and it is mostly not a problem at all:

| `kind` | Entities | Mentions | What it is |
|---|---|---|---|
| `department` | 18 | 2,699 | the executive departments |
| `office` | 51 | 1,135 | standing agencies, offices, councils |
| `collective` | 1 | 784 | "all federal agencies" and its phrasings |
| `body` | 439 | 714 | **genuine one-off** commissions, task forces, boards |
| `official` | 5 | 269 | named White House officials |
| `generic` | 44 | 262 | **bare in-document references** — see below |

The 714 `body` mentions are *correct as they stand*: they are real, distinct,
one-off entities that should each be countable, not variants of anything.

**`generic` is the one to know about.** "Task Force", "the Commission", "Board",
"Parties to the Dispute" refer to a body created or named inside their own
order — so two orders' "Task Force" are **different task forces**. They are
flagged rather than merged, because collapsing them would invent a body spanning
unrelated orders and produce a confidently wrong ranking. **Exclude them from any
cross-order aggregate:**

```sql
SELECT canonical_name, COUNT(*) FROM agency_taskings
WHERE kind NOT IN ('generic', 'collective')
GROUP BY agency_id ORDER BY 2 DESC;
```

Judgment calls baked in, which you may disagree with:
- `Secretary of X` and `Department of X` are merged as one institution.
- `Attorney General` resolves to `Department of Justice`.
- `Department of War` resolves to `Department of Defense` (EO 14347 renamed it in
  September 2025); `raw_name LIKE '%War%'` recovers that period.
- Military departments (Army, Navy, Air Force) stay **separate** from Defense.
- `each federal agency` is the collective; `all contracting agencies` is **not**,
  because it names a subset.

`agencies_tasked.agency_name` is never overwritten, so any of these is reversible
without re-extracting.

### 6.5 Deadlines are only 40% quantified

`due_description` parses to a duration for **888 of 2,240** deadlines; `due_date`
is populated for 1,643. Median-deadline statistics therefore describe the
parseable subset, not all deadlines, and that subset is not random — round
"within 90 days" phrasings parse, discursive ones do not.

### 6.6 `summary` and `task` are unverified free text

`orders.summary`, `agencies_tasked.task` and `deadlines.due_description` are
model prose with **no groundedness check**. Only `source_quote` is verified. Use
them for orientation; quote the `source_quote` when you need evidence.

### 6.7 The review queue holds 866 known-imperfect items

Across 402 orders. These are flagged, not fixed:

| Kind | Rows | Meaning |
|---|---|---|
| `dropped_unverifiable` | 259 | a claim whose quote yielded nothing verifiable; **not** in the claim tables |
| `ungrounded_*` | 540 | quote drifted and was trimmed; the claim **is** in the tables |
| `relationship` | 63 | the model contradicted a Federal Register disposition note |
| `other_without_reason` | 4 | answered `other` with no reason given |

The 63 relationship disagreements are the most interesting: the Federal
Register's cross-references are authoritative, so a contradiction is more likely
the model's error than FR's. They are the subset of the 174 `contradicts_fr`
rows where FR asserts one of `revokes`/`amends`/`supersedes`/`continues`. The
commonest shapes are FR `amends` vs model `revokes` (20), FR `amends` vs model
`references` (10), and FR `revokes` vs model `continues` (10). **As of
2026-09-04 these are resolved in FR's favour by construction** — the model's row
is retained but non-authoritative.

---

## 7. Coverage boundary

**This is not "all Executive Orders."** The Federal Register API's full-text
coverage begins around 1994. The dataset runs **EO 12890 (1993-12-30) → EO 14423
(2026-08-28)**, six presidencies. Orders below EO 12890 — back to EO 7532 in
1937, plus unnumbered orders before that — are not reachable this way and are
**out of scope**. Sources for them: the National Archives EO Disposition Tables,
or the American Presidency Project.

Also excluded: proclamations, presidential memoranda, and 22 Federal Register
documents (correction notices, annexes, reprints) that carry no EO number.

Presidential terms are not contiguous: **Trump appears in two separate terms**
(2017–2021, 2025–2026), with Biden between. Deriving "orders per year" from a
first-to-last date span attributes 2021–24 to Trump and understates his rate by
about a third.

---

## 8. Before you publish anything

A checklist for not overstating what is here.

1. **Say which model produced it.** `openai/gpt-oss-120b`, prompt v7. It is a
   cheap model, and §5 measures where it is weaker than a frontier one.
2. **Never claim recall.** No absence in this dataset is evidence of absence in
   the orders. See §6.2.
3. **Quote `source_quote`, not `summary`.** Only the former is verified.
4. **Count agencies through `agency_taskings`**, never
   `agencies_tasked.agency_name` — the latter splits Treasury three ways.
5. **State that ~21% of relationship targets do not resolve** if you present the
   revocation network.
6. **Say "parseable deadlines"** if you quote deadline medians.
7. **Treat `other` (7.3%) as a taxonomy limit**, not as a finding about the
   orders.
8. **Do not treat the gold-set agreement as precision.** Twenty labels, written
   by a model and reviewed by a non-expert. The disputed ones were resolved on
   2026-09-04; that raised instrument agreement to 95%, which is a statement
   about 20 orders, not about 1,534.
9. **Check the two-term problem** before any per-year rate.
10. **Re-verify if it matters.** Join `all_claims` to `order_text` and check the
    quotes yourself; the data ships with everything needed to do it.

---

## 9. Reproducing the checks

```sh
PYTHONPATH=src .venv/bin/python -m eo.cli validate --run-id 11   # the eight gates
PYTHONPATH=src .venv/bin/python -m eo.cli review   --run-id 11   # the 866 flagged items
PYTHONPATH=src .venv/bin/python -m eo.cli compare --baseline 9 --candidate 10
PYTHONPATH=src .venv/bin/python -m pytest -q                     # 148 tests
```

```sql
-- every stored quote really is in its order's text: expect 8,989 / 8,989
SELECT COUNT(*) FROM all_claims c JOIN order_text t USING (document_number);
```

Related: [PLAN.md](PLAN.md) for how the project got here and what is still open;
[DATA_DICTIONARY.md](DATA_DICTIONARY.md) for the schema.
