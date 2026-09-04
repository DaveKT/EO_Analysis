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


def test_war_is_not_merged_into_defense():
    """EO 14347 renamed Defense to War in 2025. Collapsing them would erase a
    real change on nothing but this module's assumption."""
    assert agencies.resolve("Secretary of War")[0] == ["Department of War"]
    assert agencies.resolve("Secretary of Defense")[0] == ["Department of Defense"]


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
