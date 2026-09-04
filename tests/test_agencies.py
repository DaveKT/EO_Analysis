"""Tests for agency-name normalisation.

The cases here are the ones that make a naive implementation wrong. Splitting
on "and" destroys `Department of Health and Human Services`. Bucketing the long
tail hides 800-odd real one-off commissions. And folding every "all ... agencies"
phrasing together turns "all contracting agencies" into a claim that the whole
executive branch was tasked.
"""

from __future__ import annotations

import pytest

from eo import agencies


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Secretary of the Treasury", "Department of the Treasury"),
        ("Department of the Treasury", "Department of the Treasury"),
        ("the Treasury", "Department of the Treasury"),
        ("Attorney General", "Department of Justice"),
        ("Office of Personnel Management (OPM)", "Office of Personnel Management"),
        ("Director of OMB", "Office of Management and Budget"),
        ("Director of the Office of Management and Budget",
         "Office of Management and Budget"),
        ("United States Trade Representative",
         "Office of the United States Trade Representative"),
    ],
)
def test_variants_of_one_institution_collapse(raw, expected):
    names, matched = agencies.resolve(raw)
    assert names == [expected]
    assert matched


def test_a_name_containing_and_is_not_split():
    """The reason resolution is alias lookup rather than splitting."""
    for raw in (
        "Department of Health and Human Services",
        "Secretary of Health and Human Services",
        "Department of Housing and Urban Development",
        "Office of Science and Technology Policy",
    ):
        names, matched = agencies.resolve(raw)
        assert len(names) == 1, f"{raw} was split into {names}"
        assert matched


def test_a_compound_row_resolves_to_every_agency_it_names():
    names, matched = agencies.resolve(
        "Attorney General and Secretary of Homeland Security"
    )
    assert sorted(names) == [
        "Department of Homeland Security",
        "Department of Justice",
    ]
    assert matched


def test_compound_with_an_and_containing_name():
    """Both halves must survive: one of them contains "and" itself."""
    names, _ = agencies.resolve(
        "Secretary of Health and Human Services and the Attorney General"
    )
    assert sorted(names) == [
        "Department of Health and Human Services",
        "Department of Justice",
    ]


def test_parentheticals_are_dropped():
    names, _ = agencies.resolve(
        "Secretary of the Treasury (in consultation with the Secretary of State)"
    )
    assert names == ["Department of the Treasury"]


@pytest.mark.parametrize(
    "raw",
    ["All Federal Agencies", "all federal agencies", "each federal agency",
     "All executive departments and agencies", "heads of executive agencies",
     "executive agencies", "All U.S. Government agencies"],
)
def test_unqualified_collectives_collapse(raw):
    names, matched = agencies.resolve(raw)
    assert names == [agencies.COLLECTIVE_NAME]
    assert matched


@pytest.mark.parametrize(
    "raw",
    ["all contracting agencies", "all rulemaking agencies",
     "heads of federal permitting agencies", "all initiative member agencies",
     "federal property managing agencies", "all three agencies"],
)
def test_qualified_subsets_are_not_the_collective(raw):
    """A qualifier is the whole point: "all contracting agencies" is not every
    agency, and merging it in would overstate who was tasked."""
    names, _ = agencies.resolve(raw)
    assert names != [agencies.COLLECTIVE_NAME]


def test_unrecognised_names_become_their_own_entity_not_a_bucket():
    raw = "Interagency Task Force on Returning Global War on Terror Heroes"
    names, matched = agencies.resolve(raw)
    assert names == [raw]
    assert not matched


def test_war_and_defense_are_one_institution():
    """EO 14347 (2025-09-05) renamed Defense to War. It is one department across
    the rename, so both names resolve to one canonical agency. The rename is not
    lost -- the raw name each order used is preserved on the mention."""
    for raw in ("Secretary of War", "Department of War",
                "Secretary of Defense", "Department of Defense"):
        assert agencies.resolve(raw)[0] == ["Department of Defense"], raw


def test_military_departments_stay_distinct_from_defense():
    assert agencies.resolve("Department of the Army")[0] == ["Department of the Army"]
    assert agencies.resolve("Department of the Navy")[0] == ["Department of the Navy"]


def test_empty_and_blank_names_resolve_to_nothing():
    assert agencies.resolve("")[0] == []
    assert agencies.resolve("   ")[0] == []
    assert agencies.resolve("(consultation only)")[0] == []


