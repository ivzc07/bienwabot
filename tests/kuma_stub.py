"""A stand-in Uptime Kuma push monitor that records every beat it was sent.

Like the other stubs it sits at the HTTP layer, so what the assertions read is
the request the real heartbeat produced: the push token in the path and the
status Kuma reads to decide the monitor is up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

PUSH_TOKEN = "kuma-push-test"
PUSH_URL = f"http://kuma:3001/api/push/{PUSH_TOKEN}"


@dataclass(frozen=True, slots=True)
class Beat:
    """One heartbeat push."""

    path: str
    status: str
    message: str


@dataclass
class FakeKuma:
    """Serves canned responses and keeps every beat, in order."""

    status: int = 200
    """Set above 400 to make Kuma reject the push the way a wrong token would."""

    fails: bool = False
    """Set to make the push fail at the transport, the way a dead host would."""

    beats: list[Beat] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.fails:
            raise httpx.ConnectError("stub failure", request=request)
        self.beats.append(
            Beat(
                path=request.url.path,
                status=request.url.params.get("status", ""),
                message=request.url.params.get("msg", ""),
            )
        )
        if self.status >= 400:
            return httpx.Response(self.status, json={"ok": False})
        return httpx.Response(self.status, json={"ok": True})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
