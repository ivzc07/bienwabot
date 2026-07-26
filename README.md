# bienwabot - `rebe-agent`

Rebe, the bien.mx WhatsApp news agent.
One Python process, one replica, two triggers (an Evolution webhook and a scheduled news leg) and one shared pacer.

The design lives in `docs/wayfinder/`; `deployment-architecture-spec.md` is the map.
This repo currently holds the skeleton - typed configuration, an injectable clock, the container, the test gate - the DeepSeek brain both legs call, the shared pacer both legs send through, and both legs themselves.
The webhook leg is what the process serves when it starts; the news leg runs on demand, and putting it on a timer is the cadence ticket.

## Running it

Configuration is entirely environment variables, listed with their purpose in `.env.example`.
`rebe_agent/config.py` is the only place the process reads the environment; everything else takes a `Settings` object.
A missing or malformed variable stops the process at boot with a message naming the variable, rather than failing later in the middle of a send.

```sh
docker build -t rebe-agent .
docker run --rm --env-file .env rebe-agent --check-config   # validate and exit
docker run --rm --env-file .env rebe-agent                  # boot and serve the webhook leg
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
