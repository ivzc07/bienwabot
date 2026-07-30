# bienwabot - `rebe-agent`

Rebe, the bien.mx WhatsApp news agent.
One Python process, one replica, two triggers (an Evolution webhook and a scheduled news leg) and one shared pacer.

The design lives in `docs/wayfinder/`; `deployment-architecture-spec.md` is the map.
This repo currently holds the skeleton - typed configuration, an injectable clock, the container, the test gate - the DeepSeek brain both legs call, the shared pacer both legs send through, both legs themselves, the daily cadence that fires the news one, and the ops channel that alerts a human and can silence her.
Booting the process is enough to make it run: it serves the webhook leg, draws the day's posting times at dawn and keeps them, and the ops channel runs alongside both.

## Running it

Configuration is entirely environment variables, listed with their purpose in `.env.example`.
`rebe_agent/config.py` is the only place the process reads the environment; everything else takes a `Settings` object.
A missing or malformed variable stops the process at boot with a message naming the variable, rather than failing later in the middle of a send.

```sh
docker build -t rebe-agent .
docker run --rm --env-file .env rebe-agent --check-config   # validate and exit
docker run --rm --env-file .env rebe-agent                  # boot: serve, roll the day, post it
```

`--check-config` validates the environment, logs the startup line, and exits - useful in CI and after changing Coolify variables.

`TZ` defaults to `America/Mexico_City`, which drives the quiet-hours window and the posting shape.
Time-dependent code reads a `Clock` (`rebe_agent/clock.py`) rather than calling `datetime.now()` inline, so tests can place the process at 03:00 without waiting for 03:00.

## The brain

`rebe_agent/brain.py` is the only place this process talks to DeepSeek.
Every call goes to `deepseek-v4-flash`, the current non-thinking chat model, with thinking mode explicitly disabled, a `max_tokens` cap, and a Pydantic model as the answer.
A response that fails validation is an error, never a half-parsed object.

```sh
docker run --rm --env-file .env rebe-agent --ask "di hola en una linea"
```

That prints a validated object and, like every other call, counts itself in the `rebe` database.

Each call's cache-hit, cache-miss and completion tokens are accumulated per day and per call type, which turns the estimates in `docs/wayfinder/token-budget-spec.md` into something checkable.
The same counter is the runaway-loop detector: past 600 calls in a day the maintainer is alerted and the bot keeps going, past 2,000 it stops calling DeepSeek until the next day while the process, the heartbeat and the scheduler stay alive.
There is no dollar cap, because the damage from a loop is a banned number rather than a bill.

## The pacer

`rebe_agent/pacer.py` is the only place this process sends a WhatsApp message.
Both legs share one instance, because the anti-ban ceilings have to span news posts and webhook replies together - two limiters, one per leg, would each stay politely under twelve a day and between them send twenty-four.

Before a message lands, Rebe appears to type.
A `composing` presence goes up, a pause scaled to the message length passes - about 30 ms a character, Gaussian-jittered, never below 1.5 s or above 5 s - and the presence is refreshed while it passes, because Baileys expires it after about ten seconds.
A first message into a quiet thread gets an extra beat first.

The pacer will also say no.
Four sends a minute, three an hour, twelve a day, counted across both legs; scheduled posts held between 23:00 and 08:00; consecutive posts 75-90 minutes apart; between 02:00 and 06:00 everything spaced four to six times further apart than by day; and never the same wording twice in a row.
Every jittered gap is read off the send it is measured against rather than drawn fresh, because a threshold redrawn on each attempt is one a caller can retry its way past - which would quietly turn "75 to 90 minutes" into a flat 75.
A refusal is a `SendRefusedError` carrying a reason and, where it is knowable, how long until the door opens - so a caller can tell "come back in forty minutes" from "Evolution is down", which is an `EvolutionError` instead.
The out-of-band soft pause is the other thing it will say no to, read fresh before every send, and a failed send is reported to the ops channel from here because this is the only place a send can fail.
The post-pairing ramp is read here too, for the same reason: its clamp on the day covers the drawn slots and the breaking-news overrides alike, and its halt covers both legs.
Deciding between deferring a post and dropping it belongs to the cadence leg, not here.

Every send is written to the `rebe` database before it goes on the wire, so a restart cannot hand a crash loop a fresh allowance, and a transport failure cannot turn a retry into a burst.

```sh
docker run --rm --env-file .env rebe-agent --say "hola" --to 1203...@g.us --as reply
```