def test_kinds_are_assigned():
    assert agencies.kind_of("Department of State") == agencies.DEPARTMENT
    assert agencies.kind_of("Office of Management and Budget") == agencies.OFFICE
    assert agencies.kind_of(agencies.COLLECTIVE_NAME) == agencies.COLLECTIVE
    assert agencies.kind_of("Some One-Off Commission") == agencies.BODY


def test_alias_matching_is_word_bounded():
    """`dod` must not match inside another word."""
    names, matched = agencies.resolve("Dodson County Advisory Board")
    assert names == ["Dodson County Advisory Board"]
    assert not matched


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Archivist", "National Archives and Records Administration"),
        ("Archivist of the United States", "National Archives and Records Administration"),
        ("FAR Council", "Federal Acquisition Regulatory Council"),
        ("Chairman of the Federal Trade Commission", "Federal Trade Commission"),
        ("Postmaster General", "United States Postal Service"),
        ("Director of Central Intelligence", "Central Intelligence Agency"),
        ("Administrator of USAID", "United States Agency for International Development"),
        ("FDA Commissioner", "Food and Drug Administration"),
        ("Commissioner of Social Security", "Social Security Administration"),
        ("NIST Director", "National Institute of Standards and Technology"),
    ],
)
def test_added_agencies_merge_their_variants(raw, expected):
    """Each of these was splitting a standing agency across spellings."""
    names, matched = agencies.resolve(raw)
    assert names == [expected]
    assert matched


@pytest.mark.parametrize(
    "raw",
    ["Task Force", "the Commission", "Board", "Committee", "Working Group",
     "Emergency Board", "Parties to the Dispute", "Interagency Working Group",
     "Secretary", "Executive Committee"],
)
def test_bare_references_are_flagged_generic(raw):
    """A bare organisational noun refers to a body created inside its own order."""
    names, _ = agencies.resolve(raw)
    assert agencies.kind_of(names[0]) == agencies.GENERIC


@pytest.mark.parametrize(
    "raw",
    ["Great Lakes Interagency Task Force", "Water Subcabinet",
     "Gulf Coast Ecosystem Restoration Task Force",
     "Task Force on Environmental Health Risks and Safety Risks to Children"],
)
def test_named_bodies_are_not_flagged_generic(raw):
    """A distinguishing proper name makes it a real, countable entity."""
    names, _ = agencies.resolve(raw)
    assert agencies.kind_of(names[0]) == agencies.BODY


def test_generic_references_are_not_collapsed_together():
    """Two orders' "Task Force" are different task forces. Flagging them is
    right; merging them would invent a body that spans unrelated orders."""
    assert agencies.resolve("Task Force")[0] != agencies.resolve("Commission")[0]
    assert agencies.resolve("Task Force")[0] == ["Task Force"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("President", "The President"),
        ("the President", "The President"),
        ("President of the United States", "The President"),
        ("President (via the Secretary of Commerce)", "The President"),
        ("Office of the President", "The President"),
    ],
)
def test_the_president_resolves_from_its_own_forms(raw, expected):
    names, matched = agencies.resolve(raw)
    assert names == [expected]
    assert matched


@pytest.mark.parametrize(
    "raw",
    ["President's Council of Advisors on Science and Technology (PCAST)",
     "President\u2019s Task Force on 21st Century Policing",
     "President of the Export-Import Bank",
     "Chair of the President's Council on Year 2000 Conversion"],
)
def test_the_president_does_not_absorb_bodies_named_after_the_office(raw):
    """Before the guard, 105 of 193 mentions credited to the President were
    councils, task forces and officials whose title merely contains the word."""
    names, _ = agencies.resolve(raw)
    assert "The President" not in names, names


def test_a_presidential_assistant_is_not_the_president():
    names, matched = agencies.resolve("Assistant to the President for Domestic Policy")
    assert names == ["Assistant to the President for Domestic Policy"]
    assert matched
    names, matched = agencies.resolve("Senior Counselor to the President for Trade")
    assert names == ["Senior Counselor to the President for Trade"]
    assert not matched


def test_possessives_survive_into_the_canonical_name():
    names, matched = agencies.resolve("President's Board of Advisors on Tribal Colleges")
    assert names == ["President's Board of Advisors on Tribal Colleges"]
    assert not matched
