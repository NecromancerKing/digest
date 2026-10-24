"""``digest`` command line. Later phases add ``publish``, ``status``, ``clusters``, ..."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated, Any

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


def _local_state() -> tuple[Settings, Any, Any]:
    """Settings, config and an engine on the pulled state copy, for local-only commands.

    These commands print third-party text, so they refuse to run on GitHub Actions, whose
    logs are public.
    """
    import os

    if os.environ.get("GITHUB_ACTIONS") == "true":
        typer.echo("local only: this command prints source text", err=True)
        raise typer.Exit(2)
    settings, config = load_all()
    if not settings.state_path.exists():
        typer.echo(f"no state at {settings.state_path}: run `digest state pull` first", err=True)
        raise typer.Exit(1)
    return settings, config, db.make_engine(settings.state_path)


@app.command()
def clusters(
    limit: Annotated[int, typer.Option(help="How many clusters")] = 30,
    items: Annotated[bool, typer.Option("--items/--no-items", help="List member items")] = True,
    selected: Annotated[bool, typer.Option(help="Only clusters selected for stories")] = False,
) -> None:
    """Show open clusters by score, from the pulled state copy (local only)."""
    from sqlalchemy import select

    from digest.db import schema as t

    _, _, engine = _local_state()
    with engine.connect() as conn:
        q = select(t.clusters).where(t.clusters.c.is_open == 1)
        if selected:
            q = q.where(t.clusters.c.selected_at.is_not(None))
        rows = conn.execute(q.order_by(t.clusters.c.score.desc()).limit(limit)).all()
        for c in rows:
            mark = "*" if c.selected_at else " "
            typer.echo(
                f"{mark} #{c.id:<6} {c.score:5.2f}  {c.category or '?':<13} "
                f"{c.source_count} sources, {c.item_count} items, last {c.last_item_at}"
            )
            if items:
                members = conn.execute(
                    select(t.items.c.source_id, t.items.c.title, t.items.c.usage_policy)
                    .join(t.cluster_items, t.cluster_items.c.item_id == t.items.c.id)
                    .where(t.cluster_items.c.cluster_id == c.id)
                    .order_by(t.items.c.published_at)
                ).all()
                for m in members:
                    tag = " (link)" if m.usage_policy == "link_only" else ""
                    typer.echo(f"      {m.source_id}{tag}: {m.title}")
    typer.echo("* = selected for a story")


@app.command("stories")
def stories_cmd(
    limit: Annotated[int, typer.Option(help="How many stories")] = 30,
    sources: Annotated[bool, typer.Option("--sources/--no-sources", help="List sources")] = True,
) -> None:
    """Show stories (current revision) by rank, from the pulled state copy (local only)."""
    import json

    from sqlalchemy import and_, select

    from digest.db import schema as t

    _, _, engine = _local_state()
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                t.stories,
                t.story_revisions.c.kind,
                t.story_revisions.c.headline,
                t.story_revisions.c.summary,
                t.story_revisions.c.sources,
                t.story_revisions.c.verification,
            )
            .join(
                t.story_revisions,
                and_(
                    t.story_revisions.c.story_id == t.stories.c.id,
                    t.story_revisions.c.rev_no == t.stories.c.current_rev,
                ),
            )
            .order_by(t.stories.c.rank_score.desc())
            .limit(limit)
        ).all()
    for r in rows:
        typer.echo(f"[{r.status}] {r.kind} · {r.category} · rev {r.current_rev} · {r.path}")
        typer.echo(f"  {r.headline}")
        if r.summary:
            typer.echo(f"  {r.summary}")
        typer.echo(f"  gates: {json.loads(r.verification)}")
        if sources:
            for s in json.loads(r.sources):
                head = f" — {s['headline']}" if s.get("headline") else ""
                typer.echo(f"    {s['name']} {s['published_at']}{head}  {s['url']}")
        typer.echo("")
    typer.echo(f"{len(rows)} stories")


@app.command()
def label(
    n: Annotated[int, typer.Option(help="Pairs to label in this session")] = 50,
    days: Annotated[int, typer.Option(help="Sample items from the last N days")] = 7,
    labels: Annotated[Path | None, typer.Option(help="Default: <CONTENT_CHECKOUT>/labels")] = None,
    seed: Annotated[int | None, typer.Option(help="Random seed")] = None,
) -> None:
    """Label item pairs as the same event or not, for tuning clustering (local only)."""
    import random

    from digest.nlp.label import append_label, load_items, read_labels, sample_pairs
    from digest.timeutil import utcnow

    settings, _, engine = _local_state()
    path = labels or settings.content_checkout / "labels" / "pairs.jsonl"
    done = [(lab.a, lab.b) for lab in read_labels(path)]
    pool = load_items(engine, since=utcnow() - dt.timedelta(days=days))
    pairs = sample_pairs(pool, n, done, random.Random(seed))  # noqa: S311 - sampling, not security
    typer.echo(f"{len(done)} pairs labelled so far in {path}; {len(pairs)} to go this session.")
    typer.echo("Same event? y = yes, n = no, s = skip, q = quit")
    typer.echo("")

    def show(tag: str, i: Any) -> None:
        typer.echo(f"  {tag}  {i.source_id} · {i.at:%Y-%m-%d %H:%M}")
        typer.echo(f"     {i.title}")
        if i.summary:
            typer.echo(f"     {i.summary[:220]}")

    count = 0
    for k, pair in enumerate(pairs, 1):
        typer.echo(f"[{k}/{len(pairs)}]")
        show("A", pair.a)
        show("B", pair.b)
        answer = ""
        while answer not in ("y", "n", "s", "q"):
            answer = typer.prompt("  same event?", default="s").strip().lower()[:1]
        if answer == "q":
            break
        if answer in ("y", "n"):
            append_label(path, pair, answer == "y", utcnow())
            count += 1
        typer.echo("")
    typer.echo(f"saved {count} labels; {len(done) + count} in total.")
    typer.echo(f"Commit {path} in the content repository.")


@app.command("cluster-eval")
def cluster_eval(
    labels: Annotated[Path | None, typer.Option(help="Default: <CONTENT_CHECKOUT>/labels")] = None,
    show: Annotated[int, typer.Option(help="Rows of the grid to print")] = 12,
) -> None:
    """Pair precision and recall of clustering on the labels, over a grid of thresholds."""
    from digest.nlp.cluster import Params
    from digest.nlp.embed import FastEmbedder
    from digest.nlp.label import default_grid, evaluate, load_items, read_labels, recommend

    settings, config, engine = _local_state()
    path = labels or settings.content_checkout / "labels" / "pairs.jsonl"
    labs = read_labels(path)
    if not labs:
        typer.echo(f"no labels in {path}: run `digest label` first", err=True)
        raise typer.Exit(1)
    pool = load_items(engine)
    keys = {lab.a for lab in labs} | {lab.b for lab in labs}
    times = [i.at for i in pool if i.key in keys]
    if not times:
        typer.echo("none of the labelled items are in this state copy", err=True)
        raise typer.Exit(1)
    lo, hi = min(times) - dt.timedelta(hours=48), max(times)
    pool = [i for i in pool if lo <= i.at <= hi]
    current = Params.from_settings(config.settings.cluster)
    embedder = FastEmbedder(settings.embed_model, settings.embed_cache_dir)
    typer.echo(f"{len(labs)} labels, {len(pool)} items in the period; clustering the grid ...")
    scores, missing = evaluate(pool, labs, embedder.embed, [current, *default_grid(current)])
    if missing:
        typer.echo(f"({missing} labels skipped: their items are no longer in the state)")
    now_score, grid = scores[0], scores[1:]

    def row(s: Any) -> str:
        p = s.params
        return (
            f"  threshold {p.join_threshold:.3f}  same source {p.same_source_threshold:.2f}  "
            f"cosine weight {p.cosine_weight:.2f}  "
            f"precision {s.precision:.3f}  recall {s.recall:.3f}  (tp {s.tp}, fp {s.fp}, fn {s.fn})"
        )

    typer.echo("current settings:")
    typer.echo(row(now_score))
    typer.echo("best by recall with precision >= 0.95:")
    for s in sorted([s for s in grid if s.precision >= 0.95], key=lambda s: -s.recall)[:show]:
        typer.echo(row(s))
    best = recommend(grid)
    if best is None:
        typer.echo("no setting reaches precision 0.95; label more pairs or review the labels")
        raise typer.Exit(1)
    target = "meets" if best.recall >= 0.85 else "misses"
    typer.echo(
        f"recommended: join_threshold {best.params.join_threshold}, same_source_threshold "
        f"{best.params.same_source_threshold}, cosine_weight "
        f"{best.params.cosine_weight}, entity_weight {best.params.entity_weight} "
        f"({target} the recall target 0.85). Put these in config/settings.yaml."
    )


def main() -> None:
    app()
