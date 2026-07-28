"""The brain: the one way this process talks to DeepSeek.

Both legs - the scheduled news post and the webhook reply - come through here,
so the model wiring, the usage accounting, and the runaway-loop guard exist once.

Three properties are structural rather than a caller's responsibility:

1. **Thinking is off.** On V4 thinking defaults to *enabled*, and leaving it on
   is a correctness problem before it is a cost problem: it silently ignores
   `temperature`, `top_p`, `presence_penalty` and `frequency_penalty` - exactly
   the knobs the persona's voice variability rides on - and it bills chain of
   thought as output tokens.
2. **Every call is capped.** `max_tokens` always has a value, taken from the call
   type's shape, so a generation bug cannot run long.
3. **Every answer is typed.** Callers ask for a Pydantic model and get a
   validated instance or an exception. There is no path that returns free text
   for a caller to parse, and a response that fails validation is a failure.
4. **An unusable answer earns one more try.** A 200 with nothing usable in it
   is how a flaky provider shows up, and on 2026-07-28 that shape cost three
   tier-one replies in twenty minutes. Every call gets exactly one retry
   (`MAX_ATTEMPTS`), each attempt reserved and billed against the day's
   ceiling, so the counter still authorises every request.

Verified against the primary source on 2026-07-25
([Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing),
[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode)):

- The current non-thinking chat model is **`deepseek-v4-flash`**, at $0.0028
  (input, cache hit) / $0.14 (input, cache miss) / $0.28 (output) per 1M tokens,
  1M context, 384K max output. `deepseek-v4-pro` is the larger sibling at roughly
  3.1x. Both support JSON output and tool calls.
- `deepseek-chat` and `deepseek-reasoner` were deprecated on 2026/07/24 15:59 UTC
  and now merely alias the non-thinking and thinking modes of `deepseek-v4-flash`.
- Thinking defaults to `enabled` and is switched off with
  `{"thinking": {"type": "disabled"}}`, passed through the OpenAI SDK's
  `extra_body`.
- The documented cap parameter is `max_tokens`. OpenAI's own newer
  `max_completion_tokens` does not appear in DeepSeek's API reference, so the
  model profile is pinned to emit `max_tokens` - a cap the provider ignores is
  no cap at all.

Prices match section 1 of `docs/wayfinder/token-budget-spec.md`, so its ~$0.22 a
month conclusion stands unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TypeVar, cast

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel
from pydantic_ai import Agent, UnexpectedModelBehavior, capture_run_messages
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.usage import RunUsage

from rebe_agent.alerts import Alerter, LoggingAlerter
from rebe_agent.clock import Clock
from rebe_agent.config import Settings
from rebe_agent.guard import CallRateGuard, DailyCallCeilingError
from rebe_agent.signals import BrainWatch, Watchtower
from rebe_agent.usage import CallType, CallUsage, UsageStore

logger = logging.getLogger("rebe_agent.brain")

DEEPSEEK_MODEL = "deepseek-v4-flash"
"""The current non-thinking chat model. Verified against the pricing page above."""

THINKING_DISABLED: dict[str, object] = {"thinking": {"type": "disabled"}}
"""Sent verbatim in the request body. DeepSeek defaults this to `enabled`."""

REQUEST_TIMEOUT_SECONDS = 30.0
"""A reply nobody is waiting for is worth less than a thread that comes back."""

MAX_ATTEMPTS = 2
"""How many times one logical call may reach DeepSeek: a first try and one more.

The retry exists for exactly one failure shape - a 200 whose answer is
unusable, which on 2026-07-28 cost three tier-one replies in twenty minutes:
DeepSeek returned whitespace `content` with a handful of tokens billed, while
the same request minutes later answered fine. A second sample of a flaky
provider is cheap insurance; a second sample of a hard down one fails the same
way and the call is no worse off than before. There is no backoff on purpose:
a wait long enough to ride out a real outage belongs to a redesign of the
reply path, not to this loop, and the group is not waiting fewer seconds for
having been made to wait more.

Every attempt reserves and books its own call, so the counter still authorises
every request that leaves the process - that invariant is why Pydantic AI's
own retry stays at 0, and the retry here does not weaken it.
"""

OutputT = TypeVar("OutputT", bound=BaseModel)


class BrainError(RuntimeError):
    """No answer came back. Callers treat this as "stay silent"."""


class BrainStoppedError(BrainError):
    """The day's call ceiling is spent; no request was made."""


