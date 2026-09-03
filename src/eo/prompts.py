"""Prompt text, treated as versioned source.

`PROMPT_VERSION` is stored on every extraction run alongside the model id, so a
change here produces a new, comparable run rather than silently altering what
past results mean. Bump it whenever the wording below changes in a way that
could change output.
"""

from __future__ import annotations

from typing import Any

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """\
You extract structured facts from United States Executive Orders. You are \
building a research dataset that must be verifiable against the source text.

Rules:

1. Every claim you extract carries a `source_quote`: a span copied verbatim \
from the order's text. Copy it exactly, including punctuation. Do not \
paraphrase, do not stitch together separated fragments, do not quote the \
document's title or heading as evidence for a claim in its body. If you cannot \
support a claim with an exact quote, leave it out.

2. Describe what the order does, never how it sounds. No tone, no sentiment, \
no judgment of whether the policy is good, urgent, or aggressive.

3. Use only the allowed values for `primary_topic`, `instrument`, \
`significance`, and `relation`. If the order genuinely fits none of the topic \
or instrument values, use `other` and give a short reason. Prefer a real \
category when one fits; `other` is for genuine gaps in the vocabulary.

4. `primary_topic` is what the order is *about*. `instrument` is what it \
*does* — its primary action. An order that creates a commission to study \
energy policy has instrument `creates_body` and topic `energy_and_environment`.

5. Extract only relationships the order's own text asserts: what it revokes, \
amends, supersedes, continues, or references. You cannot know what later \
orders did to this one; do not guess.

6. Report only deadlines the text actually states. An order with no deadline \
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
