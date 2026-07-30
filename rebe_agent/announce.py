"""The announcement twin: a high-tier post, restated for the Announcements channel.

`docs/wayfinder/announcements-spec.md` in one sentence: every high-tier item that
posts to the group also goes to the bien.mx Community's Announcements channel,
in the professional register - formal Spanish, no slang, no emoji - and nothing
else ever posts there.

The shape mirrors the news leg's, deliberately. One DeepSeek call contributes
the words and never the link; `render` enforces the mechanical rules the prompt
asks for; the link is appended by this module, which is what keeps "she never
invents or shortens a link" a property of the code in this room too. The
difference is the register, and that the emoji allowance drops from one to none:
an announcements channel is the one place a stray 👀 reads as a persona leak
rather than a person.

Two things this module deliberately does not do.

**It does not decide what is big.** `rebe_agent.tiers` classifies and the news
leg asks it; this is handed an item already judged high tier and only writes and
sends. A second opinion here would be a second bar to drift.

**It never fails the post it follows.** The group post has already landed when
`announce` runs, so every failure - a brain that gave no answer, an answer the
rules refuse, an envelope that says no, a transport that is down - is one logged
nothing. One try, no retry: an announcement landing hours after its group twin
reads staler than no announcement at all, and the next high-tier item brings the
next chance.

The send leaves through the shared pacer as `SendKind.ANNOUNCEMENT`: it spends
the same per-minute, hourly and daily allowance as everything else from the
number and obeys the overnight hold and the soft pause, but skips the
post-to-post gap - it lands moments after its twin - and is invisible to the
ramp clamp and the practical stop, which count how often the *group* hears from
her.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from rebe_agent.brain import Brain, BrainError
from rebe_agent.evolution import EvolutionError
from rebe_agent.items import NewsItem
from rebe_agent.pacer import Pacer, SendRefusedError, SentMessage
from rebe_agent.sends import SendKind
from rebe_agent.usage import CallType
from rebe_agent.voice import DIGITS, LINKISH, capitalized, emoji_count

logger = logging.getLogger("rebe_agent.announce")

MAX_ANNOUNCEMENT_CHARS = 200
"""Room for one or two plain sentences: what happened, and why it matters.

Wider than the group post's 80 on purpose. The group message is a reaction that
makes somebody tap the link; an announcement states the news to people who may
read nothing else. It is still a WhatsApp message, not a bulletin.
"""

INSTRUCTIONS = """
Escribes el canal de anuncios de bien.mx, una comunidad de WhatsApp sobre
inteligencia artificial en español.

Te paso una noticia importante de IA. Redacta el anuncio: una o dos oraciones
cortas, en español profesional y claro, que digan qué pasó y por qué es
relevante. La liga a la fuente va debajo de lo que escribas y ahí está el
detalle.

Nunca:
- Emojis. Ni uno.
- Jerga, modismos ni tono de chat; es un canal de anuncios.
- Signos de exclamación dobles ni MAYÚSCULAS sostenidas.
- Escribir ligas, URLs o "http". La liga la pongo yo.
- Inventar: ningún dato, número, fecha, cifra ni nombre de empresa que la nota
  no traiga. Si la nota no lo dice, no lo digas.
- Pasar de 200 caracteres.
""".strip()


class Announcement(BaseModel):
    """What the model may contribute: the wording, and no link."""

    text: str = Field(
        description=(
            "El anuncio completo: una o dos oraciones en español profesional, "
            "menos de 200 caracteres, sin liga y sin emojis."
        )
    )


class AnnouncementRejectedError(ValueError):
    """The model answered, and the answer is not something the channel gets."""


def render(announcement: Announcement, item: NewsItem) -> str:
    """The message as the channel will see it, or say why not.

    The same defence the group post gets: every rule here is one the prompt also
    asks for, checked at the last point where catching it is still cheap.
    """
    wording = capitalized(" ".join(announcement.text.split()))
    if not any(char.isalpha() for char in wording):
        raise AnnouncementRejectedError("the model wrote nothing")
    if len(wording) > MAX_ANNOUNCEMENT_CHARS:
        raise AnnouncementRejectedError(
            f"{len(wording)} characters is a bulletin, not an announcement; "
            f"keep it under {MAX_ANNOUNCEMENT_CHARS}"
        )
    if LINKISH.search(wording):
        raise AnnouncementRejectedError(
            "the model wrote a link; links are appended here, never generated"
        )
    if emoji_count(wording):
        raise AnnouncementRejectedError("the professional register carries no emoji at all")

    invented = [
        number for number in DIGITS.findall(wording) if number not in DIGITS.findall(item.grounding)
    ]
    if invented:
        raise AnnouncementRejectedError(f"the source never mentions {', '.join(invented)}")

    # `link`, not `canonical_url`, for the reason the news leg spells out:
    # canonicalising is for comparing two links, not for editing an address
    # somebody is about to tap.
    return f"{wording}\n{item.link}"


class Announcer:
    """One announcement into one channel, or a logged nothing."""

    def __init__(self, brain: Brain, pacer: Pacer, chat: str) -> None:
        self._brain = brain
        self._pacer = pacer
        self._chat = chat

    async def announce(self, item: NewsItem) -> SentMessage | None:
        """One try at the twin. Never raises: the group post already landed,
        and nothing that goes wrong here is allowed to look like it did not."""
        try:
            answer = await self._brain.ask(
                CallType.ANNOUNCEMENT, _prompt(item), Announcement, instructions=INSTRUCTIONS
            )
            text = render(answer, item)
        except (BrainError, AnnouncementRejectedError) as exc:
            logger.warning("no announcement for %s: %s", item.canonical_url, exc)
            return None

        try:
            message = await self._pacer.send(SendKind.ANNOUNCEMENT, self._chat, text)
        except (SendRefusedError, EvolutionError) as exc:
            logger.warning("the announcement for %s did not get out: %s", item.canonical_url, exc)
            return None

        logger.info("announced %s in %s", item.canonical_url, self._chat)
        return message


def _prompt(item: NewsItem) -> str:
    """Everything the model is given, which is also everything it may restate.

    The URL is deliberately absent, exactly as it is from the group post's
    prompt: a model that never sees a link cannot get one wrong.
    """
    lines = [f"Fuente: {item.source}", f"Título: {item.title}"]
    if item.summary:
        lines.append(f"Resumen: {item.summary}")
    return "\n".join(lines)
