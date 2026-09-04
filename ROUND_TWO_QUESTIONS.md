# Round Two Questions

Questions raised during analysis that were not executed, with the plan for each so
they can be picked up later without re-deriving the reasoning. Parked on
2026-09-04. Nothing here is in `data/analysis.db` yet.

---

## 1. Framing: does an administration present its orders as building or as fixing?

**The question as asked.** What percentage of each administration's executive orders
was presented in primarily positive language (build, make better, support) versus
negative language (fix problems, because of poor previous decisions)?

**Status.** Not started. Feasible, but not as "sentiment"; see below.

### Why this cannot be sentiment analysis

Version 1 of this project died on exactly this. The same order scored "Urgent" in
one run and "Assertive" in the next, and nothing could say which was right. The
current extraction prompt tells the model to describe what an order does and never
how it sounds, so nothing in the dataset today carries tone. `orders.summary`
cannot be used either: it is unverified prose written under a no-tone
instruction. The `significance` field was dropped for the same reason. It had no
textual referent, and the model agreed with hand labels 0% of the time.

### What can be measured instead

Executive orders state their own justification in a preamble, and that text is
checkable. In the corpus, **935 of 1,534 orders open with a titled Section 1**,
most often "Policy" (328) or "Purpose" (222), then "Establishment" (64),
"Background" (47) and "Purpose and Policy" (46). The rest usually carry the same
material in an untitled opening paragraph.

So the question becomes: *does the order's stated justification frame the action
as building something, or as correcting a failure?* That is a classification with
a textual referent, and it can carry a mandatory `source_quote` that the existing
groundedness check verifies.

**A finding from the quick scan that shapes the design.** Explicit references to a
"previous administration", "prior administration" or "predecessor" appear in only
**32 orders, and 29 of them are Trump 47**. A keyword or lexicon approach would
therefore detect one administration's house style and miss the implicit corrective
framing everyone else uses ("restore", "end", "address the failure of"). A
lexicon pass is a zero-cost sanity check at best and must not be presented as a
finding.

### Cost, scaled from run 11 (4.56M input + 2.59M output tokens = $1.05)

| Option | Model | Cost | Notes |
|---|---|---|---|
| Pilot, 100 orders | `openai/gpt-oss-120b` | under $0.05 | preamble only, ~4,000 chars per order |
| Full sweep, preamble only | `openai/gpt-oss-120b` | about $0.30 | |
| Full sweep, preamble only | `openai/gpt-5.4` | about $10 | worth it if the cheap model defaults the way it did on `primary_topic` |
| Full sweep, whole text | `openai/gpt-5.4` | about $36 | unlikely to be needed; framing lives in the preamble |

Build effort is roughly half a day, most of it reusing `providers.py`,
`extract.py` and `grounding.py`.

### Execution plan

1. **Define the field so it can be wrong.** A controlled `framing` value with four
   options: `constructive`, `corrective`, `mixed`, and `none` for orders with no
   purpose statement (pay, succession, closures). Alongside it a boolean
   `references_prior_policy`. Both require a `source_quote` from the preamble.
   Constraints go in the JSON schema, never in a validator the model cannot see
   (PLAN.md, open risks).
2. **Hand-label a gold set first.** Sixty orders, ten per administration term
   (Clinton, G.W. Bush, Obama, Trump 45, Biden, Trump 47), read from the preamble.
   **This is the go/no-go step.** Write the labelling convention before the
   labels: two gold labels were wrong in Phase 4 and were caught only because the
   convention gave an objective test.
3. **Build it as a separate pass, not a re-sweep.** A new `framing` table keyed by
   `run_id` with its own prompt version, sending only the preamble. Re-extracting
   under a widened v8 prompt would cost the full $1.05 and, given nondeterminism,
   would quietly change the existing dataset. Keep the prompt short; v6 showed that
   explaining a category teaches avoidance of it.
4. **Pilot on 100 orders and run it twice.** Gate on gold agreement at or above 80%
   and on groundedness, and measure the model's agreement *with itself* across the
   two runs. Self-disagreement is the v1 failure, and it is cheap to detect here.
5. **Escalate to the frontier model only if the pilot fails**, using the same
   head-to-head comparison the topic axis got (`eo compare`), restricted to shared
   documents.
6. **Sweep, then write the notebook section**: share of each administration's
   orders by framing, each row backed by its quote, with the caveats that the
   classification is the model's reading of the *stated* justification, that a
   preamble can be absent, and that recall of the "corrective" signal is unmeasured
   beyond the gold set.

### Decision needed before starting

Whether `corrective` means any problem-framed justification, or specifically
framing against prior policy. The second is narrower, easier to label
consistently, and closer to the question about blaming previous decisions. The
recommendation is to carry both: the boolean captures the narrow case cheaply,
and the four-way field captures the broad one.

### Caveats that will apply to the result

- It measures how an order *presents* itself, not what it does; the `instrument`
  axis already covers the latter.
- Per-administration percentages will rest on a model's reading of the preamble,
  checked against 60 hand labels. Treat the gold agreement as a sanity check, not
  a precision measurement, exactly as for `primary_topic`.
- The corpus boundary (EO 12890 onward) and the two-term Trump split apply as
  everywhere else.
