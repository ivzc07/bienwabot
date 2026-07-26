# Go-Live - Runbook

Wayfinder ticket: [Go-live: pair the numbers, deploy, and verify in the group](https://github.com/ivzc07/bienwabot/issues/25).
Executes section 6 of [the deployment architecture spec](deployment-architecture-spec.md) and section 5 of [the anti-ban ops spec](anti-ban-ops-spec.md).

Nothing here is new behaviour.
This is the sequence that puts the built system on the warmed numbers and in the real group, and the evidence that says it worked rather than that it was assumed to.

What already exists is in [the bien-evo provisioning runbook](bien-evo-provisioning.md): the Evolution service, its Postgres and Redis, the empty `rebe` database, and the `bien-dev` integration-test instance.
What comes after go-live is in [the ramp and recovery runbook](ramp-and-recovery-runbook.md): the two-week clamp, the back-offs, and the manual failover.

No secret values live in this file.
Every credential is a Coolify environment variable, and every command below takes its secrets from the shell rather than from the page.

---

## 0. Before you start

Two things gate the whole sequence, and neither is technical.

- **Both SIMs are warm.** [#15](https://github.com/ivzc07/bienwabot/issues/15) is the ~14-day warm-up on the official app: a real profile, real one-to-one messages, then manual group chat. Pairing a cold number to an API is the single most reliable way to get it banned in a week.
- **The group has been told.** [The consent and group rules spec](consent-group-rules-spec.md) is the disclosure that has to have happened before a bot starts talking in a group of real people.

Then have these to hand:

| You need | Where it comes from |
|---|---|
| The primary SIM's phone, unlocked, WhatsApp open | You |
| The backup SIM's phone, same | You |
| The group JID (`<id>@g.us`) | Evolution's `/chat/findChats` or `/group/fetchAllGroups` on the paired instance |
| Evolution's API key | `SERVICE_PASSWORD_AUTHENTICATIONAPIKEY` on the `bien-evo` service |
| The `rebe` database URL | `REBE_DATABASE_URL` on the `bien-evo` service |
| A DeepSeek API key | The DeepSeek console |
| A fresh webhook secret | `openssl rand -hex 24`, generated now and used in three places below |

Set the shell up once, so nothing below has to be edited inline:

```sh
export EVO=http://evo-i856ku5lxcr2o1v64eveeahq.45.132.242.102.sslip.io
export APIKEY='<AUTHENTICATION_API_KEY>'
export WEBHOOK_SECRET='<the value you just generated>'
```

`$EVO` is the public manager URL, used from your laptop for setup only.
The agent never uses it: on the server it reaches Evolution at `http://api-i856ku5lxcr2o1v64eveeahq:8080`, by container name, over the shared network.

---

## 1. Order of operations, and why it is this order

1. Pair the primary number.
2. Pair the backup number.
3. Deploy the agent.
4. Verify.

The ramp start is the reason.
`rebe_agent/ramp.py` stamps the ramp the first time anything asks, because the agent has no pairing event to hang it on and first boot is the closest honest thing there is.
Deploy before pairing and the two-week clamp starts counting from a day the number was not yet live, which front-loads a number that is still a fortnight old.
Pair first, deploy second, and the stamp lands on the real pairing day.

If you do end up booting the agent before the numbers are paired, section 7 says how to correct it.

---

## 2. The primary instance

### 2.1 Create `bien-rebe`

```sh
curl -sS -X POST "$EVO/instance/create" \
  -H "apikey: $APIKEY" -H 'Content-Type: application/json' \
  -d '{
    "instanceName": "bien-rebe",
    "integration": "WHATSAPP-BAILEYS",
    "qrcode": true,
    "groupsIgnore": false,
    "readMessages": false,
    "alwaysOnline": false,
    "syncFullHistory": false
  }'
```

`alwaysOnline` stays off deliberately.
A number that is online twenty-four hours a day is a number that is obviously not a person, and the presence camouflage in `rebe_agent/pacer.py` is what handles looking present at the moments that matter.

`readMessages` stays off because the agent marks messages read itself, only on the ones it is about to answer, through `markMessageAsRead`.
Blue ticks on everything is a different behaviour from blue ticks on what she replies to.

### 2.2 Scan the QR

Open `$EVO/manager`, sign in with the API key, open `bien-rebe`, and scan the QR from the primary SIM's phone.

This is the re-pairing the spec calls out: the number is currently paired on the shared `gogym-evo` Evolution, and it moves here.
The warm-up state lives on the number itself and is unaffected by which Evolution instance it links to.
Whatsapp allows the number to be linked to several devices, but only one Evolution instance should hold it, so remove the old link from the phone's "Linked devices" screen once this one is up.

Confirm the link:

```sh
curl -sS "$EVO/instance/connectionState/bien-rebe" -H "apikey: $APIKEY"
```

`"state": "open"` is paired.
Anything else is not, and none of the rest of this runbook will work until it is.

### 2.3 Find the group JID

```sh
curl -sS "$EVO/group/fetchAllGroups/bien-rebe?getParticipants=false" -H "apikey: $APIKEY"
```

The `id` of the bien.mx group, ending in `@g.us`, is `REBE_GROUP_JID`.
Copy it exactly.
A wrong JID is a bot that boots cleanly, posts nowhere, and looks healthy while doing it.

### 2.4 Point its webhook at the agent

```sh
curl -sS -X POST "$EVO/webhook/set/bien-rebe" \
  -H "apikey: $APIKEY" -H 'Content-Type: application/json' \
  -d "{
    \"webhook\": {
      \"enabled\": true,
      \"url\": \"http://rebe-agent:8000/webhook/$WEBHOOK_SECRET\",
      \"byEvents\": false,
      \"base64\": false,
      \"events\": [\"MESSAGES_UPSERT\", \"CONNECTION_UPDATE\"]
    }
  }"
```

Three things about that URL.
It is a container name and an internal port, because the agent has no public FQDN and the webhook traffic never leaves the host.
`byEvents` is false, so every event posts to that one path rather than to per-event sub-paths, which is what `rebe_agent/webhook.py` serves.
The secret is in the path, and a wrong token gets a 404 rather than a 403, so a scanner learns nothing from trying.

Only two events are subscribed.
`MESSAGES_UPSERT` is the reply leg, `CONNECTION_UPDATE` is the link-state watchtower in `rebe_agent/signals.py`, and everything else Evolution can emit is noise the agent would parse and drop.

The global webhook stays off, and already is: `WEBHOOK_GLOBAL_ENABLED=false` on the service.
Check it rather than trust it:

```sh
curl -sS "$EVO/webhook/find/bien-rebe" -H "apikey: $APIKEY"
```

---

## 3. The backup instance

Same creation call with `"instanceName": "bien-backup"`, then scan the QR from the backup SIM's phone.

Then, from that phone, join the group.
The backup being a member before it is ever needed is the whole point of a warm standby: a failover that has to be invited into the group first is a failover that happens hours late.

Give it the same webhook, pointed at the same agent:

```sh
curl -sS -X POST "$EVO/webhook/set/bien-backup" \
  -H "apikey: $APIKEY" -H 'Content-Type: application/json' \
  -d "{
    \"webhook\": {
      \"enabled\": true,
      \"url\": \"http://rebe-agent:8000/webhook/$WEBHOOK_SECRET\",
      \"byEvents\": false,
      \"base64\": false,
      \"events\": [\"MESSAGES_UPSERT\", \"CONNECTION_UPDATE\"]
    }
  }"
```

This is safe, and it is deliberate.
The agent sends only through the instance `EVOLUTION_INSTANCE` names, so a webhook on the idle instance produces no sends.
What it produces is a `connection.update` if the standby's link ever drops, which is exactly the thing you want to hear about before you need it.

**The backup never posts while the primary is alive.**
Nothing automatic ever swaps to it.
The swap is the manual procedure in [the ramp and recovery runbook](ramp-and-recovery-runbook.md), section 4, and it is a human confirming a ban on the phone first.

---

## 4. The agent on Coolify

A Coolify **Application**, from this repo, Dockerfile build.

| Setting | Value | Why |
|---|---|---|
| Project / environment | `bienwabot` / `production` | Where `bien-evo` already lives. |
| Name | `rebe-agent` | The container name the webhooks above point at. |
| Source | `github.com/ivzc07/bienwabot`, branch `main` | Dockerfile at the repo root. |
| Destination | `pxrf9uiha61v0zcdq73g5wnk` | The shared network `bfcginilwluqk0gk7wctlwz8`. Anything else cannot reach Evolution by name. |
| Domain / FQDN | none | The agent is not on the internet. Nothing outside Docker needs to reach it. |
| Ports exposed | none | The webhook listens on 8000 inside the container only. |
| Replicas | **1** | See below. This one is not cosmetic. |
| Restart policy | `unless-stopped` | A crash comes back; a deliberate stop stays stopped. |

The destination is the correction recorded in [the provisioning runbook](bien-evo-provisioning.md), section 2.
The spec says the shared network is called `coolify`; on this server it is the destination network `bfcginilwluqk0gk7wctlwz8`, misleadingly named after the n8n service that created it first, and it is what all the existing resources share.

### The single-replica invariant

> **`rebe-agent` runs exactly one replica. Do not scale it.**
>
> The pacer's counters and the scheduler's idea of what is due both live in the process.
> Two replicas would each roll their own posting day and double-fire every slot, and each would stay politely under twelve sends a day while between them sending twenty-four.
> The anti-ban envelope is a property of the number, not of the container, so it only holds while there is one container.
> Scaling out means first moving the limiter and the scheduler onto a shared Postgres or Redis lock.

That paragraph lives in four places on purpose, because an operator under pressure reads whichever one is in front of them: [the spec](deployment-architecture-spec.md) section 2.2, the `README.md` deployment section, this runbook, and **the Coolify application's own Description field**, which is the one visible on the same page as the replica setting.

Set the description when you create the application:

```
rebe-agent - bien.mx WhatsApp news bot. SINGLE REPLICA ONLY: the anti-ban pacer
and the news scheduler are in-process, so two replicas double-post and
double-count the rate limits. See docs/wayfinder/go-live-runbook.md.
```

### Environment variables

All of them, in Coolify, on the application.
None of them in git.
`.env.example` carries the names and shapes and no values, and `rebe_agent/config.py` is the only place the process reads the environment.

| Variable | Value at go-live |
|---|---|
| `DEEPSEEK_API_KEY` | From the DeepSeek console. |
| `EVOLUTION_API_URL` | `http://api-i856ku5lxcr2o1v64eveeahq:8080` |
| `EVOLUTION_API_KEY` | The `bien-evo` service's `AUTHENTICATION_API_KEY`. |
| `EVOLUTION_INSTANCE` | `bien-rebe` |
| `REBE_GROUP_JID` | The JID from section 2.3. |
| `WEBHOOK_SECRET` | The same value used in sections 2.4 and 3. |
| `REBE_DATABASE_URL` | The `rebe` URL from the `bien-evo` service, host `postgres-i856ku5lxcr2o1v64eveeahq`. |
| `TELEGRAM_BOT_TOKEN` | Section 5. |
| `TELEGRAM_CHAT_ID` | Section 5. |
| `KUMA_PUSH_URL` | Section 5. |
| `TZ` | `America/Mexico_City` |
| `LOG_LEVEL` | `INFO` |

A missing or malformed variable stops the process at boot with a message naming the variable, rather than failing later in the middle of a send.
That is the intended behaviour and it is the fastest way to check the set:

```sh
docker run --rm --env-file .env rebe-agent --check-config
```

### The tables

There is nothing to create by hand.
Every store in the agent issues its own `CREATE TABLE IF NOT EXISTS` at boot, into the empty `rebe` database: `group_memory`, `posted_items`, `sends`, `deepseek_usage`, `planned_slots`, `overnight_items`, `chime_ins`, `soft_pause`, `ramp`.

---

## 5. The ops channel

**Telegram bot.** Talk to `@BotFather`, `/newbot`, name it something that says what it is when it wakes you at 03:00.
Keep the token for `TELEGRAM_BOT_TOKEN`.
Send it a message from the chat that will receive the alerts, then read the chat id back:

```sh
curl -sS "https://api.telegram.org/bot<token>/getUpdates"
```

`message.chat.id` is `TELEGRAM_CHAT_ID`.
It is the only chat whose commands are obeyed; anything from anywhere else is ignored without an answer, which is what stops a stranger who finds the bot from pausing Rebe.

**Kuma push monitor.** In the existing `kuma` service, create a monitor of type **Push**, named `rebe-agent`, heartbeat interval **120 s**, retries 1.
The agent pushes about every 60 s from inside its own working loop, so a hung loop stops the beat as surely as a dead process does.
120 s tolerates one lost push without crying wolf.

Attach the Telegram notification to the monitor, with the same bot and chat id, and tick "apply to all existing monitors" only if you actually want that.

Copy the push URL into `KUMA_PUSH_URL`, replacing the public host with the internal one:
Kuma shows `http://<public>/api/push/<token>`, and what the agent needs is `http://kuma:3001/api/push/<token>`.
The token is a secret in its own right, because anybody holding it can keep the monitor green while Rebe is dead.

---

## 6. Verification

Deploy, then prove three things.
None of them is "the container is running".

### 6.1 Boot

```
rebe-agent 0.1.0 starting | evolution_instance=bien-rebe evolution_api=... timezone=America/Mexico_City local_time=...
```

Then, within a couple of minutes, the Kuma monitor goes green and stays green.

### 6.2 A message in the group gets a reply

From your own phone, in the real group, address Rebe by name and ask her something an AI-news bot should answer.

Expected: she appears to type for a second or two, then answers in Spanish, in one message.
The delay is the point.
An instant answer would be the tell.

If nothing happens, in order: the app's logs for a webhook line at all (if none, the webhook URL or the secret is wrong), then the reply gate's decision line (a `should_reply=false` is the gate working, not the system failing, and the fix is to address her more plainly).

### 6.3 A scheduled news post lands on its own

Do not trigger it.
The day's post times are drawn at dawn and held; wait for one.

Expected: one Spanish line and a link, in the group, at a time nobody chose by hand.
`SELECT * FROM planned_slots ORDER BY id DESC LIMIT 5;` in the `rebe` database says what the day was supposed to look like, and `SELECT * FROM sends ORDER BY id DESC LIMIT 5;` says what actually went out.

Week one of the ramp caps the day at three posts, so do not read a quiet afternoon as a fault.

### 6.4 Killing the agent fires the alert

Stop the application in Coolify.
Within about two minutes Kuma goes red and the Telegram alert arrives.
Start it again, and Kuma goes green with a recovery message.

That is the whole observability story tested end to end: the beat comes from inside the working loop, and the alert arrives on a channel that does not depend on WhatsApp being up.

### 6.5 The switches answer

From the ops chat: `/estado` says whether she is paused, `/pausa` silences her, `/reanuda` brings her back.
Do this once now rather than the first time you need it at speed.

---

## 7. The ramp start

The two weeks are dated from the real pairing day, and the record is the `ramp` table in the `rebe` database:

```sql
SELECT started_at, reason FROM ramp;
```

Read it after the first boot and check the date is the day you scanned the QR, not the day you first built the container.
If the agent was booted before pairing, the stamp is wrong and too early, and the correction is to clear it and let the next read stamp a fresh one:

```sql
DELETE FROM ramp;
```

She then starts again at three posts a day, which is where a number this new should be.

Record the date in the go-live record below, because the table is the machine's copy and a human needs one too.

Week one allows three news posts a day, week two allows four, and after two clean weeks the cadence spec's steady state applies with no clamp.
Replies are never clamped.
The details, and every way she can fall back into the ramp, are in [the ramp and recovery runbook](ramp-and-recovery-runbook.md).

---

## 8. Failover, in one line and a pointer

If the primary number is permanently banned: confirm it on the phone, set `EVOLUTION_INSTANCE=bien-backup` in Coolify, redeploy, resume with `/reanuda`, and start warming a replacement backup SIM the same day.

The full procedure, including what a temporary ban looks like instead and the four things not to do, is [the ramp and recovery runbook](ramp-and-recovery-runbook.md), section 4.

---

## 9. Go-live record

Fill this in as it happens, and paste it into [#25](https://github.com/ivzc07/bienwabot/issues/25) when it is done.

| Thing | Value |
|---|---|
| Primary number paired to `bien-rebe` | date / time, and by whom |
| Backup number paired to `bien-backup`, in the group | date / time |
| Group JID | `<id>@g.us` |
| `rebe-agent` first deployed | date / time, commit SHA |
| Ramp `started_at` | from the `ramp` table |
| Ramp ends (steady state) | pairing day + 14 |
| Kuma monitor | name and id |
| First reply in the group | timestamp |
| First unattended news post | timestamp |
| Kill-and-alert test | timestamp, and how long the alert took |