That is the real send path, not a shortcut around it: the group sees the typing indicator, the pause is drawn rather than fixed, and the envelope gets its say.
`--as post` is the default and obeys the overnight hold and the post-to-post gap; `--as reply` obeys neither, matching the reply policy.
A refusal exits 4 and a transport failure exits 5.

## The news leg

`rebe_agent/news.py` takes one interesting AI item from the open web into the group as a short Spanish post in Rebe's voice.

```sh
docker run --rm --env-file .env rebe-agent --post-news --to 1203...@g.us
```

Candidates come from Hacker News through the keyless Algolia search API - one request returns hydrated stories already above a points floor - plus the first-party RSS feeds of the major AI orgs that `docs/wayfinder/news-pipeline-research.md` verified.
A source that is down costs the run that source, never its post.

Before anything is ranked, every candidate is checked against the posted store on three deterministic layers: the stable source ID, the canonical URL with tracking stripped and the host normalised, and a SHA-256 hash of the normalised title.
DeepSeek has no embeddings endpoint, so there is no semantic near-duplicate layer behind those three; they are the whole gate, and a repost is the most visible bot tell there is.
What survives is filtered for freshness, a usable title and a resolvable link, then ranked on source authority, HN points and comments, and recency decay - all of it before a single token is spent.

The top item gets one DeepSeek call, which returns a framing word and one line and never a link.
The link is appended afterwards by the code, which is what makes "it never invents or shortens a link" a property rather than a hope.
What gets appended is the article's own address minus the tracking, not the canonical key: canonicalising forces https and drops a `www.`, which is right for comparing two links and would be an edit to an address somebody is about to tap.
The framing line may only restate the source item: any number the source did not supply, any link, a second emoji, or an answer too long to be a WhatsApp message is rejected and that item is dropped without posting.
A rejected answer moves the run on to the next candidate, up to a small bound; a brain failure ends the run, because the next candidate would fail the same way.

Then the post goes out through the shared pacer, and only after that is the item written to the posted store - so a failed send costs a re-fetch rather than losing the item for good.
`--limit N` caps how many items one run may post; the default is one, and the pacer's post-to-post gap governs the spacing regardless.
Running the command again immediately posts nothing, because every candidate is already known.

## The announcement twin

When a high-tier item posts to the group, `rebe_agent/announce.py` also posts it to the bien.mx Community's Announcements channel, restated by one extra DeepSeek call in a professional register - formal Spanish, no slang, zero emoji - with the link appended by the code as always.
The rule lives in `NewsLeg.post_one`, so every path that posts a big story announces it: a drawn slot, the breaking override, the overnight drain, `--post-news`.
The twin leaves through the same shared pacer as its own send kind: it spends the raw ceilings and obeys the overnight hold and `/pausa`, but skips the post-to-post gap and is invisible to the ramp clamp and the practical stop - it is the same story in another room, not a second story.
One try, and every failure is a logged nothing: by then the group post has landed, and the next big item brings the next chance.
`REBE_ANNOUNCE_JID` names the channel; unset means the leg is off, and Rebe's number must be a community admin to post there.
The decisions and their reasons are [`docs/wayfinder/announcements-spec.md`](docs/wayfinder/announcements-spec.md).

## The webhook leg

`rebe_agent/webhook.py` and `rebe_agent/reply.py` are the other trigger: somebody in the group says her name, and she answers.
Starting the container with no arguments serves it.

Evolution's per-instance webhook posts `messages.upsert` to `POST /webhook/<WEBHOOK_SECRET>` on the internal Docker network.
The agent has no public URL, so that traffic never leaves the host; the token in the path is defence in depth against everything else already inside the network.
A wrong or missing token is answered `404` rather than `403`, compared with `secrets.compare_digest`, and nothing else happens.
Every delivery that gets past the token is answered `200` with the same dull body, whatever Rebe then decides - a non-200 would earn a redelivery of a payload that will never work, and the answer is not something an unauthenticated caller should be able to read Rebe's behaviour off.

`rebe_agent/inbound.py` decides which of the reply policy's three tiers the message falls in, and it decides it *mechanically*.
A message is addressed when it @-mentions her, names her as a whole word, or replies to or quotes one of her messages - all facts about the payload, so tier one cannot be missed by a bad classification and the decisions are tested against recorded webhook bodies.
Her own echoed sends, media with no readable words, and private chats are silence.
Unaddressed chatter is remembered and left alone: the occasional chime-in is its own ticket.

