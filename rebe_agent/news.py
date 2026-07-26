"""The news leg: one curated AI item becomes one paced Spanish post.

The whole path, in the order it happens:

    fetch -> filter and rank -> drop what was posted before -> one DeepSeek call
    -> validate -> send through the shared pacer -> only then write it down

Two of those arrows are the ones worth defending.

**The posted store is checked before the model, and written after the send.**
Checked first because a token spent on an item that can never go out is a token
wasted; written last because the store is a record of what the group *saw*, and
an item burnt by a transport blip nobody witnessed would be lost for good.

**The model never sees the URL and never writes one.** The post is assembled here:
the model supplies a framing word and one line, and the canonical URL is appended
by this module. That is what makes "it never invents or shortens a link" a
property of the code rather than a hope about the prompt.

What the model *does* write is bounded by the anti-hallucination rule in
`docs/wayfinder/reply-policy-spec.md`: the framing line may only restate what the
source item said. The prompt asks for that; `render` enforces the mechanical half
of it, rejecting any number the source did not supply. Company names cannot be
checked the same way without a list of every company there is, so those are left
to the instruction - and the rejection of invented numbers catches the specific
failure that matters most, since a wrong figure is the one hallucination a reader
can act on.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

from pydantic import BaseModel, Field

from rebe_agent.brain import Brain, BrainError
from rebe_agent.clock import Clock
from rebe_agent.curate import Filters, Ranking, shortlist
from rebe_agent.feeds import Candidates
from rebe_agent.items import NewsItem
from rebe_agent.pacer import Pacer, SendRefusedError, SentMessage
from rebe_agent.posted import PostedStore
from rebe_agent.sends import SendKind
from rebe_agent.usage import CallType

logger = logging.getLogger("rebe_agent.news")

MAX_FRAMING_CHARS = 240
"""WhatsApp-short, per the persona spec: news is one or two lines, not a paragraph."""

MAX_EMOJI = 1
"""The persona spec dials Rebe down to 0-1 per message, often none."""

INSTRUCTIONS = """
Eres Rebe: mexicana, 28 años, te clavas con la IA y el diseño. Eres una integrante
más del grupo de WhatsApp, no una cuenta oficial ni un asistente.

Te paso una noticia de IA. Escribe cómo se la contarías al grupo.

Voz:
- Español mexicano neutro, casual y cálido. Corto: una línea, como se escribe en
  WhatsApp.
- `opener` es una palabrita humana para presentar la nota: "miren", "chequen",
  "órale", "nuevo:", "ya salió". Rótala, no uses siempre la misma. Puede ir vacía.
- `line` dice qué pasó y por qué importa, en una sola frase, en tus palabras.
- Gramática natural, no perfecta. Puede empezar en minúscula.

Nunca:
- Tono de boletín de prensa, ni "¡Claro!", ni explicar de más, ni MAYÚSCULAS.
- Más de un emoji en todo el mensaje, y casi siempre ninguno.
- Escribir ligas, URLs o "http". La liga la pongo yo.
- Inventar. Solo puedes reformular lo que dice la nota que te paso: ningún dato,
  número, fecha, cifra ni nombre de empresa que la nota no traiga. Si la nota no
  lo dice, no lo digas.
""".strip()


class NewsPost(BaseModel):
    """What the model is allowed to contribute: two short strings, no link."""

    opener: str = Field(
        default="",
        description="Palabrita humana para presentar la nota, o vacío. Ej: miren, chequen.",
    )
    line: str = Field(description="Una sola frase: qué pasó y por qué importa.")


class PostRejectedError(ValueError):
    """The model answered, and the answer is not something Rebe would send."""


@dataclass(frozen=True, slots=True)
class Posted:
    """One item that made it all the way into the group."""

    item: NewsItem
    text: str
    message: SentMessage


def render(post: NewsPost, item: NewsItem) -> str:
    """The message as the group will see it, or `PostRejectedError` saying why not.

    Every rule here is one the prompt also asks for. Asking is not enough: a
    model that ignores an instruction once in fifty is a bot tell once in fifty,
    and this is the last point at which that is still cheap to catch.
    """
    framing = " ".join(f"{post.opener} {post.line}".split())
    if not post.line.strip():
        raise PostRejectedError("the model wrote no line")
    if len(framing) > MAX_FRAMING_CHARS:
        raise PostRejectedError(f"{len(framing)} characters is not a WhatsApp message")
    if _LINKISH.search(framing):
        raise PostRejectedError("the model wrote a link; links are appended here, never generated")
    if emoji_count(framing) > MAX_EMOJI:
        raise PostRejectedError(f"{emoji_count(framing)} emoji, and Rebe sends at most {MAX_EMOJI}")

    invented = _invented_numbers(framing, item.grounding)
    if invented:
        raise PostRejectedError(f"the source never mentions {', '.join(invented)}")

    return f"{framing}\n{item.canonical_url}"


_LINKISH = re.compile(r"https?://|www\.|\.com\b|\.mx\b|\.ai\b|\.org\b", re.IGNORECASE)
"""Anything shaped like an address. The real link is appended after this check."""

_DIGITS = re.compile(r"\d+")

_EMOJI = "So"
"""Unicode's "symbol, other" - the category the emoji themselves live in."""