class BrainCallError(BrainError):
    """The request went out and did not produce a valid typed answer."""


def _why(exc: BaseException) -> str:
    """The exception, and whatever it is standing in front of.

    Pydantic AI reports a schema failure as `Exceeded maximum output retries (0)`,
    which names the policy that stopped the call and nothing about what the model
    actually did. What went wrong - the validation errors, the text it sent
    instead of calling the output tool, the provider's own message - is underneath
    it as a cause. A log line that keeps only the top of that chain is a failure
    nobody can act on without a second deploy, which is how this function came to
    exist.

    Bounded, because a validation error quoting the model's whole answer would
    otherwise put an unbounded body into a log line and a Telegram alert.
    """
    parts: list[str] = [str(exc) or type(exc).__name__]
    seen = {id(exc)}
    cause: BaseException | None = exc.__cause__ or exc.__context__
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        parts.append(f"{type(cause).__name__}: {cause}")
        cause = cause.__cause__ or cause.__context__
    body = getattr(exc, "body", None)
    if body is not None:
        parts.append(f"body: {body}")
    return _clip(" <- ".join(parts))


def _clip(detail: str, limit: int = 600) -> str:
    collapsed = " ".join(detail.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit]}..."


def _unusable_shape(messages: Sequence[ModelMessage]) -> str:
    """What the model actually sent, for the answer that would not parse.

    `_why` reports the exception chain, and on a blank answer that chain says
    only `Invalid JSON: EOF while parsing` over an input of `' '` - true, and
    useless. It cannot say whether the body was empty because the provider
    stopped early, because a filter refused, or because the whole generation
    went into `reasoning_content` where the parser never looks.

    The answer to all three is in the response object Pydantic AI already
    built, so this reads it back off the captured run: the provider's own
    `finish_reason`, the length and opening characters of every part, and the
    provider details beside them. Lengths are reported because the distinction
    that matters most here - nothing at all versus a single space - does not
    survive being trimmed into a log line.
    """
    responses = [m for m in messages if isinstance(m, ModelResponse)]
    if not responses:
        return "no response was captured"

    last = responses[-1]
    fields = [f"finish_reason={last.finish_reason!r}"]
    if last.provider_details:
        fields.append(f"provider_details={last.provider_details}")
    if not last.parts:
        fields.append("parts=none")
    for part in last.parts:
        content = getattr(part, "content", None)
        if isinstance(content, str):
            fields.append(f"{part.part_kind}[{len(content)}]={content!r}")
        else:
            fields.append(f"{part.part_kind}={content!r}")
    return _clip(" ".join(fields))


@dataclass(frozen=True, slots=True)
class CallShape:
    """How much room a call type gets, and how loose its voice is.

    Output caps were once roughly 2-3x the estimates in section 2 of the token
    budget spec. They are wider than that now, because the cap covers everything
    the model generates and V4 generates chain-of-thought whether or not it was
    asked to: a 120-token gate, four times its own estimate, was still truncated
    mid-answer in production. The principle is unchanged - generous enough that a
    normal answer is never cut off, tight enough that a runaway generation stops
    within cents - but the room a thinking model needs is not the size of its
    answer.

    A note on the numbers, so the record is straight: they were raised again on
    2026-07-28 under a theory the day's own billing data later disproved. Three
    replies had died of blank answers, and the blame was put on thinking
    exhausting the cap. The usage table showed 43 completion tokens across all
    three failed calls - the budget was never touched; DeepSeek was returning
    whitespace from the first token during a provider-side degradation, and no
    cap setting addresses that. What does is the one retry every call now gets
    (`MAX_ATTEMPTS`). The wider caps stay as headroom, but they were never the
    fix, and a debugger who reads only this file should not be told they were.
    """

    max_tokens: int
    temperature: float


