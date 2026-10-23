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
| D12 | 2026-10-24 | Coverage is global, Sweden included, rather than Sweden-first: the registry adds sources from Asia, the Middle East, the Americas and Oceania, and the category caps move to Sweden 5 (weight 0.15) and World 12 | Owner | The owner wants a global digest; replaces the spec's launch target of 6–8 Swedish sources |
| D13 | 2026-10-24 | Gray area accepted for the non-commercial period: NPR, CNA, Scroll.in, DR, Rappler and MercoPress enabled as `facts_only` although their terms limit feeds to personal or non-commercial use; BBC, The Guardian and Al Jazeera enabled as `link_only` (no AI processing; name and link only) although their terms forbid AI or text mining. France 24 stays out (machine-readable TDM reservation, a hard rule). Revisit before any monetisation (open decision 9) and in the lawyer review; on any takedown request, disable the source at once | Owner | Global coverage; realistic risk judged low for a small, free, ad-free site that uses facts and links back |

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
| 2026-10-23 | Registry guard matches exact hosts: the homepage and feed hosts, plus a new optional `extra_hosts` list per source for article pages on other hosts. The blocklist is checked first, before every request, robots.txt and each redirect hop | Default | Rule L1 with no guessing about subdomains; redirects can't escape the registry |
| 2026-10-23 | robots.txt: a 4xx answer means no rules; 5xx, 429, a network error or a redirect off the registry means keep out for this run, and that verdict is not cached | Default | Common crawler practice, erring on the side of not fetching |
| 2026-10-23 | TDM: an unreadable `tdmrep.json` means no article pages for that host; a 200 that is not a JSON list counts as no file (soft 404). Page header and meta are checked on every article, and a reserved page is dropped before extraction | Default | Rule L4; conservative where the status is unclear |
| 2026-10-23 | Article text is stored already trimmed to `article_text_max_chars` (3,000), at most `max_articles_per_run` (60) pages a run; pages over the cap are not retried | Default | Keep no more text than stage 5 may send; article fetching is off by default |
| 2026-10-23 | `link_only` items store the title only, never the feed description | Default | Their terms allow no storage beyond what matching needs (stage 3 matches on titles) |
| 2026-10-23 | Feed entries older than `max_entry_age_hours` (72) are ignored; entries linking to blocklisted domains are dropped; future dates are clamped to now | Default | Keeps old and paywalled items out of clustering |
| 2026-10-23 | A feed is due `poll_grace_minutes` (5) early, never sooner than 15 minutes; after errors the interval doubles per error, capped at 24 h. A feed disabled after 10 errors is retried once a day and re-enabled on success | Default | Runs 30 min apart would otherwise skip 30-min feeds; the state can't be edited by hand, so recovery must be automatic |
| 2026-10-23 | User agent `<PRODUCT_NAME without spaces>Bot/1.0 (+<contact>)`; robots token `DigestBot` by default. A contact URL is required; the workflows fall back to this repository's URL until `SITE_URL` exists | Default | Spec section 8; the site address doesn't exist yet |
| 2026-10-23 | New setting `STATE_UPLOAD` (default false): with `github_release`, only runs with it set (pipeline.yml) upload state. `local_file` always writes its local folder | Default | Spec section 7: local commands never upload |
| 2026-10-23 | `run --once` touches no state while no source is enabled | Default | No work to do, and no failing runs every 30 minutes before the owner adds the token |
| 2026-10-23 | A run that fails midway uploads nothing more; the last checkpoint stays the newest state. Checkpoints in Phase 1: after ingest and at the end. The daily snapshot is wired in now | Default | Spec section 14, state store row |
| 2026-10-23 | Keep-alive: the run rewrites `status/last_run.json` (counts only) once per local day; pipeline.yml commits it with `[skip ci]` | Default | Spec section 13; one commit a day, no CI run for it |
| 2026-10-23 | Public logs: JSON with a constant event name and allow-listed fields only; library messages, free text and exception messages are replaced; uncaught exceptions print only their type | Default | Spec section 13, public-repository hygiene |
| 2026-10-23 | `sources-review` keeps results per environment (`local`, `github`) in `docs/sources_review.json` and renders both in `docs/SOURCES_REVIEW.md`; a `sources-review` workflow runs it on a GitHub runner and commits the result | Default | Shows which feeds fail only from GitHub's IP ranges |
| 2026-10-23 | `data_api` feeds are registered but not ingested until Phase 3 (template stories) | Spec Phase 3 | — |
| 2026-10-23 | Registry starts with 12 sources (SVT, Polisen, Riksbank, SCB, Svenska Yle, BBC, France 24, NPR, Yle News, NRK, DR, The Guardian), all disabled. Not added, because no feed URL could be found on the source's own pages: Regeringen (feed link not in the page HTML), DW, UN News, EC Press Corner (email alerts only). Sveriges Radio left out for now: its API is unmaintained and no news feed is listed. Krisinformation offers a JSON API (v3), to consider as a data source in Phase 3. The Guardian is an addition to the spec's candidate list | Default | Spec section 4: never guess feed URLs |
| 2026-10-24 | Added 8 sources for D12: Al Jazeera English, CNA, Kyodo News, Scroll.in, Rappler, CBC News, RNZ, MercoPress, all disabled. Not added: ABC Australia and Africanews (no feed listed on their own pages), The Hindu (partly paywalled) | Default | Feed URLs from each source's own site only |
| 2026-10-24 | Terms review done by Claude Code at the owner's request (owner checkpoint, spec section 4). Enabled: Global Voices (open_license, CC BY 3.0), SCB (open_license, CC BY 4.0), The Conversation (facts_only, CC BY-ND), Polisen and Riksbank (facts_only, official), SVT and NRK (facts_only; no published feed terms found, flagged for the lawyer review), Yle News and Svenska Yle (link_only with headlines: Yle's RSS terms allow headlines linking to the article, forbid modifying or copying) | Claude Code, delegated by owner | Each source's terms, quoted in its `terms:` note in `config/sources.yaml` |
| 2026-10-24 | Not enabled, for their terms: BBC (no AI or "computer analysis" without permission), NPR (personal or US 501(c)(3) use only), DR (private pages only), The Guardian, Al Jazeera, CNA, Scroll.in, RNZ (personal or non-commercial use only, several also forbid bots, mining or AI), Kyodo (no AI or mining without a paid contract), Rappler (permission needed), France 24 (site-wide TDM reservation), MercoPress (no terms found, private agency). Not enabled for other reasons: CBC (robots.txt does not answer), VOA (public domain, but its feeds stopped in March 2025) | Claude Code, delegated by owner | Kept in the registry, disabled, with the reason |
| 2026-10-24 | Default for sources without published feed terms: enable only public broadcasters and authorities (open feeds, robots allows us, no TDM reservation), as facts_only; private outlets need terms or permission | Claude Code, delegated by owner | Copyright protects expression, not facts (spec section 3); private outlets are likelier to object |
| 2026-10-24 | Embedding model `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions, Apache 2.0, 0.22 GB) via fastembed, cached in `~/.cache/fastembed` (setting `EMBED_MODEL`, `EMBED_CACHE_DIR`) | Default | The spec's example `multilingual-e5-small` is not offered by fastembed; this one has the same size and handles Swedish, Norwegian, Danish and English in one space |
| 2026-10-24 | Entities from a capitalised-phrase heuristic (the spec's named alternative), not spaCy | Default | No model downloads per language; it only carries 0.25 weight. spaCy can replace it for links-mode labels in Phase 3 |
| 2026-10-24 | Cluster score falls back to cosine alone when the item or the cluster has no entities; `cluster-eval` also tries cosine only (weight 1.0) | Default | With the spec's weights, an item without names could never pass a threshold above 0.75 |
| 2026-10-24 | Provisional `join_threshold` 0.62, from cross-source pairs on a day of real data (same-event pairs mostly 0.65–0.80, different events mostly below 0.6); final value from the owner's labels | Default | 0.78 produced 377 clusters from 378 items with this model |
| 2026-10-24 | `link_only` items that match no cluster are retried for `link_only_max_hours_apart` (24 h), then become `skipped` | Default | The matching story often forms after the link-only item arrives |
| 2026-10-24 | New optional fields: `publisher` per source (sources sharing one count as one independent source; both Yle sources use `yle`) and `category` per feed (BBC business → economy) | Default | Independence and categories need finer grain than one source |
| 2026-10-24 | Migration 0002 adds `clusters.selected_at`; a cluster counts toward its category's daily cap on the Stockholm day it was selected, and stays selected | Default | Caps must hold across runs; the spec's schema had no place for it before Phase 3 stories |
| 2026-10-24 | Independent sources include `link_only` sources; until stage 5 a cluster's category is the majority of its members' feed categories (ties to the higher-weighted category) | Default | A link-only outlet reporting the event is still an independent report |
| 2026-10-24 | No language detection in stage 2: the feed's or source's language is used | Default | Every registered source states its language |
| 2026-10-24 | Pruning approximates a cluster's close time by its last item time | Default | Close time isn't stored; the difference is 36 hours in a 30-day window |
| 2026-10-24 | `digest clusters`, `digest label` and `digest cluster-eval` refuse to run on GitHub Actions; labels store URL hashes only, no titles | Default | They print source text, and Actions logs are public |