_MODIFIER = "Sk"
""""Symbol, modifier": the skin tones, which colour the emoji before them."""

_JOINERS = frozenset("\u200d\ufe0f\ufe0e")
"""Zero-width joiner and the variation selectors, which glue two emoji into one."""


def emoji_count(text: str) -> int:
    """How many emoji a reader would see.

    Counted the way a reader counts them, not the way Unicode stores them: a
    family or a skin-toned wave is several code points and one picture, and the
    rule the persona spec sets - at most one - is about pictures. Two emoji side
    by side with nothing between them are still two.
    """
    count = 0
    joined = False
    for character in text:
        category = unicodedata.category(character)
        if category == _EMOJI:
            count += not joined
            joined = False
        elif character in _JOINERS:
            joined = True
        elif category != _MODIFIER:
            joined = False
    return count


def _invented_numbers(framing: str, grounding: str) -> list[str]:
    """Digits in the post that the source item never supplied.

    The mechanical half of the anti-hallucination bound. A number is the one
    invented detail a reader can act on - a price, a parameter count, a date - so
    it is the half worth enforcing rather than trusting.
    """
    supplied = set(_DIGITS.findall(grounding))
    return [number for number in _DIGITS.findall(framing) if number not in supplied]


class NewsLeg:
    """One run: fetch, curate, write, send, remember. Nothing is scheduled here.

    When this runs is the cadence ticket's decision. This object answers "make a
    post happen now", and answers it at most `limit` times.
    """

    def __init__(
        self,
        brain: Brain,
        pacer: Pacer,
        candidates: Candidates,
        posted: PostedStore,
        clock: Clock,
        *,
        filters: Filters | None = None,
        ranking: Ranking | None = None,
    ) -> None:
        self._brain = brain
        self._pacer = pacer
        self._candidates = candidates
        self._posted = posted
        self._clock = clock
        self._filters = filters or Filters()
        self._ranking = ranking or Ranking()

    async def run(self, chat: str, *, limit: int = 1) -> list[Posted]:
        """Post up to `limit` fresh items into `chat`, best first.

        `limit` is the per-run cap: a busy news day is still a normal day in the
        group. In the shipped posture the pacer's 75-90 minute post gap makes any
        limit above one academic, and that is the right way round - how far apart
        posts sit belongs to the envelope, not here.

        Raises `SendRefusedError` or `EvolutionError` only when *nothing* went
        out. Once a post has landed, a refusal is the envelope working, so the
        run simply ends.
        """
        now = self._clock.now()
        pool = await self._candidates.fetch(now)
        ranked = shortlist(pool, now, filters=self._filters, ranking=self._ranking)
        logger.info("%d candidates, %d after filtering and dedup", len(pool), len(ranked))

        sent: list[Posted] = []
        for item in ranked:
            if len(sent) >= limit:
                break
            if await self._posted.knows(item):
                logger.debug("already posted: %s", item.canonical_url)
                continue
            try:
                sent.append(await self._post(chat, item))
            except (BrainError, PostRejectedError) as exc:
                # The item is dropped and the run ends rather than walking on to
                # the next candidate: a model that just produced an unusable post
                # is not more likely to produce a good one on the next try, and
                # the call budget is worth more than one extra attempt.
                logger.info("dropping %s: %s", item.canonical_url, exc)
                break
            except SendRefusedError:
                if not sent:
                    raise
                logger.info("the envelope closed after %d post(s); ending the run", len(sent))
                break

        if not sent:
            logger.info("nothing to post this run")
        return sent

    async def _post(self, chat: str, item: NewsItem) -> Posted:
        """One item, from a model call to a row in the posted store."""
        post = await self._brain.ask(CallType.NEWS_SUMMARY, _prompt(item), NewsPost)
        text = render(post, item)
        message = await self._pacer.send(SendKind.POST, chat, text)
        # After the send, never before: the store says what the group saw.
        await self._posted.remember(item, self._clock.now())
        logger.info("posted %s from %s", item.canonical_url, item.source)
        return Posted(item=item, text=text, message=message)


def _prompt(item: NewsItem) -> str:
    """Everything the model is given, which is also everything it may restate.

    The URL is deliberately absent: a model that never sees a link cannot get one
    wrong, and the canonical one is appended after it has answered.
    """
    lines = [f"Fuente: {item.source}", f"Título: {item.title}"]
    if item.summary:
        lines.append(f"Resumen: {item.summary}")
    return "\n".join(lines)
