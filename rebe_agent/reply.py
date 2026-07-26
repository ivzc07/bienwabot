"""The webhook leg: an addressed message becomes one paced reply, or nothing.

The whole path, in the order it happens:

    remember the turn -> tier gate -> conversation shape -> classify -> mark read
    -> generate -> validate -> send through the shared pacer -> remember hers

Four of those steps are worth defending.

**The turn is remembered first, before anything decides whether to answer.**
Two reasons. A redelivered webhook is refused by that write, so the duplicate
never reaches the model or the group; and a message she stays quiet about is
still part of the conversation she will be handed next time. A window with only
her half in it is not a thread.

**The tier gate is mechanical and runs before the model.** Whether a message
@-mentions her, names her, or quotes her is a fact about the payload, so tier one
cannot be missed by a bad classification and tier three costs nothing. What the
model is asked is the *topic*, which is a judgement, and that is the reply-gate
call from section 2 of `docs/wayfinder/token-budget-spec.md`.

**Everything fails toward silence.** A gate that errors, a classification that is
not confident, a generation that comes back empty or unusable, an envelope that
says no, a transport that is down: all of them end this function with `None` and
nothing in the group. `docs/wayfinder/reply-policy-spec.md` is explicit that this
overrides "she always answers when addressed" - a dropped reply looks like she
put her phone down, a broken one looks like a bot - and that there are no error
messages in the group, ever.

**The read receipt goes out once the gate has said yes, before generation.** That
is the order section 3 of the deployment spec draws, and it is also the human
one: she opened the message, and then either answered or got distracted.

What the model writes is bounded here rather than only asked for in the prompt.
A reply that carries a link, a figure, a second emoji, a deflection that steers
back to AI, or - the one that ends the persona - an admission that she is a bot,
is dropped instead of sent. A model that ignores an instruction once in fifty is
a bot tell once in fifty.

One event is handled at a time. The deployment spec's single-replica invariant
means one lock in one process is enough, and it is necessary: two deliveries
arriving together would otherwise both read a window in which she had not spoken
yet, and both would answer.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart

from rebe_agent.brain import Brain, BrainError
from rebe_agent.evolution import EvolutionError, EvolutionReader
from rebe_agent.inbound import InboundMessage, Tier, fold_accents, tier
from rebe_agent.memory import MEMORY_WINDOW, GroupMemory, Turn
from rebe_agent.pacer import Pacer, SendRefusedError, SentMessage
from rebe_agent.sends import SendKind
from rebe_agent.usage import CallType
from rebe_agent.voice import LINKISH, MAX_EMOJI, emoji_count

logger = logging.getLogger("rebe_agent.reply")

MAX_REPLY_CHARS = 200
"""Chat is "usually one line" in the persona spec, and shorter than a news post."""

MAX_FIGURE_DIGITS = 2
"""Three digits in a row is a year, a price, or a statistic - none of which she
was given anything to check against, because a chat reply has no source item the
way a news post does. Two digits or fewer is ordinary speech: "gpt 5", "los 2"."""

MIN_CONFIDENCE = 0.5
"""Below this the classification is a guess, and the failure posture says quiet."""

THREAD_TURNS = 3
"""How many times she answers inside one thread before letting it fade. The reply
policy asks for "a follow-up or two, then it dies" - and no closing message."""

THREAD_GAP = timedelta(minutes=30)
"""A silence this long ends a thread. The next message opens a new one, which the
pacer independently treats as a quiet thread worth an extra beat before typing."""

GATE_CONTEXT_TURNS = 3
"""How much of the thread the classification call sees. Section 2 of the token
budget spec sizes call B at ~400 input tokens, of which the thread is ~150."""


class Topic(StrEnum):
    """What kind of address this is, which is the only thing the model judges."""

    ON_TOPIC = "on_topic"
    """AI, tech she would have an opinion on, or one of her own news posts."""

    OFF_TOPIC = "off_topic"
    """Anything else, personal questions included. Deflect, never look it up."""

    NO_GO = "no_go"
    """Medical, legal, financial, partisan, religious, members' data, or NSFW."""

    BOT_QUESTION = "bot_question"
    """"¿eres un bot?" and its cousins. Deflect in character, never confirm."""


