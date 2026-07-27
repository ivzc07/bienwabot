# X Home-Timeline Access Research

Wayfinder ticket [#51 "Find out what X home-timeline access a Premium account actually buys"](https://github.com/ivzc07/bienwabot/issues/51), part of map [#50](https://github.com/ivzc07/bienwabot/issues/50): decide how `rebe-agent` can read the operator's own X home timeline, what each route costs, what it allows, and what one fetched post actually contains.

Research date: 2026-07-27.
Every claim below is cited to a page fetched on that date, and the pages are X's own (docs.x.com, developer.x.com, help.x.com, devcommunity.x.com) wherever the claim is about X.
Where something could not be verified first-party it is called out as unverified rather than guessed at.

The question that started this is the operator's: *"check if I can use my premium X account for it."*
The short answer is no, and the reason is worth understanding before any of the numbers below matter.

---

## What actually constrains the choice

Four things decide this one.

1. **The operator's subscription and the developer platform are two unrelated products.**
X Premium is a consumer subscription that changes what the operator's *account* can do in the app.
The X API is a developer product billed separately in credits.
Nothing on the Premium feature page touches programmatic access.

2. **The home timeline is a private, per-user resource.**
It is not public data.
Every route that reads it has to prove it is acting for that specific logged-in user, which rules out app-only bearer tokens, rules out public scrapers, and means the operator has to personally authorise whatever reads it.

3. **X priced the API per post read in February 2026, and the old free tier is gone.**
Any design that reads the timeline in a tight loop now has a running dollar cost attached to it, so polling frequency became a budget decision rather than a rate-limit decision.

4. **The bot's job needs very few posts.**
The [cadence spec](posting-cadence-spec.md) sets a soft target of ~4 news posts per weekday and ~2 per weekend day.
Rebe does not need to read the whole firehose.
She needs a modest candidate pool per day, which is what makes the paid route affordable at all.

---

## 1. Does X Premium buy any API access?

**No.**

The [About X Premium](https://help.x.com/en/using-x/x-premium) page lists the three consumer tiers - Basic, Premium, Premium+ - and enumerates their features: edit post, longer posts, longer video uploads, creating a Community, blue checkmark, reduced or removed ads, creator revenue sharing and subscriptions, ID verification, Media Studio, Articles, Radar Search, and higher Grok usage limits.
The word "API" does not appear as a benefit anywhere in that list.
Pricing is stated as "localized pricing starting at $3/month or $32/year" ([About X Premium](https://help.x.com/en/using-x/x-premium)).

Developer access is a separate purchase made in a separate console.
[Getting access](https://docs.x.com/x-api/getting-started/getting-access) is three steps: sign in at console.x.com with an X account, accept the Developer Agreement and Policy, create an app, and save the generated keys.
Signing in with an X account is the only connection between the two products - the subscription grants nothing on the developer side, and the developer side does not require a subscription.

**So the Premium subscription the operator already pays for is irrelevant to this feature.**
It is not wasted - she keeps it for the account itself - but it buys zero timeline reads.

### What happened to the Free / Basic / Pro tiers

The ticket asked us to confirm the widely-repeated claim that the Free tier is write-only.
That claim describes a world that no longer exists, and repeating it would have sent the build down the wrong path.

On **6 February 2026** X launched pay-per-use pricing and retired free scaled access for ordinary developers ([devcommunity announcement](https://devcommunity.x.com/t/announcing-the-launch-of-x-api-pay-per-use-pricing/256476)):

> "Public Utility Apps: Starting now, only apps classified as Public Utility Apps will continue to receive free scaled access."
> "Legacy Free API Users: If you've been recently active on our Legacy Free tier, you'll be transitioned to Pay-Per-Use with a one-time $10 voucher to get you started."
> "Basic & Pro Plans: These tiers remain available, and you can easily opt-in to Pay-Per-Use if it better suits your needs."

The current [pricing page](https://docs.x.com/x-api/getting-started/pricing) describes only the pay-per-use model - "The X API uses **pay-per-usage** pricing. No subscriptions - pay only for what you use" - and the [developer platform home page](https://developer.x.com/) frames the $200 and $5,000 subscriptions as the "Old Model."

The practical reading: a new developer signing up today lands on pay-per-use credits.
There is no free allowance to build against, and the write-only Free tier is a 2023-2025 artefact.
A "Public Utility App" classification exists but there is no first-party page describing how a WhatsApp news bot would qualify, so we should not plan around it.

---

## 2. The official API route

### The endpoint

`GET /2/users/{id}/timelines/reverse_chronological` - "Retrieves a reverse chronological list of Posts in the authenticated User's Timeline" ([API reference](https://docs.x.com/x-api/users/get-timeline)).

The `{id}` path parameter is constrained: "The value must be the same as the authenticated user" ([API reference](https://docs.x.com/x-api/users/get-timeline)).
You cannot read anyone else's home timeline, only your own.

### Auth

The [Timelines overview](https://docs.x.com/x-api/posts/timelines/introduction) is explicit:

> "This endpoint requires OAuth 1.0a User Context or OAuth 2.0 Authorization Code with PKCE."

App-only bearer tokens do not work here, and the [rate-limits page](https://docs.x.com/x-api/fundamentals/rate-limits) confirms it by listing no per-app allowance for this endpoint at all.

For `rebe-agent` this means a one-time interactive step: the operator opens an authorisation URL in a browser, approves the app against her own X account, and the bot stores the resulting refresh token.
This is a genuinely different operational shape from the current keyless HN/RSS fetch, and it is the main non-money cost of this route.

### Coverage and rate limits

| Property | Value | Source |
|---|---|---|
| Auth | OAuth 2.0 Authorization Code with PKCE, or OAuth 1.0a user context | [Timelines overview](https://docs.x.com/x-api/posts/timelines/introduction) |
| Per-app rate limit | none - app-only access not available | [Rate limits](https://docs.x.com/x-api/fundamentals/rate-limits) |
| Per-user rate limit | **180 requests / 15 min** | [Rate limits](https://docs.x.com/x-api/fundamentals/rate-limits) |
| `max_results` | 1-100 per request | [API reference](https://docs.x.com/x-api/users/get-timeline) |
| Incremental reads | `since_id`, `until_id`, `start_time`, `end_time` | [API reference](https://docs.x.com/x-api/users/get-timeline) |
| Filtering | `exclude=replies,retweets` | [API reference](https://docs.x.com/x-api/users/get-timeline) |
| Monthly cap | **2,000,000 Post reads per billing cycle** on pay-per-use | [Pricing](https://docs.x.com/x-api/getting-started/pricing) |

The rate limit is not the binding constraint.
180 requests per 15 minutes is one request every five seconds; the bot will make a handful per hour.
The binding constraint is money, covered next.

**A coverage discrepancy worth flagging.**
The [Timelines overview](https://docs.x.com/x-api/posts/timelines/introduction) says the home timeline serves the "Most recent 3,200 Posts (or 7 days)."
The [changelog entry dated 27 July 2022](https://docs.x.com/changelog) says the endpoint "can return every post created on a timeline over the last 7 days and the most recent 800 regardless of the creation date."
Both are X's own pages and they disagree on 800 vs 3,200.
Either number is far more than a bot polling every hour needs, so it does not change the decision - but do not build anything that assumes deep backfill without testing it.

### Cost

Reads are charged **per resource returned**, not per request ([Pricing](https://docs.x.com/x-api/getting-started/pricing)):

- **Posts: Read - $0.005 per resource.**
- Resources are **deduplicated within a 24-hour UTC window**: "If you request and are charged for a resource (such as a Post), requesting the same resource again within that window will not incur an additional charge." X calls this a "soft guarantee."

The dedup rule is the single most important economic fact here, because a polling loop re-reads the same posts constantly and only pays for each distinct post once per UTC day.
Cost therefore tracks **how many distinct posts the bot sees per day**, not how often it polls.

The home timeline does **not** qualify for the cheap Owned Reads rate.
The [$0.001 Owned Reads list](https://devcommunity.x.com/t/x-api-pricing-update-owned-reads-now-0-001-other-changes-effective-april-20-2026/263025) covers `/2/users/{id}/tweets`, `/mentions`, `/liked_tweets`, `/bookmarks`, `/followers`, `/following`, `/blocking`, `/muting`, and the list endpoints - `timelines/reverse_chronological` is absent from it.
So the timeline bills at the full $0.005.

Working the numbers for this bot:

| Distinct posts read per day | Cost per day | Cost per month (30d) |
|---|---|---|
| 100 | $0.50 | ~$15 |
| 200 | $1.00 | ~$30 |
| 500 | $2.50 | ~$75 |

Rebe needs ~4 posts out the door on a weekday.
Even a 50:1 rejection ratio through the worthiness gate only needs ~200 candidates a day.
**A realistic budget is $15-30/month**, and it is enforceable: the console supports a hard **spending limit** per billing cycle that blocks requests once hit, plus auto-recharge with a documented 5-minute-per-top-up safeguard ([Pricing](https://docs.x.com/x-api/getting-started/pricing)).
Usage is also readable programmatically from `GET /2/usage/tweets`.

The practical design that keeps this in range: poll a few times per posting window rather than continuously, always pass `since_id` so each call returns only what is new, set `max_results` to a real ceiling, and set the console spending limit at roughly double the expected spend so a runaway loop costs an annoyance rather than a bill.

---

## 3. The logged-in browser session route

The alternative is to drive a real Chrome with the operator's own X login - Playwright, a persisted profile, scroll the home timeline, read the DOM.

### What it can read

Everything the operator can see in the app, which is strictly more than the API gives: the algorithmic "For You" feed as well as "Following", the rendered tweet card itself (which is exactly the screenshot artefact the map wants), and any UI-only affordance.
It also costs nothing per read.

### Why we are not recommending it

**X's own automation rules prohibit it by name, and name the penalty.**
From [Automation rules](https://help.x.com/en/rules-and-policies/x-automation), updated April 2026, in the "Don't" list:

> "Use non-API-based forms of automation, such as scripting the X website. The use of these techniques may result in the permanent suspension of your account."

The same page opens by putting the liability on the account holder: "You are ultimately responsible for the actions taken with your account, or by applications associated with your account."

The [Terms of Service](https://x.com/en/tos) say the same thing in contract language, prohibiting users from:

> "access or search or attempt to access or search the Services by any means (automated or otherwise) other than through our currently available, published interfaces"

and stating that "crawling or scraping the Services in any form, for any purpose without our prior written consent is expressly prohibited."
The suspension clause is broad: X "may suspend or terminate your account or cease providing you with all or part of the Services at any time if we reasonably believe: (i) you have violated these Terms or our Rules and Policies..."

**Be concrete about what this risks.**
The account being automated is the operator's real, personal, Premium-subscribed X account - her handle, her following graph, her history, and the checkmark she pays for.
The documented penalty is permanent suspension of that account, not a warning and not a throttle.
The gain over the paid route is roughly $20 a month.
That is a bad trade, and it is not our account to gamble.

**It is also the more fragile engineering.**
X's web app is an unversioned React SPA with obfuscated class names.
Selectors break without notice and with no changelog, no deprecation window, and no support channel.
Sessions expire, and re-authenticating a headless browser through X's login flow is exactly the kind of thing that trips anti-automation checks and lands the account in a verification loop.
A bot whose news leg silently dies on a Tuesday because a CSS class changed is a worse bot than one that costs $20.

**Verdict: rejected.** Not on brittleness - on the operator's account.

---

## 4. Third-party / unofficial API resellers

Verified first-party by fetching the vendor's own docs, so we can be precise about what they do and do not offer.

**twitterapi.io** is a real, operating service.
Its [pricing page](https://twitterapi.io/pricing) quotes $0.15 per 1,000 tweets returned (1 USD = 100,000 credits, 15 credits per returned tweet), with a $0.00015 minimum per call - roughly 33x cheaper than X's $0.005.

But its [endpoint catalogue](https://docs.twitterapi.io/introduction) does **not include a home timeline**.
The reads on offer are user timelines, last tweets, followers, followings, mentions, replies, quotes, retweeters, thread context, advanced search, list timelines, communities, trends and Spaces.
`get_user_timeline` is a *user's own posts*, not the authenticated home feed.

It does expose a `user_login_v2` endpoint plus write actions and a `bookmarks_v2` read - meaning the only way to get anything account-scoped is to hand a third party the operator's X credentials.
That would be a credential handover to an unrelated company, and the resulting activity is precisely the non-API automation that [X's automation rules](https://help.x.com/en/rules-and-policies/x-automation) say "may result in the permanent suspension of your account."
The same vendor also links a "buy twitter accounts" page from its own navigation, which tells you what neighbourhood this is.

**Verdict: does not solve our problem, and the part of it that comes closest carries the same account risk as scraping plus a credential handover.** Rejected.

Other names surface in search results (xpoz.ai and similar) with pricing claims we could not confirm against a first-party endpoint list, and none of them advertised an authenticated home timeline.
**Treat all of them as unverified.**

---

## 5. Field inventory - what one fetched post actually contains

This is the part the downstream tickets hang on: dedup keys, worthiness scoring, and the screenshot artefact all need to know what is in hand.

The response shape is `data` (the posts), `includes` (expanded objects - `users`, `tweets`, `media`, `polls`, `places`, `topics`), and `meta` (`newest_id`, `oldest_id`, `next_token`, `previous_token`, `result_count`) ([API reference](https://docs.x.com/x-api/users/get-timeline)).
Nothing beyond `id` and `text` arrives unless explicitly requested via `tweet.fields`, `user.fields`, `media.fields` and `expansions` - which is good, because unrequested fields are unpaid-for weight.

### Required request parameters to get a full post

```
tweet.fields = id,text,created_at,author_id,conversation_id,public_metrics,
               referenced_tweets,attachments,entities,note_tweet,lang,
               possibly_sensitive,context_annotations
expansions   = author_id,attachments.media_keys,referenced_tweets.id,
               referenced_tweets.id.author_id,
               referenced_tweets.id.attachments.media_keys
user.fields  = id,name,username,profile_image_url,verified,protected,public_metrics
media.fields = media_key,type,url,preview_image_url,variants,alt_text,width,height
```

Every one of those values is drawn from the enumerated options on the [API reference](https://docs.x.com/x-api/users/get-timeline).

### The inventory

| What we need | Field | Where it lives | Notes for downstream |
|---|---|---|---|
| Stable post ID | `id` | `data[]` | The dedup key. String, not int - 19 digits, must not be parsed as a number. |
| Post text | `text` | `data[]` | Truncated for long posts; see `note_tweet`. |
| Full text of a long post | `note_tweet` | `data[]` | Long-form posts exceed `text`; required or Rebe reads half a sentence. |
| Timestamp | `created_at` | `data[]` | ISO 8601. Drives the freshness window. |
| Author ID | `author_id` | `data[]` | Join key into `includes.users`. |
| Author handle | `username` | `includes.users[]` | The `@handle`. Needed for the screenshot and the off-limits rule. |
| Author display name | `name` | `includes.users[]` | Needed for the screenshot. |
| Author avatar | `profile_image_url` | `includes.users[]` | Needed if we render the card ourselves. |
| Author verified / protected | `verified`, `protected` | `includes.users[]` | `protected` is the hard gate for the "whose posts are off-limits" fog in [#50](https://github.com/ivzc07/bienwabot/issues/50). |
| Likes | `public_metrics.like_count` | `data[]` | Worthiness signal, the HN-points analogue. |
| Reposts | `public_metrics.retweet_count` | `data[]` | Worthiness signal. |
| Replies | `public_metrics.reply_count` | `data[]` | Worthiness signal. |
| Quotes | `public_metrics.quote_count` | `data[]` | Worthiness signal. |
| Bookmarks | `public_metrics.bookmark_count` | `data[]` | Worthiness signal. |
| Impressions | `public_metrics.impression_count` | `data[]` | Present in the schema; verify at build whether it is populated for others' posts. |
| Media URLs | `url`, `preview_image_url`, `variants` | `includes.media[]` | Photo `url`; video via `variants` (`bit_rate`, `content_type`, `url`) and `preview_image_url`. |
| Media type / alt text | `type`, `alt_text` | `includes.media[]` | `type` is `photo`, `video`, or `animated_gif`. |
| Quote-tweet / reply / RT structure | `referenced_tweets[]` | `data[]` | `type` is one of `quoted`, `replied_to`, `retweeted`. Expand `referenced_tweets.id` to get the quoted post's body into `includes.tweets`. |
| Thread membership | `conversation_id` | `data[]` | The root post ID; lets us reconstruct or skip threads. |
| Links inside the post | `entities.urls` | `data[]` | Parsed URLs with positions - the bridge to the existing canonical-URL dedup. |
| Language | `lang` | `data[]` | Timeline posts are mostly English; feeds the translation prompt. |
| Sensitivity flag | `possibly_sensitive` | `data[]` | A free pre-filter before anything reaches a WhatsApp group. |
| Topic hints | `context_annotations` | `data[]` | Semantic annotations for people, places, products, topics - a free relevance signal. |

Field meanings and the `public_metrics` / `referenced_tweets` sub-field lists are from the [data dictionary](https://docs.x.com/x-api/fundamentals/data-dictionary); availability is from the [endpoint reference](https://docs.x.com/x-api/users/get-timeline).

### What the API does not give you

**A picture of the tweet.**
The API returns data, never a rendered card, so the screenshot the map calls for has to be produced separately.
Two ways exist and both are worth carrying into the screenshot ticket:

1. **Render the card ourselves** from the fields above - avatar, display name, handle, text, media, metrics, timestamp - with our own HTML/CSS template and a headless screenshot. Full control, no dependency on X's markup, and it is our own layout so it cannot break under us.
2. **X's oEmbed API**, `https://publish.x.com/oembed`, which is documented as requiring no authentication and having no rate limit, returning embed HTML plus `author_name` and `author_url` ([oEmbed API](https://docs.x.com/x-for-websites/oembed-api)). Reading a published embed endpoint is not scraping, but it does render through X's widget JavaScript, so screenshotting it means loading X-hosted script - verify that behaves headlessly before committing.

Option 1 is the safer default; option 2 is the fidelity option.
The screenshot ticket owns that call.

**Note also that the API's home timeline is the reverse-chronological "Following" feed, not the algorithmic "For You" feed.**
The [Timelines overview](https://docs.x.com/x-api/posts/timelines/introduction) states it "Excludes algorithmic ranking."
That is arguably better for us - the operator's follow graph is the curation signal we actually want - but the operator should know the bot sees a different feed from the one she scrolls.

---

## Recommendation

**Route: the official X API v2 `GET /2/users/{id}/timelines/reverse_chronological`, authenticated as the operator via OAuth 2.0 Authorization Code with PKCE, billed pay-per-use.**

**Monthly cost: ~$15-30**, being 100-200 distinct post reads per day at $0.005 each with 24-hour dedup, protected by a console spending limit set at roughly double that ([Pricing](https://docs.x.com/x-api/getting-started/pricing)).
This is **on top of** the operator's Premium subscription, which contributes nothing here and must not be counted against it.

**The three constraints that shape the build:**

1. A one-time interactive OAuth authorisation by the operator, with a stored refresh token - the bot cannot bootstrap itself.
2. Cost scales with distinct posts seen per day, not with polling frequency, so use `since_id` on every call and let the 24h dedup work. The 180-requests-per-15-minutes limit will never bind.
3. The screenshot is a separate artefact built from API fields, because the API never returns a rendered card.

**Rejected:** the logged-in browser session, because [X's automation rules](https://help.x.com/en/rules-and-policies/x-automation) name "scripting the X website" and its penalty as permanent account suspension, and the account at stake is the operator's real one - a bad trade for ~$20/month.
**Rejected:** third-party resellers, because the one we could verify first-party ([twitterapi.io](https://docs.twitterapi.io/introduction)) has no home-timeline endpoint at all, and its account-scoped path requires handing over the operator's credentials.

### Design notes that feed other tickets and the fog

- **Feeds the dedup fog in [#50](https://github.com/ivzc07/bienwabot/issues/50):** the post `id` is a stable 19-digit string and is the natural posted-store key, sitting alongside the existing `(source, id)` scheme from the [news pipeline research](news-pipeline-research.md). `entities.urls` bridges a linked article back to the existing canonical-URL layer, so a story surfaced by both X and HN can still collapse to one. The "seen but not worthy" notion needs its own table, because the API's own 24h dedup is a billing device and not a memory.
- **Feeds the worthiness ticket:** `public_metrics` gives five free ranking signals (likes, reposts, replies, quotes, bookmarks), `context_annotations` gives free topic hints, and `possibly_sensitive` plus `protected` give free hard filters - all before a single DeepSeek token is spent, exactly like the HN points floor does today.
- **Feeds the screenshot ticket:** render our own card from the field inventory by default; `publish.x.com/oembed` is the fidelity fallback and needs a headless-rendering spike.
- **Feeds the "whose posts are off-limits" fog:** `protected` on the author object is the mechanical gate. The editorial gate - personal non-AI posts, people who would not want a screenshot in a WhatsApp group - is a policy decision this doc does not make.
- **Feeds the cadence spec:** timeline polling is cheap enough to run a few times per posting window; there is no rate-limit reason to poll less, only a budget one.

### Flags / verify-at-build

- **800 vs 3,200 posts of history** - X's own overview and changelog disagree ([overview](https://docs.x.com/x-api/posts/timelines/introduction), [changelog](https://docs.x.com/changelog)). Irrelevant for hourly polling, but do not assume deep backfill.
- **`impression_count` on other people's posts** is in the field enum but X documents private metrics as being for your own posts - verify what actually comes back.
- **Per-endpoint prices are explicitly changeable** - "Prices are subject to change. Current rates are always available in the Developer Console" ([Pricing](https://docs.x.com/x-api/getting-started/pricing)). Rates already moved once in 2026 (Owned Reads to $0.001, writes to $0.015 on 20 April). Re-check before launch.
- **"Public Utility App" free scaled access** is mentioned in the [February 2026 announcement](https://devcommunity.x.com/t/announcing-the-launch-of-x-api-pay-per-use-pricing/256476) with no first-party page explaining eligibility. Worth one email to X support, but do not plan around it.
- **X Premium tier pricing** is given only as "starting at $3/month or $32/year" on the [About X Premium](https://help.x.com/en/using-x/x-premium) page, which is the Basic tier floor. The operator's actual tier and localized MXN price were not verified and do not affect this decision either way.
