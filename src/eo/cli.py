"""`eo` command line.

Phase 0 implements `status` only. The remaining stages are declared here so the
command surface is visible, and each fails loudly rather than pretending to work
-- v1's defining bug was code that reported success over empty results.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import typer

from eo import __version__, db, dispositions, ingest, prompts
from eo import extract as extract_mod
from eo.config import API_KEY_VAR, Settings, load_settings

app = typer.Typer(
    add_completion=False,
    help="Build and query a structured dataset of U.S. Executive Orders.",
)


def _not_yet(stage: str, phase: str) -> None:
    typer.secho(
        f"`eo {stage}` is not implemented yet (arrives in {phase}).",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)


@app.command()
def status() -> None:
    """Report configuration and what the database currently holds."""
    settings: Settings = load_settings()

    typer.echo(f"eo {__version__}")
    typer.echo(f"  database        {settings.db_path}")
    typer.echo(f"  raw text cache  {settings.raw_dir}")
    typer.echo(f"  model           {settings.model}")
    key_state = "set" if settings.openrouter_api_key else "MISSING (see .env.example)"
    typer.echo(f"  ${API_KEY_VAR}  {key_state}")

    existed = settings.db_path.exists()
    with db.session(settings.db_path) as con:
        counts = db.table_counts(con)
        runs = con.execute(
            "SELECT run_id, model, prompt_version, started_at, finished_at, cost_usd"
            " FROM extraction_runs ORDER BY run_id DESC LIMIT 5"
        ).fetchall()

    typer.echo("")
    typer.echo(f"database {'opened' if existed else 'created'}; row counts:")
    width = max(len(t) for t in counts)
    for table, count in counts.items():
        typer.echo(f"  {table.ljust(width)}  {count:>7,}")

    cached = (
        sum(1 for _ in settings.raw_dir.glob("*.txt"))
        if settings.raw_dir.exists()
        else 0
    )
    typer.echo(f"  {'raw/*.txt'.ljust(width)}  {cached:>7,}")

    if runs:
        typer.echo("")
        typer.echo("recent extraction runs:")
        for r in runs:
            state = r["finished_at"] or "unfinished"
            typer.echo(
                f"  #{r['run_id']} {r['model']} ({r['prompt_version']})"
                f" {state} ${r['cost_usd'] or 0:.2f}"
            )

    if not counts["documents"]:
        typer.echo("")
        typer.echo("empty database — next step is `eo fetch` (Phase 1).")
        return

    with db.session(settings.db_path) as con:
        health = db.ingest_health(con)

    typer.echo("")
    typer.echo("ingest health:")
    lo, hi = health["eo_range"]
    first, last = health["date_range"]
    typer.echo(f"  EO numbers      {lo} → {hi}")
    typer.echo(f"  signing dates   {first} → {last}")
    typer.echo(f"  mean body       {health['avg_chars']:,.0f} chars")
    typer.echo(f"  with FR notes   {health['with_disposition_notes']:,}")

    short = health["short_bodies"]
    colour = typer.colors.RED if short else typer.colors.GREEN
    typer.secho(
        f"  bodies < 500 chars {short:>5}  [{'FAIL' if short else 'ok'}]", fg=colour
    )

    # Not a failure: the FR "executive_order" filter also returns a handful of
    # annexes and notices, which FR itself leaves without an EO number. They are
    # kept for completeness and excluded from extraction.
    non_eo = health["missing_eo_number"]
    typer.echo(f"  no eo_number       {non_eo:>5}  [annex/notice, not extracted]")
    typer.echo(
        f"  extractable        {health['extractable']:>5}"
        "  [one canonical row per EO number]"
    )

    if health["relationships"]:
        typer.echo("")
        typer.echo("relationships seeded from FR disposition notes:")
        for relation, count in health["relationships"][:8]:
            typer.echo(f"  {relation:<16} {count:>5}")

    typer.echo("")
    typer.echo("by president:")
    for row in health["by_president"]:
        typer.echo(
            f"  {row['president'] or '(unknown)':<22} {row['n']:>5}"
            f"   {row['lo']} → {row['hi']}"
        )


@app.command()
def fetch(
    limit: int = typer.Option(
        None, "--limit", "-n", help="Stop after this many records (for testing)."
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-fetch and overwrite documents already stored."
    ),
    per_page: int = typer.Option(100, "--per-page", help="API page size."),
) -> None:
    """Ingest Executive Orders from the Federal Register API.

    Idempotent: documents already stored with a usable body are skipped, so an
    interrupted run is resumed simply by running the command again.
    """
    settings = load_settings()
    settings.raw_dir.mkdir(parents=True, exist_ok=True)

    def progress(report: ingest.Report, record: dict) -> None:
        if report.inserted % 50 == 0 or report.inserted == 1:
            typer.echo(
                f"  [{report.inserted:>5}] EO {record.get('executive_order_number')}"
                f" {record.get('signing_date')}  {(record.get('title') or '')[:52]}"
            )

    typer.echo("fetching from the Federal Register API (no model calls)...")
    with db.session(settings.db_path) as con:
        report = ingest.ingest(
            con,
            settings.raw_dir,
            limit=limit,
            refresh=refresh,
            per_page=per_page,
            on_progress=progress,
        )

    # Seed relationships from the Federal Register's own cross-reference chain
    # before any model runs, so extraction adds to a known-good base rather
    # than reconstructing what FR already states.
    with db.session(settings.db_path) as con:
        seeded = dispositions.seed_relationships(con)

    typer.echo("")
    typer.echo(report.summary())
    typer.echo(
        f"FR relationships: {seeded['edges_inserted']} new edge(s) from "
        f"{seeded['documents']} document(s) with disposition notes"
    )

    if report.failures:
        typer.secho(
            f"\n{len(report.failures)} document(s) failed and were NOT stored:",
            fg=typer.colors.RED,
            err=True,
        )
        for doc_num, reason in report.failures[:25]:
            typer.echo(f"  {doc_num}: {reason}", err=True)
        if len(report.failures) > 25:
            typer.echo(f"  ... and {len(report.failures) - 25} more", err=True)
        raise typer.Exit(code=1)


@app.command()
def extract(
    limit: int = typer.Option(None, "--limit", "-n", help="Extract at most N orders."),
    spread: bool = typer.Option(
        True, "--spread/--no-spread",
        help="Sample evenly across presidencies rather than taking the oldest N.",
    ),
    run_id: int = typer.Option(
        None, "--run-id", help="Resume an existing run, skipping what it already has."
    ),
    model: str = typer.Option(None, "--model", help="Override the configured model."),
    concurrency: int = typer.Option(None, "--concurrency", help="Parallel requests."),
    max_tokens: int = typer.Option(None, "--max-tokens", help="Output cap per call."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be sent. No API calls, no cost."
    ),
) -> None:
    """Run the LLM extraction pass over ingested documents."""
    settings = load_settings()
    overrides = {}
    if model:
        overrides["model"] = model
    if concurrency:
        overrides["concurrency"] = concurrency
    if max_tokens:
        overrides["max_tokens"] = max_tokens
    settings = replace(settings, **overrides)

    with db.session(settings.db_path) as con:
        selected = extract_mod.select_documents(
            con, limit=limit, run_id=run_id, spread=spread
        )
        sendable, too_short = extract_mod.filter_extractable(selected)

        if too_short:
            # Never sent to a model: v1 did exactly this and got fiction back.
            typer.secho(
                f"skipping {len(too_short)} document(s) with too little text",
                fg=typer.colors.YELLOW,
            )

        if not sendable:
            typer.echo("nothing to extract.")
            return

        chars = sum(len(d["body_text"]) for d in sendable)
        typer.echo(f"model         {settings.model}")
        typer.echo(f"prompt        {prompts.PROMPT_VERSION}")
        typer.echo(f"documents     {len(sendable)}")
        typer.echo(f"input size    {chars:,} chars (~{chars // 4:,} tokens)")
        typer.echo(f"concurrency   {settings.concurrency}")
        typer.echo(f"max_tokens    {settings.max_tokens}")

        if dry_run:
            typer.echo("")
            typer.echo("dry run — no API calls made. First five:")
            for document in sendable[:5]:
                typer.echo(
                    f"  EO {document['eo_number']} {document['signing_date']}"
                    f"  {len(document['body_text']):>7,} chars"
                    f"  {(document['title'] or '')[:44]}"
                )
            return

        settings.require_api_key()
        active_run = run_id or extract_mod.start_run(con, settings.model)
        typer.echo(f"run_id        {active_run}")
        typer.echo("")

        def progress(report: extract_mod.RunReport, document: dict, status: str) -> None:
            mark = {"ok": " ", "failed": "!", "truncated": "T", "invalid": "?"}[status]
            typer.echo(
                f" {mark} [{report.attempted:>4}/{len(sendable)}] EO"
                f" {document['eo_number']}  ${report.cost_usd:.4f}"
                f"  {(document['title'] or '')[:46]}"
            )

        report = asyncio.run(
            extract_mod.extract_all(
                con, settings, sendable, run_id=active_run,
                max_tokens=settings.max_tokens, on_progress=progress,
            )
        )
        report.skipped_short = len(too_short)
        extract_mod.finish_run(con, report)

    typer.echo("")
    typer.echo(report.summary())
    if report.failed:
        typer.secho(f"\n{len(report.failed)} failure(s):", fg=typer.colors.RED, err=True)
        for doc_num, reason in report.failed[:20]:
            typer.echo(f"  {doc_num}: {reason}", err=True)
        raise typer.Exit(code=1)


@app.command()
def validate() -> None:
    """Run the quality gates over an extraction run."""
    _not_yet("validate", "Phase 4")


@app.command()
def export() -> None:
    """Export the dataset to CSV/Parquet."""
    _not_yet("export", "Phase 5")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
