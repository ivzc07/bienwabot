"""The one place this process talks to Telegram.

Two calls, and section 2.4 of `docs/wayfinder/deployment-architecture-spec.md`
explains why they are not WhatsApp calls: an alert about Evolution being down
cannot travel through Evolution, so the ops channel has to be out of band.

- `POST /bot<token>/sendMessage` - the alert a human reads.
- `POST /bot<token>/getUpdates` - the long poll that carries the soft-pause
  switch back the other way. It is a *long* poll, held open for `POLL_SECONDS`,
  which is what makes flipping the switch feel instant without asking Telegram
  for news every second.

The bot token lives in the URL path rather than in a header, which Telegram
decided and this module has to live with: every error message is scrubbed before
it is raised or logged, because a stack trace carrying a URL here is a leaked
credential. Nothing else in the codebase builds these URLs.

Nothing here decides what an alert says or what a command means. The alert text
is `rebe_agent.alerts`; the commands are `rebe_agent.ops`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx

from rebe_agent.config import Settings

logger = logging.getLogger("rebe_agent.telegram")

API_ROOT = "https://api.telegram.org"

POLL_SECONDS = 25
"""How long Telegram holds an empty `getUpdates` open before answering."""

REQUEST_TIMEOUT_SECONDS = 15.0
"""For an ordinary call. A poll gets this on top of however long it may wait."""

REDACTED = "***"
"""What the token is replaced with anywhere an error or a log line could carry it."""

MESSAGE_UPDATES = ("message",)
"""The only update type the control channel can answer, so the only one asked for."""


class TelegramError(RuntimeError):
    """Telegram did not accept the call. Nothing was delivered.

    Callers treat this as "the maintainer was not told", never as a reason to
    fail whatever produced the alert: an undeliverable alert about a failed send
    must not also break the send path.
    """

    def __init__(self, action: str, *, status: int | None = None, detail: str = "") -> None:
        self.action = action
        self.status = status
        self.detail = detail
        described = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"Telegram refused to {action}{described}: {detail or 'no detail'}")


@dataclass(frozen=True, slots=True)
class Update:
    """One message the bot was sent, flattened to what the control channel reads.

    `chat_id` is a string because that is what `TELEGRAM_CHAT_ID` is, and the
    comparison that decides whether a command counts is between those two.
    """

    update_id: int
    chat_id: str
    text: str


class TelegramClient:
    """A thin, typed client for the one bot and the one chat this process uses."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        api_root: str = API_ROOT,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._base_url = f"{api_root.rstrip('/')}/bot{token}"
        self._timeout = timeout
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> TelegramClient:
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

    async def send_message(self, text: str) -> None:
        """Put one message in the ops chat. Raises `TelegramError` if it did not land."""
        await self._call(
            "sendMessage",
            {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True},
            action="deliver an alert",
        )

    async def poll(self, *, offset: int, timeout: int = POLL_SECONDS) -> list[Update]:
        """Wait up to `timeout` seconds for messages numbered `offset` or later.

        Telegram's own contract: an update is redelivered until an `offset` past
        it is acknowledged, so the caller advances the offset once it has acted.
        Anything that is not a text message is dropped here rather than handed on
        as an empty command.
        """
        payload = await self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": MESSAGE_UPDATES},
            action="read its updates",
            # The request has to outlive the long poll, or every poll is a timeout.
            request_timeout=timeout + self._timeout,
        )
        result = payload.get("result")
        if not isinstance(result, list):
            return []
        return [update for update in map(_as_update, result) if update is not None]

    async def _call(
        self,
        method: str,
        body: dict[str, Any],
        *,
        action: str,
        request_timeout: float | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._http.post(
                f"{self._base_url}/{method}",
                json=body,
                timeout=request_timeout if request_timeout is not None else self._timeout,
            )
        except httpx.HTTPError as exc:
            raise TelegramError(action, detail=self._scrub(str(exc))) from exc

        if response.status_code >= 400:
            raise TelegramError(
                action,
                status=response.status_code,
                detail=self._scrub(response.text.strip()[:500]),
            )
        return _payload(response)

    def _scrub(self, text: str) -> str:
        """Whatever httpx or Telegram said, minus the token it may have quoted."""
        return text.replace(self._token, REDACTED)


def _as_update(entry: object) -> Update | None:
    """One `getUpdates` result, or `None` if it is not a text message.

    Written defensively on purpose: this is the one place in the process where a
    stranger chooses the shape of the input. A missing field is a skipped update,
    never an exception in the loop that carries the pause switch.
    """
    if not isinstance(entry, dict):
        return None
    update_id = entry.get("update_id")
    payload = entry.get("message")
    if not isinstance(update_id, int) or not isinstance(payload, dict):
        return None
    chat = payload.get("chat")
    text = payload.get("text")
    if not isinstance(chat, dict) or not isinstance(text, str):
        return None
    chat_id = chat.get("id")
    if not isinstance(chat_id, int | str):
        return None
    return Update(update_id=update_id, chat_id=str(chat_id), text=text)


def _payload(response: httpx.Response) -> dict[str, Any]:
    """Telegram answers JSON; a body that is not an object is not worth guessing at."""
    try:
        decoded = response.json()
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def build_telegram(
    settings: Settings, *, http_client: httpx.AsyncClient | None = None
) -> TelegramClient:
    """The client for the bot and chat this deployment alerts through."""
    return TelegramClient(
        settings.telegram_bot_token.get_secret_value(),
        settings.telegram_chat_id,
        http_client=http_client,
    )
