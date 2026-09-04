"""`eo` command line.

Every stage is declared here so the command surface is visible, and each fails
loudly rather than pretending to work -- v1's defining bug was code that
reported success over empty results.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import typer

from eo import __version__, db, dispositions, ingest, prompts
from eo import analysis_db as analysis_db_mod
from eo import compare as compare_mod
from eo import export as export_mod
from eo import extract as extract_mod
from eo import validate as validate_mod
from eo.config import API_KEY_VAR, Settings, load_settings

DEFAULT_EXPORT_DIR = "data/export"
DEFAULT_ANALYSIS_DB = "data/analysis.db"

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
    compare_run: int = typer.Option(
        None, "--compare-run",
        help="Extract only the gold set plus what run N flagged, for a"
             " frontier-model comparison against run N.",
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
        if compare_run is not None and not con.execute(
            "SELECT 1 FROM extraction_runs WHERE run_id = ?", (compare_run,)
        ).fetchone():
            typer.secho(
                f"no such run to compare against: {compare_run}",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(code=2)

        selected = extract_mod.select_documents(
            con, limit=limit, run_id=run_id, spread=spread, compare_run=compare_run
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
        if compare_run is not None:
            typer.echo(f"comparing to  run {compare_run} (gold set + its review queue)")
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
        notes = (
            f"frontier comparison against run {compare_run}"
            if compare_run is not None
            else None
        )
        active_run = run_id or extract_mod.start_run(con, settings.model, notes)
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

    with db.session(settings.db_path) as con:
        gates = validate_mod.validate_run(con, active_run)

    typer.echo("")
    typer.echo(report.summary())
    _report_gates(gates)
    # Failures are printed before the gate verdict is acted on. When every
    # document fails, the gates fail too, and an early exit here would report
    # "0% stored quotes verified" while swallowing the one line that says why
    # -- which is how a routing 404 first presented as a quality problem.
    if report.failed:
        typer.secho(f"\n{len(report.failed)} failure(s):", fg=typer.colors.RED, err=True)
        for doc_num, reason in report.failed[:20]:
            typer.echo(f"  {doc_num}: {reason}", err=True)
    if not gates.passed:
        typer.secho("\nrun FAILED its quality gates.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if report.failed:
        raise typer.Exit(code=1)


def _report_gates(report: validate_mod.ValidationReport) -> None:
    typer.echo("")
    typer.echo(f"quality gates for run {report.run_id}:")
    for gate in report.gates:
        if gate.advisory:
            colour = typer.colors.BLUE
        else:
            colour = typer.colors.GREEN if gate.passed else typer.colors.RED
        typer.secho("  " + gate.line(), fg=colour)
        if gate.detail:
            typer.echo(f"       {gate.detail}")
    if report.review_items:
        typer.echo("")
        typer.echo(
            f"{report.review_items} item(s) in the review queue"
            f" — `eo review --run-id {report.run_id}`"
        )


@app.command()
def validate(
    run_id: int = typer.Option(..., "--run-id", help="Run to score."),
) -> None:
    """Run the quality gates over an extraction run."""
    settings = load_settings()
    with db.session(settings.db_path) as con:
        if not con.execute(
            "SELECT 1 FROM extraction_runs WHERE run_id = ?", (run_id,)
        ).fetchone():
            typer.secho(f"no such run: {run_id}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
        report = validate_mod.validate_run(con, run_id)

    _report_gates(report)
    if not report.passed:
        typer.secho("\nrun FAILED its quality gates.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho("\nall gates passed.", fg=typer.colors.GREEN)


@app.command()
def review(
    run_id: int = typer.Option(..., "--run-id", help="Run to inspect."),
    kind: str = typer.Option(None, "--kind", help="Filter by kind."),
    limit: int = typer.Option(30, "--limit", "-n"),
) -> None:
    """Show what the gates parked for human review."""
    settings = load_settings()
    sql = "SELECT * FROM review_queue WHERE run_id = ?"
    params: list = [run_id]
    if kind:
        sql += " AND kind LIKE ?"
        params.append(f"%{kind}%")
    sql += " ORDER BY kind, document_number LIMIT ?"
    params.append(limit)

    with db.session(settings.db_path) as con:
        rows = con.execute(sql, params).fetchall()
        counts = con.execute(
            "SELECT kind, COUNT(*) n FROM review_queue WHERE run_id = ?"
            " GROUP BY kind ORDER BY n DESC",
            (run_id,),
        ).fetchall()

    if not rows:
        typer.echo("review queue is empty.")
        return
    for row in counts:
        typer.echo(f"  {row['n']:>4}  {row['kind']}")
    typer.echo("")
    for row in rows:
        typer.echo(f"[{row['kind']}] {row['document_number']}")
        typer.echo(f"   {row['detail']}")


@app.command()
def export(
    run_id: int = typer.Option(..., "--run-id", help="Run to export."),
    out: str = typer.Option(
        DEFAULT_EXPORT_DIR, "--out", help="Directory to write into."
    ),
    fmt: str = typer.Option(
        export_mod.CSV, "--format", help="csv or parquet.", show_default=True
    ),
    include_text: bool = typer.Option(
        False, "--include-text",
        help="Include body_text and raw_response. Large; off by default.",
    ),
) -> None:
    """Export one extraction run to flat files, with a provenance manifest."""
    settings = load_settings()
    if fmt not in (export_mod.CSV, export_mod.PARQUET):
        typer.secho(
            f"unknown format: {fmt} (expected csv or parquet)",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)
    if fmt == export_mod.PARQUET:
        # Fail before writing anything, not halfway through the third table.
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            typer.secho(
                "parquet needs pyarrow, which is not installed."
                " Use --format csv, or `pip install pyarrow`.",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(code=2) from None

    out_dir = Path(out)
    with db.session(settings.db_path) as con:
        if not con.execute(
            "SELECT 1 FROM extraction_runs WHERE run_id = ?", (run_id,)
        ).fetchone():
            typer.secho(f"no such run: {run_id}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
        counts = export_mod.export_run(
            con, run_id, out_dir, fmt=fmt, include_text=include_text
        )

    typer.echo(f"exported run {run_id} to {out_dir}/ as {fmt}")
    for name, count in counts.items():
        size = (out_dir / f"{name}.{fmt}").stat().st_size
        typer.echo(f"  {name:<18} {count:>7,} rows  {size / 1024:>8,.0f} KB")
    typer.echo(f"  {'manifest.json':<18} {'':>7}       provenance: run, model, prompt")
    if not include_text:
        typer.echo("")
        typer.echo(
            "body_text and raw_response omitted; pass --include-text to keep them."
        )


def _axis_block(axis: compare_mod.Axis, baseline: int, candidate: int) -> None:
    typer.echo(
        f"  {axis.name:<16} {axis.baseline_rate:>6.0%} {axis.candidate_rate:>8.0%}"
        f"     {axis.baseline_hits}/{axis.n} vs {axis.candidate_hits}/{axis.n}"
    )


def _disagreement_lines(entries: list[tuple], indent: str = "      ") -> None:
    for eo_number, title, truth, base_answer, cand_answer in entries:
        typer.echo(f"{indent}EO {eo_number}  {(title or '')[:52]}")
        typer.echo(
            f"{indent}   gold={truth}  baseline={base_answer}  candidate={cand_answer}"
        )


@app.command()
def compare(
    baseline: int = typer.Option(..., "--baseline", help="The run to beat."),
    candidate: int = typer.Option(..., "--candidate", help="The run under test."),
) -> None:
    """Compare two extraction runs over the documents they both cover."""
    settings = load_settings()
    with db.session(settings.db_path) as con:
        try:
            report = compare_mod.compare_runs(con, baseline, candidate)
        except ValueError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from exc

    base, cand = report.baseline, report.candidate
    if not report.documents:
        typer.secho(
            f"runs {baseline} and {candidate} share no successfully extracted"
            " documents; nothing to compare.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)

    typer.echo(
        f"run {cand.run_id} {cand.model} vs run {base.run_id} {base.model}"
    )
    if base.prompt_version != cand.prompt_version:
        typer.secho(
            f"  prompt versions differ ({base.prompt_version} vs"
            f" {cand.prompt_version}) — this compares prompt and model together,"
            " not the model alone.",
            fg=typer.colors.YELLOW,
        )
    else:
        typer.echo(f"  prompt {base.prompt_version} in both")
    typer.echo(f"  {len(report.documents)} documents extracted by both runs")
    typer.echo("")

    typer.echo(f"gold-set agreement ({report.gold_orders} hand-labelled orders)")
    typer.echo(f"  {'':<16} {'baseline':>6} {'candidate':>8}")
    for axis in report.axes:
        _axis_block(axis, baseline, candidate)
    typer.echo("")

    for axis in report.axes:
        if axis.both_wrong:
            typer.secho(
                f"  {axis.name}: both models disagree with the label on"
                f" {len(axis.both_wrong)} order(s).",
                fg=typer.colors.YELLOW,
            )
            typer.echo(
                "    Two independent models reading the same text the same way is"
            )
            typer.echo(
                "    evidence about the label, which nothing else checks. Review these:"
            )
            _disagreement_lines(axis.both_wrong)
        if axis.candidate_only_wrong:
            typer.echo(
                f"  {axis.name}: only the candidate disagrees on"
                f" {len(axis.candidate_only_wrong)} order(s)"
            )
            _disagreement_lines(axis.candidate_only_wrong)
        if axis.baseline_only_wrong:
            typer.echo(
                f"  {axis.name}: only the baseline disagrees on"
                f" {len(axis.baseline_only_wrong)} order(s)"
            )
            _disagreement_lines(axis.baseline_only_wrong)
    typer.echo("")

    typer.echo(f"model-vs-model agreement over all {len(report.documents)} documents")
    for name, (agree, total, _) in report.field_agreement.items():
        rate = agree / total if total else 0.0
        typer.echo(f"  {name:<16} {agree:>3}/{total:<3} {rate:>6.0%}")
    typer.echo("")

    base_g, cand_g = report.grounding[baseline], report.grounding[candidate]
    typer.echo("quote grounding over the same documents")
    typer.echo(f"  {'':<16} {'baseline':>8} {'candidate':>9}")
    typer.echo(f"  {'quotes':<16} {base_g.checked:>8} {cand_g.checked:>9}")
    typer.echo(
        f"  {'grounded':<16} {base_g.rate:>8.1%} {cand_g.rate:>9.1%}"
    )
    typer.echo(
        f"  {'severe drift':<16} {base_g.severe_rate:>8.1%} {cand_g.severe_rate:>9.1%}"
    )
    typer.echo("")

    typer.echo("claims extracted (a model that scores well by saying less is not better)")
    typer.echo(f"  {'':<20} {'baseline':>8} {'candidate':>9}")
    for table in sorted(report.claims[baseline]):
        typer.echo(
            f"  {table:<20} {report.claims[baseline][table]:>8}"
            f" {report.claims[candidate][table]:>9}"
        )
    typer.echo("")
    typer.echo(
        f"cost   run {base.run_id} ${base.cost_usd:.4f} over {base.rows} rows"
        f"   |   run {cand.run_id} ${cand.cost_usd:.4f} over {cand.rows} rows"
    )


@app.command("analysis-db")
def analysis_db(
    run_id: int = typer.Option(..., "--run-id", help="Run to package."),
    out: str = typer.Option(
        DEFAULT_ANALYSIS_DB, "--out", help="Database file to write."
    ),
) -> None:
    """Build a standalone analysis database: one run, its source text, joinable."""
    settings = load_settings()
    target = Path(out)
    with db.session(settings.db_path) as con:
        try:
            counts = analysis_db_mod.build(con, target, run_id)
        except ValueError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from exc

    size = target.stat().st_size
    typer.echo(f"built {target} from run {run_id}  ({size / 1e6:,.1f} MB)")
    for name, count in counts.items():
        typer.echo(f"  {name:<24} {count:>7,}")
    typer.echo("")
    typer.echo("views: revocation_network, all_claims")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
