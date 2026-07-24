# Deployment Architecture - Spec

Wayfinder ticket: [Design the deployment architecture](https://github.com/ivzc07/bienwabot/issues/9).
The capstone of the design: how the six locked decisions run together on the existing cheap self-hosted Coolify infra.

This spec is **planning only** - it defines the topology, wiring, persistence, secrets, and failure handling a builder implements. It writes no code.

Grounds every choice in the real infra as inspected via the Coolify API on 2026-07-24, and in the earlier decisions:
[transport #3](https://github.com/ivzc07/bienwabot/issues/3) (Evolution API),
[framework #4](https://github.com/ivzc07/bienwabot/issues/4) (Pydantic AI + DeepSeek),
[news pipeline #5](https://github.com/ivzc07/bienwabot/issues/5),
[persona #6](https://github.com/ivzc07/bienwabot/issues/6),
[reply policy #7](https://github.com/ivzc07/bienwabot/issues/7),
[anti-ban #8](https://github.com/ivzc07/bienwabot/issues/8).

---

## 0. Ground truth (existing infra, inspected)

- **An Evolution API already runs on this Coolify** as the `gogym-evo` service: image `evoapicloud/evolution-api:v2.3.7`, backed by Postgres 16 + Redis 7, healthy, multi-instance, currently shared with a GoGym reminder bot.
It sits on its own private Docker network and is **not** joined to the shared `coolify` network.
- **Uptime Kuma** already runs (`kuma`, "Infra" project) - reused here for liveness.
- **Postgres + Redis** are the standard datastore pattern across this server.

The v2.3.7 build is recent enough that the Baileys `tc`/`cs` privacy tokens [anti-ban #8](https://github.com/ivzc07/bienwabot/issues/8) requires are populated, avoiding the harsher 463 reach-out limit.

---

## 1. Decisions this ticket locks

| # | Decision | Choice |
|---|---|---|
| 1 | Evolution hosting | **Dedicated `bien-evo`** Evolution service (own PG16 + Redis7), isolated from GoGym. Two instances: `bien-rebe` (primary), `bien-backup` (warm standby). |
| 2 | Agent state store | **Separate `rebe` database on bien-evo's Postgres** (schema-isolated), accessed with psycopg. No new datastore container. |
| 3 | Agent <-> Evolution networking | **Internal Docker network** (both on Coolify's shared network); agent has **no public URL**; webhook path carries a secret token. |
| 4 | Alerting | **Telegram** (out-of-band from WhatsApp), plus an Uptime Kuma push heartbeat. |
| 5 | Backup failover | **Manual, alert-driven** swap on a permanent ban; temp-ban/463 is always auto-back-off + wait, never a swap. |

Rationale, briefly:

- **Dedicated Evolution (1).** A WhatsApp ban hits one number/instance, never the server, so reuse would not cross-contaminate ban risk - but a dedicated service isolates the *operational* blast radius (a redeploy or resource spike on GoGym cannot touch bien.mx) and allows independent Evolution upgrades. Chosen for isolation over the marginally cheaper shared option.
- **Reuse bien-evo's Postgres for agent state (2).** The agent already hard-depends on bien-evo being up, so putting its tiny state (a few writes/day, single writer) in a separate `rebe` database on the same Postgres adds **no new failure coupling**, gets backed up alongside Evolution, and stays cleanly isolated by database/schema. Avoids a redundant DB container.
- **Internal network (3).** The agent never needs to be reachable from the internet. Keeping both containers on one internal network means the webhook and send traffic never leave the host (no TLS round-trip out and back) and the agent is not publicly exposed. A secret token in the webhook path is defense-in-depth even inside Docker.
- **Telegram (4).** Infra alerts must be **out-of-band**: alerting over WhatsApp-via-Evolution fails exactly when Evolution is down - the moment you most need the alert. Telegram is instant, free, and natively supported by Kuma.
- **Manual failover (5).** `bien-backup` is the only warm standby; auto-switching on a possibly-false ban signal would burn it and leave the bot cold with no backup. A human confirms the number is really dead before flipping. Fits the balanced/cautious posture.

---

## 2. Components

### 2.1 `bien-evo` - Evolution API service (dedicated)

- Deployed as a **Coolify service**, Evolution v2.x on its own **Postgres 16 + Redis 7**, mirroring the proven `gogym-evo` shape.
- **DB-backed instance store** (not Baileys `useMultiFileAuthState`), per [anti-ban #8](https://github.com/ivzc07/bienwabot/issues/8) - the WhatsApp session survives restarts.
- **Two instances** on this one service:
  - `bien-rebe` - the primary number "Rebe" posts and replies from.
  - `bien-backup` - a warmed second SIM, already a member of the target group, kept as a standby that never posts simultaneously.
- Each instance sets a **per-instance webhook** pointed at the agent (global webhook stays off), subscribing to `messages.upsert` (inbound group messages) and `connection.update` (WhatsApp link state).
- Joined to the **shared `coolify` internal network** so the agent can reach it by container name and vice versa.

> **Build note - re-pairing.** The number currently paired on `gogym-evo` must be re-paired to the new `bien-rebe` instance (one QR scan) when `bien-evo` is stood up. The warm-up state on the number itself is unaffected; only the Evolution instance it links to changes.

### 2.2 `rebe-agent` - the Python bot (single process)

Deployed as a **Coolify Application from a Git repo** (Dockerfile), same pattern as the existing `tx-bot-brain` bots, `restart: unless-stopped`, on the shared internal network, **no public FQDN**.

One long-running process, **two triggers**, per [framework #4](https://github.com/ivzc07/bienwabot/issues/4):

1. **Webhook leg** - a FastAPI endpoint `POST /webhook/<secret>` receives Evolution's `messages.upsert`. Runs the three-tier reply gate ([reply policy #7](https://github.com/ivzc07/bienwabot/issues/7)) as a typed Pydantic AI `ReplyDecision`; on a reply, marks the incoming message read, then sends through the pacer.
2. **News leg** - an **APScheduler** job pulls from HN Algolia + first-party RSS, dedups against `posted_store`, ranks, has DeepSeek write a `NewsPost` ([news pipeline #5](https://github.com/ivzc07/bienwabot/issues/5)), then sends through the pacer.

Both legs call the same **Pydantic AI agent** wrapping the DeepSeek **non-thinking** chat model, and both post back to Evolution's send endpoint over the internal network.

**One shared pacer.** A single in-process rate-limiter / scheduler enforces the [anti-ban #8](https://github.com/ivzc07/bienwabot/issues/8) envelope across **both** legs together: <=4 sends/min, 3 posts/hr, 12 posts/day hard ceiling, near-silent 02:00-06:00 America/Mexico_City, `composing` presence + length-scaled Gaussian-jittered `delay` (~30 ms/char, clamped 1500-5000 ms) before every send, presence refresh, no back-to-back duplicates. Because the ceilings must span posts *and* replies, the limiter lives in one place, in one process.

**Target instance is config.** The agent talks to whichever instance `EVOLUTION_INSTANCE` names (`bien-rebe` by default). The manual failover swap is: set `EVOLUTION_INSTANCE=bien-backup` and redeploy.

> **Single-replica invariant.** Because the pacer state and the APScheduler jobs live in-process, `rebe-agent` must run **exactly one replica** - two would double-fire news posts and double-count the rate limits. If it ever needs to scale out, the limiter and scheduler must first move to a shared Redis/Postgres lock. Documented so nobody bumps the replica count.

### 2.3 State - the `rebe` database

A separate database on bien-evo's Postgres, two tables:

- **`group_memory`** - keyed by group JID, a rolling window of recent turns + persona state, loaded into Pydantic AI's `message_history` per event ([framework #4](https://github.com/ivzc07/bienwabot/issues/4)).
- **`posted_store`** - the anti-repost gate ([news pipeline #5](https://github.com/ivzc07/bienwabot/issues/5)): every posted item keyed by `(source, id)`, canonical URL, and SHA-256 content hash; every candidate is checked against it before ranking.

### 2.4 Observability - Uptime Kuma + Telegram

- The agent **pushes a heartbeat** (~every 60 s) to an Uptime Kuma push monitor; a missed beat means Kuma fires a **Telegram** alert. This proves the process *and* its internal loop are alive, and needs no exposed health port.
- The agent also pushes **event alerts directly to the same Telegram bot**: WhatsApp 463 / temp-ban, permanent ban, Evolution `connection.update` = disconnected, and DeepSeek errors.

---

## 3. Data flows

**Inbound reply**

```
WhatsApp group message
  -> Evolution (bien-rebe) messages.upsert
  -> POST http://rebe-agent:8000/webhook/<secret>
  -> reply gate (#7): ReplyDecision(should_reply, ...)
     - if NO  -> stop (silent); persist turn to group_memory
     - if YES -> markMessageAsRead
              -> shared pacer: composing presence + jittered delay
              -> Evolution sendText (bien-rebe)
              -> group; persist both turns to group_memory
```

**Scheduled news post**

```
APScheduler tick
  -> fetch: HN Algolia (httpx) + first-party RSS (feedparser)
  -> dedup vs posted_store  (source id / canonical URL / content hash)
  -> rank (authority + HN points + recency); per-run cap
  -> DeepSeek -> validated NewsPost (Spanish, one line + link)
  -> shared pacer: quiet-hours + rate ceilings + composing + jittered delay
  -> Evolution sendText (bien-rebe)
  -> group; write posted_store
```

Both send paths pass through the **same pacer**, so the daily/hourly envelope is enforced over posts and replies jointly.

---

## 4. Secrets

All secrets are **Coolify per-app environment variables** (encrypted at rest, injected as env; nothing in git; a `.env.example` documents the names):

| Var | Purpose |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek brain. |
| `EVOLUTION_API_URL` | Internal URL of bien-evo (`http://<bien-evo-api>:8080`). |
| `EVOLUTION_API_KEY` | Evolution `AUTHENTICATION_API_KEY`. |
| `EVOLUTION_INSTANCE` | Active instance name (`bien-rebe`; swap to `bien-backup`). |
| `WEBHOOK_SECRET` | Token embedded in the webhook path. |
| `REBE_DATABASE_URL` | psycopg URL to the `rebe` database on bien-evo's Postgres. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alert channel. |
| `KUMA_PUSH_URL` | Uptime Kuma heartbeat push URL. |
| `TZ` | `America/Mexico_City` (quiet hours / circadian shape). |

---

## 5. Failure handling

| Signal | Response |
|---|---|
| Evolution `connection.update` = disconnected | Pause all sending; Telegram alert; Evolution auto-reconnects; on reconnect re-enter the brief [#8](https://github.com/ivzc07/bienwabot/issues/8) ramp (no cold resume at full rate). Heartbeat keeps flowing (agent is alive). |
| 463 reach-out / 4xx rate error on send | Back off; Telegram alert; do **not** keep hammering. |
| DeepSeek error / timeout | Drop that item silently to the group (fail-silent, [#7](https://github.com/ivzc07/bienwabot/issues/7)); Telegram alert to maintainer. |
| Permanent ban (no appeal) | Stop; Telegram alert; **wait for human**. Human sets `EVOLUTION_INSTANCE=bien-backup`, redeploys; begins warming a fresh replacement backup SIM. |
| Missed heartbeat | Kuma -> Telegram (process down / hung). |

---

## 6. Pre-launch checklist (execution prerequisites)

Planning-only spec; these are the builder's go-live steps, not done here:

- [ ] Create the `bien-evo` Evolution service on Coolify (v2.x, own PG16 + Redis7); join it to the shared internal network.
- [ ] Create the `bien-rebe` instance; scan the QR to re-pair the primary number; set its per-instance webhook to the agent with the secret.
- [ ] Create the `bien-backup` instance for the warmed backup SIM; pre-join it to the group; keep it idle.
- [ ] Create the `rebe` database on bien-evo's Postgres; create the `group_memory` and `posted_store` tables.
- [ ] Deploy `rebe-agent` (Coolify Application, Dockerfile, single replica, internal network, no public FQDN); set all env vars.
- [ ] Create the Uptime Kuma push monitor and the Telegram bot; wire both.
- [ ] Verify: send a test group message -> webhook -> reply; wait for one scheduled news post; kill the agent and confirm the Kuma -> Telegram alert fires.

---

## Bottom line

- **Two new resources on the existing Coolify:** a dedicated `bien-evo` Evolution service (own PG16 + Redis7, `bien-rebe` + `bien-backup` instances) and a single-replica `rebe-agent` Python app, both on the shared internal network; the agent has no public URL.
- **The agent is one process with two triggers and one shared pacer** - FastAPI webhook + APScheduler news loop, both through the same Pydantic AI + DeepSeek brain and the same anti-ban rate-limiter, so the envelope covers posts and replies together.
- **State is a separate `rebe` database on bien-evo's Postgres** - no new datastore, no new failure coupling, backed up with Evolution.
- **Observability reuses Uptime Kuma** (push heartbeat) with **Telegram** as the out-of-band alert channel for ban/463/disconnect/DeepSeek errors.
- **Failover is manual and alert-driven** to protect the single warm backup; temp-bans back off and wait, permanent bans wait for a human to flip `EVOLUTION_INSTANCE` and redeploy.
- **Still open (owned by [posting cadence #11](https://github.com/ivzc07/bienwabot/issues/11)):** the exact posts/day and the DeepSeek token/cost budget, both of which live inside this architecture and this envelope.
