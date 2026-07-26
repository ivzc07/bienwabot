# bienwabot - `rebe-agent`

Rebe, the bien.mx WhatsApp news agent.
One Python process, one replica, two triggers (an Evolution webhook and a scheduled news leg) and one shared pacer.

The design lives in `docs/wayfinder/`; `deployment-architecture-spec.md` is the map.
This repo currently holds the skeleton - typed configuration, an injectable clock, the container, the test gate - the DeepSeek brain both legs call, the shared pacer both legs send through, and the news leg itself.
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