class ReplyDecision(BaseModel):
    """The reply-gate verdict: call B in the token budget spec."""

    topic: Topic
    confidence: float = Field(ge=0.0, le=1.0)
    """How sure the classification is. Low confidence buys silence, not a guess."""


class Reply(BaseModel):
    """What the model is allowed to contribute: one short line, and nothing else."""

    text: str = Field(description="Un solo mensaje corto de WhatsApp, en la voz de Rebe.")


class ReplyRejectedError(ValueError):
    """The model answered, and the answer is not something Rebe would send."""


INSTRUCTIONS = """
Eres Rebe: mexicana, 28 años, te clavas con la IA y el diseño. Eres una integrante
más del grupo de WhatsApp, no una cuenta oficial, no un asistente y no un buscador.

Alguien te habló directo. Contesta como contestarías en WhatsApp.

Voz:
- Español mexicano neutro, casual y cálido. Un solo renglón, corto.
- Gramática natural, no perfecta. Puede empezar en minúscula, cabe un "jaja".
- Opinión sí, y "no estoy segura" también. Las dos suenan a persona.

Nunca:
- Tono de boletín, ni "¡Claro!", ni "con gusto te ayudo", ni explicar de más.
- Más de un emoji en todo el mensaje, y casi siempre ninguno.
- Escribir ligas, URLs ni "http".
- Inventar datos: ningún número, fecha, cifra, estadística ni fuente. Si no lo
  sabes, dilo como lo diría una persona: "ni idea", "habría que buscarlo".
- Decir que eres un bot, una IA, un modelo o un asistente, ni negar que eres
  persona. Si te preguntan, lo toreas y sigues en tu papel.
""".strip()

GUIDANCE: dict[Topic, str] = {
    Topic.ON_TOPIC: (
        "Es de lo tuyo. Da una respuesta ligera de verdad o tu opinión, en una línea."
    ),
    Topic.OFF_TOPIC: (
        "No es de lo tuyo, o es algo personal. No eres buscador ni contestas datos: "
        'sácale la vuelta como persona ocupada ("jaja luego les cuento", "ando ocupada"). '
        "Cortito y amable, sin quedarte callada y sin dar la información."
    ),
    Topic.NO_GO: (
        "Es un tema en el que no te metes: salud, leyes, dinero o inversiones, política "
        "o religión, datos personales de alguien, o algo subido de tono. Una sola línea "
        'humana del tipo "de eso ni idea, mejor pregúntale a alguien que sepa". '
        "No opines del tema y no cambies la plática a IA: redirigir suena a bot."
    ),
    Topic.BOT_QUESTION: (
        "Te están preguntando si eres un bot. Sigues en personaje: una línea humana que "
        'lo torea ("jajaja por?", "no manches, ya me dijeron eso antes"). '
        "Nunca lo confirmas, nunca lo explicas y nunca hablas de IA ni de modelos aquí."
    ),
}

RUBRIC = """
Clasificas un mensaje de WhatsApp dirigido a Rebe, una integrante del grupo a la
que le gusta la IA. Solo eliges la categoría; no escribes la respuesta.

- on_topic: IA, modelos, herramientas de IA, tecnología de la que ella opinaría,
  o algo sobre una noticia que ella misma compartió.
- off_topic: cualquier otra cosa, incluidas preguntas personales sobre ella y
  búsquedas de datos ("¿a qué hora abre X?", "¿cuánto cuesta Y?").
- no_go: consejo médico, legal o financiero (cripto incluido), política partidista
  o religión, datos personales de algún miembro, o contenido sexual o de acoso.
- bot_question: le preguntan o insinúan que es un bot, una IA o un programa.

confidence es qué tan claro está, de 0 a 1. Si dudas entre dos categorías, baja la
confianza en vez de adivinar: quedarse callada cuesta menos que contestar mal.
""".strip()


