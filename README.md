# Digest

A free, minimal news website made of short, original, multi-source summaries. The product name is a placeholder (`PRODUCT_NAME`).

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

## Docs

- `docs/DECISIONS.md`: decisions, with dates and reasons
