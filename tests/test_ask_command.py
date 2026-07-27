"""`rebe-agent --ask` end to end: one command, one validated object, one counted call.

Everything downstream of argparse is real here - configuration, the OpenAI SDK,
Pydantic AI, the `rebe` database - and only DeepSeek itself is a stand-in, served
over a genuine socket rather than a patched client. Needs a database, so it skips
locally unless `REBE_TEST_DATABASE_URL` is set (see `tests/test_usage_store.py`).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Iterator
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from rebe_agent.__main__ import EXIT_CALL_FAILED, EXIT_OK, main
from rebe_agent.usage import CallType, PostgresUsageStore
from tests.deepseek_stub import json_output_response
from tests.test_config import COMPLETE_ENV

DATABASE_URL = os.environ.get("REBE_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set REBE_TEST_DATABASE_URL to run the end-to-end ask test"
)

ANSWER = '{"answer": "Va bien, gracias.", "language": "es"}'
MEXICO_CITY = ZoneInfo("America/Mexico_City")


class StubDeepSeek:
    """A DeepSeek-shaped HTTP server on a real port."""

    def __init__(self) -> None:
        self.payload: dict[str, Any] = json_output_response(ANSWER)
        self.seen: list[dict[str, Any]] = []
        self.base_url = ""

    def respond_with(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    @property
    def last_request(self) -> dict[str, Any]:
        assert self.seen, "the command never called DeepSeek"
        return self.seen[-1]


@pytest.fixture
def deepseek() -> Iterator[StubDeepSeek]:
    stub = StubDeepSeek()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # the name http.server dispatches on
            length = int(self.headers.get("Content-Length", "0"))
            stub.seen.append(json.loads(self.rfile.read(length)))
            body = json.dumps(stub.payload).encode()
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
def env(deepseek: StubDeepSeek) -> dict[str, str]:
    return dict(
        COMPLETE_ENV,
        REBE_DATABASE_URL=DATABASE_URL,
        DEEPSEEK_BASE_URL=deepseek.base_url,
    )


async def probe_calls_today() -> int:
    async with PostgresUsageStore.connect(DATABASE_URL) as store:
        totals = await store.totals_on(datetime.now(MEXICO_CITY).date())
        row = totals.get(CallType.PROBE)
        return row.calls if row else 0


def test_one_command_prints_a_validated_typed_object(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["--ask", "¿como vas?"], env=env)

    assert exit_code == EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"answer": "Va bien, gracias.", "language": "es"}


def test_the_command_sends_a_capped_non_thinking_request(
    env: dict[str, str], deepseek: StubDeepSeek
) -> None:
    main(["--ask", "¿como vas?"], env=env)

    assert deepseek.last_request["model"] == "deepseek-v4-flash"
    assert deepseek.last_request["thinking"] == {"type": "disabled"}
    assert isinstance(deepseek.last_request["max_tokens"], int)


def test_the_command_counts_its_call_in_the_rebe_database(env: dict[str, str]) -> None:
    """Sync, like the command itself: `main` runs its own event loop."""
    before = asyncio.run(probe_calls_today())

    assert main(["--ask", "¿como vas?"], env=env) == EXIT_OK

    assert asyncio.run(probe_calls_today()) == before + 1


def test_an_unparseable_answer_exits_non_zero(
    env: dict[str, str], deepseek: StubDeepSeek, capsys: pytest.CaptureFixture[str]
) -> None:
    deepseek.respond_with(json_output_response('{"answer": "sin idioma"}'))

    exit_code = main(["--ask", "¿como vas?"], env=env)

    assert exit_code == EXIT_CALL_FAILED
    assert capsys.readouterr().out == ""
