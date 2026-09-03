"""Tests for the groundedness check.

The checker has to be forgiving about typography and strict about words. Both
halves are load-bearing: too literal and it buries real failures in false
alarms, too loose and it stops being evidence that a claim is supported.
"""

from __future__ import annotations

import pytest

from eo import grounding

BODY = (
    "Sec. 2. Establishment. The Secretary of Education shall, in consultation\n"
    "                with the other Co-Chairs of the Initiative, design a risk-\n"
    "                based approach. No later than 1 year after the date of this\n"
    "                order, the Co-Chairs shall submit a report to the President."
)


def test_exact_quote_is_grounded() -> None:
    assert grounding.is_grounded("The Secretary of Education shall", BODY)


def test_wrapped_and_indented_quote_is_grounded() -> None:
    """FR hard-wraps and indents; a substantively correct quote will not match
    the stored text character for character."""
    assert grounding.is_grounded(
        "The Secretary of Education shall, in consultation with the other Co-Chairs", BODY
    )


@pytest.mark.parametrize(
    "quote",
    [
        "in consultation with the other Co‑Chairs",  # non-breaking hyphen
        "in consultation with the other Co–Chairs",  # en dash
        "in consultation with the other Co—Chairs",  # em dash
    ],
)
def test_dash_variants_are_folded(quote: str) -> None:
    """U+2011 alone caused 20 false failures on the first 25-order run."""
    assert grounding.is_grounded(quote, BODY)


def test_hyphenated_line_break_is_closed_up() -> None:
    assert grounding.is_grounded("design a risk-based approach", BODY)


def test_federal_register_quote_marks_are_folded() -> None:
    body = "the term ``Secretary'' means the Secretary of Commerce"
    assert grounding.is_grounded('the term "Secretary" means', body)


def test_elided_quote_is_checked_fragment_by_fragment() -> None:
    """Models elide despite instructions. Every fragment must still be shown to
    come from the document, so this concedes nothing about support."""
    assert grounding.is_grounded(
        "The Secretary of Education shall ... submit a report to the President", BODY
    )


def test_elided_fragments_must_appear_in_order() -> None:
    """Ellipsis means 'text omitted', not 'reassembled in a new order'."""
    assert not grounding.is_grounded(
        "submit a report to the President ... The Secretary of Education shall", BODY
    )


def test_elision_cannot_smuggle_in_absent_text() -> None:
    assert not grounding.is_grounded(
        "The Secretary of Education shall ... invoke the Defense Production Act", BODY
    )


def test_fabricated_quote_is_rejected() -> None:
    assert not grounding.is_grounded(
        "invokes the Defense Production Act to expand domestic production", BODY
    )


def test_paraphrase_is_rejected() -> None:
    """The failure mode still present after the typography fixes: the quote
    opens correctly and then departs from the text."""
    assert not grounding.is_grounded(
        "The Secretary of Education shall consult widely and then decide", BODY
    )


def test_empty_quote_is_not_grounded() -> None:
    assert not grounding.is_grounded("", BODY)
    assert not grounding.is_grounded("   ", BODY)
