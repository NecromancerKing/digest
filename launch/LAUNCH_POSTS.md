# Ungated launch posts

Drafts for Show HN, Reddit and dev.to. Every number here was checked against the repo on 2026-10-30; sources and sections re-checked 2026-11-08 (D38). Re-check the ones marked *(check)* on the day you post.

---

## 1. Show HN

**Where:** https://news.ycombinator.com/submit
**When:** Tuesday–Thursday, 15:00–16:00 Swedish time (14:00–15:00 UTC; 9–10 am US Eastern).
**URL field:** `https://ungated.fyi`

### Title (pick one, max 80 characters)

1. `Show HN: Ungated – a news site that runs itself on free AI tiers and ~$2/month`
2. `Show HN: Ungated – global news summaries from 80 sources, no editor, no paywall`
3. `Show HN: I built a self-running news site for the price of a coffee a month`

My pick is **1**. HN likes concrete numbers and a "how is that possible" angle. Avoid words like "revolutionary" or "unbiased"; they invite pile-ons.

### First comment (post it right after submitting)

> Hi HN, I'm Siavash. Ungated is a news site with no editor: every 30 minutes a GitHub Actions job reads about 100 news feeds from 80 sources around the world, groups reports about the same event, and has an LLM write a short summary in its own words, with a link to every source. Nobody reads a story before it goes live.
>
> Some details that might interest you:
>
> - **Cost.** Hosting is Cloudflare Pages (free), the pipeline runs on GitHub Actions (free), and state is one SQLite file stored as a release asset. Groq's and OpenRouter's free tiers do most of the AI work. Claude Haiku picks up the overflow on a hard budget of $0.075 a day, released in 4-hour steps so the evening still gets updates. Total is about $2.30 a month plus the $5 domain.
> - **Sources.** It respects robots.txt and machine-readable TDM reservations, skips paywalled sites, and never reuses source headlines or images. Each story card has a small map drawn at build time from Natural Earth instead of a photo.
> - **Grouping.** Clustering uses multilingual embeddings (MiniLM) on titles, plus a second merge pass on the model's one-line English event descriptions. Cross-language follow-ups were the hard part. On my labelled pairs, recall on titles alone stalled around 0.5 at 0.95 precision.
> - **What it isn't.** It isn't fact-checked by a human. There's a corrections page and a takedown list, and every story links its sources so you can check them.
> - The page background follows the sun where you are (dawn, day, dusk, night), worked out in the browser; the sun calculation itself sends nothing anywhere.
>
> I built it over about a week with Claude Code. It's open source (MIT): https://github.com/NecromancerKing/digest
>
> I'd love feedback on summary quality, especially any story that got something wrong.

### Questions to expect, with short answers

- **"Isn't this copyright infringement?"** Facts aren't copyrightable; it writes in its own words, links every source, honours robots.txt and TDM opt-outs, and takes a story down on request. Some sources' terms say "non-commercial use"; the site is free and has no ads.
- **"How do you stop hallucinations?"** Honestly: summaries are written only from facts extracted from the reports, and every source is linked, but there's no second-model fact check any more and no human review. Corrections go through a public page.
- **"Why not just use Google News?"** No cookies or ad trackers (just cookieless Cloudflare Web Analytics), no paywalled links, one short summary per event instead of ten headlines, and it works offline as an installable app.
- **"What does it cost at scale?"** It's a static site, so traffic is free. The AI budget is the same no matter how many readers there are.

---

## 2. Reddit

Post a few days after HN, one subreddit a day. Read each subreddit's rules first. Reply to every comment in the first couple of hours.

### r/SideProject

**Title:** `I built a news site that runs itself: 80 global sources, AI summaries, about $2.30/month to run`

> No editor, no ads, no paywall links. Every 30 minutes it reads about 100 feeds from 80 sources, groups reports about the same event and writes one short summary with links to every source. Stories get a postmark-style stamp with a little map instead of a photo, and the page colour follows the sun where you are.
>
> Open source (MIT). Stack: Python, GitHub Actions, SQLite, Cloudflare Pages, free LLM tiers (Groq, OpenRouter) with Claude Haiku as a capped overflow.
>
> https://ungated.fyi. I'd love honest feedback on what's missing.

### r/InternetIsBeautiful

The rules are strict: no self-promotion wording, and the link must go straight to the site. Use just the title and the URL.

**Title:** `A minimal news site where each event gets one short summary with links to every outlet that covered it, and the page colour follows your local sun`

### r/webdev (Showoff Saturday thread only, on Saturdays)

