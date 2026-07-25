# DeepSeek Token Budget & Spend Guard - Spec

Wayfinder ticket: [Estimate the DeepSeek token budget & spend guard](https://github.com/ivzc07/bienwabot/issues/12).
Turns Rebe's LLM usage into a monthly dollar figure and decides whether the bot needs a spend guard.

Pricing checked against the primary source on 2026-07-25: [DeepSeek Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing).
Volumes come from the [cadence spec (#11)](./posting-cadence-spec.md), call shapes from the [news pipeline (#5)](./news-pipeline-research.md) and the [reply policy (#7)](./reply-policy-spec.md), alerting from the [deployment architecture (#9)](./deployment-architecture-spec.md).

**Bottom line: about $0.22/month, under $1.10/month even for a very chatty group.
No hard spend cap. Add a daily call-count alert and kill-switch instead, because the real risk is a runaway loop, not price.**

---

## 1. Current DeepSeek pricing

`deepseek-v4-flash`, the non-thinking chat model chosen in [#4](https://github.com/ivzc07/bienwabot/issues/4), per 1M tokens:

| Item | Price |
|---|---|
| Input, cache hit | **$0.0028** |
| Input, cache miss | **$0.14** |
| Output | **$0.28** |

For comparison, `deepseek-v4-pro` is $0.003625 / $0.435 / $0.87, roughly 3.1x flash.

**Two model-ID facts that change the build, not just the cost.**

1. `deepseek-chat` and `deepseek-reasoner` were **deprecated on 2026/07/24 15:59 UTC**, one day before this ticket was resolved.
The replacement IDs are `deepseek-v4-flash` and `deepseek-v4-pro`.
2. On V4 the **thinking mode defaults to `enabled`**.
Non-thinking must now be requested explicitly: `extra_body={"thinking": {"type": "disabled"}}` in the OpenAI SDK ([Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode)).
Leaving the default on is not primarily a cost problem, it is a correctness problem: thinking mode silently ignores `temperature`, `top_p`, `presence_penalty`, and `frequency_penalty`, which are exactly the knobs the persona's voice variability depends on, and it bills chain-of-thought as output tokens (roughly a 10x output blow-up per call).

**Off-peak discounting: do not assume it.**
The official pricing page carries a single flat USD rate with no off-peak table.
The historical 50-75% off-peak window (16:30-00:30 UTC) applied to the older V3/R1 generation and is gone from the current page.
Community reports describe a **peak-hour multiplier** (~2x, Beijing business hours) attached to CNY billing for V4, alongside a flat international USD rate matching the published figures.
This spec therefore budgets at the **flat USD rate** and separately shows a 2x worst case, which changes nothing about the conclusion.

**Context caching is on by default** and needs no code change ([Context Caching](https://api-docs.deepseek.com/guides/kv_cache)).
Cache hits are **50x cheaper** than misses, and every call type here leads with a stable system prompt, so the cache does real work.
It is best-effort and clears after "a few hours to a few days", so the overnight silence from 23:00 to 08:00 will drop some prefixes.
This spec assumes only **80% of the cacheable prefix actually hits**.

---

## 2. Token spend per call

Four call types. Sizes are estimates from the specs each call comes from, rounded up.

| # | Call | Fires on | Input tokens | Output tokens | Stable prefix |
|---|---|---|---|---|---|
| A | **News summarization** ([#5](./news-pipeline-research.md)) | each accepted news item | ~1,000 | ~150 | ~700 |
| B | **Reply-or-ignore gate** ([#7](./reply-policy-spec.md)) | each inbound group message | ~400 | ~30 | ~250 |
| C | **Reply generation** ([#6](./persona-spec.md) + [#7](./reply-policy-spec.md)) | each authorized reply | ~1,000 | ~60 | ~700 |
| D | **Borderline relevance gate** ([#5](./news-pipeline-research.md), optional) | each borderline candidate item | ~400 | ~50 | ~200 |

Where the numbers come from:

- **A input** = persona and style system prompt plus the `NewsPost` JSON schema and its worked example (~700, and JSON mode requires an example in the prompt) plus the item's title, URL, and feed description (~250).
- **A output** = one WhatsApp-short Spanish post wrapped in JSON: framing word, one line, link, optional hashtags.
The [persona spec](./persona-spec.md) caps this at a couple of sentences, so 150 tokens is generous.
- **B input** = the classification rubric (~250) plus the message and two or three recent messages for context (~150).
- **B output** = a small typed verdict object, `{addressed, ai_topic, confidence}`.
- **C input** = the full persona system prompt plus the thread being answered.
- **C output** = one short Spanish message, deliberately WhatsApp-length.
- **D** is a trimmed version of B against an article instead of a chat message.

**Call C is the only one that generates member-visible prose, and B is the only one whose volume the bot does not control.**
That asymmetry is what the rest of this spec is about.

---

## 3. Monthly volume

Steady state, 30 days, after the [#8](./anti-ban-ops-spec.md) ramp.

| # | Volume basis | Calls/day | Calls/month |
|---|---|---|---|
| A | ~4/day weekdays, ~2/day weekends, plus high-tier overrides: ~30/week ([#11](./posting-cadence-spec.md)) | ~4.3 | **~130** |
| B | every inbound group message; **assumed 150 msgs/day** (see sensitivity below) | 150 | **~4,500** |
| C | 2-3 chime-ins/day capped by [#7](./reply-policy-spec.md), plus addressed replies; assume 10/day total | 10 | **~300** |
| D | borderline items surviving the free ranker filters; assume 10/day | 10 | **~300** |

The 150 messages/day figure for B is the one real assumption in this document.
It is not derivable from any prior ticket, because group traffic is a property of the humans, not the bot.
Section 5 shows the answer at 50 and 500 as well.

---

## 4. The arithmetic

**Token totals per month:**

| # | Input tokens | Output tokens |
|---|---|---|
| A | 130 x 1,000 = 130,000 | 130 x 150 = 19,500 |
| B | 4,500 x 400 = 1,800,000 | 4,500 x 30 = 135,000 |
| C | 300 x 1,000 = 300,000 | 300 x 60 = 18,000 |
| D | 300 x 400 = 120,000 | 300 x 50 = 15,000 |
| **Total** | **2,350,000 (2.35M)** | **187,500 (0.19M)** |

**Worst case, pretending the cache never hits:**

```
input   2.350M x $0.14 / 1M  = $0.329
output  0.188M x $0.28 / 1M  = $0.053
                             = $0.38 / month
```

**Realistic, with context caching:**

Cacheable prefix tokens = (130 x 700) + (4,500 x 250) + (300 x 700) + (300 x 200) = 1,486,000.
At the assumed 80% hit rate, 1,188,800 tokens are billed as hits and the remaining 1,161,200 input tokens as misses.

```
cache hits    1.189M x $0.0028 / 1M = $0.003
cache misses  1.161M x $0.14   / 1M = $0.163
output        0.188M x $0.28   / 1M = $0.053
                                    = $0.22 / month
```

**Sanity checks on that figure:**

| Scenario | Monthly |
|---|---|
| Realistic, with caching | **$0.22** |
| No cache hits at all | $0.38 |
| Flat rate doubled by a peak-hour multiplier | $0.76 |
| Same traffic on `deepseek-v4-pro` instead of flash | ~$1.20 |
| Every estimate here wrong by 10x | ~$2.20 |

Annualised, the realistic figure is about **$2.60/year**.

---

## 5. Sensitivity to group traffic

Only call B scales with how chatty the group is.

| Group traffic | B calls/month | Total input | Total output | Monthly (no cache) |
|---|---|---|---|---|
| Quiet, 50 msgs/day | 1,500 | 1.15M | 0.10M | **$0.19** |
| Assumed, 150 msgs/day | 4,500 | 2.35M | 0.19M | **$0.38** |
| Very chatty, 500 msgs/day | 15,000 | 6.55M | 0.50M | **$1.06** |

A twelve-fold difference in group activity moves the bill by about ninety cents.

**This kills an optimisation before it gets designed.**
A free keyword pre-filter in front of call B would cut gate calls by roughly 90%, and it would save about **$0.15/month**.
That is not worth the extra branch, the extra tuning surface, or the risk of a regex quietly swallowing messages Rebe should have chimed in on.
Keep the gate simple: every inbound message goes to the model gate, as [#7](./reply-policy-spec.md) describes.

**The same logic applies upward.**
Cost never has to constrain the model choice here.
If flash ever proves too weak for the Spanish voice, moving the reply-generation call to `deepseek-v4-pro` costs cents, and that decision should be made on output quality alone.

---

## 6. Spend guard: no

**No hard dollar cap, no budget throttle, no cost-based circuit breaker.**

Three reasons:

1. **The DeepSeek account is already a hard ceiling.**
Billing deducts from a topped-up prepaid balance, with no auto-recharge ([Deduction Rules](https://api-docs.deepseek.com/quick_start/pricing)).
A single $10 top-up covers roughly three years at the estimated rate.
The balance running dry is a ceiling that needs no code.
2. **Running dry degrades gracefully.**
A failed DeepSeek call is already specified as **silent** in the [reply policy](./reply-policy-spec.md#failure-posture) and raises a Telegram alert per [#9](./deployment-architecture-spec.md).
Rebe goes quiet, which is in character, and the maintainer hears about it out of band.
3. **A dollar cap does not catch the thing that would actually go wrong.**
The failure mode worth defending against is a **runaway loop**: a webhook replay storm, Rebe classifying her own messages, or a retry loop against a failing endpoint.
At these prices a loop could burn thousands of calls before a dollar threshold noticed, and the damage from a loop is a banned WhatsApp number, not an API bill.

## 7. What to build instead: a call-rate guard

Cheap, and it doubles as the loop detector.

**Count DeepSeek calls per day, per call type, in the existing agent state store** (the `rebe` Postgres database from [#9](./deployment-architecture-spec.md)).

Expected steady state is about **175 calls/day** (4 summarization + 150 gate + 10 reply + 10 relevance).

| Threshold | Action |
|---|---|
| **> 600 calls/day** (~3.4x expected) | Telegram alert to the maintainer. Keep running. Could just be a busy day in the group. |
| **> 2,000 calls/day** (~11x expected) | **Stop calling DeepSeek for the rest of the day.** Telegram alert. Keep the process, the heartbeat, and the scheduler alive. |

The kill-switch day costs at most about **$0.36** even if every one of those 2,000 calls is a full-size cache-miss, so the threshold is set by loop-detection sensitivity, not by money.

Tripping the kill-switch means Rebe stays silent for the rest of the day.
That is the correct behaviour, and it is already the specified failure posture.

**Also log real usage.**
Every DeepSeek response carries `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`, and `completion_tokens` in its `usage` block.
Accumulate all three per day, per call type.
This costs one integer write per call, and it turns every estimate in this document into something checkable against reality within a week of launch.

**Not needed:** a monthly budget alert, a per-call token ceiling, or a cost dashboard.
`max_tokens` should still be set per call to prevent a truncation or runaway-generation bug, as the [news pipeline](./news-pipeline-research.md) already requires for JSON mode, but that is an output-integrity control rather than a cost control.

---

## 8. Bottom line

Rebe costs about **$0.22 a month** to run against DeepSeek, and under **$1.10** even if the group is four times chattier than assumed.
Nothing in the design needs to change to make that cheaper, and the cheapest available optimisation would save fifteen cents.
The spend guard is a **call-rate counter with a Telegram alert at 600 calls/day and a hard stop at 2,000**, because the failure worth catching is a runaway loop, and the ban that loop would earn costs far more than the tokens.

---

## 9. Verify at build

- **Model ID and thinking toggle.**
Use `deepseek-v4-flash` and pass `extra_body={"thinking": {"type": "disabled"}}` explicitly.
Do not rely on `deepseek-chat`, retired 2026/07/24 15:59 UTC.
- **Re-read the pricing page.**
DeepSeek rotates model IDs and prices frequently, and reserves the right to adjust them.
Community reports of a peak-hour multiplier for V4 could not be confirmed against the official page and may apply only to CNY billing.
- **Replace the 150 msgs/day assumption with a measured number** once the group has a week of real traffic, and re-run section 4 from the logged `usage` totals.
- **Confirm the cache hit rate** from `prompt_cache_hit_tokens` after launch.
If the overnight gap kills the prefix every morning, the realistic figure moves toward the $0.38 no-cache line, which changes nothing.
