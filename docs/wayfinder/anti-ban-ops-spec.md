# Anti-Ban Operational Playbook - Spec

Wayfinder ticket: [Design the anti-ban operational playbook](https://github.com/ivzc07/bienwabot/issues/8).
Decides how "Rebe" keeps the WhatsApp number alive and feels human at the transport level.

Grounded in [anti-ban-research.md](./anti-ban-research.md) (every rate/warm-up number there is empirical folklore, not an official WhatsApp figure - WhatsApp publishes no ban thresholds).
Transport is **Evolution API** (Baileys-based; Python talks HTTP + webhook), per [transport #3](https://github.com/ivzc07/bienwabot/issues/3).

Decided posture (human calls, this session):

- **Risk posture: Balanced** - human-like and present, never bursty; real load sits far under every cited ceiling, so timing realism matters more than raw volume.
- **Number: dedicated new physical SIM**, warmed on the official app first.
- **Recovery: warmed backup number** on standby as a second Evolution instance.

> **Scope boundary.** This spec owns the **safe envelope** (ceilings, warm-up, pacing, quiet hours, recovery) and the **pacing** of every message the [reply policy #7](https://github.com/ivzc07/bienwabot/issues/7) authorizes.
> It does **not** pick the exact number of news posts/day or their schedule - that graduates into its own [posting cadence & timing](https://github.com/ivzc07/bienwabot/issues/2) ticket, which must live inside this envelope.

---

## 1. Number strategy & warm-up

**The SIM.**
A dedicated new physical SIM, used only for Rebe.
Verify it on the **official WhatsApp app first** - never link a zero-history number straight into Evolution on day one (the top documented early-ban trigger is a brand-new number sending volume immediately).

**Pre-automation warm-up (~14 days, balanced).**
Research clusters warm-up at ~10 days minimum and ~25-30 days to "trusted"; balanced picks ~14 days before any automation.

1. Complete a real profile: Rebe's display name, a consistent photo, an "about" line - matching the [persona spec #6](https://github.com/ivzc07/bienwabot/issues/6).
2. Days 1-4: exchange genuine 1-to-1 messages with a few real contacts (receive first, then reply). No group posting.
3. Days 5-14: join the target bien.mx group as a normal member and chat lightly **by hand** - a few human messages, not automated.
4. Only after this: pair the SIM to Evolution API and begin automated posting on a ramp.

**Post-pairing ramp (first 2 weeks of automation).**
Rebe's real volume (a handful of posts + 2-3 replies/day) is already far below any folklore ramp ceiling, so the ramp is mostly "don't front-load."

- Week 1: cap **3 news posts/day**, replies as normal.
- Week 2: cap **5 news posts/day**.
- After 2 clean weeks: steady state, bounded by the envelope in section 2.
- **Re-enter the ramp after any 72h+ idle gap or a reconnect** (a cold resume at full rate is a known rate-limit trigger).

---

## 2. Rate & timing envelope (Balanced)

Hard ceilings, set well under the cited folklore ranges (~1000-2000/day). These are **safety caps, not the target cadence** - the cadence ticket picks the actual number inside them.

| Lever | Balanced cap | Note |
|---|---|---|
| News posts / day | **12 hard ceiling** | Actual cadence chosen later, expected ~4-8. |
| News posts / hour | **3** | Never batch. |
| Unprompted reply chime-ins / day | **2-3** | Already fixed by [reply policy #7](https://github.com/ivzc07/bienwabot/issues/7); counts here too. |
| Addressed replies | no fixed cap | But still paced (below); a human does not fire 10 in a row. |
| Any send / minute | **≤ 4** | Absolute floor against bursts. |

**Per-message pacing (applies to every send - post or reply):**

- **Typing indicator before every send.** Use Evolution `POST /message/sendText/{instance}` with `presence: "composing"` and a `delay`, or call `POST /chat/sendPresence/{instance}` first.
- **Delay scaled to message length:** ~**30 ms/character** (a ~45 WPM typist), with **Gaussian jitter**, clamped to **1500-5000 ms**. Never a constant delay.
- **Extra ~3000 ms** before the first message into a quiet thread (new-conversation delay).
- **Refresh presence every ~8-10 s** for longer "typing" - Baileys presence expires ~10 s.
- **Never bursty / never twice back-to-back** (the [reply policy #7](https://github.com/ivzc07/bienwabot/issues/7) floor); allow at most a small human 2-3 message opening burst, then throttle.

**Quiet hours / circadian shape (America/Mexico_City):**

- **02:00-06:00: near-silent** - slow 4-6x; effectively no scheduled news posts, replies only if directly addressed.
- Peak in normal waking/business + evening hours.
- Add jitter to post times - **never a perfectly periodic schedule** (a flat round-the-clock cadence is a robotic fingerprint).

---

## 3. Behavioral camouflage (mapped to Evolution endpoints)

- **Type before you send:** `presence: "composing"` + scaled `delay` on `sendText`, or `/chat/sendPresence` first. [Send Plain Text](https://doc.evolution-api.com/v1/api-reference/message-controller/send-text)
- **Read before you reply:** call `/chat/markMessageAsRead` on the incoming group message(s) before responding, so a read receipt precedes the reply like a human. Baileys needs explicit per-message keys.
- **Presence hygiene:** refresh presence while "typing"; send `unavailable` when idle so behavior is not permanently-online.
- **No identical repeats:** never post the same wording twice - varied phrasing is already guaranteed by the [news pipeline dedup #5](https://github.com/ivzc07/bienwabot/issues/5) and [persona voice #6](https://github.com/ivzc07/bienwabot/issues/6); this is the anti-ban reason to keep it that way.
- **Randomize all timing:** Gaussian jitter on delays, circadian slowdown overnight, jittered post times.
- **Run a current Evolution/Baileys build** so `tc`/`cs` privacy tokens are populated - stale builds get the harsher **463 "reach-out" rate-limit**.

---

## 4. Recovery & resilience (backup number on standby)

**Persist auth properly.**
Use Evolution's database-backed instance store, not Baileys' `useMultiFileAuthState` ("not for prod").

**Session drop -> auto-reconnect, then ramp.**
On reconnect, **do not resume at full rate** - re-enter the section-1 ramp briefly (a cold resume at full rate can trip the 463 reach-out limit). Evolution's webhook/connection events drive this.

**Detecting a flagged number.**
Watch send responses for **463 "reach-out time-lock"**, `4xx` rate errors, temporary-ban / logout events. On any of these: **stop sending, back off, alert the maintainer** (do not keep hammering).

**Backup SIM (warmed, parallel).**
A second dedicated SIM, warmed on the **same ~14-day process at low activity**, already a member of the target group, running as a **second Evolution instance** on the same server (Evolution is multi-instance).

- If the primary is temp-banned: pause, wait it out, resume on the ramp.
- If the primary is **permanently banned** (no appeal): swap posting to the backup instance; begin warming a fresh replacement SIM as the new backup.
- The backup is never posting simultaneously - it is a warm standby, so a swap never starts from a cold number.

**Health monitoring.**
Track error codes and connection state; back off on `4xx`/`463`; surface liveness to the maintainer.
(The concrete alerting/observability wiring belongs to [deployment architecture #9](https://github.com/ivzc07/bienwabot/issues/9).)

---

## 5. Pre-launch operational checklist (execution prerequisites)

Planning-only spec; these are the **tasks a builder runs before go-live**, not done here:

- [ ] Buy the primary dedicated SIM; verify on official WhatsApp; set Rebe profile + photo.
- [ ] Run the ~14-day warm-up (1-to-1 messages, then manual group chat).
- [ ] Buy + warm the backup SIM in parallel; pre-join it to the group.
- [ ] Confirm Evolution API is on a current build (tc/cs token support).
- [ ] Pair primary to Evolution; start the 2-week post-pairing ramp.

---

## Bottom line

- **Balanced** posture: present and human, hard-capped **12 posts/day / 3 per hour**, near-silent 02:00-06:00 Mexico time, every ceiling far under folklore limits.
- **Warm the dedicated SIM ~14 days on the official app first**, complete a real profile, then pair to Evolution and ramp over 2 weeks; re-ramp after any 72h idle or reconnect.
- **Camouflage is three documented Evolution levers** - `composing` presence + scaled `delay` on send, `markMessageAsRead` before replies, presence refresh/`unavailable` - plus Gaussian-jittered timing and no identical repeats.
- **Recovery** is a warmed backup SIM as a second Evolution instance, auto-reconnect-then-ramp, and back-off-and-alert on 463/4xx/ban signals.
- **Timing realism, not volume, is the real defense** - Rebe's true load is tiny; the risk is looking robotic, not sending too much.
- **Handoff:** the exact posts/day and schedule are decided by the newly-graduated posting-cadence ticket, inside this envelope.
