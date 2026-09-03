"""The extraction contract.

This module is the JSON schema handed to the provider as a structured-output
spec. It exists because v1 asked for prose in a fixed shape and then parsed it
with `line.startswith("sentiment:")`, which markdown bold defeated: 47 of 143
rows came back blank. Nothing here is parsed out of prose.

Two axes describe each order, decided 2026-09-03 (PLAN.md section 8):
  * `primary_topic` -- what the order is *about* (13 domains)
  * `instrument`    -- what the order *does* (6 kinds)
The second axis exists because this corpus is not shaped like a generic policy
taxonomy: roughly 200 of 1,534 orders establish councils or task forces, 78
block property, and 49 set agency succession. Domain alone files all of those
under vague buckets.

`education` was added on 2026-09-03 after hand-labelling exposed the gap: 52
orders are education-related and had no home among the original 12.

Both axes admit `other`, which requires a written reason. That is deliberate:
a forced choice would make a vocabulary gap indistinguishable from a good fit,
and Phase 4 fails any run where `other` exceeds 3%.

A `significance` field was removed on 2026-09-03. Asked to rate 25 orders, the
model returned 19 `major`, 6 `substantive` and no `routine` -- including a
one-sentence order changing a council's membership from 25 to 30. It agreed
with hand labels 0% of the time. Unlike every other field here it had no
textual referent, so it could not be checked against the source: it was the
"vibes" category the v1 post-mortem blamed for unfalsifiable output.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

MAX_SECONDARY_TOPICS = 3


class Domain(str, Enum):
    """What the order is about."""

    FOREIGN_POLICY = "foreign_policy"
    SECURITY_AND_DEFENSE = "security_and_defense"
    ECONOMY_AND_FINANCE = "economy_and_finance"
    TRADE = "trade"
    IMMIGRATION = "immigration"
    ENERGY_AND_ENVIRONMENT = "energy_and_environment"
    HEALTH = "health"
    CIVIL_RIGHTS = "civil_rights"
    LABOR_AND_WORKFORCE = "labor_and_workforce"
    GOVERNMENT_ADMINISTRATION = "government_administration"
    JUSTICE_AND_LAW_ENFORCEMENT = "justice_and_law_enforcement"
    TECHNOLOGY_AND_RESEARCH = "technology_and_research"
    EDUCATION = "education"
    OTHER = "other"


class Instrument(str, Enum):
    """What the order does.

    Exactly one per order. When an order does several things, the precedence
    rule in the prompt decides: establishing a new entity outranks everything
    else, then sanctions, then revoking/amending, then delegation, then
    reports, then pay and administration. The rule exists because "the action
    the order is mostly devoted to" is not reproducible -- two careful readers
    split on EO 13985, which both creates a working group and directs
    government-wide equity assessments.
    """

    CREATES_BODY = "creates_body"
    DELEGATES_AUTHORITY = "delegates_authority"
    IMPOSES_SANCTIONS = "imposes_sanctions"
    REVOKES_OR_AMENDS = "revokes_or_amends"
    DIRECTS_REPORT_OR_STUDY = "directs_report_or_study"
    ADJUSTS_PAY_OR_ADMIN = "adjusts_pay_or_admin"
    OTHER = "other"


class RelationKind(str, Enum):
    """Relations the model may assert from the order's own text.

    Outbound only: the body can say what this order does to earlier orders, but
    it cannot know what later orders will do to it. Inbound relations come from
    the Federal Register's disposition notes instead (see dispositions.py).
    """

    REVOKES = "revokes"
    AMENDS = "amends"
    SUPERSEDES = "supersedes"
    CONTINUES = "continues"
    REFERENCES = "references"


class Grounded(BaseModel):
    """Base for every extracted claim.

    `source_quote` is the anti-hallucination mechanism, not documentation:
    Phase 4 checks the quote actually appears in the document body. v1's
    headline failure -- a tariff order summarized as a COVID Defense Production
    Act order -- would have been caught automatically by this one field.
    """

    source_quote: str = Field(
        description=(
            "Verbatim span from the order's text supporting this claim. Must be "
            "copied exactly from the document, not paraphrased."
        ),
        min_length=10,
    )


class AgencyTask(Grounded):
    agency_name: str = Field(
        description="Agency or official as named in the order, e.g. "
        "'Secretary of the Treasury', 'Department of Homeland Security'."
    )
    task: str = Field(description="What this order directs them to do.")


class Deadline(Grounded):
    due_description: str = Field(
        description="The deadline as the order states it, e.g. 'within 90 days'."
    )
    due_date: str | None = Field(
        description="ISO date (YYYY-MM-DD) if the order gives a calendar date or "
        "one can be computed from the signing date. Null if open-ended."
    )
    responsible_party: str | None = Field(
        description="Who must meet the deadline, if the order says."
    )


class Authority(Grounded):
    authority: str = Field(
        description="A statute or constitutional provision invoked, e.g. "
        "'International Emergency Economic Powers Act (50 U.S.C. 1701)'."
    )


class ModelRelationship(Grounded):
    relation: RelationKind
    target_eo_number: int | None = Field(
        description="EO number of the target order, if it is an executive order."
    )
    target_label: str = Field(
        description="The target as named in the text, e.g. 'Executive Order "
        "13769' or 'Proclamation 9704'."
    )


class Extraction(BaseModel):
    """One model's reading of one executive order."""

    summary: str = Field(
        description="Two to four sentences: what the order does, who it binds, "
        "and what changes. Describe the order's content, never its tone.",
        min_length=40,
    )

    primary_topic: Domain
    topic_other_reason: str | None = Field(
        description="Required when primary_topic is 'other': what the order is "
        "about, in a few words. Null otherwise."
    )
    secondary_topics: list[Domain] = Field(
        # max_length so the limit reaches the provider as `maxItems`. Keeping it
        # only in the validator below meant the model never saw the rule and
        # three of the first 25 extractions were rejected for breaking it.
        max_length=MAX_SECONDARY_TOPICS,
        description=f"Up to {MAX_SECONDARY_TOPICS} further domains. May be empty.",
    )

    instrument: Instrument
    instrument_other_reason: str | None = Field(
        description="Required when instrument is 'other': what the order does, "
        "in a few words. Null otherwise."
    )

    agencies_tasked: list[AgencyTask]
    deadlines: list[Deadline]
    authorities: list[Authority]
    relationships: list[ModelRelationship]

    @property
    def missing_other_reasons(self) -> list[str]:
        """Axes answered `other` without the justification that was asked for.

        This used to raise, which threw away the whole extraction -- twenty-odd
        verified claims -- over a missing sentence. JSON Schema cannot express
        "required only when the value is `other`", so the model never saw the
        rule; a constraint it cannot see must not be enforced by discarding its
        work. Phase 4 routes these to the review queue instead.
        """
        missing = []
        if self.primary_topic is Domain.OTHER and not (self.topic_other_reason or "").strip():
            missing.append("primary_topic")
        if self.instrument is Instrument.OTHER and not (
            self.instrument_other_reason or ""
        ).strip():
            missing.append("instrument")
        return missing

    @model_validator(mode="after")
    def _secondary_topics_are_distinct(self) -> Extraction:
        """Normalise rather than reject.

        A duplicate, or the primary topic repeated in the secondary list, is
        redundancy and not a contradiction -- the topic is already recorded. An
        earlier version raised here, which threw away an otherwise sound
        extraction of EO 13489 over a repeated value the model was never told
        to avoid. Constraints the model cannot see belong in the schema; this
        one is simply cleaned up.
        """
        if len(self.secondary_topics) > MAX_SECONDARY_TOPICS:
            raise ValueError(f"at most {MAX_SECONDARY_TOPICS} secondary topics")

        seen: set[Domain] = {self.primary_topic}
        deduped: list[Domain] = []
        for topic in self.secondary_topics:
            if topic not in seen:
                seen.add(topic)
                deduped.append(topic)
        object.__setattr__(self, "secondary_topics", deduped)
        return self


def _make_strict(node: Any) -> Any:
    """Recursively conform a JSON schema to structured-output strict mode.

    Providers require every object to set additionalProperties:false and to list
    every property in `required`; optional fields are expressed as nullable
    types instead of omitted keys.
    """
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        node.pop("default", None)
        return {k: _make_strict(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_make_strict(v) for v in node]
    return node


def extraction_json_schema() -> dict[str, Any]:
    """The schema sent to the provider."""
    return _make_strict(Extraction.model_json_schema())