def render(reply: Reply, topic: Topic) -> str:
    """The reply as the group will see it, or `ReplyRejectedError` saying why not.

    Every rule here is one the prompt also asks for. Asking is not enough at the
    last point where it is still free to catch.
    """
    text = " ".join(reply.text.split())
    if not text:
        raise ReplyRejectedError("the model wrote nothing")
    if len(text) > MAX_REPLY_CHARS:
        raise ReplyRejectedError(f"{len(text)} characters is not a WhatsApp reply")
    if LINKISH.search(text):
        raise ReplyRejectedError("the model wrote a link, and it was given none to write")

    emoji = emoji_count(text)
    if emoji > MAX_EMOJI:
        raise ReplyRejectedError(f"{emoji} emoji, and Rebe sends at most {MAX_EMOJI}")

    figure = _FIGURE.search(text)
    if figure:
        raise ReplyRejectedError(f"{figure.group()} is a figure nobody gave her")

    folded = fold_accents(text)
    if _CONFESSION.search(folded):
        raise ReplyRejectedError("the model broke character; silence stays in it")
    if topic in _NO_MENTION_OF_AI and _ABOUT_AI.search(folded):
        raise ReplyRejectedError(f"a {topic} answer that steers back to AI reads as an agenda")
    return text


_FIGURE = re.compile(rf"\d{{{MAX_FIGURE_DIGITS + 1},}}")

_CONFESSION = re.compile(
    r"\b(soy|somos)\s+(un|una)\s+"
    r"(bot|robot|chatbot|ia|inteligencia\s+artificial|asistente|modelo|programa|maquina)\b"
    r"|\bno\s+soy\s+(humana?|real|una\s+persona)\b",
    re.IGNORECASE,
)
"""Her breaking character. The persona and consent specs both say she never does."""

_ABOUT_AI = re.compile(
    r"\b(ia|a\.?i\.?|inteligencia\s+artificial|chatgpt|gpt|deepseek|openai|llm|"
    r"modelos?\s+de\s+lenguaje)\b",
    re.IGNORECASE,
)
"""Her own subject. Ordinarily welcome; in two places it is the tell."""

_NO_MENTION_OF_AI = frozenset({Topic.NO_GO, Topic.BOT_QUESTION})
"""Where naming AI turns a deflection into an agenda.

On a no-go topic the reply policy is explicit that she does not steer back to
AI - "an agenda-driven redirect reads as a bot" - and the deflection is one line
of "ni idea", not a segue. Asked whether she is a bot, bringing up models at all
is halfway to the confession the persona spec forbids.
"""


@dataclass(frozen=True, slots=True)
class Silence:
    """Why nothing went out. Logged, never sent, and never shown to the group."""

    reason: str


