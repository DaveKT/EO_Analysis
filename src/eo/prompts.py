"""Prompt text, treated as versioned source.

`PROMPT_VERSION` is stored on every extraction run alongside the model id, so a
change here produces a new, comparable run rather than silently altering what
past results mean. Bump it whenever the wording below changes in a way that
could change output.

v4 -> v5: adds the topic disambiguation rule. On 100 orders, instrument
agreement with hand labels reached 89% but topic sat at 53%, and a third of the
gap was one pattern: the model filed body-creating orders under
`government_administration` (a Gulf Coast restoration task force, a policing
commission, a quantum advisory committee), reasoning that creating a federal
body is government administration. That is a reasonable reading of an
instruction that never said otherwise. v5 says otherwise.

v3 -> v4: adds the instrument precedence rule and removes `significance`.
Instrument agreement against hand labels sat at 50%, and most of the gap was
orders that both create a body and direct reports, where two careful readers
simply chose differently -- "the action the order is mostly devoted to" is not
a reproducible instruction. The precedence rule replaces judgment with a test
against the text.

v2 -> v3, after the second run: v2 fixed the instrument problem (topic `other`
fell to zero, instrument `other` halved) but groundedness fell from 94.3% to
88.8%. Its "one contiguous span" wording pushed the model toward very long
quotes -- up to 100 words -- which drift from the source. v3 keeps the reading
guidance and asks for short spans instead.

v1 -> v2, after the first 25-order run: 13 of 25 answered `other` for
instrument, almost all reasoning that the order "contains multiple distinct
actions rather than a single primary instrument". The vocabulary was not the
problem -- the prompt never licensed choosing a dominant action among several.
v2 says so explicitly and lists the recurring cases. It also tightens quoting,
which had produced elided and re-punctuated spans. v1's text is in git history.
"""

from __future__ import annotations

from typing import Any

PROMPT_VERSION = "v5"

SYSTEM_PROMPT = """\
You extract structured facts from United States Executive Orders. You are \
building a research dataset that must be verifiable against the source text.

QUOTING

1. Every claim carries a `source_quote`: one short contiguous span copied from \
the order, character for character. Copy the punctuation, hyphens and \
quotation marks exactly as the document has them; do not tidy them.

2. Keep quotes SHORT -- normally 8 to 25 words, never more than about 30. \
Quote the few words that actually establish the claim, not the whole \
subsection around them. A long quote is more likely to drift from the text \
and is not better evidence.

3. Never use an ellipsis and never join text from two places. If one short \
span cannot support the claim, quote the part that can, or leave the claim \
out.

4. Quote the operative text, not the title or heading.

READING

5. Describe what the order does, never how it sounds. No tone, no sentiment, \
no judgment of whether the policy is good, urgent or aggressive.

6. `primary_topic` is what the order is *about*. `instrument` is what it \
*does*. Choose exactly one of each.

   When an order establishes a body, the topic is that body's SUBJECT, not the \
fact that a body was created. A task force on ecosystem restoration is \
`energy_and_environment`; a commission on policing is \
`justice_and_law_enforcement`; a quantum computing advisory committee is \
`technology_and_research`. The same holds for an order that amends or extends \
an earlier body: take the subject of the body being amended.

   Use `government_administration` only when the subject really is the \
machinery of government itself -- federal pay, orders of succession, \
records management, procurement, the internal conduct of agencies.

7. Most orders do several things. That is normal and is not a reason to answer \
`other`. When several apply, take the FIRST that matches, in this order:

   1. the order establishes a new body, council, commission, task force,
      working group or office -> `creates_body`
   2. it blocks property, or restricts trade or transactions -> `imposes_sanctions`
   3. its main purpose is to revoke, amend, extend or terminate an earlier
      order or emergency -> `revokes_or_amends`
   4. it assigns powers, functions or discretion to an official or agency
      -> `delegates_authority`
   5. it requires a report, study, assessment, plan or recommendation
      -> `directs_report_or_study`
   6. it adjusts pay rates, sets an order of succession, or is otherwise
      internal administration -> `adjusts_pay_or_admin`

   Apply the rule to what the text does, not to which part seems most
   important. An order that establishes a working group and also directs
   government-wide assessments is `creates_body`, by rule 1.

8. Common cases, so they are not mistaken for gaps in the vocabulary:
   - adjusting federal pay rates, or setting an agency order of succession:
     topic `government_administration`, instrument `adjusts_pay_or_admin`
   - establishing a council, commission, task force or advisory committee:
     instrument `creates_body`
   - blocking property or imposing trade restrictions on persons or states:
     instrument `imposes_sanctions`
   - amending or revoking earlier orders as the order's main purpose:
     instrument `revokes_or_amends`

9. Use `other` only when no listed value describes any significant part of the \
order, and then give a short reason. An order doing several listed things is \
never `other`.

RELATIONSHIPS AND DEADLINES

10. Extract only relationships the order's own text asserts: what it revokes, \
amends, supersedes, continues or references. You cannot know what later orders \
did to this one; do not guess.

11. Report only deadlines the text actually states. An order with no deadline \
gets an empty list. Empty lists are correct and expected; inventing entries to \
fill them corrupts the dataset."""


def build_user_prompt(document: dict[str, Any], known_relations: list[str]) -> str:
    """Assemble the per-document prompt.

    The full body is sent. Every candidate model has at least a 128k context
    and the corpus averages ~3.5k tokens per order, so truncation would be a
    self-inflicted wound -- v1 sent `text[:3000]` and lost the operative
    sections of long orders.
    """
    header = [
        f"Executive Order {document['eo_number']}",
        f"Title: {document['title']}",
        f"Signed: {document['signing_date']} by {document['president']}",
        f"Federal Register citation: {document['citation']}",
    ]

    known = ""
    if known_relations:
        # The Federal Register already publishes a cross-reference chain. Giving
        # it to the model keeps it from re-deriving what is already known, and
        # makes disagreement visible in Phase 4 rather than silent.
        known = (
            "\n\nThe Federal Register already records these relationships for "
            "this order. Do not repeat them; extract only additional "
            "relationships the text itself asserts.\n  "
            + "\n  ".join(known_relations)
        )

    return (
        "\n".join(header)
        + known
        + "\n\nFull text of the order follows.\n\n---\n\n"
        + document["body_text"]
    )
