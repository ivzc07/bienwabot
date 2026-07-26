"""`rebe-agent --say` end to end: one command, one paced message, one recorded send.

Everything downstream of argparse is real - configuration, the HTTP client, the
pacer, the `rebe` database - and only Evolution itself is a stand-in, served over
a genuine socket rather than a patched client. This is the command behind the
"one message lands in the group" acceptance criterion; against the real bien-evo
it is the same code path with a different URL.

The envelope's own behaviour is asserted without waiting on time in
`tests/test_pacer.py`. Here the pause is real, because what is being proved is
that the wiring pauses at all - so this module is deliberately one test wide.
Needs a database, so it skips locally unless `REBE_TEST_DATABASE_URL` is set.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg
import pytest

from rebe_agent.__main__ import EXIT_OK, EXIT_SEND_REFUSED, main
from rebe_agent.clock import SystemClock
from rebe_agent.pause import PostgresPauseSwitch
from rebe_agent.sends import PostgresSendLog, fingerprint
from tests.support import GROUP, MEXICO_CITY
from tests.test_config import COMPLETE_ENV

DATABASE_URL = os.environ.get("REBE_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set REBE_TEST_DATABASE_URL to run the end-to-end say test"
)

MESSAGE = "Probando el canal de envio, una sola vez."


class StubEvolution:
    """An Evolution-shaped HTTP server on a real port."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict[str, Any]]] = []
        self.base_url = ""

    @property
    def shape(self) -> list[str]:
        return [
            body.get("presence", "text") if "sendPresence" in path else "text"
            for path, body in self.seen
        ]


@pytest.fixture
def evolution() -> Iterator[StubEvolution]:
    stub = StubEvolution()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # the name http.server dispatches on
            length = int(self.headers.get("Content-Length", "0"))
            stub.seen.append((self.path, json.loads(self.rfile.read(length))))
            body = json.dumps({"key": {"id": "STUB-MESSAGE-ID"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            """Keep the test output clean."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    stub.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def env(evolution: StubEvolution) -> dict[str, str]:
    _clear_the_send_log()
    _clear_the_soft_pause()
    return dict(
        COMPLETE_ENV,
        REBE_DATABASE_URL=DATABASE_URL,
        EVOLUTION_API_URL=evolution.base_url,
        EVOLUTION_INSTANCE="bien-rebe",
    )


def _clear_the_send_log() -> None:
    """A previous run's sends would otherwise sit inside the rolling ceilings.

    `PostgresSendLog` owns the schema, and connecting is what creates the table
    on a database that has never seen it, so this file never restates the DDL -
    a column added there would otherwise leave a stale table here.
    """

    async def create_the_table() -> None:
        async with PostgresSendLog.connect(DATABASE_URL):
            pass

    asyncio.run(create_the_table())
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM sends")


def _clear_the_soft_pause() -> None:
    """The command reads the ops channel's switch, so a pause left on by another
    module's test would silence this one - which is the switch working, and a
    confusing way to fail. Same shape as above: the switch owns its own DDL."""

    async def create_the_table() -> None:
        async with PostgresPauseSwitch.connect(DATABASE_URL, SystemClock(MEXICO_CITY)):
            pass

    asyncio.run(create_the_table())
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM soft_pause")


def _recent_send(fingerprint_value: str) -> None:
    """A send a minute ago, with wording of the caller's choosing.

    Only the repeat rule wants this. The successful-send test deliberately runs
    against an empty log, because every other rule that reads the previous send
    - the overnight hold, the hush, the post gap - would then make the result
    depend on what time of day CI happened to run.
    """
    now = datetime.now(MEXICO_CITY) - timedelta(minutes=1)
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO sends (sent_at, day, kind, chat, fingerprint) VALUES (%s, %s, %s, %s, %s)",
            (now, now.date(), "reply", GROUP, fingerprint_value),
        )


def test_one_command_types_into_the_group_and_then_sends(
    env: dict[str, str], evolution: StubEvolution
) -> None:
    """`--as reply` so the run does not depend on the hour: a scheduled post is
    held between 23:00 and 08:00, and CI runs whenever it runs."""
    exit_code = main(["--say", MESSAGE, "--to", GROUP, "--as", "reply"], env=env)

    assert exit_code == EXIT_OK
    assert evolution.shape == ["composing", "text", "paused"]
    paths = [path for path, _ in evolution.seen]
    assert paths[0] == "/chat/sendPresence/bien-rebe"
    assert paths[1] == "/message/sendText/bien-rebe"
    assert evolution.seen[1][1] == {"number": GROUP, "text": MESSAGE}

    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute("SELECT chat, kind FROM sends ORDER BY sent_at").fetchall()
    assert rows[-1] == (GROUP, "reply")


def test_a_refused_send_exits_differently_from_a_successful_one(
    env: dict[str, str], evolution: StubEvolution
) -> None:
    """The repeat rule, exercised through the command: the same wording twice in
    a row never reaches Evolution, and the exit code says why."""
    _recent_send(fingerprint(MESSAGE))

    exit_code = main(["--say", MESSAGE, "--to", GROUP, "--as", "reply"], env=env)

    assert exit_code == EXIT_SEND_REFUSED
    assert evolution.seen == []
