# Go-Live Record - what is actually running

Wayfinder ticket: [Go-live](https://github.com/ivzc07/bienwabot/issues/25).
The procedure is [the go-live runbook](go-live-runbook.md); this is what happened when it was run, on **2026-07-26**, and where it differs from the plan.

No secret values live in this file.
Every credential is a Coolify environment variable.

---

## 1. What is live

| Thing | Value |
|---|---|
| Evolution instance Rebe posts from | **`bien-dev`** on the `bien-evo` service |
| Number paired | +52 1 656 533 2108, by QR, 2026-07-26 |
| Group | `bien.mx`, JID `120363429053496005@g.us`, 4 members |
| Agent | Coolify application `rebe-agent` (`p4oasvjqsjefdb2uz28dom7v`), project `bienwabot`, environment `production` |
| Source | `github.com/ivzc07/bienwabot`, branch `main`, Dockerfile build |
| Destination | `pxrf9uiha61v0zcdq73g5wnk` (network `bfcginilwluqk0gk7wctlwz8`), no FQDN, no published port |
| Network alias | `rebe-agent`, which is the name Evolution's webhook posts to |
| Replicas | 1, and it must stay 1 - see the invariant in [the runbook](go-live-runbook.md) |
| Ops chat | Telegram bot `@copaw_gymbro_bot`, chat `6406624282` |
| Liveness | Uptime Kuma push monitor `rebe-agent`, 120 s |
| Ramp started | **2026-07-26 22:21 UTC** (`paired`), so the clamp runs to **2026-08-09** |

Week one allows three news posts a day, week two four, and replies are never clamped.

## 2. Three deviations from the spec

**The production instance is called `bien-dev`, not `bien-rebe`.**
The warmed number was already paired to `bien-dev` - the instance [#14](https://github.com/ivzc07/bienwabot/issues/14) created as the integration-test target - and re-pairing it to a fresh `bien-rebe` would have cost a second QR scan for a cosmetic gain.
Evolution has no rename, so the name stayed and `EVOLUTION_INSTANCE` points at it.
The consequence to keep in mind: **there is no separate test instance any more.**
Anything pointed at `bien-dev` now talks to the real group from the real number.
A future test instance needs a new name and a throwaway SIM.

**The Kuma push URL is public, not internal.**
The spec assumed `http://kuma:3001/...` on the shared network.
The `kuma` service has `connect_to_docker_network` disabled, so it sits only on its own per-service network and the agent cannot reach it by container name.
The heartbeat therefore goes out through the proxy at `http://uptimekuma-…sslip.io/api/push/<token>`.
It works, and the token is still a secret in the URL path.
Joining Kuma to the shared network would let this move back inside the host, and is the tidier fix whenever somebody is in there anyway.

**There is no backup instance yet.**
`bien-backup` does not exist, because the second SIM is not warm ([#15](https://github.com/ivzc07/bienwabot/issues/15) is still open).
Until it is, a permanent ban means Rebe is simply off: the failover procedure in [the ramp and recovery runbook](ramp-and-recovery-runbook.md) has nothing to fail over to.
This is the single biggest hole in the deployment right now.

## 3. What the first deploy found

The first boot against the empty `rebe` database crash-looped on `relation "planned_slots" does not exist`.
The schema is prepared beside the server rather than before it, so that a database a few seconds behind the container does not stop the process coming up - but the scheduler's first act is to read the day's plan, and that read raced the `CREATE TABLE` and lost.
Fixed in [#45](https://github.com/ivzc07/bienwabot/pull/45): the plan store makes its own table on first use, exactly as the soft pause and the ramp already did.

Because those crash-looping boots happened before the fix, the ramp was stamped at 22:21 UTC on pairing day, which is the right day and about half an hour before the process actually stayed up.

## 4. Still open

- [x] Attach the Telegram notification to the Kuma monitor.
Done 2026-07-27: the monitor now uses the notification Kuma already had, `bien.mx health`, which is Telegram on chat `6406624282` - the ops chat.
No second notification was created.
- [x] Stop the application and confirm the alert fires, then start it and confirm it clears.
Run 2026-07-27: stopped at 00:25 UTC, Kuma went red at **00:26:56** (`No heartbeat in the time window`) and green again at **00:29:59** (`rebe-agent loop alive`).
Both Telegram messages arrived in the ops chat.
So a dead agent is noticed in about two minutes, which is the heartbeat interval plus a little.
- [ ] A test message in the group draws a reply (webhook leg proven end to end).
- [ ] One scheduled news post lands unattended. The first drawn slot was 20:41 local on pairing day.
- [ ] Warm the backup SIM, create its instance, join it to the group, give it the same webhook.

## 5. Operating notes

The webhook both instances post to is `http://rebe-agent:8000/webhook/<WEBHOOK_SECRET>`, subscribed to `MESSAGES_UPSERT` and `CONNECTION_UPDATE` only.
The global webhook is off on the service.

To read the day's plan without a shell - Coolify exposes no exec API - run a one-off scheduled task against the `postgres` container of the `bien-evo` service:

```sql
SELECT day, window_name, due_at, state, tier FROM planned_slots ORDER BY due_at;
```

The command must be idempotent and under 255 characters, per [the provisioning runbook](bien-evo-provisioning.md), section 9.

`LOG_LEVEL=DEBUG` is what shows the scheduler's waits (`waiting 35m for a look at the news`); it is noisy and worth turning back to `INFO` afterwards.
