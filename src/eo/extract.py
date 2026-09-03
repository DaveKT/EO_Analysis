"""The extraction pass: resumable, bounded-concurrency, per-record commit.

Every precondition here maps to a specific v1 failure:

  * A document with too little text is skipped and never sent. v1 sent empty
    strings to GPT-4 and got confident fiction back -- a tariff order described
    as a COVID Defense Production Act order.
  * The full body is sent. v1 truncated to text[:3000] and lost the operative
    sections of long orders.
  * finish_reason is stored, and a truncated response is retried once with a
    higher cap and then recorded as invalid rather than kept.
  * Each record is committed as it completes, so an interrupted run keeps
    everything finished so far and `--run-id` resumes it.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from eo import fr_client, grounding, prompts
from eo.config import Settings
from eo.models import Extraction, extraction_json_schema
from eo.providers import Completion, OpenRouterClient

MODEL_SOURCE = "model"


@dataclass
class RunReport:
    run_id: int
    attempted: int = 0
    succeeded: int = 0
    skipped_short: int = 0
    truncated: int = 0
    invalid_json: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def summary(self) -> str:
        return (
            f"run {self.run_id}: {self.succeeded}/{self.attempted} extracted, "
            f"{self.skipped_short} skipped, {self.truncated} truncated, "
            f"{self.invalid_json} unparsable, {len(self.failed)} failed | "
            f"{self.prompt_tokens:,} in + {self.completion_tokens:,} out tokens | "
            f"${self.cost_usd:.4f}"
        )


def select_documents(
    con: sqlite3.Connection,
    *,
    limit: int | None,
    run_id: int | None,
    spread: bool,
) -> list[dict[str, Any]]:
    """Choose which orders to extract.

    Sampling happens *before* the resume filter, so `--limit 25 --run-id N`
    offers the same 25 orders the run first chose, minus the ones it finished.
    Filtering first would silently sample a different 25 on every resume.

    `spread` samples evenly across presidencies rather than taking the oldest N,
    so a pilot exercises every era's formatting instead of only Clinton's.
    """
    rows = [
        dict(row)
        for row in con.execute("SELECT * FROM extractable_documents ORDER BY eo_number")
    ]

    if limit is not None and limit < len(rows):
        rows = _sample(rows, limit) if spread else rows[:limit]

    if run_id is not None:
        done = {
            row[0]
            for row in con.execute(
                "SELECT document_number FROM extractions WHERE run_id = ?"
                " AND summary IS NOT NULL"
                " AND finish_reason NOT IN ('length', 'invalid')",
                (run_id,),
            )
        }
        # Only *successful* rows are skipped. A row that came back truncated or
        # unparsable is work still to do, so resuming retries it rather than
        # treating a failure as done.
        rows = [row for row in rows if row["document_number"] not in done]

    return rows


def _sample(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Spread a sample evenly across presidencies, deterministically."""
    by_president: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_president.setdefault(row["president"] or "unknown", []).append(row)

    picked: list[dict[str, Any]] = []
    presidents = list(by_president)
    per_president = max(1, limit // len(presidents))
    for name in presidents:
        group = by_president[name]
        step = max(1, len(group) // per_president)
        picked.extend(group[::step][:per_president])

    picked.sort(key=lambda r: r["eo_number"])
    return picked[:limit]


def known_relations(con: sqlite3.Connection, document_number: str) -> list[str]:
    """What the Federal Register already states about this order."""
    return [
        f"{row['relation']} {row['target_label']}"
        for row in con.execute(
            "SELECT relation, target_label FROM relationships"
            " WHERE document_number = ? AND run_id IS NULL",
            (document_number,),
        )
    ]


def start_run(con: sqlite3.Connection, model: str, notes: str | None = None) -> int:
    cursor = con.execute(
        "INSERT INTO extraction_runs (model, prompt_version, started_at, notes)"
        " VALUES (?, ?, ?, ?)",
        (model, prompts.PROMPT_VERSION, datetime.now(UTC).isoformat(timespec="seconds"), notes),
    )
    con.commit()
    return int(cursor.lastrowid)


def finish_run(con: sqlite3.Connection, report: RunReport) -> None:
    """Close out a run, accumulating usage.

    Totals are added to what the row already holds: a resumed run's cost is the
    cost of all its attempts, not just the last one.
    """
    con.execute(
        "UPDATE extraction_runs SET finished_at = ?,"
        " input_tokens = COALESCE(input_tokens, 0) + ?,"
        " output_tokens = COALESCE(output_tokens, 0) + ?,"
        " cost_usd = COALESCE(cost_usd, 0) + ? WHERE run_id = ?",
        (
            datetime.now(UTC).isoformat(timespec="seconds"),
            report.prompt_tokens,
            report.completion_tokens,
            report.cost_usd,
            report.run_id,
        ),
    )
    con.commit()


def _verified_quote(quote: str, body: str) -> tuple[str | None, str | None, int]:
    """Return (stored_quote, raw_quote, trimmed_flag) for one claim.

    A quote that drifts from the source is trimmed to the part that is
    verifiably in the document, and the model's original is kept alongside it.
    Nothing is silently improved: `quote_trimmed` marks every repaired row, and
    the groundedness gate scores the original, so the metric still measures the
    model rather than the repair.
    """
    if grounding.is_grounded(quote, body):
        return quote, None, 0
    trimmed = grounding.longest_grounded_span(quote, body)
    if trimmed is None:
        return None, quote, 1  # nothing verifiable; goes to review
    return trimmed, quote, 1


def persist(
    con: sqlite3.Connection,
    document_number: str,
    run_id: int,
    extraction: Extraction,
    completion: Completion,
    body_text: str = "",
) -> None:
    """Write one extraction and its grounded claims. Committed immediately."""
    con.execute(
        "INSERT OR REPLACE INTO extractions (document_number, run_id, summary,"
        " primary_topic, topic_other_reason, secondary_topics, instrument,"
        " instrument_other_reason, finish_reason, raw_response)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            document_number,
            run_id,
            extraction.summary,
            extraction.primary_topic.value,
            extraction.topic_other_reason,
            json.dumps([t.value for t in extraction.secondary_topics]),
            extraction.instrument.value,
            extraction.instrument_other_reason,
            completion.finish_reason,
            completion.content,
        ),
    )
    for table in ("agencies_tasked", "deadlines", "authorities"):
        con.execute(
            f"DELETE FROM {table} WHERE document_number = ? AND run_id = ?",
            (document_number, run_id),
        )
    con.execute(
        "DELETE FROM relationships WHERE document_number = ? AND run_id = ?",
        (document_number, run_id),
    )

    def verified(quote: str) -> tuple[str | None, str | None, int]:
        return _verified_quote(quote, body_text) if body_text else (quote, None, 0)

    con.executemany(
        "INSERT INTO agencies_tasked (document_number, run_id, agency_name, task,"
        " source_quote, raw_quote, quote_trimmed) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (document_number, run_id, a.agency_name, a.task, *verified(a.source_quote))
            for a in extraction.agencies_tasked
            if verified(a.source_quote)[0] is not None
        ],
    )
    con.executemany(
        "INSERT INTO deadlines (document_number, run_id, due_description, due_date,"
        " responsible_party, source_quote, raw_quote, quote_trimmed)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                document_number,
                run_id,
                d.due_description,
                d.due_date,
                d.responsible_party,
                *verified(d.source_quote),
            )
            for d in extraction.deadlines
            if verified(d.source_quote)[0] is not None
        ],
    )
    con.executemany(
        "INSERT INTO authorities (document_number, run_id, authority, source_quote,"
        " raw_quote, quote_trimmed) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (document_number, run_id, a.authority, *verified(a.source_quote))
            for a in extraction.authorities
            if verified(a.source_quote)[0] is not None
        ],
    )
    con.executemany(
        "INSERT INTO relationships (document_number, run_id, relation,"
        " target_eo_number, target_type, target_label, in_part, source,"
        " source_quote, raw_quote, quote_trimmed)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        [
            (
                document_number,
                run_id,
                r.relation.value,
                r.target_eo_number,
                "executive_order" if r.target_eo_number else None,
                r.target_label,
                MODEL_SOURCE,
                *verified(r.source_quote),
            )
            for r in extraction.relationships
            if verified(r.source_quote)[0] is not None
        ],
    )
    con.commit()


