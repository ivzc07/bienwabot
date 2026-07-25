"""A stand-in Evolution API that records exactly what the pacer sent it.

What these tests are about is the *sequence* on the wire: a `composing` presence,
then refreshes while the pause runs, then the text, then the presence cleared. So
the stub sits at the HTTP layer rather than replacing the client object, and the
assertions read the bodies the real client produced, headers included.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

PRESENCE_PATH = "/chat/sendPresence/"
TEXT_PATH = "/message/sendText/"

BASE_URL = "http://bien-evo:8080"
API_KEY = "evo-key-test"
INSTANCE = "bien-rebe"


@dataclass(frozen=True, slots=True)
class Call:
    """One request the agent made."""

    path: str
    body: dict[str, Any]
    api_key: str | None

    @property
    def is_presence(self) -> bool:
        return PRESENCE_PATH in self.path

    @property
    def is_text(self) -> bool:
        return TEXT_PATH in self.path

    @property
    def presence(self) -> str:
        return str(self.body.get("presence", ""))


class FakeEvolution:
    """Serves canned responses and keeps every call, in order."""

    def __init__(self, *, message_id: str = "STUB-MESSAGE-ID", status: int = 200) -> None:
        self.calls: list[Call] = []
        self.message_id = message_id
        self.status = status
        self.text_status: int | None = None
        """Set to fail only the send, leaving presence working."""

    @property
    def presences(self) -> list[str]:
        return [call.presence for call in self.calls if call.is_presence]

    @property
    def texts(self) -> list[str]:
        return [str(call.body.get("text", "")) for call in self.calls if call.is_text]

    @property
    def shape(self) -> list[str]:
        """The wire sequence as short labels, for asserting order in one line."""
        return [call.presence if call.is_presence else "text" for call in self.calls]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert PRESENCE_PATH in path or TEXT_PATH in path, f"unexpected path {path}"
        body = json.loads(request.content) if request.content else {}
        self.calls.append(Call(path=path, body=body, api_key=request.headers.get("apikey")))

        status = self.text_status if (TEXT_PATH in path and self.text_status) else self.status
        if status >= 400:
            return httpx.Response(status, json={"error": "stub failure"})
        if TEXT_PATH in path:
            return httpx.Response(status, json={"key": {"id": self.message_id}})
        return httpx.Response(status, json={"presence": body.get("presence")})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
