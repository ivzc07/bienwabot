# Python Agent Framework / ADK Research

Wayfinder ticket [#4 "Decide the Python agent framework/ADK (DeepSeek brain)"](https://github.com/ivzc07/bienwabot/issues/4): pick the Python agent framework that runs the bien.mx WhatsApp group bot, with **DeepSeek** (OpenAI-compatible API) as the brain, self-hosted cheap.

Research date: 2026-07. Every factual claim is cited to a primary source (official docs, GitHub READMEs, first-party API docs).

The transport is already decided: [Evolution API](https://github.com/ivzc07/bienwabot/issues/3) (Baileys-based, dockerized). Python talks to it over **HTTP + webhook**. So the framework's job is: receive a group-message webhook, decide whether/how to reply, call DeepSeek, and post back over HTTP - plus a scheduled loop that posts curated AI news. This is a **long-running, event-driven process**, not a request/response web API.

---

## What actually constrains the choice

Three things do the deciding here; the rest is a wash:

1. **This bot's agentic complexity is LOW.** The brief is "mostly news, light replies." The real work is: a webhook handler, a scheduler, a *reply-or-ignore* gate, and DeepSeek calls (one with a news tool). There is no deep multi-step agent-planning graph. So a heavy agent runtime is machinery this bot never uses.
2. **It is event-driven and long-running.** A framework that assumes a request/response `Runner` or ships its own web server fights the shape; a plain *library you call per event* fits it.
3. **DeepSeek is OpenAI-compatible, so every candidate "supports" it.** DeepSeek's own docs: "The DeepSeek API uses an API format compatible with OpenAI ... you can use the OpenAI SDK ... to access the DeepSeek API." Base URL `https://api.deepseek.com`. ([DeepSeek API docs](https://api-docs.deepseek.com/)) DeepSeek support is therefore **not** a differentiator - footprint, runtime fit, and built-in memory are.

**Model-name caveat:** DeepSeek periodically renames models. As of this research the `deepseek-chat` / `deepseek-reasoner` IDs were being superseded by a `deepseek-v4-flash` (non-thinking) / `deepseek-v4-pro` (thinking) line ([DeepSeek pricing/quick-start](https://api-docs.deepseek.com/quick_start/pricing)). Verify the current IDs at `api-docs.deepseek.com` at build time. **Tool-calling caveat:** use the **non-thinking** chat model for the news/tool path - the reasoner line has historically not supported tool calling / structured output across these stacks ([ChatDeepSeek docs](https://docs.langchain.com/oss/python/integrations/chat/deepseek)); DeepSeek notes tool use in thinking mode only from V3.2+ ([DeepSeek tool calls](https://api-docs.deepseek.com/guides/tool_calls)).

---

## 1. Google ADK (Agent Development Kit), Python

Google's agent framework; reaches non-Gemini models through LiteLLM.

- **DeepSeek support:** via LiteLLM - `LlmAgent(model=LiteLlm(model="deepseek/deepseek-chat"), ...)`, key in `DEEPSEEK_API_KEY`; custom endpoints through `LiteLlm(api_base=...)`. `litellm` is an **optional/extra** dependency you must add. ([ADK models](https://google.github.io/adk-docs/agents/models/), [ADK LiteLLM](https://adk.dev/agents/models/litellm/))
- **Runtime / event-driven fit:** Built around a `Runner` + session service with a yield/pause/resume event loop; you call `runner.run` / `run_async` per message (request/response style), and it ships `adk web` / `adk api_server`. Crucially, **`fastapi`, `uvicorn`, and `starlette` are CORE dependencies** - a full web stack is pulled in even when you only embed the Runner. ([ADK runtime](https://adk.dev/runtime/), [pyproject.toml](https://raw.githubusercontent.com/google/adk-python/main/pyproject.toml))
- **Tool-calling:** Native (via LiteLLM to `deepseek-chat`). ([ADK models](https://google.github.io/adk-docs/agents/models/))
- **Memory / state:** Built-in Session + Memory services, keyed by session. ([ADK runtime](https://adk.dev/runtime/))
- **Footprint:** Heaviest here - `fastapi`, `uvicorn`, `starlette`, `google-genai`, OpenTelemetry api/sdk in the core. Python >=3.10. ([pyproject.toml](https://raw.githubusercontent.com/google/adk-python/main/pyproject.toml))
- **License / maintenance:** Apache-2.0, actively maintained by Google. ([adk-python](https://github.com/google/adk-python))

**Verdict:** Over-weight and mis-shaped. A request/response Runner plus a bundled web server is the opposite of a lean event-driven bot, and the DeepSeek path is a bolt-on. Eliminate.

## 2. LangGraph

Graph/state-machine library from LangChain; you compile a graph once and invoke it per event.

- **DeepSeek support:** Dedicated `langchain-deepseek` package - `ChatDeepSeek(model="deepseek-chat")`, key `DEEPSEEK_API_KEY`, base via `api_base`; or point `ChatOpenAI(base_url="https://api.deepseek.com", ...)` at it. Both are separate packages, not in core `langgraph`. ([ChatDeepSeek docs](https://docs.langchain.com/oss/python/integrations/chat/deepseek))
- **Runtime / event-driven fit:** A library, not a server - `graph.invoke(inputs, {"configurable": {"thread_id": ...}})` per event, no web framework pulled in. Maps cleanly onto a webhook: one `invoke` per group message, `thread_id` = the group. Good fit. ([Persistence docs](https://docs.langchain.com/oss/python/langgraph/persistence))
- **Tool-calling:** Standard LangChain `bind_tools` + prebuilt ReAct agent. ([ChatDeepSeek docs](https://docs.langchain.com/oss/python/integrations/chat/deepseek))
- **Memory / state:** **Built-in checkpointers** persist per-`thread_id` state - `InMemorySaver` (dev), `SqliteSaver` (light), `PostgresSaver` (production). Reuse a `thread_id` per group to accumulate history. This is the strongest built-in per-group memory of the five. ([Persistence docs](https://docs.langchain.com/oss/python/langgraph/persistence))
- **Footprint:** Lean core (`langchain-core`, `langgraph-checkpoint`, `langgraph-sdk`, `langgraph-prebuilt`, `pydantic`); no web server, DB savers are opt-in add-on packages. Python >=3.10. But it carries the broader LangChain ecosystem and its well-known API churn. ([pyproject.toml](https://raw.githubusercontent.com/langchain-ai/langgraph/main/libs/langgraph/pyproject.toml))
- **License / maintenance:** MIT, actively maintained by LangChain Inc. ([repo](https://github.com/langchain-ai/langgraph))

**Verdict:** Strongest on the two requirements a framework *should* own for you - durable per-group memory and an explicit reply-or-ignore control graph - at a lean core. The cost is LangChain-ecosystem weight and churn for machinery this low-complexity bot mostly won't exercise. The runner-up.

## 3. Pydantic AI

Focused, type-safe agent library from the Pydantic team; you call `agent.run()` per event.

- **DeepSeek support:** First-party DeepSeek provider - shorthand `Agent('deepseek:deepseek-chat')` (`DEEPSEEK_API_KEY`), or explicit `OpenAIChatModel('deepseek-chat', provider=DeepSeekProvider(api_key=...))`, via `pydantic-ai-slim[openai]`. ([Pydantic AI OpenAI/DeepSeek models](https://ai.pydantic.dev/models/openai/))
- **Runtime / event-driven fit:** Pure library - `agent.run()` / `run_sync()` / `run_stream()` per event, no server, no imposed loop. The cleanest fit for a webhook handler. ([message history docs](https://ai.pydantic.dev/message-history/))
- **Tool-calling:** Native. ([OpenAI models docs](https://ai.pydantic.dev/models/openai/))
- **Memory / state:** **Manual** - pass prior turns via `message_history=` and persist them yourself (`result.all_messages()` / `new_messages()`, JSON variants). Full control, but you build the per-group store. ([message history docs](https://ai.pydantic.dev/message-history/))
- **Footprint:** Lightest - `pydantic-ai-slim` + the `[openai]` extra; core is Pydantic + the `openai` client, no web server, no DB. Python >=3.10. ([pyproject.toml](https://raw.githubusercontent.com/pydantic/pydantic-ai/main/pyproject.toml))
- **License / maintenance:** MIT, maintained by the Pydantic team - a focused, low-surface library. ([pyproject.toml](https://raw.githubusercontent.com/pydantic/pydantic-ai/main/pyproject.toml))

**Verdict:** Best fit for *this* bot. Lightest footprint, cleanest event-per-call model, and - the decisive edge - **typed structured outputs** make the "decide whether to reply" gate a validated `ReplyDecision(should_reply: bool, ...)` object and make DeepSeek's news summaries schema-checked, which is exactly where an unofficial, cost-sensitive bot wants guardrails. Its one gap (manual memory) is small for a "mostly news, light replies" bot. The recommendation.

## 4. Agno (formerly Phidata)

Batteries-included agent platform; cheap agent objects you call per event.

- **DeepSeek support:** First-party `DeepSeek` model class - `Agent(model=DeepSeek(id="deepseek-chat"))`, key `DEEPSEEK_API_KEY`; it extends `OpenAILike`, so a generic OpenAI-compatible `base_url` path also works. ([Agno DeepSeek](https://docs.agno.com/examples/models/deepseek/basic), [Agno OpenAILike](https://docs.agno.com/reference/models/openai-like))
- **Runtime / event-driven fit:** `agent.run(...)` per event (sync/async/stream); agents are cheap to instantiate - fits a long-running webhook loop. ([Agno agents](https://docs.agno.com/introduction/agents))
- **Tool-calling:** Native, 100+ toolkits, parallel tool calls. ([Agno intro](https://docs.agno.com/introduction))
- **Memory / state:** Built-in sessions, memory, knowledge, traces, stored "in your own database." ([Agno intro](https://docs.agno.com/introduction))
- **Footprint:** Markets an extremely low footprint (headline: ~2μs instantiation, ~3.75 KiB per agent, vs LangGraph in its own benchmark). Treat the exact numbers as vendor claims. ([Agno performance](https://docs.agno.com/performance))
- **License / maintenance:** Apache-2.0, active (agno-agi/agno). ([repo](https://github.com/agno-agi/agno))

**Verdict:** Attractive middle - built-in memory like LangGraph, footprint near Pydantic AI, first-party DeepSeek. Loses to Pydantic AI on maturity and surface: it is a broader, faster-moving "platform" (more features, more churn) where this bot wants a small, stable, type-safe core. Honorable mention.

## 5. Plain DeepSeek via the OpenAI SDK + light orchestration

No framework - `openai` client pointed at DeepSeek, plus a webhook handler, a reply-gate, and a scheduler you write.

- **DeepSeek support:** Official and direct - `OpenAI(base_url="https://api.deepseek.com", api_key=...)`, `client.chat.completions.create(...)`. Tool calls and JSON mode (`response_format={'type':'json_object'}`) supported. ([DeepSeek API docs](https://api-docs.deepseek.com/), [tool calls](https://api-docs.deepseek.com/guides/tool_calls), [JSON mode](https://api-docs.deepseek.com/guides/json_mode))
- **Runtime / event-driven fit:** Perfect - there is no framework runtime to keep alive; you own the loop. One `create(...)` call per event.
- **Tool-calling:** Yes, standard OpenAI `tools` shape (you execute the tool and feed results back). ([tool calls](https://api-docs.deepseek.com/guides/tool_calls))
- **Memory / state:** None - you build the per-group history store (e.g. SQLite/Redis).
- **Footprint:** Smallest possible - `openai` + a web framework (+ optional `APScheduler`). ([openai-python](https://github.com/openai/openai-python), Apache-2.0)
- **License / maintenance:** `openai` SDK Apache-2.0, first-party.

**Verdict:** The minimalist floor. Total control and the smallest tree, but you hand-roll memory, the tool-execution loop, retries, and the reply-decision - re-inventing what Pydantic AI gives typed and tested. Reasonable fallback if a framework dependency is ever unacceptable; not the first choice for maintainability.

---

## Comparison at a glance

| Framework | DeepSeek path | Runtime fit (event-driven) | Web server pulled in | Tool-calling | Per-group memory | Footprint | License / maintainer |
|---|---|---|---|---|---|---|---|
| **Pydantic AI** | First-party provider (`[openai]`) | **Library, `agent.run()` per event** | No | Native | Manual (`message_history`) | **Lightest** | MIT / Pydantic |
| **LangGraph** | `langchain-deepseek` | Library, `invoke` per event | No | `bind_tools` | **Built-in checkpointer** | Lean core (+ LangChain churn) | MIT / LangChain |
| **Agno** | First-party `DeepSeek` class | Library, `agent.run()` per event | No | Native (+parallel) | Built-in | Very light (vendor claim) | Apache-2.0 / agno-agi |
| **Plain SDK** | Official OpenAI SDK | You own the loop | No | Manual loop | You build it | **Smallest** | Apache-2.0 / OpenAI |
| **Google ADK** | LiteLLM (optional dep) | Runner / request-response; ships server | **Yes (core)** | Native | Built-in | **Heaviest** | Apache-2.0 / Google |

---

## Recommendation

**Use Pydantic AI as the agent framework, wrapping the DeepSeek non-thinking chat model.**

It is the best match for *this* bot on the criteria that actually differ. It is a pure library you call once per event (`agent.run(...)`), which is exactly the shape of an Evolution-API-webhook-driven, long-running bot - no `Runner`, no bundled web server to fight (the disqualifier for Google ADK, whose `fastapi`/`uvicorn`/`starlette` are core deps ([ADK pyproject](https://raw.githubusercontent.com/google/adk-python/main/pyproject.toml))). It has a first-party DeepSeek provider ([Pydantic AI models](https://ai.pydantic.dev/models/openai/)), native tool-calling for the news fetch, and the lightest dependency footprint of the five ([pyproject.toml](https://raw.githubusercontent.com/pydantic/pydantic-ai/main/pyproject.toml)) - right for a cheap VPS/Coolify. Its decisive edge is **typed structured outputs**: the "decide whether to reply" control loop becomes a validated `ReplyDecision(should_reply: bool, reason: str)` object, and DeepSeek's news summaries are schema-checked - concrete guardrails for an unofficial, cost-sensitive bot. Its one gap - no built-in memory - is minor here: "mostly news, light replies" needs only a small per-group rolling message window, which is a trivial table you own outright (more transparent and robust than an opaque checkpointer), fed back via `message_history=` ([message history docs](https://ai.pydantic.dev/message-history/)).

**Runner-up: LangGraph.** Pick it instead if durable per-group memory should be the framework's job rather than yours: its per-`thread_id` checkpointer (SQLite -> Postgres) gives production-grade conversation persistence out of the box ([Persistence docs](https://docs.langchain.com/oss/python/langgraph/persistence)), and its graph models a reply-or-ignore control loop explicitly. The trade is LangChain-ecosystem weight and API churn for a bot whose agentic complexity is low. **Honorable mention: Agno** - built-in memory at a near-Pydantic-AI footprint, but a broader, faster-moving platform. **Floor: plain OpenAI SDK** - smallest tree and total control, but you re-implement memory, the tool loop, and the reply-gate that Pydantic AI gives typed and tested. **Rejected: Google ADK** - request/response Runner plus a bundled web server is the wrong shape and the heaviest footprint.

### Design notes that feed the architecture ticket ([#9](https://github.com/ivzc07/bienwabot/issues/9))

- **One process, two triggers:** a web handler (FastAPI/Starlette) for Evolution's inbound webhook, and a scheduler (`APScheduler`) for timed news posts - both call the same Pydantic AI agent, then POST back to Evolution's send endpoint.
- **Two model calls, one model family:** use the **non-thinking** DeepSeek chat model for anything with tools/structured output (reply-gate, news summarize); reserve any thinking model only for tool-free generation, per the tool-calling caveat above.
- **Memory = one small table** keyed by group JID holding a rolling window of recent turns + persona state; loaded into `message_history` per event. Revisit sizing once posting cadence is settled (still in the map's fog).