Lead with the front-end details: sun-following background computed before first paint, inline-SVG postmarks and maps with no extra requests, Pagefind search with no server, installable PWA with offline reading, Lighthouse 95–100.

---

## 3. Build write-up (dev.to / Medium / Indie Hackers)

**Title:** `I built a news site that runs itself for $2.30 a month`
**Tags (dev.to):** `python`, `ai`, `showdev`, `sideprojects`

> ### The idea
>
> I wanted one place to see what's happening around the world, without paywalled links, ten versions of the same headline, or ad trackers. So I built [Ungated](https://ungated.fyi): every event gets one short summary in plain words, with links to every outlet that covered it.
>
> The rule I set myself: it has to run on its own. I don't want to be an editor.
>
> ### How it works
>
> Every 30 minutes a GitHub Actions job runs a pipeline:
>
> 1. **Ingest.** Read about 100 RSS feeds from 80 sources: public broadcasters, wire services, central banks, space agencies and regional outlets on every continent. Before fetching anything it checks robots.txt and TDM (text and data mining) reservations, and it never touches paywalled sites.
> 2. **Cluster.** Embed each headline with a small multilingual model and group reports about the same event, even across languages.
> 3. **Rank.** Score clusters by how many independent outlets cover them. Sections (World, Economy, Tech & Science, Health, Entertainment, Sport) take turns, so one big story can't starve the rest.
> 4. **Extract and write.** An LLM pulls the facts from the reports, then writes a 60–120-word summary and up to five key facts in its own words.
> 5. **Publish.** Build a static site and deploy it to Cloudflare Pages about once an hour.
>
> All state lives in a single SQLite file, stored as a GitHub release asset. There's no server and no database to look after.
>
> ### Making it cost almost nothing
>
> Hosting and compute are free tiers: GitHub Actions, Cloudflare Pages. The interesting part is the AI:
>
> - Free tiers (Groq, OpenRouter) do most of the work, but they run out. Groq's daily tokens were usually gone by morning.
> - Claude Haiku is the paid overflow, with a hard cap of $0.075 a day. Before every call, the worst-case cost has to fit in what's left. The budget is released in 4-hour steps, so the evening news still gets written.
> - Stories are only rewritten every few hours, so the money goes to new events rather than small edits.
>
> The result: about $2.30 a month for AI, plus $5 a year for the domain.
>
> ### Things that went wrong
>
> - **GitHub's cron is unreliable.** Scheduled runs came hours late or not at all. A free external scheduler (cron-job.org) now triggers the workflow through the API, and GitHub's own schedule stays as a backup that skips itself.
> - **Free models churn.** One was withdrawn overnight. Another spent its whole output budget on "reasoning" until I turned that off. Another answered every request with a rate limit.
> - **Cross-language grouping is hard.** A Swedish and an English report on the same event often score lower than two different events in English. A second merge pass on the model's one-line English event descriptions fixed most of it, but it once merged a story on US methane rules into one on an Arizona permit. A similarity floor now stops that.
> - **One category ate the budget.** World news always ranked highest, so for two days nothing else got written. Categories now take turns.
>
> ### The fun parts
>
> - No photos. Each story gets a postage stamp with a postmark and a small map of where it happened, drawn at build time from public-domain Natural Earth data.
> - The page background follows the sun where you are: lilac at dawn, apricot at dusk, blue-grey at night. It's computed in your browser; the calculation sends nothing.
> - It installs as an app and works offline.
>
> ### What it isn't
>
> Nobody reads a story before it's published. Every story links its sources, there's a public corrections page, and a story can be taken down by adding one line to a file. It's a quick overview, not a replacement for journalism. The summaries only exist because reporters did the work, and every one links back to them.
>
> I built it over about a week with Claude Code. It's open source (MIT) at [github.com/NecromancerKing/digest](https://github.com/NecromancerKing/digest).
>
> If you read it for a few days, I'd love to hear what's missing.

---

## 4. One-time listings (any spare hour)

- **AlternativeTo**: add Ungated as an alternative to Google News, Ground News and Feedly. Description: "Free global news site: one short AI summary per event with links to every source, no ads, no cookies, no paywalls."
- **GitHub topics** on the repo: `news`, `news-aggregator`, `rss`, `llm`, `static-site`, `github-actions`.
- **Awesome lists**: open a PR adding the site or the repo to a relevant list (search GitHub for "awesome news" or "awesome rss").
- **Product Hunt**: reuse the Show HN title and the r/SideProject text.

Feeds for the listings: `https://ungated.fyi/feed.xml` (RSS), `https://ungated.fyi/feed.json` (JSON Feed), and one per section at `/c/<section>/feed.xml`.
