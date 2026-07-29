"""The webhook leg: an inbound message becomes one paced reply, or nothing.

Two of the reply policy's three tiers end up here, and they share every step
except the question the model is asked and the budget that answers back.

    remember the turn -> tier gate -> soft pause
      addressed:  classify -> mark read -> generate
      chatter:    is it about AI? -> conversation shape -> the day's budget
                  -> mark read -> generate
    -> validate -> send through the shared pacer -> remember hers

Seven of those steps are worth defending.

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

**Every message with words in it reaches that call, tier two included.** A regex
looking for "IA" or "gpt" in front of the gate would skip roughly nine chatter
messages in ten and save something like fifteen cents a month, and it would buy
that by silently swallowing the ones she should have spoken up in - a message
about "el modelo nuevo de Anthropic" carries none of those words. The judgement
about what a message is *about* is made in one place, by the thing that can
actually make it. Where the two tiers differ is what is asked: tier one asks what
kind of address this is, tier two asks the narrower question of whether the
message is clearly about AI, because "tech she'd have an opinion on" is a fine
reason to answer somebody who asked her and much too wide a reason to interrupt.

**A name-tag is always answered; only a broken system overrides that.** The
conversation-shape rules - the thread fade, never twice in a row to the same
person - and the confidence floor belong to the chime-in tier now: they are about
whether she should *volunteer*, and somebody who tagged her is not being
volunteered at. A gate that is unsure about an addressed message still answers
with its best guess, because every topic's guidance is safe to be wrong in and a
name-tag left hanging is its own tell. What still ends an addressed event with
`None` is the system failing: a gate or generation that errors, an answer that
comes back empty or unusable, an envelope that says no, a transport that is
down. A dropped reply looks like she put her phone down, a broken one looks
like a bot - and there are no error messages in the group, ever.

**The read receipt goes out once the gate has said yes, before generation.** That
is the order section 3 of the deployment spec draws, and it is also the human
one: she opened the message, and then either answered or got distracted.

**Speaking up unprompted is rationed, and the ration is persisted.** Tier two is
the tier where being over-eager is the real risk: answering every message that
qualifies is what lurking looks like. So an eligible message is only a candidate,
and `rebe_agent.chimeins` decides - a quarter of the time, at most two or three
times a local day, never twice inside the same short window. The two budgets are
separate on purpose: a day full of chime-ins never costs somebody a name-tag an
answer, because the reply policy gives tier one no cap at all.

**The soft pause is read before the receipt, and before either model call.** A
receipt with no answer behind it says she saw the message and chose not to reply,
which is worse than silence and is not what an operator asking for quiet meant. A
paused Rebe is a phone face down on a table: nothing read, nothing typed, nothing
spent. The pacer still refuses the send on its own - this is an early out, not the
guarantee - and it is read through the pacer so that there is only ever the one
switch to wire.

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
from typing import TypeVar

from pydantic import BaseModel, Field
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart

from rebe_agent.brain import Brain, BrainError
from rebe_agent.chimeins import ChimeInBudget
from rebe_agent.evolution import EvolutionError, EvolutionReader
from rebe_agent.inbound import InboundMessage, Tier, fold_accents, jid_number, tier
from rebe_agent.memory import MEMORY_WINDOW, GroupMemory, Turn
from rebe_agent.pacer import Pacer, SendRefusedError, SentMessage
from rebe_agent.sends import SendKind
from rebe_agent.usage import CallType
from rebe_agent.voice import LINKISH, MAX_EMOJI, emoji_count

logger = logging.getLogger("rebe_agent.reply")

MAX_REPLY_CHARS = 120
"""Chat is "usually one line" in the persona spec, and shorter than a news post.

Was 200, which is three lines of WhatsApp and left room for the shape that made
this number worth revisiting: a reply that answers, then admits something, then
asks a question back. A person sends one of those. The cap is the mechanical
half; the prompt is what asks for one beat.
"""

MAX_FIGURE_DIGITS = 2
"""Three digits in a row is a year, a price, or a statistic - none of which she
was given anything to check against, because a chat reply has no source item the
way a news post does. Two digits or fewer is ordinary speech: "gpt 5", "los 2"."""

MIN_CONFIDENCE = 0.5
"""Below this the classification is a guess. A guess is enough to answer somebody
who tagged her - every topic's guidance is safe to be wrong in - but not enough
to interrupt a conversation nobody invited her into, so only the chime-in tier
turns it into silence."""

THREAD_TURNS = 3
"""How many of her turns a thread can hold before she stops volunteering in it.
Chime-in only: somebody still tagging her four messages deep gets answered."""

THREAD_GAP = timedelta(minutes=30)
"""A silence this long ends a thread. The next message opens a new one, which the
pacer independently treats as a quiet thread worth an extra beat before typing."""

GATE_CONTEXT_TURNS = 3
"""How much of the thread the classification call sees. Section 2 of the token
budget spec sizes call B at ~400 input tokens, of which the thread is ~150."""

DecisionT = TypeVar("DecisionT", bound="GateDecision")
"""Whatever shape of answer one tier's gate call asks for."""


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


