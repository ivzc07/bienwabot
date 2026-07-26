"""A stand-in Telegram Bot API that records exactly what the agent sent it.

No test in this suite reaches api.telegram.org. The stub sits at the HTTP layer
rather than replacing the client object, the way `evolution_stub.py` and
`deepseek_stub.py` do, so the assertions read the bodies the real client
produced - the chat it addressed, the text a maintainer would see, and the token
in the path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

TOKEN = "12345:telegram-token-test"
CHAT_ID = "-1001234567890"

SEND_METHOD = "sendMessage"
UPDATES_METHOD = "getUpdates"


@dataclass(frozen=True, slots=True)
class Call:
    """One request the agent made."""

    path: str
    body: dict[str, Any]

    @property
    def method(self) -> str:
        """The Bot API method name, which is the last segment of the path."""
        return self.path.rsplit("/", 1)[-1]

    @property
    def text(self) -> str:
        return str(self.body.get("text", ""))

    @property
    def chat_id(self) -> str:
        return str(self.body.get("chat_id", ""))


def message(update_id: int, text: str, *, chat_id: str = CHAT_ID) -> dict[str, Any]:
    """One `getUpdates` entry, shaped the way Telegram sends it."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "date": 1_785_000_000,
            "chat": {"id": int(chat_id), "type": "private"},
            "from": {"id": int(chat_id), "is_bot": False},
            "text": text,
        },
    }


@dataclass
class FakeTelegram:
    """Serves canned responses and keeps every call, in order."""

    updates: list[list[dict[str, Any]]] = field(default_factory=list)
    """One entry per `getUpdates` call. An exhausted queue answers with none."""

    status: int = 200
    """Set above 400 to make every call fail the way a bad token would."""

    calls: list[Call] = field(default_factory=list)

    @property
    def texts(self) -> list[str]:
        return [call.text for call in self.calls if call.method == SEND_METHOD]

    @property
    def polls(self) -> list[dict[str, Any]]:
        return [call.body for call in self.calls if call.method == UPDATES_METHOD]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append(Call(path=path, body=body))

        if self.status >= 400:
            return httpx.Response(
                self.status,
                json={"ok": False, "error_code": self.status, "description": "stub failure"},
            )
        if path.endswith(UPDATES_METHOD):
            index = len(self.polls) - 1
            batch = self.updates[index] if index < len(self.updates) else []
            return httpx.Response(200, json={"ok": True, "result": batch})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
