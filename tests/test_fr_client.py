"""Tests for the Federal Register client's parsing and guard rails.

These run against saved fixtures -- no network -- so they keep working when the
site is unreachable, and they pin the exact failure modes that broke v1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eo import fr_client

FIXTURES = Path(__file__).parent / "fixtures"
RAW_FIXTURES = sorted(FIXTURES.glob("raw_*.txt"))


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("path", RAW_FIXTURES, ids=lambda p: p.stem)
def test_clean_raw_text_strips_all_wrapper_markup(path: Path) -> None:
    body = fr_client.clean_raw_text(path.read_text(encoding="utf-8"))

    for marker in ("<pre>", "</pre>", "<html>", "<body>", "<head>", "<title>"):
        assert marker not in body.lower(), f"{marker} survived cleaning"
    assert "\x00" not in body
    assert "[[Page" not in body
    assert "[FR Doc" not in body
    assert "Billing code" not in body
    assert "gpo.gov" not in body


@pytest.mark.parametrize("path", RAW_FIXTURES, ids=lambda p: p.stem)
def test_clean_raw_text_opens_at_the_order_heading(path: Path) -> None:
    body = fr_client.clean_raw_text(path.read_text(encoding="utf-8"))
    assert body.startswith("Executive Order "), body[:80]
    assert len(body) >= fr_client.MIN_BODY_CHARS


def test_clean_raw_text_keeps_real_content() -> None:
    body = fr_client.clean_raw_text(load("raw_2013_2013-03915.txt"))
    assert "Improving Critical Infrastructure Cybersecurity" in body
    assert "By the authority vested in me as President" in body
    assert "THE WHITE HOUSE" in body


def test_angle_bracket_text_is_not_treated_as_markup() -> None:
    """FR uses <Clinton1> style placeholders for signatures. A general tag
    stripper would eat them; only <pre> and <a> may be removed."""
    body = fr_client.clean_raw_text(load("raw_1994_94-290.txt"))
    assert "<Clinton1>" in body


def test_html_entities_are_unescaped() -> None:
    payload = "<html><body><pre>\n[FR Doc No: X]\nExecutive Order 99999 of May 1, 2020\n\nR&amp;D and &quot;quoted&quot; text\n</pre></body></html>"
    assert "R&D" in fr_client.clean_raw_text(payload)
    assert '"quoted"' in fr_client.clean_raw_text(payload)


@pytest.mark.parametrize(
    "page",
    [
        "<html><body><h1>Request Access</h1><p>Due to aggressive crawling...</p></body></html>",
        "<html><body>Pardon Our Interruption... you have been blocked</body></html>",
    ],
)
def test_bot_interstitial_is_detected(page: str) -> None:
    """The failure that starved v1: this page arrives with HTTP 200, so
    raise_for_status() never sees it."""
    assert fr_client.looks_blocked(page)
    with pytest.raises(fr_client.BlockedError):
        fr_client.clean_raw_text(page)


def test_real_documents_are_not_flagged_as_blocked() -> None:
    for path in RAW_FIXTURES:
        assert not fr_client.looks_blocked(path.read_text(encoding="utf-8"))


def test_search_fixture_shape_matches_what_ingest_reads() -> None:
    payload = json.loads(load("search_page.json"))
    assert payload["count"] > 1500
    record = payload["results"][0]
    for key in ("document_number", "executive_order_number", "raw_text_url", "president"):
        assert key in record
    assert isinstance(record["president"], dict), "president is an object, not a string"
