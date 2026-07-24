# AI-News Source & Curation Pipeline Research

Wayfinder ticket [#5 "Decide the AI-news source & curation pipeline"](https://github.com/ivzc07/bienwabot/issues/5): decide where the AI news comes from and how it is curated into short, human-sounding Spanish posts for the bien.mx WhatsApp group.

Research date: 2026-07.
Every factual claim is cited to a primary source (official API docs, first-party feeds, PyPI, official GitHub READMEs).

Two decisions are already locked and constrain this one:
the transport is [Evolution API](https://github.com/ivzc07/bienwabot/issues/3) (Baileys-based, dockerized, Python talks to it over HTTP + webhook), and the brain is [Pydantic AI wrapping DeepSeek's non-thinking chat model](https://github.com/ivzc07/bienwabot/issues/4).
So this pipeline is a scheduled Python job that pulls items from the web, curates them, has DeepSeek write a Spanish post, and hands the result to the same agent process that posts to Evolution.

---

## What actually constrains the choice

Four things do the deciding here; the rest is noise.

1. **The audience is a Mexican general group, not researchers.**
The brief is bien.mx, Spanish-speaking, general audience - "mostly news, light replies."
So the source set should favor items a normal person finds interesting (a new model launches, a product ships, a company does something notable), not raw research volume.
That pushes ranking and filtering to matter more than breadth of sources.

2. **It must be cheap and self-hosted.**
Hosting is a cheap VPS / Coolify, so the source layer should be keyless or free wherever possible, and the summarization cost per post has to stay tiny.
This favors public feeds (RSS, HN, arXiv) over paid APIs, and a small DeepSeek call per accepted item over anything heavy.

3. **It is unofficial and ban-sensitive.**
Posts land in a real WhatsApp group through an unofficial gateway, so the pipeline must never fire a burst of near-duplicate links, and it must have a hard "already posted" gate.
Dedup and a persistent posted-store are load-bearing, not nice-to-haves.

4. **DeepSeek has no embeddings endpoint.**
The DeepSeek API exposes only chat/completions-family endpoints - no embeddings ([DeepSeek API reference](https://api-docs.deepseek.com/api/create-chat-completion)).
So semantic near-duplicate detection cannot lean on DeepSeek; dedup must be cheap and deterministic (stable IDs, canonical URLs, content hashes), with any embedding step deferred to the fog.

---

## 1. Candidate sources

### Hacker News - via the Algolia Search API (recommended primary)

- Two official APIs exist.
The **Firebase API** (`https://hacker-news.firebaseio.com/v0/`) returns `topstories.json` / `newstories.json` / `beststories.json` as up to 500 IDs, then one `item/<id>.json` call each for `score`, `title`, `url`, `time`, `descendants` - keyless, and "There is currently no rate limit" ([HackerNews/API](https://github.com/HackerNews/API)).
The N+1 fetch is its downside.
- The **Algolia HN Search API** (`http://hn.algolia.com/api/v1/`) is the better fit: one call returns fully-hydrated stories, filterable.
`/search_by_date?tags=story&numericFilters=points>100&query=AI` returns recent stories above a points threshold in a single request; tags AND by default and OR inside parentheses; `numericFilters` covers `points`, `num_comments`, `created_at_i`; pagination via `page` / `hitsPerPage` ([Algolia HN API](https://hn.algolia.com/api)).
Keyless, free, no documented rate limit ([algolia/hn-search](https://github.com/algolia/hn-search)).

**Why primary:** HN is already the single best real-time aggregator of what the tech world finds notable, and the points threshold is a free, built-in quality signal.
One keyless query gives ranked, deduplicated-at-source, hydrated items.

### arXiv - optional, secondary

- `http://export.arxiv.org/api/query`, filter by category with `cat:` (`cs.AI`, `cs.CL`, `cs.LG`, `cs.CV`), `sortBy=submittedDate&sortOrder=descending`, Atom XML response, `max_results` up to 2,000 ([arXiv API manual](https://info.arxiv.org/help/api/user-manual.html)).
- Usage policy asks for a **3-second delay** between calls ([arXiv API manual](https://info.arxiv.org/help/api/user-manual.html)).

**Verdict:** Keep it optional and low-weight.
Raw papers are mostly too technical for a general Mexican audience; include only when something already trending on HN is a paper, or gate arXiv items behind a stricter relevance check.

### First-party RSS feeds of major AI orgs (recommended secondary set)

Verified live and directly fetchable (returned valid feeds at research time):

| Source | Feed URL |
|---|---|
| OpenAI news | `https://openai.com/news/rss.xml` |
| Google DeepMind blog | `https://deepmind.google/blog/rss.xml` |
| Google AI (blog.google) | `https://blog.google/technology/ai/rss/` |
| Hugging Face blog | `https://huggingface.co/blog/feed.xml` |
| Microsoft Research | `https://www.microsoft.com/en-us/research/feed/` |
| MIT Technology Review AI | `https://www.technologyreview.com/topic/artificial-intelligence/feed/` |
| TechCrunch AI | `https://techcrunch.com/category/artificial-intelligence/feed/` |
| VentureBeat AI | `https://venturebeat.com/category/ai/feed/` |

Feed exists but could not be fetched first-party at research time (host blocked the fetch; confirm at build): The Verge AI `https://www.theverge.com/rss/ai-artificial-intelligence/index.xml` and Ars Technica AI `https://arstechnica.com/ai/feed/` ([Feedspot directories](https://rss.feedspot.com/verge_rss_feeds/)).

No first-party RSS (do not rely on them): **Anthropic** (anthropic.com/rss.xml is 404; only community mirrors ([anthropic-rss-feed](https://github.com/taobojlen/anthropic-rss-feed))), **Meta AI blog** (community feeds only ([RSSHub issue](https://github.com/DIYgod/RSSHub/issues/16938))), and **Google Research** (`research.google/blog/rss` is cited only by third parties, unconfirmed as official).

**Why this set:** first-party vendor blogs are where launches are announced, which is exactly the "a new model / product shipped" news a general audience cares about.
RSS is keyless, cacheable, and parseable with one library.

### Rejected / not worth the cost

- **Reddit** (e.g. `r/MachineLearning/.rss` or `.json`): Reddit moved to a paid API on 1 July 2023.
Non-OAuth traffic is throttled to 10 queries/min and blocked at scale; the free tier is 100 queries/min per OAuth client with no commercial use ([Reddit Data API wiki](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki)).
The OAuth overhead and terms are not worth it when HN already covers the same links.
- **Papers with Code**: shut down (sunset 24 July 2025 by Meta; domain redirects to Hugging Face trending papers) ([Coursera writeup](https://www.coursera.org/articles/papers-with-code)).
Its de-facto replacement is Hugging Face Papers (`https://huggingface.co/papers`), usable later if paper coverage is wanted.
- **TLDR AI newsletter**: no official first-party RSS, only community-generated feeds ([tldr.tech/ai](https://tldr.tech/ai)) - unreliable, skip.

---

## 2. Curation

The pipeline turns a raw pool of items into a small, deduplicated, ranked, quality-filtered shortlist before any DeepSeek call is spent.

### Deduplication (deterministic, no embeddings)

Three cheap layers, because DeepSeek has no embeddings endpoint ([DeepSeek API reference](https://api-docs.deepseek.com/api/create-chat-completion)):

1. **Stable source ID.**
HN gives a stable integer `id`, arXiv a stable `id`/DOI, RSS items a `guid` ([HackerNews/API](https://github.com/HackerNews/API)).
Key the posted-store by `(source, id)`.
2. **Canonical URL.**
Normalize scheme/host, strip tracking query params (UTM etc.), lowercase host, drop trailing slash - so the same article surfaced by HN and by a vendor feed collapses to one key.
3. **Content hash.**
SHA-256 of the normalized `title` (plus any short text) catches the same story reposted under a different URL.

Any item whose ID, canonical URL, or content hash is already in the posted-store is dropped before ranking.
Near-duplicate semantic detection (embeddings) is deliberately deferred - it needs a non-DeepSeek embedder (a local `sentence-transformers` model or another provider) and is not worth the weight for launch.

### Ranking (relevance to a Mexican general audience)

A cheap, transparent score computed before any model call:

- **Source authority weight** - a first-party launch (OpenAI/DeepMind/HF) outranks a random blog.
- **Popularity signal** - HN `points` and `num_comments`, already returned by Algolia; a points floor (e.g. `points>100`) is the first free quality gate ([Algolia HN API](https://hn.algolia.com/api)).
- **Recency** - decay by age using the item timestamp (`time` / `created_at_i` / feed `published`).
- **Optional LLM relevance gate** - for borderline items, one cheap DeepSeek JSON call returns `{relevant: bool, audience_fit: 0-1, reason}`; this reuses the same typed-output pattern the framework doc already chose for the reply gate, and is only spent on items that pass the free filters.

Take the top N per run after ranking.

### Quality / recency filtering

- Drop items older than a freshness window (e.g. 24-48h) so the group never gets stale news.
- Drop items missing a resolvable URL or a usable title.
- Apply the points floor for HN and a stricter relevance threshold for arXiv.

---

## 3. Summarization into Spanish posts

Each accepted item gets one DeepSeek call that produces the post.

- **Model:** the **non-thinking** DeepSeek chat model (`deepseek-v4-flash` as of 2026-07; the legacy `deepseek-chat` ID was retired and remapped on 2026-07-24, so verify the current ID at build) - consistent with the framework doc's tool-calling caveat, which reserves the thinking model for tool-free work ([DeepSeek pricing/quick-start](https://api-docs.deepseek.com/quick_start/pricing)).
- **Structured output:** use JSON mode (`response_format={'type':'json_object'}`), which requires the word "json" in the prompt plus an example, and a sane `max_tokens` to avoid truncation ([DeepSeek JSON mode](https://api-docs.deepseek.com/guides/json_mode)).
The schema is a typed post object, e.g. `NewsPost(headline_es, body_es, url, hashtags?)`, validated by Pydantic AI before it is ever sent - the same guardrail pattern as the reply gate.
- **Style contract (feeds the persona ticket, [#6](https://github.com/ivzc07/bienwabot/issues/6)):** short (a couple of sentences, WhatsApp-length), natural Mexican Spanish, no marketing hype, one clear "what happened and why it matters" line, then the link.
The persona ticket owns the exact voice; this pipeline only guarantees the shape (Spanish, short, one link, schema-valid).
- **Link handling:** post the canonical URL as plain text (WhatsApp auto-previews); never invent or shorten links.
- **Cost:** `deepseek-v4-flash` is ~$0.14 / 1M input (cache-miss) and $0.28 / 1M output ([DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing)).
A post is a few hundred tokens each way, so summarization cost is effectively negligible at any realistic group cadence; the token-budget number is quantified in the fog once cadence is set.

---

## 4. Freshness / cadence hooks

- **Scheduling:** run the pull-curate-post loop on a timer with **APScheduler** (MIT, on PyPI, supports both cron-style and interval jobs ([APScheduler on PyPI](https://pypi.org/pypi/APScheduler/json))), inside the same long-running process the framework doc described (one process, webhook trigger + scheduler trigger).
- **Posted-store:** a small persistent table (the same one the bot already needs for per-group memory) keyed by `(source, id)` / canonical URL / content hash; every posted item is written here, and every candidate is checked against it first.
This is the anti-repost gate.
- **Per-run cap:** each scheduled run posts at most a small number of items (e.g. 1-3), so a busy news day never becomes a flood - the exact number and the human-like spacing between posts belong to the cadence fog and the anti-ban ticket ([#8](https://github.com/ivzc07/bienwabot/issues/8)).
- **Fetch libraries:** **feedparser** (BSD-2, RSS/Atom, maintained ([feedparser on PyPI](https://pypi.org/pypi/feedparser/json))) for the RSS set, **httpx** (BSD-3 ([httpx on PyPI](https://pypi.org/pypi/httpx/json))) for the JSON APIs (HN Algolia, arXiv).

---

## Recommendation

**Source:** Hacker News via the keyless **Algolia Search API** as the primary feed (`search_by_date`, `tags=story`, `numericFilters=points>N`), plus a curated set of **first-party AI-org RSS feeds** (OpenAI, DeepMind, Google AI, Hugging Face, Microsoft Research, plus the general-tech AI feeds) as a secondary launch-announcement layer.
arXiv (`cs.AI`/`cs.CL`/`cs.LG`) is optional and low-weight, gated behind a stricter relevance check.
Reddit, Papers with Code, and newsletter feeds are rejected (paid/OAuth, dead, or no first-party feed).

**Curation:** deterministic three-layer dedup (stable ID, canonical URL, SHA-256 content hash) against a persistent posted-store; a cheap transparent ranker (source authority + HN points/comments + recency decay) with an optional per-item DeepSeek JSON relevance gate only for borderline items; freshness window and points/relevance floors as quality filters.

**Summarization:** one non-thinking DeepSeek chat call per accepted item, JSON mode, into a Pydantic-validated `NewsPost` object - short, natural Mexican Spanish, one "what happened / why it matters" line plus the canonical link.
Voice specifics defer to the persona ticket.

**Cadence hooks:** APScheduler-driven loop in the existing process, a per-run cap of a few items, and the posted-store as the hard anti-repost gate.
feedparser for RSS, httpx for JSON.

### Design notes that feed other tickets and the fog

- **Feeds the architecture ticket ([#9](https://github.com/ivzc07/bienwabot/issues/9)):** the news pipeline is the scheduler leg of the "one process, two triggers" shape - APScheduler job -> fetch (httpx/feedparser) -> curate -> DeepSeek summarize -> post to Evolution; it shares the single per-group SQLite/Redis store (memory + posted-store) with the webhook leg.
- **Feeds the persona ticket ([#6](https://github.com/ivzc07/bienwabot/issues/6)):** this pipeline fixes the post *shape* (Spanish, short, one link, schema-valid); the persona ticket fixes the *voice* inside that shape.
- **Feeds the anti-ban ticket ([#8](https://github.com/ivzc07/bienwabot/issues/8)):** the per-run cap and the spacing between posts are set there, not here; the pipeline only guarantees it will never emit duplicates or a burst.
- **Graduates cadence fog toward a ticket:** with the source layer and per-run cap defined, "posting cadence & timing" and "cost / token budget" are now answerable - cadence is a number of posts/day times the human-like spacing (anti-ban), and the token budget is that post count times a few-hundred-token DeepSeek call, so both are now sharp enough to ticket once persona and anti-ban land.

### Flags / verify-at-build

- The Verge AI and Ars Technica AI feed URLs were confirmed via directories but not fetched first-party - confirm before wiring them in.
- Google Research first-party RSS is unconfirmed - treat as community-only.
- DeepSeek model IDs rotate (the `deepseek-chat`/`deepseek-reasoner` line retired 2026-07-24) - verify the current non-thinking chat ID at `api-docs.deepseek.com` at build time.
- DeepSeek off-peak discount pricing is not on the current V4 pricing page (it existed on older models) - do not assume it.
