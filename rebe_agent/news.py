"""The news leg: one curated AI item becomes one paced Spanish post.

The whole path, in the order it happens:

    fetch -> filter and rank -> drop what was posted before -> one DeepSeek call
    -> validate -> send through the shared pacer -> only then write it down

Two of those arrows are the ones worth defending.

**The posted store is checked before the model, and written after the send.**
Checked first because a token spent on an item that can never go out is a token
wasted; written last because the store is a record of what the group *saw*, and
an item burnt by a transport blip nobody witnessed would be lost for good.

The ticket puts that check "before anything is ranked". It runs just after,
walking the ranked list until an unposted item turns up, because ranking and
dropping commute - the top *unposted* item is the same either way - and this
order asks the database about one item instead of about the whole pool.

**The model never sees the URL and never writes one.** The post is assembled here:
the model supplies her words, and the link is appended by this module. That is
what makes "it never invents or shortens a link" a property of the code rather
than a hope about the prompt. The link appended is the article's
own address with the tracking taken off, not the canonical key - canonicalising
forces https and drops a `www.`, which is right for comparing two links and is an
edit to an address somebody is about to tap.

What the model *does* write is bounded by the anti-hallucination rule in
`docs/wayfinder/reply-policy-spec.md`: the framing line may only restate what the
source item said. The prompt asks for that; `render` enforces the mechanical half
of it, rejecting any number the source did not supply. Company names cannot be
checked the same way without a list of every company there is, so those are left
to the instruction - and the rejection of invented numbers catches the specific
failure that matters most, since a wrong figure is the one hallucination a reader
can act on.

**The post is a reaction, not a report.** She is a group member throwing a link
at her friends, so the message is a hook plus a few words naming the subject, and
the article itself is what the link is for. That shape was not what went out at
first: `INSTRUCTIONS` existed here from the beginning and was never passed to the
model, so the only thing a call carried was `Fuente / Título / Resumen` and a
schema - and what came back was the headline, translated. Every call now carries
them, and the schema is one free field rather than two, because a field described
as "one sentence: what happened and why it matters" is a request for a newspaper
sentence however the instructions are worded.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field

from rebe_agent.announce import Announcer
from rebe_agent.brain import Brain, BrainError
from rebe_agent.clock import Clock
from rebe_agent.curate import Filters, Ranking, shortlist
from rebe_agent.feeds import Candidates
from rebe_agent.items import NewsItem
from rebe_agent.pacer import Pacer, SendRefusedError, SentMessage
from rebe_agent.posted import PostedStore
from rebe_agent.preview import PreviewLookup
from rebe_agent.sends import SendKind
from rebe_agent.tiers import Tier, classify
from rebe_agent.usage import CallType
from rebe_agent.voice import DIGITS, LINKISH, MAX_EMOJI, capitalized, emoji_count

logger = logging.getLogger("rebe_agent.news")

MAX_POST_CHARS = 80
"""A reaction, not a report.

The persona spec asks for WhatsApp-short and the cap used to say 240, which is
three lines of chat - wide enough that the three posts which prompted this change
(77, 103 and 141 characters) sailed through it. 80 is the width of the thing she
is actually being asked for: a hook and a few words naming the subject. It is the
mechanical half only. Nothing here can tell a short headline from a short
reaction; that is the prompt's job, and this is the ceiling it works under.
"""

RETRIES_PER_ITEM = 1
"""How many second chances one article gets before the run moves on.

A rejected answer is usually about the wording rather than the article - too
long, a number the source never gave - and the article was chosen because it was
the best thing available. Dropping the story to punish the sentence throws away
the wrong half, so the reason is handed back and the same item is asked again.
"""

REJECTIONS_PER_RUN = 2
"""How many articles a run gives up on before it stops paying for answers.

Counted per item, after its retry: a run that kept buying rejected answers would
be the loop the budget fears.
"""

DISMISSALS_PER_RUN = 3
"""How many "not for the group" verdicts a run pays for before it stops.

A dismissal is cheaper than a rejection - one call, no retry, and the item never
comes back - but it is still a call, and a morning when HN is all compiler
politics could otherwise spend the shortlist's whole depth learning that. Higher
than the rejection bound because the third-best item on such a morning may still
be a perfectly good post."""

RECENT_POSTS = 5
"""How many of her own last posts she is shown before writing a new one.

