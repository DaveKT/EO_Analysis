"""`eo` command line.

Phase 0 implements `status` only. The remaining stages are declared here so the
command surface is visible, and each fails loudly rather than pretending to work
-- v1's defining bug was code that reported success over empty results.
"""

from __future__ import annotations

import typer

from eo import __version__, db, ingest
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

    typer.echo("")
    typer.echo(report.summary())

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
def extract() -> None:
    """Run the LLM extraction pass over ingested documents."""
    _not_yet("extract", "Phase 3")


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
