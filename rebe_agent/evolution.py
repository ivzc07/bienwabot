"""The one place this process talks to Evolution API.

Four calls, the first three from section 3 of `docs/wayfinder/anti-ban-ops-spec.md`:

- `POST /chat/sendPresence/{instance}` - "Rebe is typing".
- `POST /message/sendText/{instance}` - the message itself.
- `POST /chat/markMessageAsRead/{instance}` - the blue ticks before a reply.
- `POST /message/sendMedia/{instance}` - a news post that carries the article's
  own preview image, her words as the caption.

`sendPresence` cannot be asked to only *set* a presence. Its schema makes `delay`
required, and the handler always sets the presence, waits that long, and then
puts the chat back to `paused` - so the hold is Evolution's to execute whatever
we do. What stays here is the decision: the pacer draws the pause from its
jittered distribution and passes it down, which is the part that has to be
testable. Evolution re-asserts the presence itself on holds over twenty seconds;
`TypingProfile` keeps the clamp well under that, so in practice one call covers
one pause.

`sendText` can carry the same `presence` and `delay` and fold typing into the
send. This module still keeps them apart, because the pacer records a send
before it puts it on the wire and that ordering needs the two to be separate
calls.

Nothing here knows about ceilings or quiet hours. Failures raised from this
module mean the message did not get out; a message the *envelope* refused is a
`SendRefusedError` from `rebe_agent.pacer`, and the two are deliberately separate
types so a caller can tell "try later" from "something is broken".
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Protocol

import httpx

from rebe_agent.config import Settings

logger = logging.getLogger("rebe_agent.evolution")

REQUEST_TIMEOUT_SECONDS = 20.0
"""Evolution is one hop away on the internal Docker network; it answers fast or not at all."""

COMPOSING = "composing"
"""Presence shown while Rebe types."""

PAUSED = "paused"
"""Presence sent once the message has landed, so she is not permanently typing."""

RATE_LIMIT_STATUSES = frozenset({429, 463})
"""429 is HTTP's own; 463 is WhatsApp's "reach-out time-lock", surfaced by Baileys.

