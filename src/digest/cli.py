"""``digest`` command line. Later phases add ``run --once``, ``publish``, ``status``, ..."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from digest import db
from digest.config import ConfigError, Settings, load_all

app = typer.Typer(no_args_is_help=True, add_completion=False)
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


def main() -> None:
    app()
