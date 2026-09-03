"""The Phase 2 exit gate.

Three hand-written extractions -- written by a human reading the orders, not by
a model -- must validate against the contract, and every quote in them must be
findable in the real document text. No API calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from eo import fr_client, grounding
from eo.models import Domain, Extraction, Instrument, extraction_json_schema

FIXTURES = Path(__file__).parent / "fixtures"
EXTRACTIONS = sorted((FIXTURES / "extractions").glob("*.json"))

# Each hand-labelled extraction is paired with the raw document it describes.
RAW_FOR_EO = {
    12890: "raw_1994_94-290.txt",
    13636: "raw_2013_2013-03915.txt",
    14423: "raw_2026_2026-18141.txt",
}


def load_fixture(path: Path) -> tuple[int, dict]:
    payload = json.loads(path.read_text())
    eo_number = payload["_eo_number"]
    data = {k: v for k, v in payload.items() if not k.startswith("_")}
    return eo_number, data


@pytest.mark.parametrize("path", EXTRACTIONS, ids=lambda p: p.stem)
def test_hand_written_extraction_validates(path: Path) -> None:
    _, data = load_fixture(path)
    Extraction.model_validate(data)


@pytest.mark.parametrize("path", EXTRACTIONS, ids=lambda p: p.stem)
def test_every_quote_is_grounded_in_the_real_document(path: Path) -> None:
    """The check the dataset rests on. v1's tariff-order-as-COVID-order failure
    would have been caught here."""
    eo_number, data = load_fixture(path)
    body = fr_client.clean_raw_text((FIXTURES / RAW_FOR_EO[eo_number]).read_text())
    extraction = Extraction.model_validate(data)

    claims = [
        *extraction.agencies_tasked,
        *extraction.deadlines,
        *extraction.authorities,
        *extraction.relationships,
    ]
    assert claims, f"EO {eo_number} fixture has no grounded claims to check"
    for claim in claims:
        assert grounding.is_grounded(claim.source_quote, body), (
            f"quote not found in EO {eo_number}: {claim.source_quote[:80]!r}"
        )


def test_a_quote_that_is_not_in_the_document_is_rejected() -> None:
    body = fr_client.clean_raw_text((FIXTURES / RAW_FOR_EO[12890]).read_text())
    assert not grounding.is_grounded(
        "invokes the Defense Production Act to expand domestic production", body
    )


def test_normalisation_survives_wrapping_and_hyphenation() -> None:
    body = "the Secretary shall establish a risk-\n                based approach\n   to review"
    assert grounding.is_grounded("shall establish a risk-based approach to review", body)


def test_other_without_a_reason_is_flagged_not_discarded() -> None:
    """JSON Schema cannot express "required only when the value is `other`", so
    the model never sees this rule. Enforcing it by throwing away the whole
    extraction cost two orders of ~20 verified claims each on the 100-order
    run; it goes to the review queue instead."""
    _, data = load_fixture(FIXTURES / "extractions" / "eo_14423.json")
    data["primary_topic"] = Domain.OTHER.value
    data["secondary_topics"] = []
    data["topic_other_reason"] = None

    extraction = Extraction.model_validate(data)

    assert extraction.primary_topic is Domain.OTHER
    assert extraction.missing_other_reasons == ["primary_topic"]
    assert extraction.agencies_tasked  # the real work survives


def test_other_with_a_reason_is_not_flagged() -> None:
    _, data = load_fixture(FIXTURES / "extractions" / "eo_14423.json")
    data["primary_topic"] = Domain.OTHER.value
    data["secondary_topics"] = []
    data["topic_other_reason"] = "a genuine gap in the vocabulary"

    assert Extraction.model_validate(data).missing_other_reasons == []


def test_other_instrument_without_a_reason_is_flagged() -> None:
    _, data = load_fixture(FIXTURES / "extractions" / "eo_12890.json")
    data["instrument"] = Instrument.OTHER.value

    assert Extraction.model_validate(data).missing_other_reasons == ["instrument"]


def test_repeated_topic_is_normalised_away_not_rejected() -> None:
    """Redundancy is not a contradiction. Rejecting it threw away an otherwise
    sound extraction over a rule the model was never given."""
    _, data = load_fixture(FIXTURES / "extractions" / "eo_13636.json")
    primary = data["primary_topic"]
    data["secondary_topics"] = [primary, "health", "health"]

    extraction = Extraction.model_validate(data)

    assert extraction.primary_topic.value == primary
    assert [t.value for t in extraction.secondary_topics] == ["health"]


def test_too_many_secondary_topics_is_still_rejected() -> None:
    _, data = load_fixture(FIXTURES / "extractions" / "eo_13636.json")
    data["secondary_topics"] = ["health", "trade", "immigration", "civil_rights"]
    with pytest.raises(ValidationError, match="at most"):
        Extraction.model_validate(data)


def test_claims_without_a_usable_quote_are_rejected() -> None:
    _, data = load_fixture(FIXTURES / "extractions" / "eo_12890.json")
    data["authorities"][0]["source_quote"] = "too short"
    with pytest.raises(ValidationError):
        Extraction.model_validate(data)


def test_schema_is_strict_mode_compatible() -> None:
    """Providers require additionalProperties:false and every property listed
    in `required`, on every object in the schema."""
    schema = extraction_json_schema()

    def check(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)


def test_topic_and_instrument_vocabularies_are_closed() -> None:
    """The vocabularies are locked before the full run; changing them after
    invalidates cross-run comparison. This pins them."""
    assert len(Domain) == 15  # 14 domains + other
    assert len(Instrument) == 8  # 7 instruments + other
