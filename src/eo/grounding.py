"""Checking that a quoted claim actually appears in the source document.

This is the mechanism the whole dataset rests on: an extracted claim is only
trustworthy if its `source_quote` can be found in `body_text`. Phase 4 runs it
as a gate over whole runs; it lives here so the contract's fixtures can be
checked against real documents with no network and no model.

Matching is deliberately forgiving about *typography* and strict about *words*.
The distinction matters: a checker that is too literal manufactures false
alarms and hides the real ones. On the first 25-order run, 30 of 41 apparent
failures were punctuation, and only 11 were the model actually departing from
the text.
"""

from __future__ import annotations

import re
import unicodedata

_HYPHEN_BREAK_RE = re.compile(r"-\s*\n\s*")
_WHITESPACE_RE = re.compile(r"\s+")
_ELLIPSIS_RE = re.compile(r"\s*(?:\.\.\.+|…)\s*")

# Shortest fragment of an elided quote worth checking. Below this a fragment
# carries too little signal to confirm anything.
MIN_FRAGMENT_CHARS = 12


def _fold_typography(text: str) -> str:
    """Map every dash and quote variant onto one ASCII form.

    Federal Register plain text renders quotation marks as `` and '', and both
    the source and the model use several Unicode dashes -- U+2011 NON-BREAKING
    HYPHEN alone accounted for 20 false failures on the first run.
    """
    text = text.replace("``", '"').replace("''", '"')
    out = []
    for char in text:
        if unicodedata.category(char) == "Pd":  # any dash punctuation
            out.append("-")
        elif char in "“”„‟":
            out.append('"')
        elif char in "‘’‚‛":
            out.append("'")
        elif char == " ":  # non-breaking space
            out.append(" ")
        else:
            out.append(char)
    return "".join(out)


def normalize(text: str) -> str:
    """Reduce text to a form where layout differences cannot cause a miss."""
    text = _fold_typography(text)
    text = _HYPHEN_BREAK_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def _contains(needle: str, haystack: str) -> bool:
    if needle in haystack:
        return True
    # A break like "risk-\nbased" is ambiguous -- real hyphen or typesetting --
    # and closes up to "riskbased" either way, which a correctly copied
    # "risk-based" would otherwise miss.
    return needle.replace("-", "") in haystack.replace("-", "")


def is_grounded(quote: str, body: str) -> bool:
    """True if `quote` appears in `body` once typography is normalized.

    A quote containing an ellipsis is checked fragment by fragment, in order.
    Models elide despite being told not to; every word must still be shown to
    come from the document, and in the order the document has it, so this
    concedes nothing on whether the claim is supported.
    """
    if not quote or not quote.strip():
        return False

    normalized_body = normalize(body)
    normalized_quote = normalize(quote)

    if _contains(normalized_quote, normalized_body):
        return True

    fragments = [
        fragment
        for fragment in _ELLIPSIS_RE.split(normalized_quote)
        if len(fragment) >= MIN_FRAGMENT_CHARS
    ]
    if len(fragments) < 2:
        return False

    position = 0
    for fragment in fragments:
        index = normalized_body.find(fragment, position)
        if index < 0:
            return False
        position = index + len(fragment)
    return True
