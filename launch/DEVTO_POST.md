## The idea

I wanted one place to see what's happening around the world, without paywalled links, ten versions of the same headline, or ad trackers. So I built [Ungated](https://ungated.fyi): every event gets one short summary in plain words, with links to every outlet that covered it.

The rule I set myself: it has to run on its own. I don't want to be an editor.

## How it works

Every 30 minutes a GitHub Actions job runs a pipeline:

1. **Ingest.** Read about 100 RSS feeds from 80 sources: public broadcasters, wire services, central banks, space agencies and regional outlets on every continent. Before fetching anything it checks robots.txt and TDM (text and data mining) reservations, and it never touches paywalled sites.
2. **Cluster.** Embed each headline with a small multilingual model and group reports about the same event, even across languages.
3. **Rank.** Score clusters by how many independent outlets cover them. Sections (World, Economy, Tech & Science, Health, Entertainment, Sport) take turns, so one big story can't starve the rest.
4. **Extract and write.** An LLM pulls the facts from the reports, then writes a 60–120-word summary and up to five key facts in its own words.
5. **Publish.** Build a static site and deploy it to Cloudflare Pages about once an hour.

All state lives in a single SQLite file, stored as a GitHub release asset. There's no server and no database to look after.

## Making it cost almost nothing

Hosting and compute are free tiers: GitHub Actions, Cloudflare Pages. The interesting part is the AI:

- Free tiers (Groq, OpenRouter) do most of the work, but they run out. Groq's daily tokens were usually gone by morning.
- Claude Haiku is the paid overflow, with a hard cap of $0.075 a day. Before every call, the worst-case cost has to fit in what's left. The budget is released in 4-hour steps, so the evening news still gets written.
- Stories are only rewritten every few hours, so the money goes to new events rather than small edits.

The result: about $2.30 a month for AI, plus $5 a year for the domain.

## Things that went wrong

- **GitHub's cron is unreliable.** Scheduled runs came hours late or not at all. A free external scheduler (cron-job.org) now triggers the workflow through the API, and GitHub's own schedule stays as a backup that skips itself.
- **Free models churn.** One was withdrawn overnight. Another spent its whole output budget on "reasoning" until I turned that off. Another answered every request with a rate limit.
- **Cross-language grouping is hard.** A Swedish and an English report on the same event often score lower than two different events in English. A second merge pass on the model's one-line English event descriptions fixed most of it, but it once merged a story on US methane rules into one on an Arizona permit. A similarity floor now stops that.
- **One category ate the budget.** World news always ranked highest, so for two days nothing else got written. Categories now take turns.
- **Mastodon said no.** I wanted new stories posted to Mastodon too, but the big instances either forbid accounts that only post AI-generated content or limit link-sharing bots, and the news-focused ones aren't taking new accounts. It posts to Bluesky instead, where hashtags and topic feeds let an account with no followers be found.

## The fun parts

- No photos. Each story gets a postage stamp with a postmark and a small map of where it happened, drawn at build time from public-domain Natural Earth data.
- The page background follows the sun where you are: lilac at dawn, apricot at dusk, blue-grey at night. It's computed in your browser; the calculation sends nothing.
- It installs as an app and works offline.

## What it isn't

Nobody reads a story before it's published. Every story links its sources, there's a public corrections page, and a story can be taken down by adding one line to a file. It's a quick overview, not a replacement for journalism. The summaries only exist because reporters did the work, and every one links back to them.

I built it over about a week with Claude Code, and Claude helped me draft this post. It's open source (MIT) at [github.com/NecromancerKing/digest](https://github.com/NecromancerKing/digest).

New stories are also posted to Bluesky as [@ungated.fyi](https://bsky.app/profile/ungated.fyi). If you read it for a few days, I'd love to hear what's missing.