An addressed message costs two DeepSeek calls.
The first classifies the topic - on-topic, off-topic or personal, a no-go area, or "¿eres un bot?" - and the second writes one short line under the instructions that topic earns.
On-topic gets a real light answer; the rest get a human deflection rather than a lookup, and the no-go areas get exactly one deflection before the thread goes quiet, with no steering back to AI.
Then the incoming message is marked read, so the blue ticks land before the reply, and the reply goes out through the same shared pacer as every news post.

Everything fails toward silence, and there is never an error message in the group.
A gate that errors, a classification under 50% confidence, a generation that comes back empty, a reply carrying a link, a figure nobody gave her, a second emoji, or an admission that she is a bot, a closed envelope, a dead transport: all of them end with nothing sent.
That deliberately overrides "she always answers when addressed" - a dropped reply looks like she put her phone down, a broken one looks like a bot.

The rolling window of recent turns per group lives in `group_memory` in the `rebe` database and is handed back to the model on every event, so a follow-up lands as a follow-up and a restart does not lose the thread.
The same table is what refuses a redelivered webhook, keeps her from answering the same person twice with nobody speaking in between, and lets a thread fade after two or three turns without a closing message.

## The cadence

`rebe_agent/cadence.py` decides when Rebe posts, and `rebe_agent/scheduler.py` is the loop that keeps to it.
Booting the process with no arguments starts that loop beside the webhook server and the ops channel; there is no separate scheduler container, because the pacer's ceilings span both legs and a limiter cannot span two processes.

Once a day at 06:00 a single job draws the whole day.
A weekday gets four loose windows - 08:00-10:30, 13:00-15:00, 18:00-20:00, 21:30-23:00 - and a weekend drops the morning and shifts the rest later, because people wake later on a Saturday and AI news genuinely dries up on one.
Inside each window one time is drawn from a Gaussian centred on the midpoint, sigma a fifth of the width, clipped to the edges: the central tendency is the point, since a flat draw is as likely to post at 08:00 as at 10:29 and a habit is what a person has.
Nothing is ever planned between 23:00 and 08:00, which a `Cadence` cannot be configured out of - a window reaching into the night refuses to build.

The whole day is drawn at once rather than window by window, because the 75-90 minute minimum gap is only enforceable with a global view: independent draws cannot see each other and would eventually put two posts ten minutes apart across a window boundary.
A time that cannot be spaced far enough from the one before it is redrawn a few times and then given up, so the day's count drifts rather than the gap bending.

Each drawn time becomes a row in the `rebe` database, not an object in a job store.
That is what makes a restart part-way through the day pick the day back up: the times already committed to are read back rather than redrawn, a second roll of the same day cannot double-register it, and a slot that already went out is not posted twice.

When a slot comes due it runs the news leg once.
Three things can happen.
It posts; or nothing in the curated pool cleared the quality bar and the window is skipped in silence, because Rebe never posts filler to hit a number; or the slot is dropped - it came due long after its window because the process was down, a live conversation deferred it past the edge, or the pacer refused it.
A post that comes due within minutes of one of her own messages, post or reply, waits 10-20 jittered minutes past that message, because a news link landing seconds after she answered somebody reads as two programs running side by side.
If that deferral would push it more than about thirty minutes past its window edge, the slot is dropped rather than posted late.

Both the clock and the randomness are injected, so `tests/test_scheduler.py` runs a whole day of posting in milliseconds and `tests/test_cadence.py` checks the shape of the draw over three hundred simulated days.

## The ops channel

Everything the maintainer hears, and the one thing they can say back, goes through Telegram rather than WhatsApp.
That is the whole point: an alert about Evolution being down cannot travel through Evolution.

```sh
docker run --rm --env-file .env rebe-agent    # boots: the webhook leg, the scheduler, and this
```

**The heartbeat.** The process pushes to an Uptime Kuma push monitor about every sixty seconds, and a missed beat is what makes Kuma fire the Telegram alert.
The beat is emitted from inside the running loop rather than by a health endpoint, so it proves the loop is turning and not merely that the process exists - a wedged loop and a crashed process look the same to Kuma, which is the point.
It also needs no exposed port, and the agent has no public URL.

**The alerts.** `rebe_agent/signals.py` names the closed set of things worth waking somebody for, and `rebe_agent/alerts.py` is the way out: a 463 reach-out time-lock or other rate error on send, any other send Evolution would not take, an Evolution connection that has dropped, the two shapes a ban arrives in, and a DeepSeek call that came back with nothing.
Each alert carries what happened, whether Rebe is still sending, and what to do about it - the permanent-ban one says to point `EVOLUTION_INSTANCE` at `bien-backup`, the temporary one says explicitly not to.
Repeats of one signal are collapsed into one message per half hour and counted into the next one, because an alert storm is the same as no alerts.