class ReplyLeg:
    """One inbound webhook event: a paced reply, or nothing. Never an error."""

    def __init__(
        self,
        brain: Brain,
        pacer: Pacer,
        reader: EvolutionReader,
        memory: GroupMemory,
    ) -> None:
        # No `Clock`, unlike the news leg: every instant this leg reasons about
        # is either WhatsApp's own `messageTimestamp` on the message in hand or
        # the pacer's record of when a send landed. Asking the wall clock what
        # time it is would be asking a third source about the same two events.
        self._brain = brain
        self._pacer = pacer
        self._reader = reader
        self._memory = memory
        # Held for a whole event, model calls and typing pause included. Two
        # deliveries landing together would otherwise both read the window before
        # either wrote to it, and both would find that she had not spoken yet -
        # which is exactly how "never twice in a row" and the thread fade get
        # past. The deployment spec's single-replica invariant makes one lock in
        # one process enough; it does not cover two events inside that process.
        self._turnstile = asyncio.Lock()

    async def handle(self, message: InboundMessage) -> SentMessage | None:
        """Answer `message` if she should, and say nothing at all if she should not.

        Never raises for anything the group could be affected by: a broken brain,
        a closed envelope and a dead transport all come back as `None`.
        """
        async with self._turnstile:
            return await self._decide(message)

    async def _decide(self, message: InboundMessage) -> SentMessage | None:
        """One event, from the window to the wire. Called one at a time."""
        if not await self._memory.remember(_turn_of(message)):
            logger.info("webhook redelivered %s; already answered or ignored", message.message_id)
            return None

        window = await self._memory.recent(message.chat, MEMORY_WINDOW)
        history = [turn for turn in window if turn.message_id != message.message_id]
        hers = frozenset(turn.message_id for turn in window if turn.by_rebe and turn.message_id)

        where = tier(message, hers=hers)
        if where is not Tier.ADDRESSED:
            logger.debug(
                "%s in %s is tier %s; staying quiet", message.message_id, message.chat, where
            )
            return None

        thread = _thread(history, message.at)
        quiet = _shape_forbids(message, history, thread)
        if quiet is not None:
            logger.info("not answering %s: %s", message.message_id, quiet.reason)
            return None

        decision = await self._classify(message, history)
        if decision is None:
            return None
        if decision.topic is Topic.NO_GO and _already_deflected(thread):
            logger.info("no-go topic already deflected in this thread; dropping it")
            return None

        await self._mark_read(message)

        text = await self._write(message, history, decision.topic)
        if text is None:
            return None
        return await self._send(message, text, decision.topic)

    async def _classify(
        self, message: InboundMessage, history: Sequence[Turn]
    ) -> ReplyDecision | None:
        """Call B: what kind of address this is, or `None` for stay quiet."""
        try:
            decision = await self._brain.ask(
                CallType.REPLY_GATE,
                _gate_prompt(message, history),
                ReplyDecision,
                instructions=RUBRIC,
            )
        except BrainError as exc:
            logger.info("the gate gave no verdict on %s: %s", message.message_id, exc)
            return None

        if decision.confidence < MIN_CONFIDENCE:
            logger.info(
                "the gate is only %.2f sure %s is %s; staying quiet",
                decision.confidence,
                message.message_id,
                decision.topic,
            )
            return None
        return decision

    async def _write(
        self, message: InboundMessage, history: Sequence[Turn], topic: Topic
    ) -> str | None:
        """Call C: the one member-visible generation, validated, or `None`."""
        try:
            reply = await self._brain.ask(
                CallType.REPLY_GENERATION,
                _said(message.author_name, message.text),
                Reply,
                instructions=f"{INSTRUCTIONS}\n\n{GUIDANCE[topic]}",
                message_history=_as_history(history),
            )
        except BrainError as exc:
            logger.info("no reply was generated for %s: %s", message.message_id, exc)
            return None

        try:
            return render(reply, topic)
        except ReplyRejectedError as exc:
            logger.info("dropping the reply to %s: %s", message.message_id, exc)
            return None

    async def _send(self, message: InboundMessage, text: str, topic: Topic) -> SentMessage | None:
        """Through the shared pacer, and then into the window she reads back."""
        try:
            sent = await self._pacer.send(SendKind.REPLY, message.chat, text)
        except SendRefusedError as exc:
            logger.info("the envelope refused the reply to %s: %s", message.message_id, exc)
            return None
        except EvolutionError as exc:
            logger.warning("the reply to %s did not get out: %s", message.message_id, exc)
            return None

        await self._memory.remember(
            Turn(
                chat=message.chat,
                at=sent.at,
                message_id=sent.message_id,
                author=message.rebe,
                author_name="Rebe",
                text=text,
                by_rebe=True,
                reply_to=message.author,
                topic=str(topic),
            )
        )
        logger.info(
            "answered %s in %s (%s)", message.author_name or message.author, message.chat, topic
        )
        return sent

    async def _mark_read(self, message: InboundMessage) -> None:
        """Blue ticks before the answer. Never costs the reply if it fails.

        The receipt is camouflage and the reply is the point, so a transport that
        will not take the receipt is logged and stepped over.
        """
        try:
            await self._reader.mark_read(message.chat, message.message_id)
        except EvolutionError as exc:
            logger.warning("could not mark %s read before replying: %s", message.message_id, exc)


