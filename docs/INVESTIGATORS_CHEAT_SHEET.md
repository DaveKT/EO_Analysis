# Investigator's Cheat Sheet

This is the one page to read before you use `data/analysis.db`. It lists the
ways the data will fool you if you are not careful. None of these are bugs to be
fixed; they are facts about how the data was made, and you handle them in how
you query and how you describe what you find.

The long version, with the evidence, is [DATA_QUALITY.md](DATA_QUALITY.md). The
list of tables and columns is [DATA_DICTIONARY.md](DATA_DICTIONARY.md).

---

## Five words you need

- **Order**: one executive order. There are 1,534 of them, from December 1993
  to August 2026.
- **The model**: the AI program (`openai/gpt-oss-120b`) that read every order
  and filled in the fields. It is a cheap model, and it makes mistakes.
- **Claim**: one thing the model said about an order, such as "this order
  tasks the Treasury" or "this order sets a 90-day deadline."
- **Quote** (`source_quote`): the exact words from the order that back up a
  claim. Every claim has one, and every quote has been checked against the
  order's text. This is the only part of the data that is verified.
- **Recall**: whether the model found *everything*. It was never measured. We
  know the claims it made are backed by quotes; we do not know what it missed.

---

## Four queries that give the wrong answer unless you change them

**1. Counting agency rows counts the same thing twice.** The `agency_taskings`
view lists an agency once for each task an order gives it *and* once for each
deadline it gives it. The same job often appears both ways, so 30% of rows are
repeats. Homeland Security shows 357 rows but appears in 115 orders.

```sql
-- WRONG                                 -- RIGHT
COUNT(*)                                 COUNT(DISTINCT document_number)
```

**2. Agency rankings must leave out two kinds of row.** Rows of kind
`collective` mean "all federal agencies" (784 of them). Rows of kind `generic`
are bare names like "the Task Force" that refer to a body created inside that
one order (262 of them), so two orders' "Task Force" are different task forces.
Neither is a real agency that spans orders.

```sql
WHERE kind NOT IN ('collective', 'generic')
```

**3. Counting relationships double-counts unless you keep only the authoritative
rows.** The Federal Register and the model both recorded many of the same "this
order revokes that one" links. Counting all 5,766 rows overstates the revocation
network by 27%. The Federal Register's row is the one to count.

```sql
WHERE authoritative = 1     -- the revocation_network view already does this
```

**4. Claim ids repeat across tables.** An `id` of 12 exists in `agencies_tasked`,
in `deadlines`, and in `authorities`, and they are different claims. When you
join, always match on the table name too.

```sql
LEFT JOIN raw_quotes q
       ON q.claim_table = c.claim_table AND q.claim_id = c.id
```

---

## "Not found" does not mean "not there"

The model found at least one tasked agency in 66% of orders. That does **not**
mean 34% of orders task nobody. It means the model did not find one, and we
never measured how much it misses.

| The model found at least one … | in this many orders | and none in |
|---|---|---|
| tasked agency | 1,007 (66%) | 527 |
| deadline | 837 (55%) | 697 |
| legal authority | 1,069 (70%) | 465 |
| link to an earlier order | 898 (59%) | 636 |
| claim of any kind | 1,482 | 52 |

> ❌ "34% of orders task no agency."
> ✅ "The model extracted at least one tasked agency from 66% of orders."

**For legal authority we did measure it, and it is low.** Every order opens with
a sentence naming the laws it relies on. Checked against that sentence, the
`authorities` table names the International Emergency Economic Powers Act in
only 45% of the orders that use it, the National Emergencies Act in 29%, and
the delegation statute (3 U.S.C. 301) in 7%. If your question is "what laws do
orders cite", read the opening sentence from `order_text` instead; the notebook's
legal-authority section shows how. See DATA_QUALITY §6.2.

**259 claims were left out on purpose.** Where the model's quote could not be
found in the order at all, the claim was dropped and logged in `review_queue` as
`dropped_unverifiable`. They are not in your counts.

---

## Fields you can read but must not quote

Three fields are the model's own words, never checked against the order:
`summary`, `agencies_tasked.task`, and `deadlines.due_description`. Only
`source_quote` is checked.

This matters. The summary of EO 14081 says the order creates an "Interagency
Technical Working Group" and a "Biosafety and Biosecurity Innovation Initiative."
Neither phrase appears anywhere in the order. Use `summary` to get your
bearings; quote `source_quote` as evidence.

**540 quotes were trimmed.** Sometimes the model quoted correctly and then
drifted in the last few words. Those quotes were cut back to the part that
matches the order, and `quote_trimmed = 1` marks them. The model's original
wording is kept in `raw_quotes`. So the model's own quoting accuracy is 94%,
even though every stored quote checks out.

