"""What Evolution posts at the webhook, and which of the three tiers it falls in.

Three steps, kept apart on purpose.

`parse` turns a `messages.upsert` body into an `InboundMessage`, or into nothing.
It is the only place in the codebase that knows Baileys' field names, and it
trusts none of them: the body arrives on an open port, so every field is read
defensively and anything that is not shaped like an inbound group message simply
becomes `None`.

`tier` is the reply gate from section "The reply gate" of
`docs/wayfinder/reply-policy-spec.md`, and it is **mechanical**. Whether a
message @-mentions Rebe, names her, or quotes something she wrote is a fact about
the payload, not a judgement, so no model is asked. That matters twice: the
tier-one promise ("she always answers when addressed") cannot be broken by a bad
classification, and the decision is testable against recorded payloads.

The three tiers:

- `ADDRESSED` - a name-tag in any of its forms. This leg answers these.
- `CHATTER` - readable text she was not addressed by. The unaddressed chime-in is
  its own ticket, so this leg stays quiet and only remembers the turn.
- `SILENT` - nothing to answer: her own echo, a sticker or voice note with no
  words, or a private chat, which the consent spec says she never replies in.

`parse_connection` is the other event each instance subscribes to, and the only
other one this codebase acts on: `connection.update`, which says whether the
WhatsApp link is usable. It is read as defensively as the messages are, and what
a state change *means* is decided in `rebe_agent.signals` rather than here.

Rebe's own number is read from the envelope's `sender`, which Evolution fills
with the JID the instance is paired to. Nothing else has to be configured for a
mention to be recognised, and a failover to `bien-backup` - a different number -
keeps working without an edit.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Container, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

MESSAGE_EVENT = "messages.upsert"
"""The Evolution event the reply leg acts on."""

CONNECTION_EVENT = "connection.update"
"""The Evolution event that says whether the WhatsApp link is usable."""

_REASON_FIELDS = ("statusReason", "statusCode")
"""Where Evolution puts Baileys' disconnect reason, newest spelling first."""

GROUP_SUFFIX = "@g.us"
"""What makes a JID a group rather than a person."""

_NESTED = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "documentWithCaptionMessage")
"""Wrappers Baileys puts around a real message. The payload nests one inside."""

_CAPTIONED = ("imageMessage", "videoMessage", "documentMessage")
"""Media that can carry words. Media without any is treated as silence."""

_MAX_UNWRAP = 4
"""How deep the wrappers are followed. A cycle in a hostile body ends here."""

_HER_NAME = re.compile(r"\b(rebe|rebeca)\b", re.IGNORECASE)
"""Her name as a whole word. "rebeldes" is not a name-tag."""


class Tier(StrEnum):
    """Which of the reply policy's three tiers a message falls in."""

    ADDRESSED = "addressed"
    """Tier one: @-mentioned, named, or quoted. She always answers these."""

    CHATTER = "chatter"
    """Tier two: words she was not addressed by. The chime-in ticket owns these."""

    SILENT = "silent"
    """Tier three: nothing a person would answer, so nothing happens."""


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One group message, with the fields the gate and the reply leg read."""

    chat: str
    """The group's JID, and the chat any reply goes back to."""

    message_id: str
    """WhatsApp's own id. The duplicate-delivery key, and the read-receipt key."""

    author: str
    """Who wrote it, as a JID."""

    author_name: str
    """Their WhatsApp display name, for the model's benefit rather than for logic."""

    text: str
    """The readable words, caption included. Empty means media with nothing to read."""

    at: datetime
    """When WhatsApp says it was sent, in UTC."""

    from_me: bool
    """True when this is Rebe's own send, echoed back through the same webhook."""

    mentioned: frozenset[str]
    """JIDs the message @-mentions."""

    quoted_id: str
    """The id of the message this one replies to, or empty."""

    quoted_author: str
    """Who wrote the quoted message, or empty."""

    rebe: str
    """The JID this Evolution instance is paired to, from the envelope's `sender`."""

    @property
    def in_a_group(self) -> bool:
        return self.chat.endswith(GROUP_SUFFIX)


@dataclass(frozen=True, slots=True)
class ConnectionUpdate:
    """Evolution's word on the WhatsApp link, and Baileys' reason if it named one."""

    state: str
    """Baileys' own word: `open`, `close`, `connecting`. Read, never trusted."""

    reason: int | None = None
    """The disconnect reason, where there was one. 401 and 403 read as bans."""


