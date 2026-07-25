# Posting Cadence & Timing - Spec

Wayfinder ticket: [Decide posting cadence & timing](https://github.com/ivzc07/bienwabot/issues/11).
Decides how often "Rebe" posts AI news, when in the day she posts, and how those times are randomized.

This spec sits **inside** the safety envelope set by [anti-ban ops #8](https://github.com/ivzc07/bienwabot/issues/8).
That spec owns the ceilings; this one picks the actual numbers under them.
All times are **America/Mexico_City**.

Decided posture (human calls, this session):

- **Volume: ~4 news posts/day on weekdays**, drifting 3-5, supply-capped.
- **Shape: four loose windows** across the waking day, two on weekends.
- **Important news is never held back by the quota** - it posts on top of the plan, bounded only by the anti-ban envelope.
- **Nothing is scheduled overnight.** Sleep is the strongest human signal available.

---

## 1. Volume

| Lever | Value | Source |
|---|---|---|
| Weekday soft target | **~4 posts/day** (drifts **3-5**) | this spec |
| Weekend soft target | **~2 posts/day** | this spec |
| Practical hard stop | **8 posts/day** | this spec, under the #8 ceiling |
| Absolute ceiling | 12/day, 3/hour, ≤4 sends/min | [#8](https://github.com/ivzc07/bienwabot/issues/8) |
| Minimum gap between posts | **75-90 min** (jittered) | this spec |
| Reply chime-ins | 2-3/day, counted separately | [#7](https://github.com/ivzc07/bienwabot/issues/7) |

**The target is soft in both directions.**
It is a target, not a quota, and not a cap.

- **Downward:** if the curated pool from [#5](https://github.com/ivzc07/bienwabot/issues/5) has nothing that clears the quality bar for a window, the window is **skipped silently**.
  Rebe never posts filler to hit a number.
  HN plus first-party AI RSS realistically yields only ~4-6 items a day that a general Mexican audience finds interesting, so a rigid quota would guarantee weak posts on thin days.
- **Upward:** genuinely important news posts regardless of the target (see section 4).

**Why the count drifts rather than sitting at exactly 4.**
A number that never moves is a machine signature.
The drift is a natural consequence of the two soft edges above (skipped weak windows, added important items), so it does not need its own random draw.

**Ramp interaction.**
During the [#8](https://github.com/ivzc07/bienwabot/issues/8) post-pairing ramp the target is clamped by the ramp cap, whichever is lower:

- Week 1 of automation: target **3/day**.
- Week 2: target 4/day (the ramp cap of 5 is not binding).
- After two clean weeks: steady state as specified here.
- Re-entering the ramp after a 72h idle gap or a reconnect re-applies the week-1 clamp.

---

## 2. Daily shape - the windows

One post lands somewhere inside each window.
Windows are wide, so the exact minute never repeats, and they map onto the moments a person actually picks up their phone.

**Weekday (Mon-Fri):**

| Window | Range | Feel |
|---|---|---|
| Morning | **08:00-10:30** | coffee, start of the day |
| Midday | **13:00-15:00** | lunch |
| Evening | **18:00-20:00** | after work, group is most alive |
| Late | **21:30-23:00** | winding down before bed |

**Weekend (Sat-Sun):**

The morning window is **dropped** and the rest shift roughly two hours later.

| Window | Range |
|---|---|
| Midday | **14:00-16:30** |
| Evening | **19:30-22:00** |

**Why weekends differ.**
Two independent reasons point the same way.
Real people wake and post later on weekends, and AI news supply genuinely dries up - companies do not announce on Saturdays and HN slows down.
A bot holding a perfect Tuesday rhythm through Sunday morning is a clear tell.

**Overnight: nothing is ever scheduled between 23:00 and 08:00.**
This is stricter than the #8 near-silent band of 02:00-06:00, and deliberately so.
Directly-addressed replies may still fire overnight per [#7](https://github.com/ivzc07/bienwabot/issues/7), paced as usual.

---

## 3. Jitter model - roll the whole day at dawn

**Once per day at ~06:00**, a scheduler job draws the entire day's plan:

1. **Pick the day's window set** from the weekday/weekend table above.
2. **Draw one time per window:** a Gaussian centred on the window midpoint, with `sigma = window_width / 5`, **clipped to the window edges**.
   The central tendency is the point - it gives Rebe habits, rather than a flat "equally likely at 08:00 or 10:29" distribution.
3. **Enforce the global minimum gap** of 75-90 min (itself jittered) between consecutive planned times.
   Redraw the offending time until the gap holds, or drop the later slot after a few failed attempts.
4. **Register the resulting times** as one-shot APScheduler jobs, per [#5](https://github.com/ivzc07/bienwabot/issues/5) and [#9](https://github.com/ivzc07/bienwabot/issues/9).

**Why the whole day at once, rather than drawing each post when its window opens.**
Global spacing is only enforceable with a global view.
Independent per-window draws cannot see each other and will eventually place two posts ten minutes apart across a window boundary, which is exactly the burst the envelope forbids.

**Never a fixed offset, never a cron expression for the post itself.**
The only fixed-time job in the system is the 06:00 roll.

---

## 4. Important news overrides the plan

Two tiers of item.

**Normal tier** fills the rolled plan and nothing more.

**High tier** bypasses the daily target entirely.
An item is high tier when it clears a high importance bar:

- top-of-HN by points (well above the ranker's normal floor from [#5](https://github.com/ivzc07/bienwabot/issues/5)), **or**
- a first-party announcement from a major AI org - a model or product launch, not commentary about one.

**How a high-tier item posts:**

- It posts **as soon as pacing allows** - subject to the min-gap, the 3/hour cap, the chat-collision rule in section 5, and the overnight hold in section 6.
- It is **additive**: the day goes from 4 posts to 5, not 4 with something displaced.
  Delaying real news to the next window is the restriction this rule exists to avoid.
- After it posts, any remaining **normal-tier** slot whose best available candidate is weak is **pruned**.
  A person who just shared the big thing does not also drop a mediocre link at 22:00.
  High-tier slots are never pruned.
- The practical stop is **8 posts/day**; the absolute stop is the #8 envelope.
  The ceiling exists to catch a runaway loop, not to shape a normal day.

---

## 5. Collision with live conversation

A scheduled post that comes due while Rebe is mid-conversation is **deferred by 10-20 minutes (jittered) after her last message of any kind** - post or reply.

The shared anti-ban pacer from [#9](https://github.com/ivzc07/bienwabot/issues/9) owns this, since it already sees both the news leg and the webhook leg of the process.

**Why.**
Dropping a news link seconds after answering someone reads as two programs running side by side, because it is.
A person finishes the conversation and shares the link a bit later.

If the deferral pushes a post past the end of its window, it may still post up to ~30 min past the window edge; beyond that the slot is dropped.

---

## 6. Overnight breaking news

Important news that breaks between 23:00 and 08:00 is **queued, not posted**.
It goes out when the morning window opens, jittered as usual, **ahead of everything else in the queue**.

**Why.**
A person who reliably posts AI links at 03:00 is not a person, and a single such post is enough to give the game away to anyone scrolling back through group history.
The item is a few hours old by morning, which nobody in a Mexican WhatsApp group will notice or care about.

If more than one high-tier item queues overnight, only the strongest takes the morning slot; the rest fall back to normal-tier and compete for later windows.

---

## 7. Worked example - a normal Wednesday

```
06:00  roll the day -> 4 windows, times drawn:
       09:14, 13:47, 19:02, 22:19   (min gap ok)

09:14  post 1  (normal tier)
13:47  post 2  (normal tier)
16:30  a major model launch lands -> high tier
16:41  post 3  (override, +1 on the day; min gap from 13:47 ok)
19:02  post 4  (normal tier)
       19:00 someone asks Rebe a question, she replies 19:05
       -> the 22:19 slot's best candidate is weak, and the day
          already ran long -> pruned
       day ends at 4 posts, one of them off-schedule
```

---

## 8. Bottom line

Rebe posts roughly four AI links on a weekday and two on a weekend, spread across four loose windows that follow a real person's day, with every time drawn fresh each dawn from a clipped Gaussian and spaced at least 75-90 minutes apart.
She stays quiet from 23:00 to 08:00.
When something genuinely big happens she posts it anyway, on top of the plan, and then skips whatever weak link she would otherwise have shared later.
Nothing here comes close to the anti-ban ceilings; the ceilings only exist to catch a bug.

---

## 9. What this unblocks

**Cost / token budget.**
The fog item on the [map](https://github.com/ivzc07/bienwabot/issues/2) reduces to a number now.
Steady state is ~4-5 posts/day on weekdays and ~2 on weekends, so roughly **28-30 DeepSeek summarization calls per week**, each a few hundred tokens in and a few hundred out.
Add the reply gate (2-3 chime-ins/day plus addressed replies) and the optional borderline-relevance calls from [#5](https://github.com/ivzc07/bienwabot/issues/5).
At DeepSeek's pricing this is a rounding error, and the ranker's free filters mean the pool size does not drive cost.
