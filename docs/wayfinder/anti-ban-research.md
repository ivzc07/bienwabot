# WhatsApp Anti-Ban / Detection-Surface Research

Wayfinder ticket: ground an **anti-ban operational playbook** for "Rebe", a Python-driven AI-news bot that posts and lightly replies inside a real WhatsApp **group**.
Transport is **Evolution API** (dockerized, Baileys-based HTTP gateway; Python talks to it over HTTP + webhook).
Brain is DeepSeek. Hosting is a cheap VPS / Coolify. The number is a **dedicated new physical SIM**, with a warmed **backup number** on standby.

Research date: 2026-07.
Every factual claim is cited with a primary-source URL.
Claims are labeled **[DOCUMENTED]** (official docs/code) or **[COMMUNITY]** (empirical folklore / third-party heuristics - not official).

> **Honest baseline:** WhatsApp does **not** publish ban thresholds, rate limits, or a detection spec.
> Nearly every number below that is not an official WhatsApp figure is empirical community folklore.
> Treat all rate/warm-up numbers as heuristics, not guarantees.
> The only hard official facts are: unofficial clients violate the Terms of Service, and accounts can be banned without warning or appeal.

---

## 0. The unavoidable truth: this is an unofficial client

- Baileys itself: "The maintainers of Baileys do not in any way condone the use of this application in practices that violate the Terms of Service of WhatsApp" and "Do not spam people with this. We discourage any stalkerware, bulk or automated messaging usage." **[DOCUMENTED]** [Baileys README](https://github.com/WhiskeySockets/Baileys/blob/master/README.md)
- Baileys also warns the session is fragile: "If you mess up one of your updates, WhatsApp can log you out of all your devices and you'll have to log in again." **[DOCUMENTED]** [Baileys npm](https://www.npmjs.com/package/baileys)
- WhatsApp bans unofficial/altered clients (GB WhatsApp, WhatsApp Plus, etc.), "frequently on a permanent basis with no warning," and a permanent ban after appeal is final. Evolution/Baileys are a different category (a library, not a modded app), but they share the same "not an official client" exposure. **[DOCUMENTED, WhatsApp policy]** [WhatsApp FAQ - unsupported/unauthorized versions](https://faq.whatsapp.com/general/security-and-privacy/about-unsupported-or-unauthorized-versions-of-whatsapp)
- WhatsApp does publish some official limits that bound the environment: individual broadcast lists are being capped (Meta cited an example of ~30 broadcast messages/month), and unverified WhatsApp Business API accounts start at 250 messages/day. Neither is a group-posting limit, but both show WhatsApp actively meters "reaching out." **[DOCUMENTED]** [TechCrunch, 2025-03-18](https://techcrunch.com/2025/03/18/whatsapp-will-soon-limit-number-of-broadcast-messages-users-and-businesses-can-send/)

**Trade-off:** posting to a group the number already belongs to (members already "know" each other) is materially lower-risk than cold-messaging strangers, because WhatsApp's harshest rate-limits target "reaching out" to unknown contacts (see 463 error below). Rebe's job - post news into one joined group and lightly reply - sits on the safer side of that line.

---

## 1. Baileys / Evolution API ban-detection surface

**What makes Baileys (NOWEB) safer or more detectable:**

- Baileys speaks the WhatsApp Web multi-device WebSocket protocol directly, with **no browser / no Puppeteer**, so its host footprint is smaller than headless-Chrome clients (whatsapp-web.js, wppconnect). This is a footprint advantage, not invisibility. **[DOCUMENTED]** [Baileys README](https://github.com/WhiskeySockets/Baileys/blob/master/README.md)
- The strongest *documented* detection vector found is the **463 "Reach-out Time-lock" error**. Baileys maintainers' investigation concludes: "The server on WhatsApp's end is counting any outgoing `<message>` or `<call>` with missing privacy fields (tc/cstokens) as 'reaching out' and is enforcing time-based limits for those actions." That is, older/stock Baileys builds that omit `<tctoken>`/`<cstoken>` privacy fields get rate-limited *harder* by WhatsApp. The fix is client-side token support (PRs #2257/#2339/#2438). Practical takeaway: **run a current Baileys/Evolution build** so these tokens are populated. **[DOCUMENTED]** [Baileys issue #2441](https://github.com/WhiskeySockets/Baileys/issues/2441)
- Reconnection is a known danger window: sending at full rate immediately after a reconnect can trip rate-limit alarms (community observation on the same class of issue). **[COMMUNITY]** [Baileys issue #2441](https://github.com/WhiskeySockets/Baileys/issues/2441)

**Evolution API controls that exist for camouflage (the important part):**

Evolution exposes the three human-like levers Baileys provides, as first-class HTTP parameters:

- **Per-message send delay + typing presence, inline on send.** `POST /message/sendText/{instance}` accepts a delay (milliseconds) and a `presence` value (`composing`) so the message shows a typing indicator before it lands. Example body: `{ "number": "...", "text": "...", "delay": 1200, "presence": "composing" }` (v2 flattens these fields; v1 nested them under an `options` object as `{ "delay": 123, "presence": "composing" }`). **[DOCUMENTED]** [Evolution API - Send Plain Text](https://doc.evolution-api.com/v1/api-reference/message-controller/send-text)
- **Standalone presence endpoint:** `POST /chat/sendPresence/{instance}` with `{ "number": "...", "delay": 1200, "presence": "composing" }`. `presence` accepts `composing`, `recording`, `paused` (Baileys also defines `available` / `unavailable`); `delay` is how long (ms) the indicator is shown. Open question in the tracker: whether `delay: 0` holds typing indefinitely or how to cancel it - not officially answered. **[DOCUMENTED]** [Evolution sendPresence issue #1639](https://github.com/EvolutionAPI/evolution-api/issues/1639)
- **Read receipts:** Evolution exposes a mark-as-read chat endpoint (`/chat/markMessageAsRead`), corrected in a release to cover regular, business, broadcast, and group message keys. Underneath, Baileys `readMessages` requires explicit per-message keys: "A set of message keys must be explicitly marked read now. You cannot mark an entire 'chat' read as it were with Baileys Web." **[DOCUMENTED]** [Evolution releases](https://github.com/EvolutionAPI/evolution-api/releases), [Baileys README](https://github.com/WhiskeySockets/Baileys/blob/master/README.md)
- **Presence semantics from Baileys:** `sendPresenceUpdate` "lets the person/group with `jid` know whether you're online, offline, typing etc." and "the presence expires after about 10 seconds" - so typing/online must be **refreshed** for longer simulated typing. Also: "If a desktop client is active, WA doesn't send push notifications... mark your Baileys client offline using `sock.sendPresenceUpdate('unavailable')`." **[DOCUMENTED]** [Baileys README](https://github.com/WhiskeySockets/Baileys/blob/master/README.md)

---

## 2. Number strategy & warm-up (fresh dedicated SIM)

WhatsApp publishes no warm-up spec. The following are third-party vendor/community heuristics and disagree with each other - treat as ranges, not rules.

- **Warm-up length:** community and warm-up-vendor guidance clusters around **~10 days minimum** before meaningful automation, with a number considered "trusted" only after **~25-30 days** of no suspicious activity. **[COMMUNITY / vendor]** [Warmer/Wadesk](https://warmer.wadesk.io/blog/whatsapp-account-warm-up), [Whapi.cloud warm-up](https://support.whapi.cloud/help-desk/blocking/warming-up-new-phone-numbers-for-whatsapp-api)
- **Early ramp shape (vendor example):** days 2-4 receive incoming messages (~1 msg / 2 h), start replying on ~day 4, then grow from ~12 to ~100 messages over the first 7 days. **[COMMUNITY / vendor]** [GREEN-API warm-up](https://green-api.com/en/docs/faq/warming-up-whatsapp-number/)
- **baileys-antiban ramp preset** (a Baileys middleware, stress-tested to 1000 messages on a real number with no ban) uses a 7-day exponential ramp: **Day 1: 20, Day 2: 36, Day 3: 65, Day 4: 117, Day 5: 210, Day 6: 378, Day 7: 680** messages, unrestricted after day 7; **re-enters warm-up if idle 72+ hours.** **[COMMUNITY]** [baileys-antiban](https://github.com/kobie3717/baileys-antiban), [discussion #2357](https://github.com/WhiskeySockets/Baileys/discussions/2357)
- **What triggers early bans (consensus):** a brand-new number that immediately sends high volume - especially to people who have not messaged first - is the top ban trigger; also completing profile (name, photo) and having real 2-way conversations first lowers risk. **[COMMUNITY / vendor]** [GREEN-API protect-from-ban](https://green-api.com/en/docs/faq/how-to-protect-number-from-ban/)
- **Trade-off for Rebe:** the SIM should be verified on the **official** app, have a normal profile/photo, exchange some genuine 1-to-1 messages, and only *then* be paired to Evolution and slowly start posting in the target group. Do not link a zero-history SIM straight into automation on day one.

---

## 3. Rate & timing (numbers people actually cite)

No official per-number posting rate exists. Cited community/middleware defaults:

- **Volume ceilings (folklore):** a single number's practical automated ceiling is often cited at **~1000-2000 messages/day**; the baileys-antiban defaults are **maxPerMinute 8, maxPerHour 200, maxPerDay 1500** (with conservative/moderate/aggressive presets). **[COMMUNITY]** [baileys-antiban](https://github.com/kobie3717/baileys-antiban)
- **Inter-message delay:** baileys-antiban uses **minDelayMs 1500 - maxDelayMs 5000** between messages, plus **newChatDelayMs 3000** extra when starting a new conversation, and **Gaussian jitter** (delays cluster mid-range instead of uniform random). **[COMMUNITY]** [baileys-antiban](https://github.com/kobie3717/baileys-antiban)
- **Typing-duration model:** ~**30 ms per character**, or a **45 WPM (+/- 15 stdDev)** typing model, with mid-typing pauses (~8% chance per 10 chars, 800-3500 ms each). Note Baileys presence expires ~10 s, so long typing must be re-sent. **[COMMUNITY]** [baileys-antiban](https://github.com/kobie3717/baileys-antiban) / **[DOCUMENTED]** [Baileys README](https://github.com/WhiskeySockets/Baileys/blob/master/README.md)
- **Active-hours / circadian pattern:** slow the bot **4-6x** in the 02:00-06:00 window and peak during business hours, with smooth transitions - i.e. do not post at a flat rate around the clock. **[COMMUNITY]** [baileys-antiban](https://github.com/kobie3717/baileys-antiban)
- **Avoid bursts:** allow a small human-like burst (first ~3 messages faster) then throttle; never fire a batch instantly, especially right after reconnect. **[COMMUNITY]** [baileys-antiban](https://github.com/kobie3717/baileys-antiban), [Baileys issue #2441](https://github.com/WhiskeySockets/Baileys/issues/2441)

For a news bot posting a handful of items a day plus light replies, real volume is far under any of these ceilings - the timing/jitter/typing camouflage matters more than the raw cap.

---

## 4. Behavioral camouflage (map to Evolution endpoints)

- **Typing before every send:** set `delay` + `presence: "composing"` on `sendText`, or call `/chat/sendPresence` first; scale delay to message length (~30 ms/char) with jitter, not a constant. **[DOCUMENTED]** [Send Plain Text](https://doc.evolution-api.com/v1/api-reference/message-controller/send-text), [issue #1639](https://github.com/EvolutionAPI/evolution-api/issues/1639)
- **Read what you reply to:** call `/chat/markMessageAsRead` on incoming group messages before replying, so read receipts precede responses like a human. **[DOCUMENTED]** [Evolution releases](https://github.com/EvolutionAPI/evolution-api/releases)
- **Presence hygiene:** refresh presence (it expires ~10 s) and consider going `unavailable` when idle. **[DOCUMENTED]** [Baileys README](https://github.com/WhiskeySockets/Baileys/blob/master/README.md)
- **Don't repeat identical content:** vary wording/order; identical repeated messages are a classic bulk-sender fingerprint (community). **[COMMUNITY]** [GREEN-API protect-from-ban](https://green-api.com/en/docs/faq/how-to-protect-number-from-ban/)
- **Randomize everything time-based:** Gaussian jitter on delays, circadian slowdown overnight, no perfectly periodic posting. **[COMMUNITY]** [baileys-antiban](https://github.com/kobie3717/baileys-antiban)

---

## 5. Recovery / session resilience

- **Session drops happen and can log out all devices** on a bad update - persist auth state properly and expect re-pairing. Baileys' `useMultiFileAuthState` is explicitly "not for prod." **[DOCUMENTED]** [Baileys README](https://github.com/WhiskeySockets/Baileys/blob/master/README.md), [baileys.wiki](https://baileys.wiki/docs/intro)
- **Reconnect carefully:** after a reconnection, ramp back up gradually rather than resuming full rate (avoids re-tripping the 463 reach-out limit). baileys-antiban re-enters warm-up after 72 h idle for the same reason. **[DOCUMENTED + COMMUNITY]** [Baileys issue #2441](https://github.com/WhiskeySockets/Baileys/issues/2441), [baileys-antiban](https://github.com/kobie3717/baileys-antiban)
- **What a flagged number looks like:** 463 rate-limit errors / "reaching out" time-locks on sends and calls, or a temporary ban ("this account can no longer use WhatsApp"), escalating to a permanent ban with no appeal. WhatsApp gives no ban-reason detail. **[DOCUMENTED]** [Baileys issue #2441](https://github.com/WhiskeySockets/Baileys/issues/2441), [WhatsApp FAQ](https://faq.whatsapp.com/general/security-and-privacy/about-unsupported-or-unauthorized-versions-of-whatsapp)
- **Backup-number swap:** keep the standby SIM **warmed in parallel** (same 10-30 day process, low activity) and already a member of the target group, so a swap does not start from a cold, high-risk number. Evolution is multi-instance, so the backup can live as a second instance on the same server. **[COMMUNITY]** [GREEN-API warm-up](https://green-api.com/en/docs/faq/warming-up-whatsapp-number/) / **[DOCUMENTED, multi-instance]** [Evolution API GitHub](https://github.com/EvolutionAPI/evolution-api)
- **Health monitoring:** baileys-antiban ships health checks to catch degradation before a ban; the general principle (watch error codes, back off on 4xx/463) applies regardless of middleware. **[COMMUNITY]** [baileys-antiban](https://github.com/kobie3717/baileys-antiban)

---

## Bottom line

- WhatsApp publishes **no** ban thresholds; every rate/warm-up number here except the official broadcast (~30/mo) and unverified-Business (250/day) limits is **empirical folklore**, not a guarantee.
- Evolution API **does** expose the three camouflage levers as documented HTTP params: **`delay` + `presence: "composing"` on `/message/sendText`**, standalone **`/chat/sendPresence`**, and **`/chat/markMessageAsRead`**. Use all three.
- Baileys presence **expires ~10 s** (refresh it); read receipts need **explicit per-message keys**; run a **current build** so tc/cs privacy tokens are sent (avoids the harsher 463 "reach-out" rate-limit).
- Warm the fresh SIM on the **official app** first (~10 days min, ~25-30 to "trusted"), ramp volume gradually, keep the **backup SIM warmed in parallel** as a second Evolution instance.
- Rebe's actual load (a few posts/day + light replies into an already-joined group) is far under every cited ceiling - **timing realism (jitter, typing, circadian, no identical repeats) matters more than raw volume.**
- Biggest documented risk levers: cold-number high-volume start, "reaching out" to non-contacts, bursty sends right after reconnect, and simply being an unofficial client (permanent ban, no appeal).

*Present evidence only - no final policy numbers are decided here.*
