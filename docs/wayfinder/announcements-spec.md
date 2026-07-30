# The announcement twin

When something genuinely big posts to the group, the same story also goes to the bien.mx Community's Announcements channel, restated in a professional register.
This document records the decisions and why they fell the way they did.

## 1. What announces

The existing HIGH tier from `posting-cadence-spec.md` section 4, unchanged: top of Hacker News well above the ranker's floor, or a first-party model/product announcement from a major AI org.
There is deliberately no second bar.
Two definitions of "big" would drift, and the drift would show up as a story the group saw celebrated and the channel never mentioned.

Every HIGH item that posts gets its twin, whichever path posted it: a drawn slot whose best candidate happened to be big, the mid-day breaking override, the overnight queue draining into the morning slot, or an operator's `--post-news`.
The rule lives in `NewsLeg.post_one`, the one door every post leaves through, precisely so it cannot be path-dependent.

Normal-tier posts never announce.
The channel is the low-noise room; filling it at the group's rate would make it the group.

## 2. Where it goes, and through what

The channel JID is `REBE_ANNOUNCE_JID`, optional.
Unset means the leg is off and the process behaves exactly as before the spec - so a deploy that has not provisioned the channel boots unchanged, and go-live is one Coolify variable.
Rebe's number must be an admin of the community to post in an announcements channel; that is a runbook step, not code.

The send leaves through the shared pacer as its own kind, `announcement`, and the envelope treats it in three deliberate ways:

- **It spends the raw allowance.** Four a minute, three an hour, the daily ceiling: the envelope is a property of the number, and a send to a second room is still a send from the number. It also obeys the overnight hold and the soft pause - `/pausa` silences the channel as surely as the group.
- **It skips the post-to-post gap.** The twin lands moments after its group post, the way a person forwards their own message to a second chat. Holding it 75-90 minutes would put two copies of one story an hour and a half apart, and a second big story that day could starve it entirely.
- **It is invisible to the ramp clamp and the practical stop.** Both count how much of the *group's* day is Rebe sharing links; the twin is the same story in another room, not a second story. If it consumed the week-one clamp, one big day would spend two of three clamped posts on one item and the group would go near-silent whenever something big happened - backwards. What bounds the channel instead is what bounds the tier: HIGH items are rare by construction.

The one rule that was nearly bypassed and must never be: the twin goes *through* the pacer.
A bypass would have meant `/pausa`, the ban stops and the ramp halts covered the group and not the channel - a number that has just been rate-limited hammering WhatsApp from a second door.

## 3. The professional register

The twin's text is one extra DeepSeek call (`CallType.ANNOUNCEMENT`), not a reuse of the group line.
The group post is a reaction in Rebe's casual voice; the channel gets one or two plain sentences in formal Spanish - what happened, why it matters - under 200 characters.

The same mechanical defences apply, with one tightened: no self-written links (the code appends the article's own address afterwards), no invented figures, and *zero* emoji rather than the group's zero-to-one.
A register is a way of speaking, not a second persona; the rules that make her not-a-bot travel with her.

## 4. Failure

One try, and every failure is a logged nothing.
By the time the twin runs, the group post has landed and is remembered; a brain error, a rejected answer, a refused or broken send all cost the channel one message and nothing else.
There is no retry queue: an announcement landing hours after its group twin reads staler than no announcement at all, and the next HIGH item brings the next chance.

This is the news leg's "everything fails toward silence", applied to a second room.
