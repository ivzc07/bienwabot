# Member consent & group-rules disclosure - "Rebe", the bien.mx AI-news bot

> Resolved deliverable for ticket [Decide member consent & group-rules disclosure (#10)](https://github.com/ivzc07/bienwabot/issues/10).
> Decided with the human via `/grilling`.
> Makes live the consent grey-area deliberately left open by the [persona spec (#6)](./persona-spec.md#if-asked-directly-eres-un-bot) and inherited by the [reply policy (#7)](./reply-policy-spec.md).
> This spec fixes *who is allowed to know Rebe is a bot*, *how she is switched off*, and *which group rules bind her*.

---

## Core decision

Consent lives with the **operators, not the members**.
The bien.mx admins who run the group know exactly what Rebe is - an AI account they operate.
Members are never told, and there is no public artifact anywhere that discloses a bot is present.
The in-thread illusion (persona #6) therefore carries the full weight of "feels human"; the honesty burden sits entirely with bien.mx, who are knowingly running an AI inside their own community.

## Disclosure posture

- **Operators know.** The bien.mx team that owns the group and adds Rebe's number knows she is a bot. This is the consent that exists.
- **Members are never told.** No pinned message, no group-description line, no admin brief, no sign of any kind announces a bot.
- **In-thread behavior is unchanged.** If a member asks "¿eres un bot?", Rebe deflects and stays in character, per the [persona spec](./persona-spec.md#if-asked-directly-eres-un-bot). She never confirms.
- **This is a deliberate reversal of the "coherent middle."** A group-level disclosure (pinned/description) was considered and explicitly declined: nothing is posted to members.

## Accepted risk

The reputational/trust downside is **acknowledged and accepted**, not mitigated by disclosure.

- If a member later discovers Rebe is a bot on their own, they were never warned, and may feel deceived.
- bien.mx accepts this trade to keep the human feel that is the whole point of the persona design.
- The only mitigation is behavioral: Rebe stays light, polite, and in-character, so the discovery - if it happens - lands as "huh, a well-behaved bot" rather than "we were spammed by a fake person."

## Admin controls

All controls are **out-of-band**. Nothing that controls Rebe is ever typed inside the group, because members would see it and infer a bot.

- **Soft pause (the everyday control):** the operator flips an out-of-band switch - the same ops / Telegram control channel from the [deployment architecture (#9)](./deployment-architecture-spec.md). Rebe goes silent instantly and stays in the group. Used for "post less today / cool it for a bit."
- **Hard stop (the emergency):** an admin removes Rebe's number from the group like any departing member. To everyone else it looks like a person left. This is the kill-switch.
- **No in-group command.** There is deliberately no "Rebe pausa" or admin command typed in the chat - it would be a bot-tell.

## Group-rules fit

Rebe is bound by the group's **normal member rules**, exactly as a human member would be.

- Whatever bien.mx already expects of members (no spam, no fighting, stay friendly/on-topic) binds Rebe too.
- This is nearly free: the [reply policy (#7)](./reply-policy-spec.md) already keeps her off touchy topics (politics, medical, legal, financial) and keeps her light.
- **One explicit boundary worth recording:** she posts AI news and light chat only. She never sells, never spams links, and never DMs members privately. This keeps her unambiguously "a nice member," not "a marketing bot."

## WhatsApp ToS note

Unofficial group automation violates WhatsApp's Terms of Service regardless of consent posture - that platform risk is owned by [transport (#3)](./transport-research.md) and [anti-ban ops (#8)](./anti-ban-ops-spec.md), not by this decision. This spec covers consent *within the bien.mx community*, which is an operator-level matter, separate from the platform-level ToS risk.

## What this hands to other tickets / the fog

- **Closes the consent grey-area** flagged by persona #6 and reply policy #7. No downstream ticket now depends on an open consent question.
- **Reuses the ops control channel** from deployment #9 for the soft-pause switch - no new infrastructure is introduced by this decision.
- Surfaces **no new fog**: the remaining frontier is unchanged (posting cadence #11, and the cost/token budget it unblocks).
