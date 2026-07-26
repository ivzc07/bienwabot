"""The recorded Evolution webhook payloads, and small edits to them.

The gate's tier decisions are asserted against real `messages.upsert` bodies
rather than against hand-built objects, because the shape of that body is the one
part of this leg nobody here controls: a field renamed upstream should break a
test, not a group.

`edited` exists so a test that is about one field - a different author, a later
timestamp, other wording - says only that, instead of copying a whole payload.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from tests.support import fixture

REBE = "5215551234567@s.whatsapp.net"
"""The number `bien-rebe` is paired to, as Evolution reports it in `sender`."""

ANA = "5215559876543@s.whatsapp.net"
BETO = "5215551112222@s.whatsapp.net"

AT_EPOCH = 1_785_002_400
"""`messageTimestamp` on every recorded payload: 2026-07-25 12:00 in Mexico City."""

CAPTIONED = ("imageMessage", "videoMessage", "documentMessage")
"""Media whose readable words are a caption rather than a message body."""


def payload(name: str) -> dict[str, Any]:
    """One recorded webhook body, as Evolution POSTs it."""
    decoded: dict[str, Any] = json.loads(fixture(f"webhook_{name}.json"))
    return decoded


def edited(
    name: str,
    *,
    text: str | None = None,
    author: str | None = None,
    message_id: str | None = None,
    at_epoch: int | None = None,
    quoted_id: str | None = None,
    quoted_author: str | None = None,
    mentioned: list[str] | None = None,
) -> dict[str, Any]:
    """A recorded payload with one or two fields changed."""
    body = deepcopy(payload(name))
    data = body["data"]
    if text is not None:
        _set_text(data["message"], text)
    if mentioned is not None:
        data["message"]["extendedTextMessage"]["contextInfo"]["mentionedJid"] = mentioned
    if author is not None:
        data["key"]["participant"] = author
    if message_id is not None:
        data["key"]["id"] = message_id
    if at_epoch is not None:
        data["messageTimestamp"] = at_epoch
    if quoted_id is not None:
        data["message"]["extendedTextMessage"]["contextInfo"]["stanzaId"] = quoted_id
    if quoted_author is not None:
        data["message"]["extendedTextMessage"]["contextInfo"]["participant"] = quoted_author
    return body


def _set_text(message: dict[str, Any], text: str) -> None:
    """Put `text` wherever this kind of message keeps its readable words.

    On a photo that is the caption, which is how "the same media, this time with
    something to read" is written as one edit rather than a second fixture.
    """
    if "conversation" in message:
        message["conversation"] = text
        return
    if "extendedTextMessage" in message:
        message["extendedTextMessage"]["text"] = text
        return
    media = next((name for name in CAPTIONED if name in message), None)
    if media is None:
        raise AssertionError(f"no text to edit in {sorted(message)}")
    message[media]["caption"] = text