def _turn_of(message: InboundMessage) -> Turn:
    """The inbound message as a row in the window.

    `from_me` deliveries are her own sends coming back through the same webhook -
    a news post, or a reply the send path already wrote down. They are stored as
    *hers*, not as somebody else's line: a news post filed as a member turn would
    read as a stranger talking in the window she is handed, and its id would never
    reach the set that makes "a quote of one of her messages" tier one.
    """
    return Turn(
        chat=message.chat,
        at=message.at,
        message_id=message.message_id,
        author=message.author,
        author_name=message.author_name,
        text=message.text,
        by_rebe=message.from_me,
    )


def _shape_forbids(
    message: InboundMessage, history: Sequence[Turn], thread: Sequence[Turn]
) -> Silence | None:
    """The two conversation-shape rules, from the reply policy.

    Both are about what she has already said, and both fail toward quiet.
    """
    latest = _last_of_hers(history)
    if latest is not None and latest.reply_to == message.author and message.at <= latest.at:
        # Written before her last answer landed, so answering it too would be
        # two of hers in a row to somebody who has not spoken in between.
        return Silence("she answered this person already and they have not spoken since")

    turns = sum(1 for turn in thread if turn.by_rebe)
    if turns >= THREAD_TURNS:
        return Silence(f"{turns} turns into this thread; letting it fade")
    return None


def _thread(history: Sequence[Turn], now: datetime) -> list[Turn]:
    """The turns that belong to the conversation happening right now.

    Walked backwards from `now` until a gap longer than `THREAD_GAP`, so the fade
    is per conversation rather than per group: an hour of silence and the next
    name-tag opens a fresh thread with a fresh allowance.
    """
    thread: list[Turn] = []
    later = now
    for turn in reversed(history):
        if later - turn.at >= THREAD_GAP:
            break
        thread.append(turn)
        later = turn.at
    thread.reverse()
    return thread


def _already_deflected(thread: Sequence[Turn]) -> bool:
    """Has she already said "ni idea" about a no-go topic in this thread?"""
    return any(turn.by_rebe and turn.topic == Topic.NO_GO for turn in thread)


def _last_of_hers(history: Sequence[Turn]) -> Turn | None:
    return next((turn for turn in reversed(history) if turn.by_rebe), None)


def _as_history(history: Sequence[Turn]) -> list[ModelMessage]:
    """The window as the conversation Pydantic AI hands back to the model.

    Section 2.3 of the deployment spec asks for exactly this - the rolling window
    loaded into `message_history` per event - which is also what makes a follow-up
    cost cache-hit tokens rather than a re-explained thread.
    """
    messages: list[ModelMessage] = []
    for turn in history:
        if turn.by_rebe:
            messages.append(ModelResponse(parts=[TextPart(content=turn.text)], timestamp=turn.at))
        else:
            messages.append(
                ModelRequest(
                    parts=[
                        UserPromptPart(
                            content=_said(turn.author_name, turn.text), timestamp=turn.at
                        )
                    ]
                )
            )
    return messages


def _gate_prompt(message: InboundMessage, history: Sequence[Turn]) -> str:
    """The message to classify, with just enough thread to read it in context."""
    recent = list(history)[-GATE_CONTEXT_TURNS:]
    context = "\n".join(_said(turn.author_name, turn.text) for turn in recent)
    head = f"Contexto reciente:\n{context}\n\n" if context else ""
    return f"{head}Mensaje a clasificar:\n{_said(message.author_name, message.text)}"


def _said(name: str, text: str) -> str:
    """One line of the conversation, named the way the group sees it."""
    return f"{name or 'Alguien'}: {text}"