def parse(body: Mapping[str, Any]) -> InboundMessage | None:
    """One webhook body as an `InboundMessage`, or `None` if it is not one.

    `None` covers every uninteresting case together - a `connection.update`, a
    body with no `data`, a receipt, a hostile POST - because the caller does the
    same thing with all of them: nothing. The connection events it turns down are
    picked up by `parse_connection`.
    """
    if body.get("event") != MESSAGE_EVENT:
        return None

    data = body.get("data")
    if not isinstance(data, dict):
        return None
    key = data.get("key")
    if not isinstance(key, dict):
        return None

    chat = _text_of(key.get("remoteJid"))
    message_id = _text_of(key.get("id"))
    at = _moment(data.get("messageTimestamp"))
    if not chat or not message_id or at is None:
        return None

    content = _unwrap(data.get("message"))
    context = _mapping(content.get("extendedTextMessage")).get("contextInfo")
    context = context if isinstance(context, dict) else {}

    return InboundMessage(
        chat=chat,
        message_id=message_id,
        # A group message names its author separately; a direct one is from the
        # chat itself. Either way the reply and the shape rules want a person.
        author=_text_of(key.get("participant")) or chat,
        author_name=_text_of(data.get("pushName")),
        text=_readable(content),
        at=at,
        from_me=bool(key.get("fromMe")),
        mentioned=frozenset(
            _text_of(jid) for jid in _sequence(context.get("mentionedJid")) if _text_of(jid)
        ),
        quoted_id=_text_of(context.get("stanzaId")),
        quoted_author=_text_of(context.get("participant")),
        rebe=_text_of(body.get("sender")),
    )


def parse_connection(body: Mapping[str, Any]) -> ConnectionUpdate | None:
    """One webhook body as a `ConnectionUpdate`, or `None` if it is not one.

    A body with no readable state is nothing rather than a disconnect: guessing
    that a malformed delivery meant "the link is down" would let anything that
    can reach the port stop Rebe sending.
    """
    if body.get("event") != CONNECTION_EVENT:
        return None

    data = _mapping(body.get("data"))
    state = _text_of(data.get("state"))
    if not state:
        return None
    return ConnectionUpdate(state=state, reason=_reason(data))


def _reason(data: Mapping[str, Any]) -> int | None:
    """Baileys' disconnect reason, however Evolution's build spells it."""
    for name in _REASON_FIELDS:
        value = data.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    return None


def tier(message: InboundMessage, *, hers: Container[str]) -> Tier:
    """Which tier this message falls in. `hers` is the ids of Rebe's own messages.

    Quoting is checked against what she remembers sending as well as against the
    quoted author's JID, so a reply to her still counts when Evolution omits the
    author or when the instance's own number is not in the envelope.
    """
    if message.from_me or not message.in_a_group or not message.text.strip():
        return Tier.SILENT
    if _addressed(message, hers):
        return Tier.ADDRESSED
    return Tier.CHATTER


def _addressed(message: InboundMessage, hers: Container[str]) -> bool:
    """The three forms of a name-tag, from the reply policy's tier one."""
    if message.quoted_id and message.quoted_id in hers:
        return True
    number = _number(message.rebe)
    if number and _number(message.quoted_author) == number:
        return True
    if number and any(_number(jid) == number for jid in message.mentioned):
        return True
    if number and f"@{number}" in message.text:
        return True
    return _HER_NAME.search(fold_accents(message.text)) is not None


def _number(jid: str) -> str:
    """The phone number inside a JID, without the device suffix or the domain.

    `5215551234567:12@s.whatsapp.net` and `5215551234567@s.whatsapp.net` are the
    same person on two of their devices, and a mention carries whichever the
    sender's client had to hand.
    """
    return jid.split("@", 1)[0].split(":", 1)[0].strip()


def fold_accents(text: str) -> str:
    """The text with its accents folded away, so "Rebé" reads as "rebe".

    Shared with the reply leg, which matches the same words against what the
    model wrote: a rule that "máquina" slips past is not a rule.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _unwrap(message: object) -> Mapping[str, Any]:
    """The real message inside however many Baileys wrappers it arrived in."""
    current = _mapping(message)
    for _ in range(_MAX_UNWRAP):
        nested = next((current[name] for name in _NESTED if name in current), None)
        if nested is None:
            return current
        inner = _mapping(nested)
        current = _mapping(inner.get("message")) or inner
    return current


def _readable(content: Mapping[str, Any]) -> str:
    """Whatever a member would read: a message, or the caption on a photo.

    Media with no caption comes back empty, which the gate turns into silence:
    the reply policy is explicit that she does not guess at what a picture or a
    voice note said.
    """
    conversation = _text_of(content.get("conversation"))
    if conversation:
        return conversation

    extended = _text_of(_mapping(content.get("extendedTextMessage")).get("text"))
    if extended:
        return extended

    for name in _CAPTIONED:
        caption = _text_of(_mapping(content.get(name)).get("caption"))
        if caption:
            return caption
    return ""


def _moment(value: object) -> datetime | None:
    """Baileys' epoch seconds as an instant. Strings happen; so do absences."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _sequence(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _text_of(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
