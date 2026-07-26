"""The one place this process talks to Evolution API.

Three calls, all from section 3 of `docs/wayfinder/anti-ban-ops-spec.md`:

- `POST /chat/sendPresence/{instance}` - "Rebe is typing".
- `POST /message/sendText/{instance}` - the message itself.
- `POST /chat/markMessageAsRead/{instance}` - the blue ticks before a reply.

Evolution's `sendText` can carry `presence` and `delay` and do the typing pause
for you. This module deliberately does not use that: the playbook wants the
presence *refreshed* mid-pause (Baileys expires it after about ten seconds) and
the pause drawn from a jittered distribution the pacer owns. A transport that
sleeps on our behalf can do neither, and it would put the one timing decision
that has to be testable behind an HTTP call.

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

    async def send_presence(self, chat: str, presence: str) -> None:
        """Set Rebe's presence in `chat` and return, without waiting.

        The pacer owns how long the presence is held, and re-asserts it before
        Baileys expires it, so an implementation that blocked here would be
        taking a timing decision away from the component that has to be tested
        on it. Raises `EvolutionError` if the transport will not take it.
        """

    async def send_text(self, chat: str, text: str) -> str:
        """Send one text message and answer with the WhatsApp message ID.

        The ID is best-effort: an empty string means the message went out but the
        response did not name it, which is worth a log line and nothing more.
        Raises `EvolutionError` if the message did not get out.
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

    async def send_presence(self, chat: str, presence: str) -> None:
        """Tell the chat that Rebe is typing, paused, or away.

        No `delay` is sent: the pause belongs to the pacer, which refreshes this
        presence for as long as the pause lasts.
        """
        await self._post(
            f"/chat/sendPresence/{self._instance}",
            {"number": chat, "presence": presence},
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

    async def send_text(self, chat: str, text: str) -> str:
        """Send one text message, and answer with the WhatsApp message ID."""
        payload = await self._post(
            f"/message/sendText/{self._instance}",
            {"number": chat, "text": text},
            action="send a message",
        )
        key = payload.get("key")
        if isinstance(key, dict) and key.get("id") is not None:
            return str(key["id"])
        return ""

    async def _post(self, path: str, body: dict[str, Any], *, action: str) -> dict[str, Any]:
        try:
            response = await self._http.post(
                f"{self._base_url}{path}", json=body, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise EvolutionError(action, detail=str(exc)) from exc

        if response.status_code in RATE_LIMIT_STATUSES:
            raise EvolutionRateLimitedError(
                action, status=response.status_code, detail=_describe(response)
            )
        if response.status_code >= 400:
            raise EvolutionError(action, status=response.status_code, detail=_describe(response))

        return _payload(response)


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
