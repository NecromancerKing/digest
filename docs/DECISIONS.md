# Decisions

Every choice the spec leaves open, with date, decision and reason. Newest entries at the bottom of each section. "Default" marks a choice Claude Code made that the owner can change at any time.

## Owner decisions (spec section 18, 23 Oct 2026)

| # | Date | Decision | By | Reason |
| --- | --- | --- | --- | --- |
| D1 | 2026-10-23 | The MVP runs only the `zero_cost` profile; the `vps` profile, Postgres and the admin console move to the backlog | Owner | €0 running cost; much less to build in Phases 5–6 |
| D2 | 2026-10-23 | No domain for the MVP; use the free `<project>.pages.dev` address | Owner | €0 |
| D3 | 2026-10-23 | `llm` mode with free providers: Groq first, mixed with other free providers when limits run out; paid Claude models are a later upgrade | Owner | €0, with a quality upgrade path |
| D4 | 2026-10-23 | Free tiers that may use our inputs for training are allowed; `allow_training_providers` can switch them off later | Owner | Non-commercial site; gray area accepted |
| D5 | 2026-10-23 | Code repository public, content repository private | Owner | Free Actions minutes; third-party text stays private |
| D6 | 2026-10-23 | Python stays. No database server: one SQLite file stored as a release file in the private content repository | Owner | Simplest free storage; Node.js brings no cost benefit |
| D7 | 2026-10-23 | Owner review through pull requests in the content repository | Owner | No admin server needed |
| D8 | 2026-10-23 | Windows-friendly tooling with no Make; the project folder is renamed `digest` | Owner | The owner develops on Windows |
| D9 | 2026-10-23 | The task runner is `poethepoet`, run through `uv` | Default | Declared in `pyproject.toml`; nothing extra to install |
| D10 | 2026-10-23 | The summary length is a setting, 30–60 words by default, tuned in Phase 3 | Default | Feed text is short |
| D11 | 2026-10-23 | Gray areas accepted: GitHub's terms for Actions, and Gemini's free-tier terms | Owner | Revisit after the lawyer review |

## Still open (spec section 18)

Built with the default until the owner decides.

| # | Decision | Default until decided | Needed by |
| --- | --- | --- | --- |
| 1 | Product name and production Pages project name | `PRODUCT_NAME=Digest` | Phase 6 |
| 2 | v1 edition language | English, with Swedish-ready templates | Phase 4 |
| 3 | Which sources to enable, article-page fetching, headlines in links mode | All disabled; feed-only; no headlines | Phase 1 |
| 4 | Review mode after launch | Approve everything during the shadow week, then auto-publish non-sensitive | Phase 6 |
| 5 | Utgivningsbevis and responsible editor | Not applied | Before launch |
| 6 | Budget for an IP lawyer review | Required before launch; amount open | Phase 6 |
| 7 | Paid AI upgrade | Off (`LLM_DAILY_BUDGET_USD=0`) | After the MVP |
| 8 | External scheduler for punctual morning runs | Off | Phase 6, if the 06:45 target slips |
| 9 | Monetisation | None in v1 | After launch |
| 10 | Licence for the public code repository | None yet (all rights reserved) | Before launch |

## Implementation decisions

| Date | Decision | By | Reason |
| --- | --- | --- | --- |
| 2026-10-23 | GitHub repositories: public `NecromancerKing/digest` (code) and private `NecromancerKing/digest-content` (content), created with `gh` | Owner asked Claude Code to create them | Spec Phase 0 |
| 2026-10-23 | Python 3.12 pinned in `.python-version`; dependencies locked in `uv.lock` | Default | Lowest version the spec allows; widest library support |
| 2026-10-23 | CLI built with Typer | Default | Small, typed, works the same on every OS |
| 2026-10-23 | Secret scanner: `detect-secrets` (Python) in pre-commit, with `.secrets.baseline` | Default | Installs through pre-commit without a Go toolchain, so it works on Windows |
| 2026-10-23 | `pyright` in standard mode everywhere, strict on `state`, `ingest`, `gates`, `llm` | Spec section 17 | — |
| 2026-10-23 | `StateStore` is one class with the logic (newest good file, fallback, prune after upload, snapshot retention) over a minimal `Blobs` backend: a local folder (`local_file`) or release assets (`github_release`) | Default | The fallback and pruning rules are written and tested once |
| 2026-10-23 | State file names carry millisecond UTC timestamps (`state-20261023T090000000Z.sqlite.gz`) | Default | Unique per checkpoint and sortable by name |
| 2026-10-23 | Weekly snapshots: the newest snapshot of each of the 8 most recent ISO weeks not already covered by the 14 daily ones | Default | Spec says "14 daily and 8 weekly" without defining weekly |
| 2026-10-23 | Snapshots are made with SQLite's backup API, then `full_text` is set to NULL and the file is vacuumed | Default | Rule L5: no text survives in free pages |
| 2026-10-23 | New setting `STATE_RELEASE_TAG` (default `pipeline-state`) | Default | Lets the integration test use a throwaway release instead of production |
| 2026-10-23 | New setting `CONFIG_DIR` (default `config`) | Default | Tests and tools can point at another config folder |
| 2026-10-23 | Sources marked `enabled: true` must have `terms_checked_on`; `link_only` sources may not set `fetch_article_pages`; source hosts on the blocklist fail validation | Default | Enforces the owner review and rules L1, L2, L6 at startup |
| 2026-10-23 | Category weights (Sweden 0.3, World 0.3, Economy 0.2, Tech & Science 0.1, Climate 0.1, Sport 0) | Default | Spec gives the formula but no values; tuned in Phase 2 |
| 2026-10-23 | `config/blocklist.yaml` starts with major paywalled Swedish and international outlets, including partly paywalled Aftonbladet and Expressen | Default | Conservative; the owner can remove entries after a terms review |
| 2026-10-23 | `llm_providers.yaml` ships without base URLs or Gemini model ids; Phase 3 fills them from the providers' docs | Spec section 9 | "Never guess them" |
| 2026-10-23 | Project folder stays `NewsBites` locally until the owner renames it to `digest` (VS Code holds the folder open) | Default | Renaming an open workspace folder fails on Windows |
| 2026-10-23 | `SPEC.md` and `SPEC.v1.md` are git-ignored, not published in the public repository | Default | The owner can decide to publish them |
