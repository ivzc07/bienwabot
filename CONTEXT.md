# CONTEXT

The ubiquitous language of `rebe-agent`.
Terms only - implementation lives in code and `docs/wayfinder/`.

## Glossary

**Rebe** - the bien.mx WhatsApp news persona.
One number, one process, one casual Spanish voice in the group.

**The group** - the bien.mx members' chat (`REBE_GROUP_JID`).
Where scheduled posts, breaking posts and replies happen.

**The Announcements channel** - the bien.mx Community's admin-only broadcast channel (`REBE_ANNOUNCE_JID`).
Members cannot speak there, so it has posts and nothing else - no replies, no chime-ins.

**HIGH tier** - a candidate big enough to break the day's plan: top of Hacker News well above the ranker's floor, or a first-party model/product announcement from a major AI org.
The one definition of "very important news" in the system; there is no second bar.

**Announcement twin** - the professional-register copy of a HIGH-tier post, sent to the Announcements channel right after its group post lands.
Every HIGH item that posts gets one, whichever path posted it; no other post does.
It is a copy in another room, not a second story: it never consumes the ramp clamp or the practical stop, only the raw envelope.

**Professional register** - the announcement twin's voice: formal Spanish, no slang, no emoji.
Same mechanical gates as every other line Rebe types (no self-written links, no invented figures); a different register, not a different persona.

**The envelope** - the per-number anti-ban ceilings (4/min, 3/hour, 12/day) and holds enforced by the one shared pacer.
A property of the number, so every send from it - post, reply or announcement twin - spends the same allowance.
