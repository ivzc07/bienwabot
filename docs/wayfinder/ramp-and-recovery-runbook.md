# Ramp and Recovery - Runbook

What Rebe does on her own when WhatsApp pushes back, and the one thing she never does without a human.

Implements section 1 (post-pairing ramp) and section 4 (recovery and resilience) of [anti-ban-ops-spec.md](./anti-ban-ops-spec.md), the ramp clamp in section 1 of [posting-cadence-spec.md](./posting-cadence-spec.md), and the failure table in section 5 of [deployment-architecture-spec.md](./deployment-architecture-spec.md).
The code is `rebe_agent/ramp.py`, read by `rebe_agent/pacer.py` and moved by `rebe_agent/signals.py`.

---

## 1. The post-pairing ramp

For the first two weeks of automation the day is clamped, whatever the cadence plan drew.

| Week of the ramp | News posts per day | Replies |
|---|---|---|
| Week 1 | **3** | as normal |
| Week 2 | **4** | as normal |
| After two clean weeks | the cadence spec's steady state, unclamped | as normal |

The playbook's own week-two cap is five.
Section 1 of the cadence spec puts the target at four and says the ramp cap of five is not binding, so four is the number that actually governs and four is what the code enforces.

The clamp is on **news posts**, counted per local day in `America/Mexico_City`, and it covers breaking-news overrides as well as drawn slots.
It is read in the pacer, which is the one place a message leaves through, so there is no path into the day that can miss it.
A slot the clamp refuses is dropped rather than deferred: the day's shape is the point, and a post that arrives two hours late is a machine catching up.

**The ramp start is persisted** in the `ramp` table of the `rebe` database, so a restart neither resets nor skips it.
It is stamped the first time anything reads it, which in practice is the first boot after the number was paired.
The boot log says when it started and what today allows:

```
the ramp started 2026-07-26T09:12:04-06:00 (paired); today allows 3 news posts
```

## 2. Re-entering the ramp

Either of these puts Rebe back on the week-one clamp:

- **An idle gap of 72 hours or more.** Measured against the send log, so it is a statement about the number rather than about this process. With no sends at all it is measured from the ramp start, because a number that has never sent anything is the coldest one there is. The clamp holds for the whole silence, so the ramp effectively starts when she starts talking again and runs its full two weeks from there.
- **A reconnect.** Evolution's `connection.update` reporting the link open again, after it was seen to go down. A link hold that lapses without an `open` ever arriving counts as one too: every way back from a disconnect is a cold resume, and none of them resumes at the old rate.

A cold resume at full rate is a documented way to trip the 463 reach-out time-lock, which is why neither is followed by business as usual.

An `open` that did not follow a disconnect changes nothing.
Evolution announces an open link at every boot, and treating that as a reconnect would put a redeployed agent back on week one every time it started.

## 3. Backing off

| Signal | What Rebe does | What you do |
|---|---|---|
| 463 reach-out time-lock, or a 429 | Stops sending for **one hour**, alerts the ops chat. The failed send is not retried. A second push-back restarts the hour. | Nothing, unless it repeats. If it does, pause her from the ops chat for a few hours. |
| Any other 4xx or 5xx from Evolution | Alerts the ops chat. The send is dropped, not retried. | Check that `bien-evo` is up and still paired. |
| `connection.update` = disconnected | Stops sending for up to **30 minutes**, alerts the ops chat. Each repeated disconnect extends the hold. Coming back, announced or not, re-enters the ramp. | Nothing. Evolution reconnects on its own; on reconnect she resumes under the week-one clamp. If it does not come back, the number needs re-pairing (one QR scan). |
| Disconnect with reason 401 | Reads as a temporary ban or an unlinked device. Stops sending **and** flips the soft pause. | Check the number on the phone, wait it out, then `/reanuda` from the ops chat. She comes back on the week-one clamp, because the link hold lapsed into a re-entry while you waited. **Do not swap to the backup number for this.** |
| Disconnect with reason 403 | Reads as a permanent ban. Stops sending and flips the soft pause. | Section 4 below. |

**The heartbeat keeps flowing throughout.**
That distinction is deliberate and it is what the ops chat is for: the agent is alive, the number is not sending.
A silent Kuma monitor means the process is down or wedged; a disconnect alert with a green monitor means the process is fine and WhatsApp is not.

Nothing is queued while sending is stopped.
A held send is dropped, so coming back does not fire a backlog at the group, which would be the exact burst the whole envelope exists to prevent.

## 4. Manual failover to the backup instance

**There is no automatic swap to the backup number, ever.**
Auto-switching on a possibly-false ban signal would burn the only warm standby and leave the bot cold with no backup.
A human confirms the number is really dead, points the agent at the backup instance, and redeploys.

Which instance is live is **configuration, not code**: `EVOLUTION_INSTANCE` in `rebe_agent/config.py`, read once in `build_client` and nowhere else.
Nothing in the agent writes it, and `Settings` is frozen, so no signal, alert or ramp can change which number Rebe posts from.

### The procedure

1. **Confirm the ban is real.**
   Open WhatsApp on the primary SIM's phone.
   A permanent ban says so; a temporary one says how long.
   A 403 from Baileys is the best reading available, not a verdict, since WhatsApp publishes no ban-reason detail.
2. **If it is temporary:** leave the instance alone.
   Wait it out, then `/reanuda` from the ops chat.
   She comes back on the week-one clamp.
3. **If it is permanent:** in Coolify, on the `rebe-agent` application, set

   ```
   EVOLUTION_INSTANCE=bien-backup
   ```

   and redeploy.
   The backup SIM is already warmed, already a member of the group, and already running as a second Evolution instance on the same server, so the swap never starts from a cold number.
4. **Check the boot line** in the container logs to confirm which number is live:

   ```
   rebe-agent 0.1.0 starting | evolution_instance=bien-backup ...
   ```

5. **Resume her** with `/reanuda` from the ops chat.
   The ban flipped the soft pause, and it stays flipped until a human says otherwise.
   The ramp is per deployment rather than per number, so if you want the new number to start quiet as well, delete the row first:

   ```sql
   DELETE FROM ramp;
   ```

   The next read stamps a fresh ramp, and she starts at three posts a day.
6. **Start warming a replacement SIM** as the new backup, on the same ~14-day process at low activity, and pre-join it to the group.
   Until that is done there is no standby left.

### What not to do

- Do not point both instances at the group at once. The backup is a warm standby; two numbers posting is two bots.
- Do not swap on a 463, a 429 or a plain disconnect. Those are back-offs, and they clear on their own.
- Do not clear the soft pause without checking the phone. The pause after a ban signal is the only thing stopping a banned number from carrying on.
