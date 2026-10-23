"""``digest`` command line. Later phases add ``publish``, ``status``, ``clusters``, ..."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from digest import db
from digest.config import ConfigError, Settings, load_all

# No rich tracebacks: they print local variables, which can hold fetched text.
app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False)
state_app = typer.Typer(no_args_is_help=True, help="Work with the pipeline state file.")
app.add_typer(state_app, name="state")


@app.command()
def migrate(
    path: Annotated[Path | None, typer.Option(help="State file; default STATE_PATH")] = None,
) -> None:
    """Create or upgrade the SQLite state file to the latest schema."""
    target = path or Settings().state_path
    db.migrate(target)
    typer.echo(f"migrated {target}")


@app.command()
def validate() -> None:
    """Load and cross-check every config file and environment setting."""
    try:
        settings, config = load_all()
    except (ConfigError, ValueError) as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(
        f"ok: {len(config.categories)} categories, {len(config.sources)} sources "
        f"({len(config.enabled_sources)} enabled), {len(config.blocklist.domains)} blocked "
        f"domains, {len(config.llm.providers)} LLM providers; mode={settings.summary_mode}"
    )


@state_app.command("pull")
def state_pull() -> None:
    """Download the newest good state file to STATE_PATH. Local commands never upload."""
    from digest.state import make_state_store

    settings, config = load_all()
    store = make_state_store(settings, config.settings)
    result = store.pull(settings.state_path)
    for name in result.rejected:
        typer.echo(f"skipped damaged file {name}", err=True)
    if result.source is None:
        typer.echo("no state file found; nothing downloaded")
        raise typer.Exit(1)
    typer.echo(f"downloaded {result.source} to {settings.state_path}")


@app.command()
def run(
    once: Annotated[
        bool, typer.Option("--once", help="Run every due stage once, then exit")
    ] = False,
) -> None:
    """One pipeline run: download state, run due stages, upload checkpoints."""
    import sys

    from digest.logging import safe_excepthook, setup_logging
    from digest.runner import run_once

    if not once:
        typer.echo("only --once is supported: the schedule lives in pipeline.yml", err=True)
        raise typer.Exit(2)
    setup_logging()
    sys.excepthook = safe_excepthook  # the Actions log is public: no tracebacks
    try:
        settings, config = load_all()
    except (ConfigError, ValueError) as e:
        typer.echo(f"config error: {type(e).__name__}", err=True)
        raise typer.Exit(1) from None
    result = run_once(settings, config)
    if not result.ok:
        raise typer.Exit(1)


@app.command("sources-review")
def sources_review(
    out: Annotated[Path, typer.Option(help="Markdown report")] = Path("docs/SOURCES_REVIEW.md"),
    data: Annotated[Path, typer.Option(help="Results per environment")] = Path(
        "docs/sources_review.json"
    ),
    source: Annotated[list[str] | None, typer.Option(help="Only these source ids")] = None,
) -> None:
    """Check robots.txt, TDM status and feed health for every registered source."""
    import json

    from digest.ingest.fetcher import Fetcher
    from digest.ingest.guard import RequestGuard
    from digest.ingest.review import (
        MemoryPolicyCache,
        current_environment,
        load_results,
        merge_results,
        render_markdown,
        run_review,
    )
    from digest.logging import setup_logging
    from digest.timeutil import utcnow

    setup_logging()
    settings, config = load_all()
    fetcher = Fetcher(
        RequestGuard.for_sources(config, config.sources),
        MemoryPolicyCache(),
        config.settings.fetch,
        user_agent=settings.user_agent,
        robots_token=settings.bot_name,
    )
    env = current_environment()
    try:
        results = run_review(fetcher, config, only=set(source) if source else None)
    finally:
        fetcher.close()
    merged = merge_results(load_results(data), env, results, utcnow())
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text(
        json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    out.write_text(render_markdown(config, merged), encoding="utf-8", newline="\n")
    typer.echo(f"reviewed {len(results)} sources from {env}; wrote {out}")


def main() -> None:
    app()
