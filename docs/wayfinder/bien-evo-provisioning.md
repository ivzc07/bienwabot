# bien-evo Provisioning - Runbook

Wayfinder ticket: [Provision bien-evo Evolution service and the rebe database](https://github.com/ivzc07/bienwabot/issues/14).
Implements sections 2.1 and 2.3 of [the deployment architecture spec](deployment-architecture-spec.md), plus the first and fourth items of its section 6 checklist.

This records what is actually running, verified on 2026-07-25.
No secret values live in this file.
Every credential is a Coolify environment variable.

---

## 1. What exists now

| Thing | Value |
|---|---|
| Coolify project | `bienwabot` (`lr19helgb12xgu7f4x3pk7w7`) |
| Service | `bien-evo` (`i856ku5lxcr2o1v64eveeahq`), environment `production` |
| Evolution image | `evoapicloud/evolution-api:v2.3.7` |
| Postgres | `postgres:16-alpine` |
| Redis | `redis:7-alpine` |
| Server | `localhost` (`s3stbebt2f7t117c2gr7y9jw`) |
| Destination | `pxrf9uiha61v0zcdq73g5wnk`, Docker network `bfcginilwluqk0gk7wctlwz8` |
| Manager UI | `http://evo-i856ku5lxcr2o1v64eveeahq.45.132.242.102.sslip.io/manager` |

Container names, which are the addresses other containers use:

- `api-i856ku5lxcr2o1v64eveeahq` - Evolution, port 8080
- `postgres-i856ku5lxcr2o1v64eveeahq` - Postgres, port 5432
- `redis-i856ku5lxcr2o1v64eveeahq` - Redis, port 6379

All three report `running:healthy`.

## 2. Correction to the spec: which network is "shared"

The spec calls the shared internal network `coolify`, based on the earlier inspection.
That is wrong for this server in practice.

Every application and database on this Coolify deploys to destination `pxrf9uiha61v0zcdq73g5wnk`, whose Docker network is `bfcginilwluqk0gk7wctlwz8`.
The name is a leftover from the n8n service that first created it, and is misleading, but it is the network all 32 existing resources share.
`bien-evo` was created on that destination with "Connect to Predefined Network" enabled, so its containers sit on both their own per-service network and the shared one.

The practical consequence: `rebe-agent` must be deployed to this same destination, and then it reaches Evolution at `http://api-i856ku5lxcr2o1v64eveeahq:8080`.

## 3. The instance store is database-backed

Set on the service:

- `DATABASE_SAVE_DATA_INSTANCE=true`
- `CACHE_REDIS_SAVE_INSTANCES=false`

So instance state lives in Postgres, not in Baileys' file-based auth state and not in Redis.
This is what lets a WhatsApp session survive a restart, per [anti-ban #8](https://github.com/ivzc07/bienwabot/issues/8).

Verified two ways, because surviving a restart alone would also be explained by the persistent `/evolution/instances` volume:

1. The service was restarted, and `bien-dev` was still present afterwards.
2. Evolution's own `Instance` table in Postgres holds the row:

   ```
      name   | connectionStatus
   ----------+------------------
    bien-dev | close
   ```

## 4. Evolution version

`2.3.7`, reported by the API root endpoint.

This is the same build as the proven `gogym-evo` service, which section 0 of the spec already established is recent enough that the Baileys `tc`/`cs` privacy tokens are populated.
That is what keeps the number off the harsher 463 reach-out limit.

## 5. Reachability, verified from another container

Run from `tx-bot-brain`, an application outside this service and on the shared network, addressing Evolution by container name with no public URL involved:

| Call | Result |
|---|---|
| `GET http://api-i856ku5lxcr2o1v64eveeahq:8080/instance/fetchInstances` with the API key | `200` |
| the same call with no API key | `401` |

The `401` matters as much as the `200`.
It is Evolution's own auth layer answering, which proves the name resolved and the connection was made, rather than a network failure that happened to look like a rejection.

The test was deliberately not run from `bien-evo`'s own `postgres` container, since those two also share the per-service network and so would prove nothing about the shared one.

## 6. The `rebe` database

A separate database on `bien-evo`'s Postgres, for `group_memory` and `posted_store` ([spec section 2.3](deployment-architecture-spec.md)).

It is empty.
No tables yet, by design - each later ticket creates the tables it needs.

Isolation model:

- `rebe` is a plain `LOGIN` role, not a superuser, owning only the `rebe` database.
- `CONNECT` on Evolution's own database (`postgres`) is revoked from `PUBLIC`, which is what actually stops the `rebe` credentials reaching Evolution's data.
- `CONNECT` on `rebe` is revoked from `PUBLIC` and granted back to `rebe` alone.
- Evolution is unaffected, because it connects as the bootstrap superuser, which the revoke does not apply to.

Verified:

- as `rebe` into `rebe`: returns `rebe@rebe`
- as `rebe` into `postgres`: refused, `User does not have CONNECT privilege.`

## 7. The dev instance

`bien-dev` exists on this service, integration `WHATSAPP-BAILEYS`, status `close` (created with `qrcode: false`, so no number is paired).

**This is the integration-test target.**
Later code tickets point their tests at `bien-dev` so no test traffic ever touches the production number.

To use it for a test that needs a real WhatsApp link, pair a throwaway SIM through the manager UI.
For tests that only exercise Evolution's HTTP surface, no pairing is needed.

`bien-rebe` and `bien-backup` are deliberately **not** created here.
Per the ticket they are created at go-live, once the SIMs are warm.

## 8. Secrets

Stored as Coolify environment variables on the `bien-evo` service, never in git:

| Variable | Meaning |
|---|---|
| `SERVICE_PASSWORD_AUTHENTICATIONAPIKEY` | Evolution's `AUTHENTICATION_API_KEY`. Copy to the agent's `EVOLUTION_API_KEY`. |
| `REBE_DATABASE_URL` | psycopg URL for the `rebe` database, using the `postgres-…` container name. |
| `REBE_DB_PASSWORD` | Password for the `rebe` role, kept so the role can be recreated. |
| `SERVICE_USER_POSTGRES`, `SERVICE_PASSWORD_POSTGRES` | Coolify-generated Postgres superuser, used by Evolution itself. |

`.env.example` in the repo carries the names and shapes only.

## 9. Operating notes

Coolify exposes no exec API, so one-off commands inside these containers are run as a throwaway scheduled task: create a task with frequency `* * * * *` and the target container, read the execution's `message` for stdout, then delete the task.
The cron can fire more than once before the delete lands, so any such command must be idempotent.
Coolify stores `command` in a `varchar(255)`, and rejects anything longer with a bodyless HTTP 500.

`psql -tAc` output sometimes comes back with an empty `message`.
Use `psql -c` when the output matters.

## 10. Still open

From the section 6 checklist, not this ticket:

- Create `bien-rebe`, re-pair the primary number, set its per-instance webhook.
- Create `bien-backup` and pre-join it to the group.
- Create the `group_memory` and `posted_store` tables.
- Deploy `rebe-agent` to destination `pxrf9uiha61v0zcdq73g5wnk` so it lands on the shared network.
- Wire the Uptime Kuma push monitor and the Telegram bot.
