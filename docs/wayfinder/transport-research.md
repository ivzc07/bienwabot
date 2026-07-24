# WhatsApp Group Transport Research

Wayfinder ticket: pick the WhatsApp **group** transport for a self-hosted, **Python**-driven AI-news bot that reads and posts inside a real WhatsApp group.
Brain is DeepSeek (Python). Hosting is a cheap VPS / Coolify. There is **no official WhatsApp Business API for groups**, so every option here is an unofficial, reverse-engineered client.

No pre-existing docs/notes convention was found in the repo (no `docs/`, `notes/`, or `.wayfinder/`), so this file was created at `docs/wayfinder/transport-research.md`.

Research date: 2026-07. Every factual claim is cited with a primary-source URL (official docs, GitHub repos/READMEs, or first-party API docs).

---

## The key decision axis

The strongest WhatsApp libraries are all **Node**. Our brain is **Python**. So the central question is:

> Can Python treat the WhatsApp part as a **black box behind an HTTP API + webhooks**, or must we run/embed a Node process ourselves?

Candidates split into two groups:

- **Libraries** (you embed and run Node yourself): Baileys, whatsapp-web.js, wppconnect (library).
- **Dockerized HTTP servers** (Node runs as a black box; you talk HTTP + receive webhooks): WAHA, Evolution API, wppconnect-server.

For a Python bot, the HTTP-server group is the natural fit.

---

## 1. Baileys (`@whiskeysockets/baileys`)

Node/TypeScript, WebSocket-based, **no browser** (talks the WhatsApp Web protocol directly).