Section 5 of the deployment spec answers both the same way - back off, alert,
do not keep hammering - so they get one type here.
"""


class EvolutionError(RuntimeError):
    """Evolution did not accept the request. The message did not get out."""

    def __init__(self, action: str, *, status: int | None = None, detail: str = "") -> None:
        self.action = action
        self.status = status
        self.detail = detail
        described = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"Evolution refused to {action}{described}: {detail or 'no detail'}")


class EvolutionRateLimitedError(EvolutionError):
    """WhatsApp is pushing back: a 463 reach-out time-lock or a 429.

    Its own type because the response is not "retry" but "stop sending and tell
    the maintainer" - wired up with the alert channel in a later ticket.
    """


class EvolutionSender(Protocol):
    """What the pacer needs from a transport: type, then send."""

    async def send_presence(self, chat: str, presence: str, hold_seconds: float = 0.0) -> None:
        """Show `presence` in `chat` for `hold_seconds`, then let it fall to paused.

        The call does not return until the hold is over: Evolution runs the wait,
        because its endpoint offers no way to set a presence and leave it set. The
        pacer still decides `hold_seconds`, which is the half of this that has to
        be testable. Raises `EvolutionError` if the transport will not take it.
        """

    async def send_text(self, chat: str, text: str) -> str:
        """Send one text message and answer with the WhatsApp message ID.

        The ID is best-effort: an empty string means the message went out but the
        response did not name it, which is worth a log line and nothing more.
        Raises `EvolutionError` if the message did not get out.
        """

    async def send_media(self, chat: str, media_url: str, caption: str) -> str:
        """Send one image with a caption and answer with the WhatsApp message ID.

        Same contract as `send_text`: the ID is best-effort, and an
        `EvolutionError` means the message did not get out.
        """


class EvolutionReader(Protocol):
    """What the webhook leg needs from a transport: read it before answering it.

    Its own protocol rather than another method on `EvolutionSender`, because the
    pacer has no business marking anything read and nothing that implements the
    send path should have to grow a method it will never call.
    """

    async def mark_read(self, chat: str, message_id: str) -> None:
        """Put the read receipt on one incoming message.

        Baileys needs the message's own key, so `chat` and `message_id` together
        are the smallest thing that identifies it. Raises `EvolutionError` if the
        transport will not take it; the caller treats that as cosmetic, because a
        reply that never went out is worse than a receipt that never showed.
        """

    async def group_roster(self, chat: str) -> dict[str, str]:
        """The group's members as `lid JID -> phone JID`, best-effort.

        WhatsApp's lid addressing means an @-mention arrives as an anonymous
        `...@lid` that shares nothing with the phone JID the envelope names, and
        this roster is the only place the two are written next to each other.
        Entries with no phone number are dropped rather than guessed at. Raises
        `EvolutionError` if the transport will not answer.
        """


class EvolutionClient:
    """A thin, typed client for the one instance this process posts from."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        instance: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._instance = instance
        self._headers = {"apikey": api_key, "Content-Type": "application/json"}
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> EvolutionClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP client, but only the one this object made."""
        if self._owns_client:
            await self._http.aclose()

    async def send_presence(self, chat: str, presence: str, hold_seconds: float = 0.0) -> None:
        """Tell the chat that Rebe is typing, paused, or away, and hold it.

        `delay` is required by the endpoint and is in milliseconds; it is how long
        Evolution keeps the presence up before dropping the chat back to `paused`.
        A zero hold is the honest value for clearing a presence rather than
        showing one, and the schema takes it.
        """
        await self._post(
            f"/chat/sendPresence/{self._instance}",
            {
                "number": chat,
                "presence": presence,
                "delay": max(round(hold_seconds * 1000), 0),
            },
            action=f"set presence {presence!r}",
        )

    async def mark_read(self, chat: str, message_id: str) -> None:
        """Put the read receipt on one incoming message, before Rebe answers it.

        `readMessages` takes a list because Baileys reads by explicit per-message
        key; this leg answers one message at a time, so the list has one entry.
        """
        await self._post(
            f"/chat/markMessageAsRead/{self._instance}",
            {"readMessages": [{"remoteJid": chat, "fromMe": False, "id": message_id}]},
            action="mark a message read",
        )

    async def group_roster(self, chat: str) -> dict[str, str]:
        """The group's members as `lid JID -> phone JID`.

        A GET, unlike everything else here, because that is the verb the endpoint
        answers to. Members Evolution lists without a phone number are left out:
        a mapping to nothing identifies nobody.
        """
        payload = await self._get(
            f"/group/participants/{self._instance}",
            {"groupJid": chat},
            action="fetch the group roster",
        )
        roster: dict[str, str] = {}
        participants = payload.get("participants")
        for member in participants if isinstance(participants, list) else []:
            if not isinstance(member, dict):
                continue
            lid, phone = member.get("id"), member.get("phoneNumber")
            if isinstance(lid, str) and lid and isinstance(phone, str) and phone:
                roster[lid] = phone
        return roster

    async def send_text(self, chat: str, text: str) -> str:
        """Send one text message, and answer with the WhatsApp message ID."""
        payload = await self._post(
            f"/message/sendText/{self._instance}",
            {"number": chat, "text": text},
            action="send a message",
        )
        return _message_id(payload)

    async def send_media(self, chat: str, media_url: str, caption: str) -> str:
        """Send one image with a caption, and answer with the WhatsApp message ID.

        `media` is a URL rather than bytes: WhatsApp's servers fetch the image
        themselves, which is also why the URL must be absolute and fetchable -
        the check `rebe_agent.preview` makes before one ever gets this far.
        """
        payload = await self._post(
            f"/message/sendMedia/{self._instance}",
            {"number": chat, "mediatype": "image", "media": media_url, "caption": caption},
            action="send a photo",
        )
        return _message_id(payload)

    async def _post(self, path: str, body: dict[str, Any], *, action: str) -> dict[str, Any]:
        try:
            response = await self._http.post(
                f"{self._base_url}{path}", json=body, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise EvolutionError(action, detail=str(exc)) from exc
        return self._answer(response, action)

    async def _get(self, path: str, params: dict[str, str], *, action: str) -> dict[str, Any]:
        try:
            response = await self._http.get(
                f"{self._base_url}{path}", params=params, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise EvolutionError(action, detail=str(exc)) from exc
        return self._answer(response, action)

    def _answer(self, response: httpx.Response, action: str) -> dict[str, Any]:
        if response.status_code in RATE_LIMIT_STATUSES:
            raise EvolutionRateLimitedError(
                action, status=response.status_code, detail=_describe(response)
            )
        if response.status_code >= 400:
            raise EvolutionError(action, status=response.status_code, detail=_describe(response))
        return _payload(response)


def _message_id(payload: dict[str, Any]) -> str:
    """The WhatsApp message ID, best-effort.

    An empty string means the message went out but the response did not name
    it, which is worth a log line and nothing more.
    """
    key = payload.get("key")
    if isinstance(key, dict) and key.get("id") is not None:
        return str(key["id"])
    return ""


def _describe(response: httpx.Response) -> str:
    """Whatever the body says, trimmed - it goes into a log line, not a report."""
    return response.text.strip()[:500]


def _payload(response: httpx.Response) -> dict[str, Any]:
    """Evolution answers JSON; a body that is not an object is not worth guessing at."""
    try:
        decoded = response.json()
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def build_client(
    settings: Settings, *, http_client: httpx.AsyncClient | None = None
) -> EvolutionClient:
    """The client for whichever instance `EVOLUTION_INSTANCE` names.

    Failover is a configuration change - point this at `bien-backup` and redeploy
    - so the instance is read here and nowhere else.
    """
    return EvolutionClient(
        settings.evolution_api_url,
        settings.evolution_api_key.get_secret_value(),
        settings.evolution_instance,
        http_client=http_client,
    )
