"""A stand-in DeepSeek that records what the agent actually sent it.

The point of these tests is the *request*: that thinking is off, that a cap is
present under the name DeepSeek documents, and that the model ID is the current
one. So the stub sits at the HTTP layer rather than replacing the model object,
and the assertions read the bytes Pydantic AI and the OpenAI SDK really produced.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

COMPLETION_PATH = "/chat/completions"


def tool_call_response(
    arguments: str,
    *,
    usage: dict[str, int] | None = None,
    model: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    """A DeepSeek chat completion answering with the `final_result` tool call.

    That is how Pydantic AI asks for a typed output, so this is the shape a real
    successful structured call comes back in.
    """
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": 1_785_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-stub",
                            "type": "function",
                            "function": {"name": "final_result", "arguments": arguments},
                        }
                    ],
                },
            }
        ],
        "usage": usage
        if usage is not None
        else {
            "prompt_tokens": 1000,
            "completion_tokens": 150,
            "total_tokens": 1150,
            "prompt_cache_hit_tokens": 700,
            "prompt_cache_miss_tokens": 300,
        },
    }


class FakeDeepSeek:
    """Serves canned responses and keeps every request body it was sent."""

    def __init__(self, *responses: dict[str, Any] | int) -> None:
        """Each argument is either a response payload or an HTTP status to fail with.

        The last entry repeats once the queue is exhausted, so a test that makes
        many identical calls only has to describe one response.
        """
        self._responses: list[dict[str, Any] | int] = list(responses) or [
            tool_call_response('{"answer": "hola", "language": "es"}')
        ]
        self.requests: list[dict[str, Any]] = []

    @property
    def last_request(self) -> dict[str, Any]:
        assert self.requests, "the agent never called DeepSeek"
        return self.requests[-1]

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(COMPLETION_PATH), f"unexpected path {request.url.path}"
        self.requests.append(json.loads(request.content))
        index = min(len(self.requests) - 1, len(self._responses) - 1)
        canned = self._responses[index]
        if isinstance(canned, int):
            return httpx.Response(canned, json={"error": {"message": "stub failure"}})
        return httpx.Response(200, json=canned)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
