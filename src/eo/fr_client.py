"""Federal Register API client and raw-text cleaning.

Two hard-won rules from the v1 post-mortem live here:

1. Only the JSON API and `raw_text_url` are used. A plain GET of a
   federalregister.gov/documents/... HTML page returns a "Request Access"
   interstitial with HTTP 200, so `raise_for_status()` does not catch it. That
   is what silently starved v1 -- empty strings went to the model, which
   confabulated content for them.
2. `raw_text_url` is not clean text. It is the document wrapped in
   <html><head>..<body><pre>, around FR page furniture. `clean_raw_text`
   unwraps and strips it.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Iterator
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

API_URL = "https://www.federalregister.gov/api/v1/documents.json"
USER_AGENT = "eo-pipeline/0.1 (+https://github.com/; research use)"

# Requested explicitly: the API omits several of these unless asked.
FIELDS = [
    "document_number",
    "executive_order_number",
    "title",
    "president",
    "signing_date",
    "publication_date",
    "citation",
    "pdf_url",
    "raw_text_url",
    "full_text_xml_url",
    "disposition_notes",
    "agencies",
    "start_page",
    "end_page",
]

# Minimum plausible length for a real EO body. Anything shorter is treated as a
# failed fetch, never passed downstream.
MIN_BODY_CHARS = 500


class BlockedError(RuntimeError):
    """The site served a bot interstitial instead of the document."""


class EmptyBodyError(RuntimeError):
    """The fetch succeeded but produced too little text to be a real EO."""


_BLOCK_SIGNATURES = (
    "request access",
    "pardon our interruption",
    "you have been blocked",
    "enable javascript and cookies to continue",
)


def looks_blocked(text: str) -> bool:
    head = text[:4000].lower()
    return any(sig in head for sig in _BLOCK_SIGNATURES)


# --- cleaning ---------------------------------------------------------------

_PRE_RE = re.compile(r"<pre>(.*?)</pre>", re.IGNORECASE | re.DOTALL)
_ANCHOR_RE = re.compile(r"<a\s[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_HEADER_END_RE = re.compile(r"^\[FR Doc No:.*?\]\s*$", re.MULTILINE)
_TRAILER_RE = re.compile(r"^\[FR Doc\.\s.*", re.MULTILINE | re.DOTALL)

_DROP_LINE_RES = (
    re.compile(r"^\s*\[\[Page[^\]]*\]\]\s*$"),                  # [[Page 56737]]
    re.compile(r"^\s*\[Federal Register[^\]]*\]\s*$"),          # dateline repeat
    re.compile(r"^\s*\[Presidential Documents\]\s*$"),
    re.compile(r"^\s*\[Pages?\s+[^\]]*\]\s*$"),
    # Running head, which wraps onto a second line on modern documents.
    re.compile(r"^\s*Federal Register\s*/\s*Vol\..*$"),
    re.compile(r"^\s*Presidential Documents\s*$"),
    re.compile(r"^\s*From the Federal Register Online.*$"),
    re.compile(r"^\s*<GRAPHIC\(S\).*?>\s*$"),
    re.compile(r"^\s*Billing code\s+\S+\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{20,}\s*$"),                              # rule lines
    re.compile(r"^\s*_{20,}\s*$"),
)

_BLANKS_RE = re.compile(r"\n{3,}")

# The document proper opens with this line in every era sampled (1994 and 2026).
# Everything above it is masthead: volume, issue, part number, contents entry.
_DOC_START_RE = re.compile(
    r"^[ \t]*Executive Order\s+\d+\s+of\s+\w+\s+\d{1,2},\s+\d{4}\s*$",
    re.MULTILINE,
)


def clean_raw_text(payload: str) -> str:
    """Turn a raw_text_url response into the document body.

    Deliberately conservative: angle brackets appear in the text itself
    (signature placeholders like ``<Clinton1>``), so this never runs a general
    tag stripper -- only the known <pre> wrapper and <a> links are removed.
    """
    if looks_blocked(payload):
        raise BlockedError("response is a bot interstitial, not a document")

    match = _PRE_RE.search(payload)
    text = match.group(1) if match else payload

    text = _ANCHOR_RE.sub(r"\1", text)
    text = html.unescape(text)
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")

    # Everything above the "[FR Doc No: ...]" line is FR page furniture.
    header = _HEADER_END_RE.search(text)
    if header:
        text = text[header.end() :]

    # Everything from the "[FR Doc. 94-290 Filed ...]" colophon down is too.
    text = _TRAILER_RE.sub("", text)

    # Drop the masthead above the order's own heading. Falls through untouched
    # if the heading is not found, rather than risking cutting real content.
    start = _DOC_START_RE.search(text)
    if start:
        text = text[start.start() :]

    kept = [
        line.rstrip()
        for line in text.split("\n")
        if not any(rx.match(line) for rx in _DROP_LINE_RES)
    ]
    text = "\n".join(kept)

    text = _dedent(text)
    text = _BLANKS_RE.sub("\n\n", text)
    return text.strip()


def _dedent(text: str) -> str:
    """Remove the common left margin. FR indents bodies by ~16 spaces."""
    indents = [
        len(line) - len(line.lstrip(" ")) for line in text.split("\n") if line.strip()
    ]
    if not indents:
        return text
    margin = min(indents)
    if margin == 0:
        return text
    return "\n".join(
        line[margin:] if len(line) >= margin else line for line in text.split("\n")
    )


# --- HTTP -------------------------------------------------------------------

_RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)


def make_client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    )


@retry(
    retry=retry_if_exception_type(_RETRYABLE),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _get(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    response = client.get(url, **kwargs)
    response.raise_for_status()
    return response


def iter_documents(
    client: httpx.Client,
    *,
    per_page: int = 100,
    pause: float = 0.34,
) -> Iterator[dict[str, Any]]:
    """Yield EO metadata records oldest-first, paging the search API.

    The bracketed condition params must be sent URL-encoded; httpx handles that
    for us, but the shape below is the one that was verified to work.
    """
    page = 1
    while True:
        params: list[tuple[str, Any]] = [
            ("conditions[type][]", "PRESDOCU"),
            ("conditions[presidential_document_type][]", "executive_order"),
            ("per_page", per_page),
            ("page", page),
            ("order", "oldest"),
        ]
        params += [("fields[]", f) for f in FIELDS]

        payload = _get(client, API_URL, params=params).json()
        results = payload.get("results") or []
        if not results:
            return
        yield from results

        if page >= (payload.get("total_pages") or 0):
            return
        page += 1
        time.sleep(pause)


def fetch_body(client: httpx.Client, raw_text_url: str, *, pause: float = 0.2) -> str:
    """Fetch and clean one document body. Raises rather than returning junk."""
    response = _get(client, raw_text_url)
    time.sleep(pause)
    body = clean_raw_text(response.text)
    if len(body) < MIN_BODY_CHARS:
        raise EmptyBodyError(
            f"cleaned body is {len(body)} chars (< {MIN_BODY_CHARS}) for {raw_text_url}"
        )
    return body
