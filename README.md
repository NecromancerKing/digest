# Ungated

A free, minimal news website made of short, original, multi-source summaries, live at **[ungated.fyi](https://ungated.fyi)** (the name is `PRODUCT_NAME`, D20).

A scheduled GitHub Actions workflow runs the pipeline (ingest, cluster, rank, summarise, verify, publish), keeps its state in one SQLite file stored privately, and deploys a static site to Cloudflare Pages. Story files and state live in a separate private content repository; this public repository holds only code, config and docs.

## Development

Requires [uv](https://docs.astral.sh/uv/). Works the same on Windows, macOS and Linux.

```sh
uv sync                     # install Python 3.12 and dependencies
uv run pre-commit install   # lint and secret scan on every commit
cp .env.example .env        # then fill in what you need
uv run poe check            # lint, typecheck, test
uv run poe migrate          # create or upgrade the local state file
```

Tasks: `uv run poe --help`.

## Taking a story down

Nobody reviews stories before they go live (D19). To take one down, add it to `takedowns.yaml` in the content repository (GitHub's web or mobile app can edit it):

```yaml
- story: https://ungated.fyi/story/2026-10-25/some-story
  note: Taken down because it gave the wrong date for the vote.
```

The next pipeline run unpublishes it, deploys at once and lists the note on the corrections page. To act sooner, start a run: Actions → pipeline → Run workflow. Removing the entry puts the story back.

## Docs

- `docs/DECISIONS.md`: decisions, with dates and reasons
- `docs/SOURCES_REVIEW.md`: robots.txt, TDM and feed health per source (`uv run poe sources-review`)

## Licence

The code is under the [MIT licence](LICENSE). The bundled fonts (Fraunces, Figtree) keep their SIL Open Font Licence, next to them in `src/digest/publish/static/fonts/`; the map data comes from Natural Earth (public domain). Published stories are not part of this repository.