CALL_SHAPES: dict[CallType, CallShape] = {
    # A: a WhatsApp-short Spanish post wrapped in JSON. Estimated at ~150 tokens,
    # and raised with C rather than waiting for its own outage: 400 is below the
    # gate's 600, and 600 was already the number a *thirty-token* verdict needed
    # once thinking was counted. A truncated post is not a rejected post - it
    # raises out of the run and the slot goes empty - so this one is not worth
    # learning from the group.
    CallType.NEWS_SUMMARY: CallShape(max_tokens=1500, temperature=0.8),
    # B: a small typed verdict. Estimated at ~30 tokens, and a judgement call
    # wants to be repeatable, so the temperature is low. The cap is nowhere near
    # 2-3x that estimate because on V4 the cap covers the chain-of-thought too,
    # and 120 truncated a live call mid-answer.
    CallType.REPLY_GATE: CallShape(max_tokens=600, temperature=0.2),
    # C: the only member-visible prose. Estimated at ~60 tokens, with 2500 of
    # room as headroom. The 2026-07-28 raise from 600 was made under a theory
    # the billing data disproved - see the CallShape docstring: the three blank
    # answers that day were a provider degradation, not an exhausted cap, and
    # the retry in `MAX_ATTEMPTS` is what addresses them.
    CallType.REPLY_GENERATION: CallShape(max_tokens=2500, temperature=0.9),
    # D: B against an article instead of a chat message. Estimated at ~50 tokens.
    CallType.RELEVANCE_GATE: CallShape(max_tokens=600, temperature=0.2),
    # The `--ask` smoke test. Room to say something, not room to ramble.
    CallType.PROBE: CallShape(max_tokens=300, temperature=0.7),
}


class Probe(BaseModel):
    """What `rebe-agent --ask` gets back: proof the whole path is typed."""

    answer: str
    """The model's reply, in whatever language the prompt was written in."""

    language: str
    """ISO 639-1 code of `answer`, for example `es`."""


def usage_from_run(run_usage: RunUsage) -> CallUsage:
    """Pull DeepSeek's three billed numbers out of a Pydantic AI run.

    `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens` are provider-specific
    and arrive in `details`; the fallbacks keep the accounting honest against a
    response that omits them rather than silently recording zeros.
    """
    details = run_usage.details or {}
    cache_hit = int(details.get("prompt_cache_hit_tokens", run_usage.cache_read_tokens))
    if "prompt_cache_miss_tokens" in details:
        cache_miss = int(details["prompt_cache_miss_tokens"])
    else:
        cache_miss = max(run_usage.input_tokens - cache_hit, 0)
    return CallUsage(
        cache_hit_tokens=cache_hit,
        cache_miss_tokens=cache_miss,
        completion_tokens=run_usage.output_tokens,
    )


def _deepseek_profile(resolved: ModelProfile) -> ModelProfile:
    """Keep Pydantic AI's DeepSeek profile, but send the cap DeepSeek documents,
    and ask for the typed answer the way this model is reliable at giving it.

    Pydantic AI defaults to OpenAI's `max_completion_tokens`; DeepSeek's API
    reference only lists `max_tokens`, and an ignored cap is not a cap.

    The output mode is the harder one. Pydantic AI's default is a tool call, and
    on V4 that is where the reply gate died in production: the model wrote its
    reasoning into the tool's arguments, and what came back was
    `Invalid JSON: 'Let me analyze this message...'`. V4 is a thinking model that
    does not reliably stop thinking when asked - `thinking: disabled` goes out on
    every request here and the leak happened anyway - and it rejects the forced
    `tool_choice` that would otherwise pin the shape down. Asking for a JSON
    object in the message body instead takes the tool call out of the path
    entirely, and leaves DeepSeek somewhere else to put chain-of-thought: the
    `reasoning_content` field, which is beside the answer and is not parsed as one.
    """
    pinned: dict[str, Any] = dict(resolved)
    pinned["openai_chat_supports_max_completion_tokens"] = False
    pinned["supports_json_object_output"] = True
    pinned["default_structured_output_mode"] = "prompted"
    # The profile TypedDicts are open in practice - the provider's own keys and
    # the OpenAI-specific ones live in the same mapping - so the cast is where
    # that merge is stated once rather than fought with at every key.
    return cast(ModelProfile, pinned)


def build_model(
    settings: Settings, *, http_client: httpx.AsyncClient | None = None
) -> OpenAIChatModel:
    """The DeepSeek model, wired from configuration.

    `max_retries=0` on the client is deliberate: one logical call must be one
    HTTP request, or the counter that detects a retry loop would be blind to
    retries the SDK made on its own.

    `http_client` exists so tests can read the bytes this really puts on the
    wire. Nothing in production passes it, and the request is built the same way
    either way - which is the point, since what the tests assert is the request.
    """
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=settings.deepseek_base_url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
        http_client=http_client,
    )
    return OpenAIChatModel(
        DEEPSEEK_MODEL,
        provider=DeepSeekProvider(openai_client=client),
        profile=_deepseek_profile,
    )


