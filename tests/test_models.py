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


def test_other_topic_requires_a_reason() -> None:
    _, data = load_fixture(FIXTURES / "extractions" / "eo_14423.json")
    data["primary_topic"] = Domain.OTHER.value
    data["secondary_topics"] = []
    data["topic_other_reason"] = None
    with pytest.raises(ValidationError, match="topic_other_reason"):
        Extraction.model_validate(data)

    data["topic_other_reason"] = "a genuine gap in the vocabulary"
    assert Extraction.model_validate(data).primary_topic is Domain.OTHER


def test_other_instrument_requires_a_reason() -> None:
    _, data = load_fixture(FIXTURES / "extractions" / "eo_12890.json")
    data["instrument"] = Instrument.OTHER.value
    with pytest.raises(ValidationError, match="instrument_other_reason"):
        Extraction.model_validate(data)


def test_primary_topic_may_not_repeat_in_secondary_topics() -> None:
    _, data = load_fixture(FIXTURES / "extractions" / "eo_13636.json")
    data["secondary_topics"] = [data["primary_topic"]]
    with pytest.raises(ValidationError, match="repeated"):
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
    assert len(Domain) == 14  # 13 domains + other
    assert len(Instrument) == 7  # 6 instruments + other
