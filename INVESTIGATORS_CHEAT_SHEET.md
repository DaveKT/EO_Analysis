# Investigator's Cheat Sheet

The traps in `data/analysis.db` that are **not** fixed in the data — you have to
handle them in your query or your wording. One page, scannable.

Full reasoning: [DATA_QUALITY.md](DATA_QUALITY.md). Schema:
[DATA_DICTIONARY.md](DATA_DICTIONARY.md).

---

## The four queries that are wrong by default

**1. Counting agency rows double-counts.** `agency_taskings` unions agency
taskings *and* deadline responsibilities, and **30% of (agency, order) pairs
appear in both** — the same obligation twice. DHS looks like 357; it is 115
orders.

```sql
-- WRONG                                 -- RIGHT
COUNT(*)                                 COUNT(DISTINCT document_number)
```

**2. Agency rankings must exclude two kinds.** `collective` is "all federal
agencies" (784 mentions); `generic` is bare references like "Task Force" (262).
Neither is a cross-order entity.

```sql
WHERE kind NOT IN ('collective', 'generic')
```

**3. Relationships must be filtered to authoritative rows.** The Federal Register
and the model both assert many of the same edges. Counting all 5,765 inflates the
network by 27%.

```sql
WHERE authoritative = 1     -- revocation_network already does this
```

**4. `all_claims.id` is not unique across tables.** Ids restart per table and the
ranges overlap. Always carry `claim_table`.

```sql
LEFT JOIN raw_quotes q
       ON q.claim_table = c.claim_table AND q.claim_id = c.id
```

---

## Absence is not evidence of absence

**Recall was never measured.** Every quality gate tests whether what was
extracted is correct, never whether everything was extracted. There is no
exhaustive human-annotated set to measure recall against.

| | Orders with ≥1 | With none |
|---|---|---|
| tasked agency | 1,007 (66%) | 527 |
| deadline | 837 (55%) | 697 |
| authority | 1,069 (70%) | 465 |
| model relationship | 898 (59%) | 636 |
| **any claim at all** | 1,482 | **52** |

Only 192 of 1,534 orders carry any secondary topic.

> ❌ "34% of orders task no agency."
> ✅ "The model extracted at least one tasked agency from 66% of orders."

**259 claims are missing on purpose.** Where a quote could not be verified at
all, the claim was dropped from the tables and recorded in `review_queue` as
`dropped_unverifiable`. They are not in your counts.

---

## Fields you must not quote as evidence

`summary`, `agencies_tasked.task`, `deadlines.due_description` are **model prose
with no groundedness check**. Only `source_quote` is verified.

This is not theoretical. The summary of **EO 14081** names an "Interagency
Technical Working Group" and a "Biosafety and Biosecurity Innovation Initiative"
as bodies the order establishes. **Neither phrase appears anywhere in the
order.** Quote `source_quote`; use `summary` to orient only.

**540 quotes were repaired.** `quote_trimmed = 1` means the model's quote drifted
and was cut back to the part that verifies. The original is in `raw_quotes`. The
model's own accuracy is 94.0%, not the 100% that stored quotes show.

---

## Classification limits

**`primary_topic` comes from a cheap model.** On the gold set, a frontier model
assigned a more specific domain in roughly **1 case in 5** — the weak model
defaults to `government_administration` or `foreign_policy`. This is the largest
known quality gap in the data.

**`other` is 7.3%, above its 3% gate** — a taxonomy limit, not a finding about
the orders. Topic is fine at 1.8%; `instrument` carries 5.5%. Roughly 40 of the
112 had a correct category available and did not use it.

**One instrument per order, by precedence** — new body → sanctions →
revoke/amend → **status/honour** → delegation → reports → pay/admin. It is not
"what the order is mostly about". A *body* requires membership; a program or
Initiative is not one.

**Gold agreement is 90% topic / 95% instrument on 20 orders** labelled by a model
and reviewed by a non-expert. A sanity check, not a precision measurement.

**Results are not deterministic.** The same model and prompt scored 80% on gold
topic in the pilot and 90% in the full sweep. Never present "all gates pass" as a
property of the pipeline.

---

## Shape of the corpus

**Trump has two non-contiguous terms** (2017–21, 2025–26) with Biden between.
A first-to-last date span attributes 2021–24 to him and understates his rate by
about a third.

```sql
-- orders per ACTIVE year
COUNT(*) * 1.0 / COUNT(DISTINCT substr(signing_date, 1, 4))
```

Clinton 34.2 · G.W. Bush 32.3 · Obama 30.7 · **Trump 71.0** · Biden 32.4

**Coverage starts ~1994.** EO 12890 → EO 14423. Orders below 12890 (back to 1937)
are **not** here. Never call this "all Executive Orders". Proclamations,
memoranda, and 22 further FR documents are also excluded — 19 that carry no EO
number, plus 3 duplicate rows for orders already in the corpus.

**~21% of relationship targets do not resolve.** `target_document_number` is NULL
where the target is a pre-1994 order or a proclamation. Say so if you present the
network.

**Only 40% of deadlines quantify.** 893 of 2,240 `due_description` values parse
to a duration, and the subset is not random — round "within 90 days" phrasings
parse, discursive ones do not. Say "parseable deadlines". The count moves with
how you define a duration (DATA_QUALITY §6.5 pins it) — quote "about 40%".

**18% of agency mentions are unmatched**, but that is mostly correct: 772
mentions are genuine one-off commissions and task forces that *should* be their
own entities. `agencies.matched = 0` marks them.

---

## Two columns that look alike and are not

| Column | Means |
|---|---|
| `relationships.source_quote` | verbatim text **from the order** — model rows only |
| `relationships.fr_disposition_note` | a Federal Register editorial note **about** the order — FR rows only |

An FR note ("Revokes: EO 12088, October 13, 1978") appears nowhere inside the
order. Treating them as one column makes a groundedness check read ~70% instead
of 100%.

---

## Sanity checks before you publish

```python
# 1. every stored quote is in its order's text. Expect 8989 / 8989.
#    Not a SQL instr(): Federal Register typography makes that read 421.
import sqlite3
from eo.grounding import is_grounded     # PYTHONPATH=src
rows = sqlite3.connect("data/analysis.db").execute(
    "SELECT c.source_quote, t.body_text FROM all_claims c JOIN order_text t USING (document_number)"
).fetchall()
print(sum(is_grounded(q, b) for q, b in rows), "/", len(rows))
```

```sql
-- 2. which model produced this, and when
SELECT model, prompt_version, cost_usd, coverage FROM run_metadata;

-- 3. what is already known to be imperfect, and how
SELECT kind, COUNT(*) FROM review_queue GROUP BY 1 ORDER BY 2 DESC;
```

Then state, in the write-up: **which model**, **that recall is unmeasured**, and
**the ~1994 boundary**. Those three cover most of the ways this dataset can be
overstated.
