"""Parse the Federal Register's own disposition notes into relationship rows.

The FR publishes a cross-reference chain per order ("Revoked by: EO 13062,
September 29, 1997"). It is authoritative and free, so the model is never asked
to reconstruct it -- the model's job is to *add* to a known-good base. 1,127 of
1,534 orders carry notes, yielding roughly 4,000 edges.

Notes record both directions. An order's own text can only assert what it does
to earlier orders; "Revoked by" is knowledge from the future. Both are kept,
with direction encoded in the relation name, so nothing is inferred or dropped.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

FR_SOURCE = "fr_disposition_notes"

# "Label: targets" segments. Labels are short verb phrases ending in a colon,
# occasionally with a parenthetical qualifier: "Superseded by (in part):". The
# qualifier must be part of the label, or the whole segment is swallowed into
# the preceding label's body and the target is credited to the wrong relation
# -- which is how EO 14109 (Biden) came to "supersede" EO 14354 (Trump 47).
_LABEL_RE = re.compile(r"([A-Za-z][A-Za-z .]{2,34}?(?:\s*\([^)]{0,20}\))?):\s*")

# Labels that introduce prose or a citation rather than a document reference.
_SKIP_LABELS = {"note", "federal register page and date", "see also note"}

_VERBS = {
    "revokes": "revokes",
    "revoked": "revokes",
    "amends": "amends",
    "amended": "amends",
    "supersedes": "supersedes",
    "superseded": "supersedes",
    "suspersedes": "supersedes",  # FR's own typo, appears once
    "continues": "continues",
    "continued": "continues",
    "supplements": "supplements",
    "supplemented": "supplements",
    "suspends": "suspends",
    "suspended": "suspends",
    "rescinds": "rescinds",
    "rescinded": "rescinds",
    "see": "references",
}

_INBOUND = {
    "revokes": "revoked_by",
    "amends": "amended_by",
    "supersedes": "superseded_by",
    "continues": "continued_by",
    "supplements": "supplemented_by",
    "suspends": "suspended_by",
    "rescinds": "rescinded_by",
}

_TARGET_RE = re.compile(
    r"(?P<eo>\bEO\s*(?:No\.\s*)?(?P<eonum>\d{3,5}))"
    r"|(?P<proc>\bProc(?:\.|lamation)\s*(?:No\.\s*)?(?P<procnum>\d{3,5}))"
    r"|(?P<notice>\bNotice\s+of\s+[A-Z][a-z]+\s+\d{1,2},\s*\d{4})"
    r"|(?P<memo>\bMemorandum\s+of\s+[A-Z][a-z]+\s+\d{1,2},\s*\d{4})"
    r"|(?P<determination>\bDetermination\s+(?:No\.\s*)?[\w-]+)"
    r"|(?P<bare>(?<![\w.])(?P<barenum>1[23]\d{3})\b)",  # 'Amends: 12864, September 15, 1993'
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedRelation:
    relation: str
    target_type: str
    target_eo_number: int | None
    target_label: str
    in_part: bool
    fragment: str


def _relations_for_label(label: str) -> list[str]:
    """Map an FR label to canonical relation names.

    Handles direction ('Revoked by'), partiality ('Revokes in part'), and the
    one compound label in the corpus ('Revokes in part and supplements').
    """
    text = re.sub(r"\s*\([^)]*\)", " ", label).strip().lower().rstrip(".")
    text = re.sub(r"\s+", " ", text)
    if text in _SKIP_LABELS:
        return []

    inbound = bool(re.search(r"\bby$", text))
    text = re.sub(r"\bby$", "", text)
    text = re.sub(r"\bin part\b", " ", text)

    relations = []
    for word in re.split(r"\band\b", text):
        verb = _VERBS.get(word.strip())
        if verb:
            relations.append(_INBOUND[verb] if inbound and verb in _INBOUND else verb)
    return relations


def _targets(body: str) -> Iterator[tuple[str, int | None, str]]:
    for m in _TARGET_RE.finditer(body):
        if m.group("eo"):
            yield "executive_order", int(m.group("eonum")), m.group("eo").strip()
        elif m.group("bare"):
            yield "executive_order", int(m.group("barenum")), f"EO {m.group('barenum')}"
        elif m.group("proc"):
            yield "proclamation", None, m.group("proc").strip()
        elif m.group("notice"):
            yield "notice", None, m.group("notice").strip()
        elif m.group("memo"):
            yield "memorandum", None, m.group("memo").strip()
        elif m.group("determination"):
            yield "determination", None, m.group("determination").strip()


def parse_notes(notes: str | None) -> list[ParsedRelation]:
    """Turn one document's disposition_notes into relation rows."""
    if not notes or not notes.strip():
        return []

    parts = _LABEL_RE.split(notes)
    # parts == [text_before_first_label, label, body, label, body, ...]
    out: list[ParsedRelation] = []
    seen: set[tuple[str, str]] = set()

    for i in range(1, len(parts) - 1, 2):
        label, body = parts[i], parts[i + 1]
        in_part = "in part" in label.lower()
        for relation in _relations_for_label(label):
            for target_type, number, target_label in _targets(body):
                key = (relation, target_label.lower())
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    ParsedRelation(
                        relation=relation,
                        target_type=target_type,
                        target_eo_number=number,
                        target_label=target_label,
                        in_part=in_part,
                        fragment=f"{label.strip()}: {body.strip()}"[:300],
                    )
                )
    return out


SEED_SQL = """
INSERT OR IGNORE INTO relationships (
  document_number, run_id, relation, target_eo_number, target_type,
  target_label, in_part, source, source_quote
) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)
"""


def seed_relationships(con: sqlite3.Connection) -> dict[str, int]:
    """Populate relationships from FR notes. Idempotent.

    Runs before any extraction so the model adds to a known-good base rather
    than reconstructing what the Federal Register already states.
    """
    rows = con.execute(
        "SELECT document_number, disposition_notes FROM documents"
        " WHERE disposition_notes IS NOT NULL AND disposition_notes != ''"
    ).fetchall()

    documents = 0
    edges = 0
    for row in rows:
        parsed = parse_notes(row["disposition_notes"])
        if not parsed:
            continue
        documents += 1
        for rel in parsed:
            cursor = con.execute(
                SEED_SQL,
                (
                    row["document_number"],
                    rel.relation,
                    rel.target_eo_number,
                    rel.target_type,
                    rel.target_label,
                    int(rel.in_part),
                    FR_SOURCE,
                    rel.fragment,
                ),
            )
            edges += cursor.rowcount
    con.commit()
    return {"documents": documents, "edges_inserted": edges}
