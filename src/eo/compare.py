"""Head-to-head comparison of two extraction runs over the same documents.

The question this exists to answer is not "which model is better" but
"is the cheap model's score the model's ceiling or the task's?". Those look
identical in a single run's gate report and need two runs to separate.

The sharpest signal here is the `both_wrong` set on each gold axis. An order
that two independently-trained models both label differently from the gold set
is evidence about the *label*, not about the models -- and the gold labels were
read by hand, so they are exactly the thing with no other check on it. Every
other number in this report is context for that one.

Everything is computed over the intersection of the two runs, never over each
run's own population: the comparison set is deliberately enriched with the
baseline's failures, so any rate taken over one run alone is not comparable to
anything.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from eo import grounding
from eo.validate import QUOTED_TABLES, SEVERE_DRIFT_KEPT, load_gold_set

GOLD_AXES = ("primary_topic", "instrument")


@dataclass
class RunInfo:
    run_id: int
    model: str
    prompt_version: str
    cost_usd: float
    rows: int


@dataclass
class Axis:
    """One gold-labelled field, scored for both runs over the same orders."""

    name: str
    n: int
    baseline_hits: int
    candidate_hits: int
    both_wrong: list[tuple] = field(default_factory=list)
    baseline_only_wrong: list[tuple] = field(default_factory=list)
    candidate_only_wrong: list[tuple] = field(default_factory=list)

    @property
    def baseline_rate(self) -> float:
        return self.baseline_hits / self.n if self.n else 0.0

    @property
    def candidate_rate(self) -> float:
        return self.candidate_hits / self.n if self.n else 0.0


@dataclass
class GroundingStats:
    checked: int
    grounded: int
    severe: int

    @property
    def rate(self) -> float:
        return self.grounded / self.checked if self.checked else 0.0

    @property
    def severe_rate(self) -> float:
        return self.severe / self.checked if self.checked else 0.0


@dataclass
class ComparisonReport:
    baseline: RunInfo
    candidate: RunInfo
    documents: list[str]
    axes: list[Axis]
    gold_orders: int
    field_agreement: dict[str, tuple[int, int, list[tuple]]]
    grounding: dict[int, GroundingStats]
    claims: dict[int, dict[str, int]]


def run_info(con: sqlite3.Connection, run_id: int) -> RunInfo:
    row = con.execute(
        "SELECT model, prompt_version, COALESCE(cost_usd, 0) cost_usd"
        " FROM extraction_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no such run: {run_id}")
    rows = con.execute(
        "SELECT COUNT(*) FROM extractions WHERE run_id = ? AND summary IS NOT NULL",
        (run_id,),
    ).fetchone()[0]
    return RunInfo(run_id, row["model"], row["prompt_version"], row["cost_usd"], rows)


def shared_documents(
    con: sqlite3.Connection, baseline: int, candidate: int
) -> list[str]:
    """Documents both runs extracted successfully.

    A row that failed in either run is excluded rather than counted as a
    disagreement: comparing an answer against a failure measures nothing.
    """
    return [
        row[0]
        for row in con.execute(
            "SELECT a.document_number FROM extractions a"
            " JOIN extractions b USING (document_number)"
            " WHERE a.run_id = ? AND b.run_id = ?"
            "   AND a.summary IS NOT NULL AND b.summary IS NOT NULL"
            "   AND a.primary_topic IS NOT NULL AND b.primary_topic IS NOT NULL"
            " ORDER BY a.document_number",
            (baseline, candidate),
        )
    ]


def _labelled_rows(
    con: sqlite3.Connection, run_id: int, documents: list[str]
) -> dict[str, sqlite3.Row]:
    if not documents:
        return {}
    placeholders = ",".join("?" * len(documents))
    return {
        row["document_number"]: row
        for row in con.execute(
            f"SELECT e.*, d.eo_number, d.title FROM extractions e"
            f" JOIN documents d USING (document_number)"
            f" WHERE e.run_id = ? AND e.document_number IN ({placeholders})",
            (run_id, *documents),
        )
    }


def gold_axis(
    name: str,
    gold: dict[int, dict[str, Any]],
    baseline_rows: dict[str, sqlite3.Row],
    candidate_rows: dict[str, sqlite3.Row],
) -> Axis:
    axis = Axis(name=name, n=0, baseline_hits=0, candidate_hits=0)
    for doc, base in sorted(baseline_rows.items(), key=lambda kv: kv[1]["eo_number"]):
        eo_number = base["eo_number"]
        if eo_number not in gold:
            continue
        cand = candidate_rows[doc]
        truth = gold[eo_number][name]
        base_ok = base[name] == truth
        cand_ok = cand[name] == truth
        axis.n += 1
        axis.baseline_hits += base_ok
        axis.candidate_hits += cand_ok
        entry = (eo_number, base["title"], truth, base[name], cand[name])
        if not base_ok and not cand_ok:
            axis.both_wrong.append(entry)
        elif not base_ok:
            axis.baseline_only_wrong.append(entry)
        elif not cand_ok:
            axis.candidate_only_wrong.append(entry)
    return axis


def field_agreement(
    name: str,
    baseline_rows: dict[str, sqlite3.Row],
    candidate_rows: dict[str, sqlite3.Row],
) -> tuple[int, int, list[tuple]]:
    """How often the two models say the same thing, gold set or not.

    This covers every shared document, so it is the widest read on whether the
    two models see the corpus the same way.
    """
    agree = 0
    disagreements: list[tuple] = []
    rows = sorted(baseline_rows.items(), key=lambda kv: kv[1]["eo_number"])
    for doc, base in rows:
        cand = candidate_rows[doc]
        if base[name] == cand[name]:
            agree += 1
        else:
            disagreements.append(
                (base["eo_number"], base["title"], base[name], cand[name])
            )
    return agree, len(rows), disagreements


def grounding_stats(
    con: sqlite3.Connection, run_id: int, documents: list[str]
) -> GroundingStats:
    """Score the model's own quotes, not the repaired ones.

    Mirrors the groundedness gate: `raw_quote` is what the model wrote where a
    quote had to be trimmed, and scoring the trimmed text instead would report
    the repair's success as the model's.
    """
    if not documents:
        return GroundingStats(0, 0, 0)
    placeholders = ",".join("?" * len(documents))
    checked = grounded = severe = 0

    for table in (*QUOTED_TABLES, "relationships"):
        where = " AND t.source = 'model'" if table == "relationships" else ""
        for row in con.execute(
            f"SELECT t.source_quote, t.raw_quote, d.body_text"
            f" FROM {table} t JOIN documents d USING (document_number)"
            f" WHERE t.run_id = ? AND t.document_number IN ({placeholders}){where}",
            (run_id, *documents),
        ):
            original = row["raw_quote"] or row["source_quote"]
            checked += 1
            if grounding.is_grounded(original, row["body_text"]):
                grounded += 1
                continue
            kept = (
                len(grounding.normalize(row["source_quote"]).split())
                if grounding.is_grounded(row["source_quote"], row["body_text"])
                else 0
            )
            total = len(grounding.normalize(original).split())
            if (kept / total if total else 0.0) < SEVERE_DRIFT_KEPT:
                severe += 1
    return GroundingStats(checked, grounded, severe)


def claim_counts(
    con: sqlite3.Connection, run_id: int, documents: list[str]
) -> dict[str, int]:
    """How much each run actually extracted.

    A model that scores well by saying less is not better, and groundedness
    alone cannot see that -- quoting nothing is perfectly grounded.
    """
    if not documents:
        return {}
    placeholders = ",".join("?" * len(documents))
    counts: dict[str, int] = {}
    for table in (*QUOTED_TABLES, "relationships"):
        where = " AND source = 'model'" if table == "relationships" else ""
        counts[table] = con.execute(
            f"SELECT COUNT(*) FROM {table}"
            f" WHERE run_id = ? AND document_number IN ({placeholders}){where}",
            (run_id, *documents),
        ).fetchone()[0]
    return counts


def compare_runs(
    con: sqlite3.Connection, baseline: int, candidate: int
) -> ComparisonReport:
    documents = shared_documents(con, baseline, candidate)
    baseline_rows = _labelled_rows(con, baseline, documents)
    candidate_rows = _labelled_rows(con, candidate, documents)
    gold = load_gold_set()

    axes = [gold_axis(name, gold, baseline_rows, candidate_rows) for name in GOLD_AXES]
    return ComparisonReport(
        baseline=run_info(con, baseline),
        candidate=run_info(con, candidate),
        documents=documents,
        axes=axes,
        gold_orders=sum(
            1 for row in baseline_rows.values() if row["eo_number"] in gold
        ),
        field_agreement={
            name: field_agreement(name, baseline_rows, candidate_rows)
            for name in GOLD_AXES
        },
        grounding={
            baseline: grounding_stats(con, baseline, documents),
            candidate: grounding_stats(con, candidate, documents),
        },
        claims={
            baseline: claim_counts(con, baseline, documents),
            candidate: claim_counts(con, candidate, documents),
        },
    )