def record_failure(
    con: sqlite3.Connection,
    document_number: str,
    run_id: int,
    finish_reason: str,
    raw: str,
) -> None:
    """Store the failure rather than dropping it.

    A row with a finish_reason and no fields is visibly broken; a missing row
    looks like work that was never attempted. v1 could not tell the difference.
    """
    con.execute(
        "INSERT OR REPLACE INTO extractions (document_number, run_id,"
        " finish_reason, raw_response) VALUES (?, ?, ?, ?)",
        (document_number, run_id, finish_reason, raw[:20000]),
    )
    con.commit()


async def _call(
    client: OpenRouterClient,
    settings: Settings,
    document: dict[str, Any],
    relations: list[str],
    schema: dict[str, Any],
    max_tokens: int,
) -> Completion:
    return await client.complete(
        model=settings.model,
        system=prompts.SYSTEM_PROMPT,
        user=prompts.build_user_prompt(document, relations),
        schema=schema,
        max_tokens=max_tokens,
    )


async def extract_all(
    con: sqlite3.Connection,
    settings: Settings,
    documents: Sequence[dict[str, Any]],
    *,
    run_id: int,
    max_tokens: int,
    on_progress: Callable[[RunReport, dict[str, Any], str], None] | None = None,
) -> RunReport:
    report = RunReport(run_id=run_id)
    schema = extraction_json_schema()
    client = OpenRouterClient(
        settings.require_api_key(),
        settings.openrouter_base_url,
        timeout=settings.request_timeout,
    )
    semaphore = asyncio.Semaphore(settings.concurrency)

    # Relations are read up front: SQLite reads during the async gather would
    # interleave with the writes below.
    relations = {d["document_number"]: known_relations(con, d["document_number"]) for d in documents}

    async def worker(document: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        async with semaphore:
            try:
                completion = await _call(
                    client, settings, document, relations[document["document_number"]],
                    schema, max_tokens,
                )
                if completion.truncated:
                    # One retry with a higher cap before calling it invalid.
                    completion = await _call(
                        client, settings, document,
                        relations[document["document_number"]], schema, max_tokens * 2,
                    )
                return document, completion
            except Exception as exc:  # noqa: BLE001
                # Deliberately broad: one document's failure must not abort a
                # run of 1,500. It is returned, recorded, and reported -- the
                # opposite of v1, where errors became rows reading "Error".
                return document, exc

    tasks = [asyncio.create_task(worker(d)) for d in documents]
    try:
        for coro in asyncio.as_completed(tasks):
            document, outcome = await coro
            doc_num = document["document_number"]
            report.attempted += 1

            if isinstance(outcome, BaseException):
                report.failed.append((doc_num, f"{type(outcome).__name__}: {outcome}"))
                status = "failed"
            else:
                report.prompt_tokens += outcome.prompt_tokens
                report.completion_tokens += outcome.completion_tokens
                report.cost_usd += outcome.cost_usd

                if outcome.truncated:
                    report.truncated += 1
                    record_failure(con, doc_num, run_id, "length", outcome.content)
                    status = "truncated"
                else:
                    try:
                        extraction = Extraction.model_validate_json(outcome.content)
                    except (ValidationError, ValueError) as exc:
                        report.invalid_json += 1
                        record_failure(con, doc_num, run_id, "invalid", outcome.content)
                        report.failed.append((doc_num, f"schema: {exc}"[:200]))
                        status = "invalid"
                    else:
                        persist(
                            con, doc_num, run_id, extraction, outcome,
                            body_text=document.get("body_text") or "",
                        )
                        report.succeeded += 1
                        status = "ok"

            if on_progress:
                on_progress(report, document, status)
    finally:
        await client.aclose()

    return report


def filter_extractable(
    documents: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into (sendable, too_short). Nothing short is ever sent."""
    ok, short = [], []
    for document in documents:
        body = document.get("body_text") or ""
        (ok if len(body) >= fr_client.MIN_BODY_CHARS else short).append(document)
    return ok, short