Not a rule and not a list of banned words - memory. The same prompt at the same
temperature drifts to the same opener, and a hook she used yesterday reads as a
template the third time the group sees it. Showing her what she wrote lets the
model route around itself, which is the only part of this that belongs to a model
at all.
"""

INSTRUCTIONS = """
Eres Rebe: mexicana, 28 años, te clavas con la IA y el diseño. Eres una integrante
más del grupo de WhatsApp, no una cuenta oficial ni un asistente.

Te paso una noticia de IA. No la reportes: reacciona a ella, como cuando avientas
un link al grupo nomás porque se te hizo interesante. La liga va debajo de lo que
escribas y ahí está el detalle; tu mensaje solo tiene que dar ganas de abrirla.

Primero decide si la nota va para el grupo. Son cuates normales, no
programadores: les late lo que pueden probar, ver o platicar - un modelo nuevo,
una app, algo de dinero, un escándalo de las empresas grandes. La política
interna de proyectos de software, los papers académicos, los compiladores,
kernels y la infraestructura de devs no les dicen nada: eso márcalo como que no
va, sin pena, y deja el mensaje vacío.

Escribe un solo renglón, cortísimo, de menos de 80 caracteres, con dos cosas:
- tu reacción, en tus palabras;
- y dos o tres palabras que digan de qué va la nota.
Empieza siempre con mayúscula, como cualquier mensaje bien escrito.

Ejemplos del tono - no son plantillas, cambia las palabras cada vez:
- Ojo con lo de los libros raros y la IA 👀
- Ya salió el modelo que corre sin internet, está cañón
- No manches lo de apple y la burbuja

Nunca:
- Resumir la nota ni traducir el titular. Si lo que escribiste se puede leer como
  el encabezado de un periódico, está mal.
- Explicar por qué importa. Eso lo decide quien abra la liga.
- Tono de boletín, ni "¡Claro!", ni MAYÚSCULAS.
- Más de un emoji en todo el mensaje, y casi siempre ninguno.
- Escribir ligas, URLs o "http". La liga la pongo yo.
- Inventar: ningún dato, número, fecha, cifra ni nombre de empresa que la nota no
  traiga. Si la nota no lo dice, no lo digas.