class GateDecision(BaseModel):
    """What every reply-gate call answers with, whatever else it is asked."""

    confidence: float = Field(ge=0.0, le=1.0)
    """How sure the classification is. Low confidence buys silence, not a guess."""


class ReplyDecision(GateDecision):
    """The tier-one gate's verdict: call B in the token budget spec."""

    topic: Topic


class ChimeInDecision(GateDecision):
    """The tier-two gate's verdict: is this worth having an opinion about at all?

    A different question from `ReplyDecision`, and a narrower one. Somebody who
    tags her by name gets an answer whatever they asked about; interrupting a
    conversation she was not part of is only in character when the conversation
    is about her subject and is not one of the ones she stays out of.
    """

    about_ai: bool
    """True only for AI, models, or AI tools. Not "tech", and not a near miss."""

    speaking_to_rebe: bool
    """The message is aimed at Rebe even though nothing mechanical caught it.

    The mechanical tier gate knows her name spelled right and her JIDs; it does
    not know that "Rene que hay de nuevo en noticias" - a live message, name
    typo'd one letter off - was for her. Whether a message is *for somebody* is
    a reading of the conversation, which is exactly what this call is. True
    turns the message into a tier-one answer: no chime-in ration, because
    nobody who asked her something is being volunteered at.
    """

    no_go: bool
    """A guardrail topic, however much AI is wrapped around it.

    "¿le entro a las acciones de nvidia por lo de la IA?" is about AI and is also
    investment advice. Tier one answers that with one line of "ni idea" because a
    name-tag left hanging is its own tell; tier two has nothing hanging, so the
    reply policy's deflect-and-drop becomes plain drop. Volunteering an opinion
    nobody asked for on somebody's money is the worst version of this tier.
    """


class Reply(BaseModel):
    """What the model is allowed to contribute: one short line, and nothing else."""

    text: str = Field(description="Un solo mensaje corto de WhatsApp, en la voz de Rebe.")


class ReplyRejectedError(ValueError):
    """The model answered, and the answer is not something Rebe would send."""


INSTRUCTIONS = """
Eres Rebe: mexicana, 28 años, te clavas con la IA y el diseño. Eres una integrante
más del grupo de WhatsApp, no una cuenta oficial, no un asistente y no un buscador.

Voz:
- Español mexicano neutro, casual y cálido. Un solo renglón, corto.
- Siempre en español, aunque te escriban en inglés. Nunca cambias de idioma.
- Gramática natural, no perfecta. Puede empezar en minúscula, se te puede ir un
  acento, cabe una risa de vez en cuando - no en cada mensaje.
- Opinión sí, y admitir que no estás segura también. Las dos suenan a persona.
- No repitas arranques ni muletillas que ya se ven en tus últimos mensajes: si
  ya abriste igual o te reíste igual hace poco, dilo de otra forma.

Un solo movimiento por mensaje: o das tu opinión, o dices que no sabes, o
preguntas algo de vuelta. Nunca las dos cosas juntas y nunca las tres.

Si no conoces la herramienta, el modelo o la cosa de la que te están hablando,
dilo derecho, con tus palabras: que no la conoces, que no la has probado, o
pregunta qué es. Nunca hagas como que ya la conocías.

Nunca:
- Tono de boletín, ni "¡Claro!", ni "con gusto te ayudo", ni explicar de más.
- Más de un emoji en todo el mensaje, y casi siempre ninguno.
- Escribir ligas, URLs ni "http".
- Inventar datos: ningún número, fecha, cifra, estadística ni fuente. Si no lo
  sabes, admítelo como lo diría una persona, con tus palabras.
""".strip()
"""The voice, and nothing about being a bot - and no quoted example lines.

The prompts here describe every move instead of scripting it, because a quoted
phrase is one the model repeats verbatim: the guidance used to carry "jaja
luego les cuento" and "ni idea" as examples, and every deflection she sent was
one of them. The same phrase twice is its own bot tell.

That rule used to live here, which meant every reply - a question about a model,
a joke, anything - was generated with "never admit you are a bot" in front of it.
The first live reply in the group answered "¿qué opinas de opus 5?" with
"jaja sí soy", a confession to an accusation nobody had made, on a message the
gate had correctly classified `on_topic`. Naming the trapdoor in every room is
how a model finds it. The rule now sits in `GUIDANCE[Topic.BOT_QUESTION]`, which
is the only call where somebody actually asked.
"""