---

## How much to trust the labels

Each order has two labels: **`primary_topic`** (what it is about, one of
fourteen subjects) and **`instrument`** (what it does, one of seven actions).

**The topic label is the weakest field.** When a stronger, more expensive model
read the same test orders, it chose a more specific subject about one time in
five. The cheap model tends to fall back on `government_administration` or
`foreign_policy` when it is unsure.

**Five topic labels were corrected by hand.** Five near-identical Railway Labor
Act orders had four different labels; they are now `labor_and_workforce`. On
those rows `primary_topic_as_extracted` holds the model's original label; it is
empty everywhere else. The corrections and reasons are in
`corrections/primary_topic.json`.

**`other` is a gap in the menu, not a fact about the orders.** 7.3% of orders
got `other` on one label or the other, above the 3% the project aimed for. Most
of those had a fitting category the model did not pick. The topic label is fine
at 1.8%; the instrument label carries 5.5%.

**The instrument label follows a fixed order of tests, not a judgment about
emphasis.** The first test that matches wins: creates a body → imposes
sanctions → revokes or amends → confers status or honour → delegates authority →
directs a report → adjusts pay or administration. A "body" is something with
members, like a council or task force; a program or initiative is not one.

**The labels were checked against 20 orders read by hand** and agreed 90% on
topic and 95% on instrument. Twenty orders is a spot check, not a measurement.

**The model does not give the same answer every time.** On the same 20 orders,
the same model and prompt scored 80% one run and 90% the next. Never describe
the checks as something the pipeline "always passes."

---

## Facts about the corpus that change your numbers

**Trump served two separate terms** (2017–21 and 2025–26) with Biden in
between. If you count his years from first order to last, you give him 2021–24
too and cut his rate by a third. Count only the years in which each president
signed, and treat the two terms separately where it matters.

```sql
-- orders per year in which the president actually signed
COUNT(*) * 1.0 / COUNT(DISTINCT substr(signing_date, 1, 4))
```

Orders per active year: Clinton 34 · G.W. Bush 32 · Obama 31 · Trump 45 44 ·
Biden 32 · Trump 47 277 orders in its first 19 months.

**The data starts in late 1993.** The Federal Register's full text begins with
EO 12890. Orders before that, back to 1937, are not here. Never call this "all
executive orders." Proclamations and memoranda are not here either.

**About one link in five points outside the data.** When an order revokes an
order from before 1994, or a proclamation, the target is not in this database,
so `target_document_number` is empty. The target's number is still recorded in
`target_eo_number`. Say so if you present the revocation network.

**Only about 40% of deadlines have a usable number of days.** Of 2,240
deadlines, 893 say something like "within 90 days" that can be turned into a
number. The rest are worded in ways that cannot, and they are not a random
sample. Say "of the deadlines that quantify," and quote "about 40%," because the
count depends on how you define a number of days (DATA_QUALITY §6.5).

**18% of agency names did not match a known agency, and that is mostly fine.**
Those 772 mentions are one-off commissions, boards and task forces that really
are their own thing. `agencies.matched = 0` marks them.

---

## Two columns that look alike and are not

| Column | What it holds |
|---|---|
| `relationships.source_quote` | words copied **from the order** itself (model rows only) |
| `relationships.fr_disposition_note` | a Federal Register editor's note **about** the order (Register rows only) |

A note like "Revokes: EO 12088, October 13, 1978" is the Register's summary and
appears nowhere in the order's text. If you treat the two columns as one, a
check that every quote is in its order drops from 100% to about 70%.

---

## Three checks to run before you publish

```python
# 1. Every stored quote really is in its order. Expect 8989 / 8989.
#    (A plain SQL text search reads 421, because the Register's punctuation
#    differs from the model's. This uses the project's matcher instead.)
import sqlite3
from eo.grounding import is_grounded     # run with PYTHONPATH=src
rows = sqlite3.connect("data/analysis.db").execute(
    "SELECT c.source_quote, t.body_text FROM all_claims c JOIN order_text t USING (document_number)"
).fetchall()
print(sum(is_grounded(q, b) for q, b in rows), "/", len(rows))
```

```sql
-- 2. Which model produced this, and what it covers
SELECT model, prompt_version, cost_usd, coverage FROM run_metadata;

-- 3. What is already known to be imperfect
SELECT kind, COUNT(*) FROM review_queue GROUP BY 1 ORDER BY 2 DESC;
```

Then, in whatever you write, say three things: **which model made the data**,
**that it may have missed things**, and **that the data starts in late 1993**.
Those three cover most of the ways this dataset gets overstated.