- **Group support:** Yes. Send to a group by using its `@g.us` JID: `await sock.sendMessage(jid, { text: 'hello word' })`; read messages via the upsert event; group info via `sock.groupMetadata(jid)` and `sock.groupFetchAllParticipating()`. Group JID format is `123456789-123345@g.us`. [Baileys README](https://raw.githubusercontent.com/WhiskeySockets/Baileys/master/README.md)
- **Event model:** EventEmitter socket. Incoming messages arrive via `sock.ev.on('messages.upsert', ({ messages }) => { ... })`. There is **no built-in HTTP API** - it is an embeddable library. [Baileys README](https://raw.githubusercontent.com/WhiskeySockets/Baileys/master/README.md), [baileys.wiki intro](https://baileys.wiki/docs/intro)
- **Docker/Coolify fit:** No official image; you build and containerize your own Node app. Auth state must be persisted (`useMultiFileAuthState` is shown as a starting point, and the docs explicitly warn "DO NOT rely on it in prod"). [baileys.wiki intro](https://baileys.wiki/docs/intro)
- **Python boundary:** Poor by itself. No HTTP layer - you would have to write the Node service and a Python<->Node bridge yourself. (This is exactly what WAHA / Evolution do for you.)
- **Maintenance:** Actively maintained, MIT license, v7.0.0+, ~2,257 commits. Community-maintained by WhiskeySockets after the original author (adiwajshing) stepped away/archived the original repo - so it has a history of a maintainer handover and breaking major versions. [Baileys GitHub](https://github.com/WhiskeySockets/Baileys)
- **Ban surface:** Unofficial. README: "not affiliated ... with WhatsApp" and discourages "bulk or automated messaging." No browser means a smaller footprint than Puppeteer clients, but still detectable. [Baileys GitHub](https://github.com/WhiskeySockets/Baileys)

## 2. whatsapp-web.js

Node library driving **Puppeteer / headless Chrome** against WhatsApp Web.

- **Group support:** Yes - create groups, add/remove/promote participants, modify settings, send/receive in groups. [wwebjs docs](https://docs.wwebjs.dev/)
- **Event model:** `client.on('message', (msg) => { ... })`. **No HTTP API** - embeddable Node library, requires Node >= 18. [wwebjs docs](https://docs.wwebjs.dev/)
- **Docker/Coolify fit:** Heavier - it runs a real Chromium instance, so more RAM/CPU and more fragile in containers. No official server image. You containerize your own app + session volume.
- **Python boundary:** Poor by itself (no HTTP layer).
- **Maintenance:** Apache-2.0, npm `whatsapp-web.js` v1.34.7. Maintained (Pedro S. Lopez), but historically slower to react when WhatsApp Web changes because it depends on the web UI internals. [wwebjs docs](https://docs.wwebjs.dev/)
- **Ban surface:** Its own disclaimer: "WhatsApp does not allow bots or unofficial clients on their platform, so this shouldn't be considered totally safe" and "it is not guaranteed you will not be blocked." Puppeteer footprint is larger than Baileys. [wwebjs docs](https://docs.wwebjs.dev/)

## 3. WAHA (WhatsApp HTTP API)

Dockerized HTTP API that **wraps multiple engines** (NOWEB = Baileys-based, WEBJS = whatsapp-web.js, plus WPP, GOWS, VENOM) behind one REST + webhook interface.

- **Group support:** Yes, extensive - 30+ group endpoints (create, list, join, leave, participants add/remove/promote/demote, subject/description/picture, invite codes). Group messages are delivered through the normal message webhook; group chats are identified by the `@g.us` JID and the payload includes a `participant` field for group senders. Group support is **not** gated. [WAHA groups](https://waha.devlike.pro/docs/how-to/groups/), [WAHA receive messages](https://waha.devlike.pro/docs/how-to/receive-messages/)
- **Event model (the Python boundary):** **HTTP webhook POST** to your URL is the primary mechanism; a **WebSocket** option also exists. Message events include `message`, `message.any`, `message.reaction`, `message.ack`, `message.revoked`, etc. Configured per session:
  ```json
  { "name": "default", "config": { "webhooks": [ { "url": "https://your-endpoint.com/webhook", "events": ["message"] } ] } }
  ```
  This is exactly the black-box-behind-HTTP model our Python bot wants. [WAHA receive messages](https://waha.devlike.pro/docs/how-to/receive-messages/)
- **Docker/Coolify fit:** Official image `devlikeapro/waha`; docker-compose with `./.sessions:/app/.sessions` volume to persist auth. Docs include a **dedicated Coolify guide** (and EasyPanel). [WAHA install](https://waha.devlike.pro/docs/how-to/install/)
- **Python boundary:** Best in class - Python calls REST to send, and receives incoming group messages via webhook POST. No Node code to write; the whole engine is a black box. [WAHA intro](https://waha.devlike.pro/docs/overview/introduction/)
- **Maintenance / cost:** **Important 2026 change:** starting **v2026.6.1**, all features formerly gated behind the paid "WAHA Plus" (unlimited sessions, multimedia messages, all storages, built-in security) are now in **WAHA Core - 100% free and open source**. The old Core-vs-Plus split (which previously limited free users on media and multi-session) is gone; only a voluntary $5/mo Community tier remains. This removes the earlier concern that group/media features sat behind a paywall. [WAHA Plus / pricing](https://waha.devlike.pro/docs/how-to/waha-plus/)
- **Ban surface:** Unofficial. WAHA states plainly: "it is not guaranteed that you will not be blocked by using this method. WhatsApp does not allow bots or unofficial clients." You can pick the NOWEB (Baileys) engine to avoid the browser footprint. [WAHA intro](https://waha.devlike.pro/docs/overview/introduction/)

## 4. Evolution API

Dockerized REST API (Node 20+, TypeScript, Express), **Baileys-based** WhatsApp Web connector plus an optional official WhatsApp Cloud API connector.

- **Group support:** Yes (group endpoints exist), though the README front page is light on detail. Baileys underneath supports groups. [Evolution API GitHub](https://github.com/EvolutionAPI/evolution-api)
- **Event model:** Rich - **webhooks**, **Socket.io** WebSocket, **RabbitMQ**, **Apache Kafka**, **Amazon SQS**. Good HTTP boundary for Python. [Evolution API GitHub](https://github.com/EvolutionAPI/evolution-api)
- **Docker/Coolify fit:** Dockerized, **multi-instance** (multiple numbers per server via per-instance tokens). Self-hostable on Coolify like any Docker app. [Evolution API GitHub](https://github.com/EvolutionAPI/evolution-api)
- **Python boundary:** Good - REST to send, webhook/socket/queue to receive. Same black-box benefit as WAHA.
- **Maintenance:** Apache-2.0 "with additional brand-protection conditions," open source, active (~2,629 commits, many open PRs). Broader/heavier feature surface (queues, Chatwoot, Typebot integrations) than a group bot needs. [Evolution API GitHub](https://github.com/EvolutionAPI/evolution-api)
- **Ban surface:** Same unofficial Baileys risk profile as WAHA's NOWEB engine.

## 5. wppconnect / wppconnect-server

Puppeteer-based Node library, plus a separate **wppconnect-server** that exposes a REST API.

- **Group support:** Yes - server handles "sessions, messages, contacts, groups, and webhooks." [wppconnect.io](https://wppconnect.io/)
- **Event model:** Library uses `client.onMessage((msg) => { ... })`; the server exposes REST + webhooks with a Swagger UI at `/swagger/wppconnect-server`. [wppconnect.io](https://wppconnect.io/)
- **Docker/Coolify fit:** Dockerizable; runs Puppeteer/Chromium (heavier than Baileys-based servers).
- **Python boundary:** Good via wppconnect-server (REST + webhooks). [wppconnect.io](https://wppconnect.io/)
- **Maintenance:** MIT, active team, large community (library ~3.4k stars, server ~1k stars, 120+ contributors). [wppconnect.io](https://wppconnect.io/)
- **Ban surface:** Unofficial + Puppeteer footprint (like whatsapp-web.js).

---

## Comparison at a glance

| Transport | Group read+send | Python boundary | Docker / Coolify | Cost | Engine footprint | Maintenance |
|---|---|---|---|---|---|---|
| **WAHA** | Yes, not gated | **REST + webhook (native)** | Official image + Coolify guide | Free since v2026.6.1 | Choose NOWEB (Baileys, no browser) | Active, single-vendor |
| **Evolution API** | Yes | REST + webhook/socket/queue | Dockerized, multi-instance | Free (Apache-2.0) | Baileys (no browser) | Active, heavier scope |
| **wppconnect-server** | Yes | REST + webhook | Dockerized (Puppeteer) | Free (MIT) | Chromium (heavy) | Active |
| **Baileys** | Yes | **None - embed Node yourself** | Build your own | Free (MIT) | No browser | Active, handover history |
| **whatsapp-web.js** | Yes | **None - embed Node yourself** | Build your own (Chromium) | Free (Apache-2.0) | Chromium (heavy) | Active, slower to patch |

---

## Recommendation

**Use WAHA (WhatsApp HTTP API), running the NOWEB (Baileys) engine.**

WAHA is the only candidate that hits every requirement for this exact use case at once: it exposes a first-party **REST API to send** and **webhook POST to receive** group messages, so the Python/DeepSeek bot treats WhatsApp as a pure black box with zero Node code to write or maintain ([WAHA receive messages](https://waha.devlike.pro/docs/how-to/receive-messages/)). It ships an official Docker image (`devlikeapro/waha`) with a documented **Coolify** deployment and a simple `./.sessions` persistence volume ([WAHA install](https://waha.devlike.pro/docs/how-to/install/)), full un-gated group support ([WAHA groups](https://waha.devlike.pro/docs/how-to/groups/)), and as of **v2026.6.1 it is 100% free** with the old paid "Plus" gating removed ([WAHA Plus](https://waha.devlike.pro/docs/how-to/waha-plus/)). Picking the NOWEB engine keeps it on the lightweight Baileys WebSocket protocol (no headless Chrome), which minimizes VPS cost and the detection footprint - though, like all options here, it remains an unofficial client and WhatsApp "does not allow bots or unofficial clients," so use a dedicated/burner number and modest posting cadence ([WAHA intro](https://waha.devlike.pro/docs/overview/introduction/)).

**Runner-up: Evolution API.** Same black-box HTTP + webhook model on the same Baileys engine, plus multi-instance and queue transports (RabbitMQ/Kafka/SQS) if you later scale ([Evolution API GitHub](https://github.com/EvolutionAPI/evolution-api)). It loses to WAHA only on ergonomics: a heavier, broader feature surface and thinner group-specific docs than WAHA's purpose-built group/webhook how-tos. Avoid raw **Baileys/whatsapp-web.js** here because they force you to build and babysit a Node service and a Python<->Node bridge, and skip **whatsapp-web.js/wppconnect** engines where possible since their headless-Chrome footprint costs more RAM and is easier to detect than Baileys.