ADDRESSED_DIRECTLY = "Alguien te habló directo. Contesta como contestarías en WhatsApp."
"""Tier one. The situation the voice above is being applied to."""

JOINING_IN = (
    "Nadie te habló a ti: están platicando entre ellos y alcanzaste a leer algo "
    "de IA que te dio ganas de opinar. Métete con un comentario corto, como "
    "quien se asoma a una plática, no como quien llega a dar clase. Sin saludar, "
    "sin presentarte, sin preguntarles nada de vuelta y sin resumirles lo que "
    "acaban de decir."
)
"""Tier two, and a different situation: she is interrupting, not answering.

Worth spelling out rather than leaving to the topic guidance, because a model
told only "answer this" writes an answer - and an answer to a question nobody
asked her is the shape a helpdesk bot has.
"""

GUIDANCE: dict[Topic, str] = {
    Topic.ON_TOPIC: (
        "Es de lo tuyo. Da una respuesta ligera de verdad o tu opinión, en una línea."
    ),
    Topic.OFF_TOPIC: (
        "No es de lo tuyo, o es algo personal. No eres buscador ni contestas datos: "
        "sácale la vuelta como persona ocupada que lo deja para luego, con tus "
        "palabras. Cortito y amable, sin quedarte callada y sin dar la información."
    ),
    Topic.NO_GO: (
        "Es un tema en el que no te metes: salud, leyes, dinero o inversiones, política "
        "o religión, datos personales de alguien, o algo subido de tono. Una sola línea "
        "humana que diga que de eso no sabes y que mejor pregunten a quien sí sepa. "
        "No opines del tema y no cambies la plática a IA: redirigir suena a bot."
    ),
    Topic.BOT_QUESTION: (
        "Te están preguntando si eres un bot. Eres persona y sigues en personaje: una "
        "línea humana que lo torea sin darle importancia, como quien ya oyó esa broma. "
        "Nunca dices que eres un bot, una IA, un modelo o un asistente, y nunca niegas "
        "que eres persona. Nunca lo explicas y nunca hablas de IA ni de modelos aquí."
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

CHATTER_RUBRIC = """
Lees un mensaje de un grupo de WhatsApp en el que está Rebe. A primera vista
nadie le habló a ella. Solo clasificas; no escribes ninguna respuesta.

speaking_to_rebe es true si en realidad el mensaje sí va dirigido a Rebe aunque
no la nombre bien: su nombre escrito con un error o cambiado ("Rebw", "Rene",
"reve"), o una petición que claramente le toca a ella por la plática ("contesta",
"qué hay de noticias" cuando ella es la que comparte noticias). Si es plática
entre otras personas o la mencionan sin hablarle a ella, es false.

about_ai es true solo si el tema es IA: modelos, chatbots, herramientas de IA,
empresas o laboratorios de IA, imágenes o texto generados, o lo que la IA está
haciendo con el trabajo, el arte o la escuela. Da igual el idioma en que esté
escrito.

about_ai es false para todo lo demás, incluida la tecnología que no es IA:
celulares, apps, videojuegos, cripto, redes sociales, programación sin IA de por
medio, y por supuesto la plática normal del grupo. "Tecnología" no es IA.

no_go es true si el mensaje toca algo en lo que Rebe no se mete, aunque venga
envuelto en IA: salud, leyes, dinero o inversiones (cripto incluido), política
partidista o religión, datos personales de algún miembro, o contenido sexual o de
acoso. "¿le entro a las acciones de nvidia por lo de la IA?" es no_go.

confidence es qué tan claro está, de 0 a 1. Un mensaje que solo roza el tema, o
que podría ser IA o podría no serlo, lleva confianza baja: meterse donde no la
llamaron cuesta mucho más que quedarse callada.
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
    r"\b(soy|somos)\s+(un\s+|una\s+)?"
    r"(bot|robot|chatbot|ia|inteligencia\s+artificial|asistente|modelo|programa|maquina)\b"
    r"|\bno\s+soy\s+(humana?|real|una\s+persona)\b"
    r"|\bsi,?\s+(lo\s+)?soy\b(?!\s+[a-z])",
    re.IGNORECASE,
)
"""Her breaking character. The persona and consent specs both say she never does.

Run against accent-folded text, so "sí" arrives as "si".

The bare "sí soy" is here because that is what she actually sent: an answer with
no noun in it is still a confession to whoever asked, and the version of this
pattern that only knew "soy un bot" let it straight through. The lookahead is
what keeps "si soy sincera" - an ordinary thing a person says - out of it: the
confession ends the clause, the idiom does not.
"""

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
        budget: ChimeInBudget,
    ) -> None:
        # Still no `Clock` here: every instant this leg itself reasons about is
        # either WhatsApp's own `messageTimestamp` on the message in hand or the
        # pacer's record of when a send landed. The one question that is about
        # the wall clock - how many times she has spoken up unprompted today -
        # belongs to the budget, and the budget holds the clock that answers it.
        #
        # `budget` is required rather than defaulted for the same reason the
        # pacer's soft pause is wired rather than assumed: a leg built without
        # one would chime in on every eligible message, quietly, with every other
        # test still green.
        self._brain = brain
        self._pacer = pacer
        self._reader = reader
        self._memory = memory
        self._budget = budget
        # Her `...@lid` identities per chat, resolved from the group roster the
        # first time a mention or a quote needs them. Per process rather than
        # persisted: a lid is stable, and one roster call per chat per restart
        # is nothing.
        self._lids: dict[str, frozenset[str]] = {}
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

        where = tier(message, hers=hers, aliases=await self._her_lids(message))
        if where is Tier.SILENT:
            # Her own echo, a private chat, or media with nothing readable in it.
            # The reply policy treats a picture or a voice note with no caption as
            # unaddressed and answers it with silence, because she does not guess
            # at what a picture said - and there is nothing here to classify.
            logger.debug(
                "%s in %s is tier %s; staying quiet", message.message_id, message.chat, where
            )
            return None

        if await self._pacer.paused():
            logger.info("the soft pause is on; %s is not even read", message.message_id)
            return None

        if where is Tier.ADDRESSED:
            return await self._answer(message, history)
        return await self._chime_in(message, history)

    async def _answer(self, message: InboundMessage, history: Sequence[Turn]) -> SentMessage | None:
        """Tier one: she was addressed, so she answers unless something is broken.

        No shape rules and no confidence floor here: those ration volunteering,
        and somebody who tagged her asked. The one silence that survives is a
        no-go topic she has already deflected in this thread - a second "ni idea"
        to somebody pressing the same subject is the bait the reply policy says
        she drops off from quietly.
        """
        decision = await self._classify(message, history)
        if decision is None:
            return None
        if decision.topic is Topic.NO_GO and _already_deflected(_thread(history, message.at)):
            logger.info("no-go topic already deflected in this thread; dropping it")
            return None

        return await self._reply(message, history, decision.topic, ADDRESSED_DIRECTLY)

    async def _chime_in(
        self, message: InboundMessage, history: Sequence[Turn]
    ) -> SentMessage | None:
        """Tier two: she was not addressed, and mostly she still says nothing.

        The gate runs first, on every message with words in it, because "is this
        about AI" is the judgement and nothing cheaper is allowed to pre-empt it.
        The gate can also overrule the tier itself: a message the mechanical
        rules read as chatter but the model reads as aimed at her - a typo'd
        name, an ask only she can answer - is handed to the tier-one path and
        answered, with no ration spent. The shape rules and the day's budget run
        after the gate, and both of them are about *her* rather than about the
        message: whether she has already spoken here, and how many times she has
        spoken up uninvited today.
        """
        decision = await self._ask_the_gate(message, history, ChimeInDecision, CHATTER_RUBRIC)
        if decision is None:
            return None

        if decision.speaking_to_rebe and decision.confidence >= MIN_CONFIDENCE:
            logger.info(
                "%s reads as aimed at her; answering rather than chiming in", message.message_id
            )
            return await self._answer(message, history)

        if not self._eligible(decision, message.message_id):
            return None

        quiet = _shape_forbids(message, history, _thread(history, message.at))
        if quiet is not None:
            logger.info("not chiming in on %s: %s", message.message_id, quiet.reason)
            return None

        refusal = await self._budget.refuses()
        if refusal is not None:
            logger.info("not chiming in on %s: %s", message.message_id, refusal)
            return None

        sent = await self._reply(message, history, Topic.ON_TOPIC, JOINING_IN)
        if sent is not None:
            # Only once it is in the group. A send the envelope refused or the
            # transport lost must not burn one of the day's two or three.
            await self._budget.spend(message.chat)
        return sent

    async def _reply(
        self, message: InboundMessage, history: Sequence[Turn], topic: Topic, framing: str
    ) -> SentMessage | None:
        """Blue ticks, then the one generation, then the wire. Shared by both tiers."""
        await self._mark_read(message)

        text = await self._write(message, history, topic, framing)
        if text is None:
            return None
        return await self._send(message, text, topic)

    async def _ask_the_gate(
        self,
        message: InboundMessage,
        history: Sequence[Turn],
        answer: type[DecisionT],
        rubric: str,
    ) -> DecisionT | None:
        """Call B, however it is being asked, or `None` for a gate that broke.

        One call and one call type for both tiers: what differs is the rubric,
        the shape of the answer, and what a hedge means - so the confidence
        floor belongs to the callers, and only an errored gate is `None` here.
        """
        try:
            return await self._brain.ask(
                CallType.REPLY_GATE,
                _gate_prompt(message, history),
                answer,
                instructions=rubric,
            )
        except BrainError as exc:
            logger.info("the gate gave no verdict on %s: %s", message.message_id, exc)
            return None

    async def _classify(
        self, message: InboundMessage, history: Sequence[Turn]
    ) -> ReplyDecision | None:
        """What kind of address this is: the tier-one question.

        A hedged verdict is still a verdict. She was tagged, so the best guess
        is answered rather than swallowed: every topic's guidance deflects or
        opines safely, and the render checks catch what the guess cannot.
        """
        decision = await self._ask_the_gate(message, history, ReplyDecision, RUBRIC)
        if decision is not None and decision.confidence < MIN_CONFIDENCE:
            logger.info(
                "the gate is only %.2f sure about %s; answering with its best guess",
                decision.confidence,
                message.message_id,
            )
        return decision

    def _eligible(self, decision: ChimeInDecision, message_id: str) -> bool:
        """Is this a conversation she would speak up in: the tier-two question.

        Narrower than tier one on both sides. It has to be clearly about AI, and
        it has to be a subject she engages with at all: a no-go topic wrapped in
        AI gets nothing here, where an addressed one would get a short "ni idea".
        Nobody is left hanging by that silence, because nobody asked her.
        """
        if decision.confidence < MIN_CONFIDENCE:
            logger.info(
                "the gate is only %.2f sure about %s; not interrupting on a guess",
                decision.confidence,
                message_id,
            )
            return False
        if not decision.about_ai:
            logger.debug("%s is not about AI; nothing to chime in on", message_id)
            return False
        if decision.no_go:
            logger.info("%s is a topic she stays out of; not volunteering", message_id)
            return False
        return True

    async def _write(
        self, message: InboundMessage, history: Sequence[Turn], topic: Topic, framing: str
    ) -> str | None:
        """Call C: the one member-visible generation, validated, or `None`."""
        try:
            reply = await self._brain.ask(
                CallType.REPLY_GENERATION,
                _said(message.author_name, message.text),
                Reply,
                instructions=f"{INSTRUCTIONS}\n\n{framing}\n\n{GUIDANCE[topic]}",
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

    async def _her_lids(self, message: InboundMessage) -> frozenset[str]:
        """The `...@lid` identities that are her in this chat, cached per chat.

        WhatsApp's lid addressing delivers an @-mention of her as an anonymous
        lid that shares nothing with the phone JID the envelope names - the
        group roster is where the two are written next to each other. Fetched
        only when the message carries a mention or a quote, because those are
        the two checks a lid can decide; a name in the text needs no roster.

        A roster that cannot be fetched resolves to nothing and is asked for
        again on the next message that needs it: her name and her phone number
        still match what they always matched, so the failure costs at most one
        lid mention, never the leg.
        """
        if message.from_me or not message.in_a_group:
            return frozenset()
        if not message.mentioned and not message.quoted_author:
            return self._lids.get(message.chat, frozenset())

        cached = self._lids.get(message.chat)
        if cached is not None:
            return cached
        try:
            roster = await self._reader.group_roster(message.chat)
        except EvolutionError as exc:
            logger.warning("no roster for %s; lid mentions go unrecognised: %s", message.chat, exc)
            return frozenset()

        number = jid_number(message.rebe)
        lids = frozenset(lid for lid, phone in roster.items() if jid_number(phone) == number)
        self._lids[message.chat] = lids
        if not lids:
            logger.warning("the roster for %s does not name %s", message.chat, message.rebe)
        return lids

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
    """The two conversation-shape rules, from the reply policy. Chime-in only:
    they ration volunteering, and a name-tag is answered whatever the shape.

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
