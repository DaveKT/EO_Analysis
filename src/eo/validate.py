"""Quality gates. A run that fails these is a failed run.

v1's defining property was that it could not tell success from failure: zero
rows said "Failed", and a third of them were empty. Every gate here turns a
silent defect into a loud one, and the run's exit status depends on them.

Gates 1-3 are absolute properties of a run. Gate 4 compares the model against
the Federal Register's own cross-references. Gate 5 scores the run against a
hand-labelled gold set -- the only one of the five that can catch a model that
is confidently, fluently wrong about what an order *is*.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eo import grounding

GOLD_SET_PATH = Path(__file__).resolve().parents[2] / "gold" / "gold_set.json"

GROUNDEDNESS_MIN = 0.95
NULL_RATE_MAX = 0.02
OTHER_RATE_MAX = 0.03
GOLD_TOPIC_MIN = 0.80
GOLD_INSTRUMENT_MIN = 0.75

QUOTED_TABLES = ("agencies_tasked", "deadlines", "authorities")


@dataclass
class Gate:
    name: str
    passed: bool
    value: str
    threshold: str
    detail: str = ""
    advisory: bool = False

    def line(self) -> str:
        if self.advisory:
            mark = "info"
        else:
            mark = "PASS" if self.passed else "FAIL"
        return f"[{mark}] {self.name:<24} {self.value:>12}   (target {self.threshold})"


@dataclass
class ValidationReport:
    run_id: int
    gates: list[Gate] = field(default_factory=list)
    review_items: int = 0

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates if not gate.advisory)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def gate_groundedness(con: sqlite3.Connection, run_id: int) -> tuple[list[Gate], list[tuple]]:
    """Two questions, deliberately separated.

    "groundedness" scores what the MODEL produced -- `raw_quote` where a quote
    had to be repaired, `source_quote` where it did not. Scoring the repaired
    text instead would report the repair's success and call it the model's,
    which is precisely the self-congratulating metric this project exists to
    avoid.

    "stored quotes verified" is an invariant, not a quality measure: every
    quote in the dataset must appear in its document. It is 100% by
    construction, and a failure means the write path is broken.
    """
    checked = grounded = 0
    stored_checked = stored_ok = 0
    trimmed = 0
    failures: list[tuple] = []

    for table in (*QUOTED_TABLES, "relationships"):
        where = " AND t.source = 'model'" if table == "relationships" else ""
        rows = con.execute(
            f"SELECT t.document_number, t.source_quote, t.raw_quote,"
            f" t.quote_trimmed, d.body_text"
            f" FROM {table} t JOIN documents d USING (document_number)"
            f" WHERE t.run_id = ?{where}",
            (run_id,),
        ).fetchall()
        for row in rows:
            original = row["raw_quote"] or row["source_quote"]
            checked += 1
            if grounding.is_grounded(original, row["body_text"]):
                grounded += 1
            else:
                failures.append((row["document_number"], table, original[:300]))
            if row["quote_trimmed"]:
                trimmed += 1
            stored_checked += 1
            stored_ok += grounding.is_grounded(row["source_quote"], row["body_text"])

    rate = _rate(grounded, checked)
    stored_rate = _rate(stored_ok, stored_checked)
    return (
        [
            Gate(
                name="groundedness",
                passed=rate >= GROUNDEDNESS_MIN,
                value=f"{rate:.1%}",
                threshold=f">= {GROUNDEDNESS_MIN:.0%}",
                detail=f"{grounded}/{checked} quotes as the model wrote them",
            ),
            Gate(
                name="stored quotes verified",
                passed=stored_rate == 1.0,
                value=f"{stored_rate:.1%}",
                threshold="100%",
                detail=f"{trimmed} quote(s) trimmed to their verified span",
            ),
        ],
        failures,
    )


def gate_null_rate(con: sqlite3.Connection, run_id: int) -> Gate:
    """v1 shipped 33% empty rows and reported success."""
    total = con.execute(
        "SELECT COUNT(*) FROM extractions WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    nulls = con.execute(
        "SELECT COUNT(*) FROM extractions WHERE run_id = ? AND (summary IS NULL"
        " OR TRIM(summary) = '' OR primary_topic IS NULL OR instrument IS NULL)",
        (run_id,),
    ).fetchone()[0]
    rate = _rate(nulls, total)
    return Gate(
        name="null rate",
        passed=rate <= NULL_RATE_MAX,
        value=f"{rate:.1%}",
        threshold=f"<= {NULL_RATE_MAX:.0%}",
        detail=f"{nulls}/{total} rows missing summary, topic or instrument",
    )


def gate_truncation(con: sqlite3.Connection, run_id: int) -> Gate:
    """A truncated response is invalid, not partial."""
    truncated = con.execute(
        "SELECT COUNT(*) FROM extractions WHERE run_id = ? AND finish_reason = 'length'",
        (run_id,),
    ).fetchone()[0]
    return Gate(
        name="truncation",
        passed=truncated == 0,
        value=str(truncated),
        threshold="0",
        detail="rows cut off by the output cap",
    )


def gate_other_rate(con: sqlite3.Connection, run_id: int) -> Gate:
    """`other` above this rate means the vocabulary has a gap, not that the
    orders are unusual. Education was found exactly this way."""
    total = con.execute(
        "SELECT COUNT(*) FROM extractions WHERE run_id = ? AND primary_topic IS NOT NULL",
        (run_id,),
    ).fetchone()[0]
    others = con.execute(
        "SELECT COUNT(*) FROM extractions WHERE run_id = ?"
        " AND (primary_topic = 'other' OR instrument = 'other')",
        (run_id,),
    ).fetchone()[0]
    rate = _rate(others, total)
    return Gate(
        name="other rate",
        passed=rate <= OTHER_RATE_MAX,
        value=f"{rate:.1%}",
        threshold=f"<= {OTHER_RATE_MAX:.0%}",
        detail=f"{others}/{total} rows used 'other' on either axis",
    )


def gate_relationship_agreement(
    con: sqlite3.Connection, run_id: int
) -> tuple[Gate, list[tuple]]:
    """The Federal Register's cross-references are authoritative.

    A model that contradicts them -- calling a revocation an amendment -- is
    flagged for review rather than being written silently into the table.
    Additions are not contradictions: FR notes are not exhaustive, and finding
    more is the point of the extraction.
    """
    rows = con.execute(
        "SELECT fr.document_number, fr.target_eo_number, fr.relation fr_relation,"
        "       m.relation model_relation"
        " FROM relationships fr"
        " JOIN relationships m"
        "   ON m.document_number = fr.document_number"
        "  AND m.target_eo_number = fr.target_eo_number"
        "  AND m.run_id = ?"
        " WHERE fr.run_id IS NULL AND fr.source = 'fr_disposition_notes'"
        "   AND fr.target_eo_number IS NOT NULL"
        "   AND fr.relation IN ('revokes', 'amends', 'supersedes', 'continues')"
        "   AND m.relation != fr.relation",
        (run_id,),
    ).fetchall()

    compared = con.execute(
        "SELECT COUNT(*) FROM relationships fr JOIN relationships m"
        "   ON m.document_number = fr.document_number"
        "  AND m.target_eo_number = fr.target_eo_number AND m.run_id = ?"
        " WHERE fr.run_id IS NULL AND fr.target_eo_number IS NOT NULL",
        (run_id,),
    ).fetchone()[0]

    disagreements = [
        (
            row["document_number"],
            "relationship",
            (
                f"FR says {row['fr_relation']} EO {row['target_eo_number']};"
                f" model says {row['model_relation']}"
            ),
        )
        for row in rows
    ]
    return (
        Gate(
            name="relationship agreement",
            passed=True,  # advisory: disagreements go to review, not to failure
            value=f"{len(disagreements)} flagged",
            threshold="review",
            detail=f"{compared} model/FR pairs compared",
            advisory=True,
        ),
        disagreements,
    )


def load_gold_set(path: Path = GOLD_SET_PATH) -> dict[int, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {int(entry["eo_number"]): entry for entry in payload["labels"]}


def gate_gold_set(
    con: sqlite3.Connection, run_id: int, gold: dict[int, dict[str, Any]]
) -> list[Gate]:
    """Score the run against hand-read labels.

    Groundedness proves quotes are real. It says nothing about whether the
    *reading* is right, which is the failure mode a fluent model produces at
    scale. Only this gate can see that.
    """
    if not gold:
        return [
            Gate("gold set", True, "absent", "20 orders", "no gold set found", True)
        ]

    rows = con.execute(
        "SELECT d.eo_number, e.primary_topic, e.instrument"
        " FROM extractions e JOIN documents d USING (document_number)"
        " WHERE e.run_id = ? AND e.primary_topic IS NOT NULL",
        (run_id,),
    ).fetchall()
    overlap = [row for row in rows if row["eo_number"] in gold]

    if not overlap:
        return [
            Gate(
                "gold set", True, "0 overlap", "20 orders",
                "run contains no gold-set orders", True,
            )
        ]

    topic_hits = sum(
        1 for r in overlap if r["primary_topic"] == gold[r["eo_number"]]["primary_topic"]
    )
    instrument_hits = sum(
        1 for r in overlap if r["instrument"] == gold[r["eo_number"]]["instrument"]
    )
    n = len(overlap)
    return [
        Gate(
            "gold: primary_topic",
            _rate(topic_hits, n) >= GOLD_TOPIC_MIN,
            f"{_rate(topic_hits, n):.0%}",
            f">= {GOLD_TOPIC_MIN:.0%}",
            f"{topic_hits}/{n} agree",
        ),
        Gate(
            "gold: instrument",
            _rate(instrument_hits, n) >= GOLD_INSTRUMENT_MIN,
            f"{_rate(instrument_hits, n):.0%}",
            f">= {GOLD_INSTRUMENT_MIN:.0%}",
            f"{instrument_hits}/{n} agree",
        ),
    ]


def record_review_items(
    con: sqlite3.Connection, run_id: int, items: list[tuple]
) -> None:
    con.execute("DELETE FROM review_queue WHERE run_id = ?", (run_id,))
    con.executemany(
        "INSERT INTO review_queue (run_id, document_number, kind, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (run_id, doc, kind, detail, datetime.now(UTC).isoformat(timespec="seconds"))
            for doc, kind, detail in items
        ],
    )
    con.commit()


def validate_run(con: sqlite3.Connection, run_id: int) -> ValidationReport:
    report = ValidationReport(run_id=run_id)

    groundedness, ungrounded = gate_groundedness(con, run_id)
    agreement, disagreements = gate_relationship_agreement(con, run_id)

    report.gates = [
        *groundedness,
        gate_null_rate(con, run_id),
        gate_truncation(con, run_id),
        gate_other_rate(con, run_id),
        agreement,
        *gate_gold_set(con, run_id, load_gold_set()),
    ]

    review = [
        (doc, f"ungrounded_{table}", quote) for doc, table, quote in ungrounded
    ] + disagreements
    record_review_items(con, run_id, review)
    report.review_items = len(review)
    return report
