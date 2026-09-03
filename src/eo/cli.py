"""`eo` command line.

Phase 0 implements `status` only. The remaining stages are declared here so the
command surface is visible, and each fails loudly rather than pretending to work
-- v1's defining bug was code that reported success over empty results.
"""

from __future__ import annotations

import typer

from eo import __version__, db
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


@app.command()
def fetch() -> None:
    """Ingest Executive Orders from the Federal Register API."""
    _not_yet("fetch", "Phase 1")


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
