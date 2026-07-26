# bienwabot - `rebe-agent`

Rebe, the bien.mx WhatsApp news agent.
One Python process, one replica, two triggers (an Evolution webhook and a scheduled news leg) and one shared pacer.

The design lives in `docs/wayfinder/`; `deployment-architecture-spec.md` is the map.
This repo currently holds the skeleton - typed configuration, an injectable clock, the container, the test gate - the DeepSeek brain both legs call, the shared pacer both legs send through, the news leg itself, and the ops channel that alerts a human and can silence her.
The news leg runs on demand; putting it on a timer is the cadence ticket, and the webhook leg lands in a later one.

## Running it

Configuration is entirely environment variables, listed with their purpose in `.env.example`.
`rebe_agent/config.py` is the only place the process reads the environment; everything else takes a `Settings` object.
A missing or malformed variable stops the process at boot with a message naming the variable, rather than failing later in the middle of a send.

```sh
docker build -t rebe-agent .
docker run --rm --env-file .env rebe-agent --check-config   # validate and exit
docker run --rm --env-file .env rebe-agent                  # boot and stay up
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

## The ops channel

Everything the maintainer hears, and the one thing they can say back, goes through Telegram rather than WhatsApp.
That is the whole point: an alert about Evolution being down cannot travel through Evolution.

```sh
docker run --rm --env-file .env rebe-agent    # boots, then the ops channel is the loop
```

**The heartbeat.** The process pushes to an Uptime Kuma push monitor about every sixty seconds, and a missed beat is what makes Kuma fire the Telegram alert.
The beat is emitted from inside the running loop rather than by a health endpoint, so it proves the loop is turning and not merely that the process exists - a wedged loop and a crashed process look the same to Kuma, which is the point.
It also needs no exposed port, and the agent has no public URL.

**The alerts.** `rebe_agent/alerts.py` names the closed set of things worth waking somebody for: a 463 reach-out time-lock or other rate error on send, any other send Evolution would not take, an Evolution connection that has dropped, the two shapes a ban arrives in, and a DeepSeek call that came back with nothing.
Each alert carries what happened, whether Rebe is still sending, and what to do about it - the permanent-ban one says to point `EVOLUTION_INSTANCE` at `bien-backup`, the temporary one says explicitly not to.
Repeats of one signal are collapsed into one message per half hour and counted into the next one, because an alert storm is the same as no alerts.

The two ban shapes also stop her, by flipping the same switch an operator uses, since "stop sending, do not keep hammering" needs a mechanism and that is the one there is.
A dropped connection does not: Evolution reconnects on its own, and a pause nobody undoes is worse than a few failed sends.

**The soft pause.** `/pausa [motivo]` in the ops chat and Rebe goes quiet while staying in the group; `/reanuda` and she carries on; `/estado` says where the switch stands.
The switch is a row in the `rebe` database, so a redeploy does not silently undo it, and the pacer reads it before every send - which is what makes one switch cover posts and replies alike, `--say` and `--post-news` included.
Nothing is queued while she is quiet, so resuming is one ordinary message rather than a backlog fired at the group.
The heartbeat keeps flowing and the scheduler keeps ticking throughout, so a pause is visibly different from an outage.

There is deliberately no in-group command - no "Rebe pausa", nothing a member could see and read as a bot - and a Telegram message from any chat other than `TELEGRAM_CHAT_ID` gets silence rather than an answer.
The hard stop is not code at all: an admin removes her number from the group like any departing member.

## Working on it

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy
pytest
```

The counters and the send log are asserted against a real Postgres, so those tests skip unless one is pointed at:

```sh
docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=rebe --name rebe-pg postgres:16
REBE_TEST_DATABASE_URL=postgresql://postgres:rebe@127.0.0.1:5432/postgres pytest
```

CI runs exactly these, plus a container build and boot, in the `test` job of `.github/workflows/tests.yml`.
That job is the merge gate - see `AGENTS.md`.
