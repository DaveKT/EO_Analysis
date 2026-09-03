"""Checking that a quoted claim actually appears in the source document.

This is the mechanism the whole dataset rests on: an extracted claim is only
trustworthy if its `source_quote` can be found in `body_text`. Phase 4 runs it
as a gate over whole runs; it lives here so the contract's fixtures can be
checked against real documents with no network and no model.

Matching is deliberately forgiving about layout and strict about words. The
Federal Register hard-wraps its text at a fixed column and indents bodies by
about sixteen spaces, so a quote that is correct in substance will differ from
the stored text in whitespace. It also hyphenates across line breaks, which is
why `-\\n` is closed up rather than collapsed to a space.
"""

from __future__ import annotations

import re

_HYPHEN_BREAK_RE = re.compile(r"-\s*\n\s*")
_WHITESPACE_RE = re.compile(r"\s+")
# FR renders quotation marks as `` and '' in plain text; models tend to emit
# real quotation marks when copying. Fold both to a single form.
_QUOTE_CHARS = {
    "``": '"', "''": '"', "“": '"', "”": '"',
    "‘": "'", "’": "'", "—": "-", "–": "-",
}


def normalize(text: str) -> str:
    """Reduce text to a form where layout differences cannot cause a miss."""
    text = _HYPHEN_BREAK_RE.sub("", text)
    for old, new in _QUOTE_CHARS.items():
        text = text.replace(old, new)
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def is_grounded(quote: str, body: str) -> bool:
    """True if `quote` appears in `body` once layout is normalized.

    Falls back to a hyphen-insensitive comparison. A break like "risk-\nbased"
    is ambiguous -- it may be a real hyphen or only a typesetting break -- and
    closing it up yields "riskbased" either way, which a correctly copied quote
    of "risk-based" would otherwise miss.
    """
    if not quote or not quote.strip():
        return False
    normalized_quote, normalized_body = normalize(quote), normalize(body)
    if normalized_quote in normalized_body:
        return True
    return normalized_quote.replace("-", "") in normalized_body.replace("-", "")