class Brain:
    """One typed, counted, capped call to DeepSeek."""

    def __init__(
        self, model: OpenAIChatModel, guard: CallRateGuard, watch: BrainWatch | None = None
    ) -> None:
        self._model = model
        self._guard = guard
        self._watch = watch or Watchtower()

    async def ask(
        self,
        call_type: CallType,
        prompt: str,
        output_type: type[OutputT],
        *,
        instructions: str | None = None,
        message_history: Sequence[ModelMessage] | None = None,
    ) -> OutputT:
        """Ask DeepSeek for one `output_type`, or raise.

        The cap and the temperature come from the call type's shape rather than
        from the caller, so there is no way to make an uncapped call.

        `retries=0` keeps Pydantic AI from answering a bad response with a
        second billed call the counter did not authorise. The one retry a call
        does get lives here instead - see `MAX_ATTEMPTS` - where every attempt
        reserves and books its own call before it reaches the wire.
        """
        shape = CALL_SHAPES[call_type]
        settings = OpenAIChatModelSettings(
            max_tokens=shape.max_tokens,
            temperature=shape.temperature,
            extra_body=dict(THINKING_DISABLED),
        )

        billed = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                reservation = await self._guard.reserve(call_type)
            except DailyCallCeilingError as exc:
                raise BrainStoppedError(str(exc)) from exc

            agent = Agent(
                self._model,
                output_type=output_type,
                instructions=instructions,
                retries=0,
            )
            # Pydantic AI accumulates into this as the run goes, so the tokens
            # DeepSeek billed are still readable when the run ends in an
            # exception. A call that fails is a call that cost money; leaving
            # it out of the totals would understate exactly the days worth
            # understanding.
            tally = RunUsage()
            try:
                with capture_run_messages() as exchange:
                    result = await agent.run(
                        prompt,
                        model_settings=settings,
                        message_history=list(message_history) if message_history else None,
                        usage=tally,
                    )
            except UnexpectedModelBehavior as exc:
                unusable = exc
                # The exception says the answer did not parse; this says what
                # the answer was. Logged per attempt rather than once at the
                # end, because two attempts can fail differently and the pair
                # is the evidence.
                logger.warning(
                    "DeepSeek %s call %d came back unusable: %s",
                    call_type,
                    attempt,
                    _unusable_shape(exchange),
                )
            except Exception as exc:
                logger.warning("DeepSeek %s call failed: %s", call_type, _why(exc))
                # The caller's answer to this is silence - the item is dropped
                # and the group is told nothing - so the only way anybody learns
                # is the out-of-band channel. The ceiling above is deliberately
                # not alerted here: the guard already says the day is spent, in
                # its own words.
                failure = BrainCallError(f"{call_type} call failed: {_why(exc)}")
                await self._watch.brain_failed(failure)
                raise failure from exc
            else:
                return result.output
            finally:
                if tally.requests:
                    await self._guard.record_usage(reservation, usage_from_run(tally))
                    billed += tally.output_tokens

            # Only an unusable answer gets the second try, and only once. HTTP
            # and transport failures took the branch above; a provider that is
            # down hard does not get asked twice per call.
            if attempt < MAX_ATTEMPTS:
                logger.info(
                    "DeepSeek %s call %d came back unusable; one more try",
                    call_type,
                    attempt,
                )
                continue

            logger.warning(
                "DeepSeek %s call failed after %d attempts (%d output tokens billed): %s",
                call_type,
                attempt,
                billed,
                _why(unusable),
            )
            failure = BrainCallError(
                f"{call_type} call failed after {attempt} attempts: {_why(unusable)}"
            )
            await self._watch.brain_failed(failure)
            raise failure from unusable

        raise AssertionError("the attempt loop always returns or raises")


def build_brain(
    settings: Settings,
    clock: Clock,
    store: UsageStore,
    alerter: Alerter | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> Brain:
    """Assemble the brain both legs share.

    One alert channel for both things worth telling a human about here: the day's
    call count running away, which the guard watches, and a call that came back
    with nothing, which the brain itself sees. A ban is not among them, so no
    pause switch is wired in: a DeepSeek outage must not silence WhatsApp.
    """
    channel = alerter or LoggingAlerter()
    guard = CallRateGuard(store, clock, channel)
    return Brain(build_model(settings, http_client=http_client), guard, Watchtower(channel))
