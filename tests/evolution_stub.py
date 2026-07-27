"""A stand-in Evolution API that records exactly what the pacer sent it.

What these tests are about is the *sequence* on the wire: a `composing` presence
carrying the pause it should be held for, then the text, then the presence
cleared. So the stub sits at the HTTP layer rather than replacing the client
object, and the assertions read the bodies the real client produced, headers
included.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

PRESENCE_PATH = "/chat/sendPresence/"
TEXT_PATH = "/message/sendText/"
READ_PATH = "/chat/markMessageAsRead/"

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
    def is_read(self) -> bool:
        return READ_PATH in self.path

    @property
    def presence(self) -> str:
        return str(self.body.get("presence", ""))

    @property
    def read_ids(self) -> list[str]:
        """The message ids this call put a read receipt on."""
        entries = self.body.get("readMessages", [])
        return [str(entry.get("id", "")) for entry in entries] if isinstance(entries, list) else []


class FakeEvolution:
    """Serves canned responses and keeps every call, in order."""

    def __init__(self, *, message_id: str = "STUB-MESSAGE-ID", status: int = 200) -> None:
        self.calls: list[Call] = []
        self.message_id = message_id
        self.status = status
        self.text_status: int | None = None
        """Set to fail only the send, leaving presence working."""

        self.read_status: int | None = None
        """Set to fail only the read receipt, leaving the send working."""

    @property
    def presences(self) -> list[str]:
        return [call.presence for call in self.calls if call.is_presence]

    @property
    def texts(self) -> list[str]:
        return [str(call.body.get("text", "")) for call in self.calls if call.is_text]

    @property
    def reads(self) -> list[str]:
        """Every message id that was marked read, in order."""
        return [id for call in self.calls if call.is_read for id in call.read_ids]

    @property
    def shape(self) -> list[str]:
        """The wire sequence as short labels, for asserting order in one line."""
        return [_label(call) for call in self.calls]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        known = (PRESENCE_PATH, TEXT_PATH, READ_PATH)
        assert any(prefix in path for prefix in known), f"unexpected path {path}"
        body = json.loads(request.content) if request.content else {}
        self.calls.append(Call(path=path, body=body, api_key=request.headers.get("apikey")))

        status = self.status
        if TEXT_PATH in path and self.text_status:
            status = self.text_status
        elif READ_PATH in path and self.read_status:
            status = self.read_status
        if status >= 400:
            return httpx.Response(status, json={"error": "stub failure"})
        if TEXT_PATH in path:
            return httpx.Response(status, json={"key": {"id": self.message_id}})
        if READ_PATH in path:
            return httpx.Response(status, json={"message": "read"})
        return httpx.Response(status, json={"presence": body.get("presence")})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


def _label(call: Call) -> str:
    if call.is_presence:
        return call.presence
    return "read" if call.is_read else "text"