""".strip()


class NewsPost(BaseModel):
    """What the model is allowed to contribute: the message, and no link.

    One field, deliberately. The shape before this was `{opener, line}` with
    `line` described as "one sentence: what happened and why it matters" - a
    slot shaped like a news sentence, which is what kept coming back. It also
    made the code join two strings, and that join is what produced the live post
    "Miren esto Un analista predice que...": a bare space between a framing word
    and a capitalised sentence.

    Both problems dissolve in one field. The model decides whether there is a
    hook at all, where it ends and how the words run together, because those are
    decisions about her voice; the code is left holding only the mechanics.

    The verdict came later and is not words: the curator's gates are all
    mechanical - points, freshness, title length - so "GCC steering committee
    announces AI policy" sailed through them on HN points and went to a group
    that has never compiled anything. The model is the only thing in the
    pipeline that has actually read the story, so whether it is news *to this
    group* is asked here, first, in the same call that was already being paid
    for.
    """

    for_the_group: bool = Field(
        description=(
            "¿Le dice algo esta nota a un grupo de cuates mexicanos que no "
            "programan? true si la abrirían; false si es grilla interna del "
            "software: política de proyectos, papers, compiladores, kernels, "
            "infra de devs."
        )
    )
    text: str = Field(
        description=(
            "El mensaje completo, en la voz de Rebe: un renglón corto de reacción, "
            "menos de 80 caracteres, sin liga. Vacío si la nota no va para el grupo."
        )
    )


class PostRejectedError(ValueError):
    """The model answered, and the answer is not something Rebe would send."""


class UnfitItemError(PostRejectedError):
    """The model read the story, and it is not news this group would open.

    A subclass, so every caller that already treats a rejected answer as "skip
    this one and carry on" - the override leg above all - handles the verdict
    without knowing it exists. The difference is in what it says about the item:
    a rejection blames the wording and the article deserves another try; this
    blames the article, so it is dismissed for the life of the process and never
    retried."""


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
    framing = capitalized(" ".join(post.text.split()))
    if not any(char.isalpha() for char in framing):
        # Not `if not framing`: a lone ":" is as empty as "", and one went out
        # live over the GCC AI-policy story - a colon, a newline and a link.
        raise PostRejectedError("the model wrote nothing")
    if len(framing) > MAX_POST_CHARS:
        raise PostRejectedError(
            f"{len(framing)} characters is a report, not a reaction; keep it under {MAX_POST_CHARS}"
        )
    if LINKISH.search(framing):
        raise PostRejectedError("the model wrote a link; links are appended here, never generated")

    emoji = emoji_count(framing)
    if emoji > MAX_EMOJI:
        raise PostRejectedError(f"{emoji} emoji, and Rebe sends at most {MAX_EMOJI}")

    invented = _invented_numbers(framing, item.grounding)
    if invented:
        raise PostRejectedError(f"the source never mentions {', '.join(invented)}")

    # `link`, not `canonical_url`: the canonical form is the key two candidates
    # are compared on, and forcing https or dropping a `www.` to make that key
    # would be editing an address somebody is about to tap.
    return f"{framing}\n{item.link}"


def _invented_numbers(framing: str, grounding: str) -> list[str]:
    """Digits in the post that the source item never supplied.

    The mechanical half of the anti-hallucination bound. A number is the one
    invented detail a reader can act on - a price, a parameter count, a date - so
    it is the half worth enforcing rather than trusting.
    """
    supplied = set(DIGITS.findall(grounding))
    return [number for number in DIGITS.findall(framing) if number not in supplied]


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
        preview: PreviewLookup | None = None,
        announcer: Announcer | None = None,
    ) -> None:
        self._brain = brain
        self._pacer = pacer
        self._candidates = candidates
        self._posted = posted
        self._clock = clock
        self._filters = filters or Filters()
        self._ranking = ranking or Ranking()
        # `None` means no Announcements channel is configured and high-tier
        # items post to the group and nothing more - the behaviour from before
        # the announcements spec, which is also the dry-run and test default.
        self._announcer = announcer
        # `None` means no previews at all - the dry-run wiring and any test that
        # does not care about images get exactly the behaviour from before the
        # preview ticket, without having to stub a lookup that answers `None`.
        self._preview = preview
        # Items the model judged not for the group. In memory rather than in the
        # posted store, because that store is a record of what the group saw and
        # these are precisely what it never will; the cost of forgetting them on
        # a restart is one call each, once.
        self._dismissed: set[str] = set()

    @property
    def filters(self) -> Filters:
        """The quality gates this leg curates by.

        Readable because the override leg classifies tiers against the same
        points floor. Two copies of that number - one that decides what is worth
        ranking and one that decides what is breaking news - would drift, and the
        drift would show up as Rebe treating an ordinary story as the day's big one.
        """
        return self._filters

    @property
    def ranking(self) -> Ranking:
        """How this leg trades the three signals off. Read for the same reason."""
        return self._ranking

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
        rejected = 0
        dismissed = 0
        for item in ranked:
            if len(sent) >= limit:
                break
            if item.canonical_url in self._dismissed:
                logger.debug("already dismissed as not for the group: %s", item.canonical_url)
                continue
            if await self._posted.knows(item):
                logger.debug("already posted: %s", item.canonical_url)
                continue
            try:
                sent.append(await self.post_one(chat, item))
            except BrainError as exc:
                # The brain itself is the problem - the endpoint, or the day's
                # call ceiling - so the next candidate would fail the same way.
                logger.info("the brain gave no answer for %s: %s", item.canonical_url, exc)
                break
            except UnfitItemError as exc:
                # Before the plain rejection, because it is one: the article is
                # the problem, `_write` has already dismissed it for good, and
                # the next candidate may be exactly what the slot wants.
                dismissed += 1
                logger.info("not for the group: %s: %s", item.canonical_url, exc)
                if dismissed >= DISMISSALS_PER_RUN:
                    logger.info("%d items judged not for the group; ending the run", dismissed)
                    break
            except PostRejectedError as exc:
                # The item has already had its second chance inside `_write`, so
                # what is left is an article she cannot write about within the
                # rules - a headline that is a number, usually. The run moves on
                # to the next one. Bounded, because a run that kept paying for
                # rejected answers would be the loop the budget fears.
                rejected += 1
                logger.info("dropping %s: %s", item.canonical_url, exc)
                if rejected >= REJECTIONS_PER_RUN:
                    logger.info("%d unusable answers in a row; ending the run", rejected)
                    break
            except SendRefusedError:
                if not sent:
                    raise
                logger.info("the envelope closed after %d post(s); ending the run", len(sent))
                break

        if not sent:
            logger.info("nothing to post this run")
        return sent

    async def unposted(self, now: datetime) -> list[NewsItem]:
        """The whole curated shortlist, minus what the group has already seen.

        `run` walks the ranked list lazily instead, because it only ever needs the
        head of it and a token spent on an item that can never go out is a token
        wasted. The override leg in `rebe_agent.breaking` needs the list itself:
        "is the best thing left for the 22:00 slot weak" is a question about the
        list rather than about its first entry.
        """
        return await self.fresh(await self._candidates.fetch(now), now)

    async def fresh(self, pool: Iterable[NewsItem], now: datetime) -> list[NewsItem]:
        """Curate `pool` and drop everything already posted, best first.

        The whole free half of the pipeline in one call, so the overnight queue's
        contents are judged in the morning by the same filters, ranker and
        anti-repost gate as anything fetched that minute - including the freshness
        window, which is what drops a queued item the night outlived.
        """
        ranked = shortlist(pool, now, filters=self._filters, ranking=self._ranking)
        return [
            item
            for item in ranked
            if item.canonical_url not in self._dismissed and not await self._posted.knows(item)
        ]

    async def post_one(self, chat: str, item: NewsItem) -> Posted:
        """One item, from a model call to a row in the posted store."""
        text = await self._write(item, await self._posted.recent(RECENT_POSTS))
        # Looked up after the post is written and never in a position to stop
        # it: `preview_image_url` answers `None` for every failure, and `None`
        # is exactly the text send below - a missing picture never costs a post.
        image = await self._preview(item.link) if self._preview is not None else None
        if image is not None:
            # The caption is the whole post - her words, a newline, the link -
            # because the link is still how anybody reads the article.
            message = await self._pacer.send_photo(SendKind.POST, chat, image, text)
        else:
            message = await self._pacer.send(SendKind.POST, chat, text)
        # `render` collapses every run of whitespace in her words, so the first
        # line is exactly what she wrote and the second is the link this module
        # appended. Only the first is worth remembering: it is what the next post
        # is asked not to sound like, and the address is a column already.
        words = text.split("\n", 1)[0]
        # After the send, never before: the store says what the group saw.
        await self._posted.remember(item, self._clock.now(), words)
        logger.info("posted %s from %s", item.canonical_url, item.source)
        # The announcement twin, after the post is already remembered: every
        # high-tier item that posts gets one, whichever path posted it - a
        # drawn slot, the override leg, the overnight drain, `--post-news` -
        # because this is the one place they all post through. `announce`
        # never raises; a twin that could not go out costs the channel one
        # message, never the group its post.
        if self._announcer is not None and classify(item, filters=self._filters) is Tier.HIGH:
            await self._announcer.announce(item)
        return Posted(item=item, text=text, message=message)

    async def _write(self, item: NewsItem, recent: Sequence[str]) -> str:
        """The model call, and a second chance at the same article.

        A rejection is a statement about the wording, and the article underneath
        it was picked because it was the best thing on the shortlist. So the
        reason goes back to the model - in its own words, the ones `render`
        raised - and the same item is asked again before the run gives up on it.

        Raises the last `PostRejectedError` if the retries are spent.
        """
        rejection = ""
        for _ in range(RETRIES_PER_ITEM + 1):
            post = await self._brain.ask(
                CallType.NEWS_SUMMARY,
                _prompt(item, recent, rejection),
                NewsPost,
                instructions=INSTRUCTIONS,
            )
            if not post.for_the_group:
                # No retry: a rejection is about the wording and the wording can
                # change, but the article is the article. Dismissed here rather
                # than in `run` so the override leg's direct `post_one` calls
                # stop seeing the item too.
                self._dismissed.add(item.canonical_url)
                raise UnfitItemError("she read it and it is not news this group would open")
            try:
                return render(post, item)
            except PostRejectedError as exc:
                rejection = str(exc)
                logger.info("unusable answer for %s: %s", item.canonical_url, exc)
        raise PostRejectedError(rejection)


def _prompt(item: NewsItem, recent: Sequence[str] = (), rejection: str = "") -> str:
    """Everything the model is given, which is also everything it may restate.

    The URL is deliberately absent: a model that never sees a link cannot get one
    wrong, and the canonical one is appended after it has answered. `recent`
    carries the same absence, for the same reason.

    Her last few posts go in here rather than in the instructions because they
    change on every call, and the instructions are the part worth keeping
    identical from one request to the next - an identical prefix is what DeepSeek
    bills as a cache hit.
    """
    lines = [f"Fuente: {item.source}", f"Título: {item.title}"]
    if item.summary:
        lines.append(f"Resumen: {item.summary}")
    if recent:
        lines.append("")
        lines.append("Esto ya lo escribiste tú hace poco. No repitas el arranque ni la forma:")
        lines.extend(f"- {written}" for written in recent)
    if rejection:
        lines.append("")
        lines.append(f"Tu intento anterior no sirvió ({rejection}). Escríbelo otra vez.")
    return "\n".join(lines)