The two ban shapes also stop her, by flipping the same switch an operator uses, since "stop sending, do not keep hammering" needs a mechanism and that is the one there is.
A dropped connection and a rate limit stop her too, through the ramp rather than through the switch - see below - because those come back on their own and the operator's switch must not be undone by anything but the operator.

**The soft pause.** `/pausa [motivo]` in the ops chat and Rebe goes quiet while staying in the group; `/reanuda` and she carries on; `/estado` says where the switch stands.
The switch is a row in the `rebe` database, so a redeploy does not silently undo it, and the pacer reads it before every send - which is what makes one switch cover posts and replies alike, `--say` and `--post-news` included.
Nothing is queued while she is quiet, so resuming is one ordinary message rather than a backlog fired at the group.
The heartbeat keeps flowing and the scheduler keeps ticking throughout, so a pause is visibly different from an outage.

There is deliberately no in-group command - no "Rebe pausa", nothing a member could see and read as a bot - and a Telegram message from any chat other than `TELEGRAM_CHAT_ID` gets silence rather than an answer.
The hard stop is not code at all: an admin removes her number from the group like any departing member.

## The ramp

`rebe_agent/ramp.py` is what makes Rebe start quiet and stay quiet when WhatsApp pushes back.
It is the last safety behaviour before go-live, and the full operational detail is in [`docs/wayfinder/ramp-and-recovery-runbook.md`](docs/wayfinder/ramp-and-recovery-runbook.md).

For the first week after the number is paired the day is clamped to three news posts, whatever the cadence plan drew; the second week it is four; after two clean weeks the cadence spec's steady state applies with no clamp.
Replies are not clamped at any point.
The start date is a row in the `rebe` database, so a restart neither resets nor skips the ramp, and an idle gap of 72 hours or more or a reconnect puts her back on the week-one clamp - a cold resume at full rate is a documented way to trip the 463 reach-out limit.

A 463 or a 429 stops every send for an hour rather than retrying it, and each fresh push-back restarts the hour.
An Evolution `connection.update` that says the link is down stops sending until the link is back, and each repeated disconnect extends the hold, so a real outage stays held while a lost `open` event costs half an hour rather than an indefinite silence.
The heartbeat keeps flowing through all of it, which is how a maintainer tells "the agent is alive and the number is not sending" from "the process is down".

There is no automatic swap to the backup number, ever.
Auto-switching on a possibly-false ban signal would burn the only warm standby and leave the bot cold with no backup, so a permanent ban stops everything and waits for a human.
Which instance is live is `EVOLUTION_INSTANCE`, read in one place and never written, and the swap is the manual procedure in the runbook.

## Deploying it

`rebe-agent` runs as a Coolify application built from this repo's Dockerfile, on the shared internal network, with no public FQDN and no published port: Evolution reaches it by container name and nothing else needs to reach it at all.
The step-by-step go-live sequence - pairing the numbers, the per-instance webhooks, every environment variable, the Kuma monitor and the Telegram bot, and the three live checks that say it works - is [`docs/wayfinder/go-live-runbook.md`](docs/wayfinder/go-live-runbook.md).
What is already provisioned, with the container names to address it by, is [`docs/wayfinder/bien-evo-provisioning.md`](docs/wayfinder/bien-evo-provisioning.md).

**One replica. Never two.**
The pacer's counters and the scheduler's idea of what is due both live in this process, so a second replica would roll its own posting day and double-fire every slot, and the two would each stay politely under twelve sends a day while between them sending twenty-four.
The anti-ban envelope is a property of the number rather than of the container, and it only holds while there is one container.
Scaling out is possible, but only after the limiter and the scheduler move onto a shared Postgres or Redis lock.

## Working on it

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy
pytest
```

The counters, the send log, the posted store and the day's plan are asserted against a real Postgres, so those tests skip unless one is pointed at:

```sh
docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=rebe --name rebe-pg postgres:16
REBE_TEST_DATABASE_URL=postgresql://postgres:rebe@127.0.0.1:5432/postgres pytest
```

CI runs exactly these, plus a container build and boot, in the `test` job of `.github/workflows/tests.yml`.
That job is the merge gate - see `AGENTS.md`.
