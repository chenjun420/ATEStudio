"""ATERag review Agent (P4, D2) — a read-only chat face over ATERag's MCP tools.

What this is
------------
An engineer asks a question in prose; the agent answers using ATERag's
read-only MCP tools. It retrieves nothing from the database directly — every
fact comes from ``aterag-mcp`` over MCP, so the retrieval surface an LLM can
reach stays exactly the surface a human can audit.

Three properties are enforced here rather than assumed, because each of them
fails silently rather than loudly:

1. **No write tools.** The agent is given only the ATERag read tools. Anything
   that could change a criterion — approving a condition, committing a bundle
   — is absent by construction, not by policy. An agent able to approve would
   launder an unreviewed criterion into a production pass/fail bound, which is
   the one thing the red line exists to prevent.

2. **No numeric invention.** The system prompt forbids stating a number the
   tool did not return, and requires naming the tool and requirement code a
   figure came from. A language model will otherwise produce a plausible
   current limit from training data, and a plausible wrong current limit is
   indistinguishable from a right one once it reaches a work instruction.

3. **Refuses rather than guesses.** When ATERag's tools report ambiguity or
   absence, the agent must say so. The MCP tools already refuse to default to
   the first registered model; that refusal has to survive the round trip
   through the model, or the safety check was theatre.

Runtime
-------
The agent is assembled lazily and cached. Building it opens an MCP session, so
doing that at import time would make the whole app fail to start whenever
``aterag-mcp`` is unreachable — turning an optional feature into a hard
dependency of the service.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: Default ATERag MCP endpoint. Overridable by env so the same build talks to a
#: board at the factory or a laptop over a tunnel.
#:
#: Loopback, not the board's LAN address. ATERag runs on the same host as this
#: service, and a hardcoded LAN address was a standing outage waiting to happen:
#: the board moved from 192.168.5.24 to 192.168.5.25, and every product-type
#: lookup started failing with "无法连接 ATERag MCP (http://192.168.5.24:8080/mcp)"
#: while nothing about ATERag had actually broken. A default that is correct only
#: until the next DHCP lease is not a default, it is a countdown. ATERAG_MCP_URL
#: still overrides it for the laptop-over-a-tunnel case the comment above names.
DEFAULT_MCP_URL = os.getenv("ATERAG_MCP_URL", "http://127.0.0.1:8080/mcp")

#: Read-only tools this agent is allowed to use.
#:
#: Kept as an explicit allow-list with the denied tools named alongside it,
#: because the previous version of this comment claimed "everything ATERag
#: exposes beyond this list is a write or a side effect". That was false:
#: ``list_models`` and ``list_domain_rules`` are pure reads and were simply
#: missed, so the agent could not tell the engineer which model it was even
#: talking about. Naming the denials makes the next omission visible — a tool
#: that appears on the server and in neither set is caught by
#: ``tests/unit/test_aterag_agent.py``, which pins the server's tool list.
ALLOWED_TOOLS = frozenset(
    {
        # --- review surface (P4) -------------------------------------------
        "get_condition_detail",
        "list_pending_review",
        "get_coverage_summary",
        # --- catalogue / spec lookup ---------------------------------------
        "list_models",
        "list_domain_rules",
        "search_requirements",
        "query_parameters",
        "get_test_cases",
        "search_cases",
        "get_fixture_spec",
        # --- computation over already-stored facts -------------------------
        # calculate / optimize_process evaluate domain rules; validate_constraints
        # checks a caller-supplied graph. None of them persist anything, which is
        # what makes them safe to expose: they can be wrong, not destructive.
        "calculate",
        "validate_constraints",
        "optimize_process",
        # extract_test_conditions re-runs extraction in memory from the stored
        # blocks. It writes nothing (verified: no INSERT/UPDATE/commit in its
        # body), so it is a read that happens to be expensive.
        "extract_test_conditions",
        "health",
    }
)

#: Tools deliberately withheld, with the reason. Asserted against the server's
#: advertised tool list in tests so this cannot rot into a stale comment.
DENIED_TOOLS = {
    "ingest_document": "imports a spec document and builds a model KB (write)",
    "build_domain_kb": "builds/updates the shared product-type KB (write)",
}

SYSTEM_PROMPT = """你是 ATERag 产测规格助手, 帮助工程师理解规格书的抽取与评审结果。

## 数据来源
所有事实来自 ATERag 的 MCP 工具(只读)。你没有其他信息源。

## 硬性规则

1. **不得编造数值。** 只能引用工具返回的数字。每个数字都要能说出它来自哪个
   工具、哪条需求编号。工具没返回的量就说"没有数据" —— 猜一个看起来合理的
   限值, 会被当成规格书的判据写进作业指导书, 而那时没人能分辨它是真是假。

2. **工具报错就如实转述。** ATERag 会在下列情况拒绝回答, 你必须照实说,
   不要绕过:
   - `model_not_registered`: 型号没注册, 需要先导入规格书
   - `requirement_ambiguous`: 该规格编号下有多行, 需要用带消歧后缀的编号重查
   - `requirement_not_found`: 没找到该需求
   尤其不要在编号不明确时"选一个看起来对的"。

3. **区分判据来源。** 每条条件都带 source 与 status:
   - `status=approved` 且 `source=limits/notes`: 规格书原文, 是判据
   - `status=draft` 且 `source=industry_method`: 业界方法补齐的**提案**, 未经人审
   未经人审的条件**不得作为产测判据**, 已被排除在执行序列之外。提到它们时
   必须说明这一点。

4. **单边限值不是缺失。** 很多判据只有上限或只有下限(如过压保护只有上限)。
   这是常态, 不是抽取失败。不要因为"没有下限"就说判据不完整。

## 回答方式
先给结论, 再给依据(需求编号 + 章节号)。涉及"还剩多少没评审"这类问题时,
用 list_pending_review 的计数, 并说明未签字的条件不会被排进产测。
"""


class AgentUnavailableError(RuntimeError):
    """The agent could not be assembled — usually ``aterag-mcp`` unreachable."""


#: Control-flow exceptions that must never be swallowed into an
#: "unavailable" error — a cancelled request is not a dead MCP server.
_CONTROL_FLOW = (KeyboardInterrupt, SystemExit, asyncio.CancelledError)


def _is_control_flow(exc: BaseException) -> bool:
    """Whether an exception (or any nested one) is a control-flow signal.

    ``anyio`` runs MCP sessions in a TaskGroup, so a connection failure
    surfaces as a ``BaseExceptionGroup`` — which is *not* an ``Exception``
    subclass. A plain ``except Exception`` therefore misses it entirely and the
    caller gets a traceback instead of a handled error, which is the exact
    degradation this module exists to prevent.

    Unwrapping has to look at nested leaves: the group itself is not a
    control-flow exception, but one it carries may be.
    """
    if isinstance(exc, _CONTROL_FLOW):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_control_flow(leaf) for leaf in exc.exceptions)
    return False


def _build_client(url: str):
    from langchain_mcp_adapters.client import MultiServerMCPClient

    return MultiServerMCPClient(
        {"aterag": {"url": url, "transport": "streamable_http"}},
    )


async def _fetch_tools(url: str) -> list:
    """Fetch the MCP tool list, converting any failure to one error type.

    The conversion has to catch ``BaseException``: ``anyio`` runs MCP sessions
    in a TaskGroup, so a connection failure arrives as a
    ``BaseExceptionGroup``, which is not an ``Exception`` subclass. Two call
    sites need this, and the one that "forgets" turns a dead board into a
    traceback instead of the handled error callers branch on.
    """
    client = _build_client(url)
    try:
        return list(await client.get_tools(server_name="aterag"))
    except BaseException as exc:  # noqa: BLE001
        if _is_control_flow(exc):
            raise
        raise AgentUnavailableError(f"无法连接 ATERag MCP ({url}): {exc}") from exc


async def list_available_tools(url: str = DEFAULT_MCP_URL) -> list[str]:
    """Tools the ATERag MCP server actually exposes.

    Read-only and side-effect-free, so it is safe to call for diagnostics and
    for the UI's "is ATERag reachable" indicator.
    """
    return sorted(t.name for t in await _fetch_tools(url))


async def build_agent(url: str = DEFAULT_MCP_URL, *, model: str = ""):
    """Assemble the agent with the allowed read tools bound in.

    Raises :class:`AgentUnavailableError` rather than degrading to a tool-less
    agent: an agent with no tools answers from its training data, which is
    precisely the failure this module exists to prevent. A hard error here is
    visible; a plausible answer is not.
    """
    from langchain.agents import create_agent

    tools = await _fetch_tools(url)

    allowed = [t for t in tools if t.name in ALLOWED_TOOLS]
    dropped = sorted(t.name for t in tools if t.name not in ALLOWED_TOOLS)
    if dropped:
        # Logged rather than raised: a new upstream tool should not break the
        # agent, but it must be visible that it is not reachable from here.
        logger.warning(
            "ATERag MCP 暴露的工具未授权给 Agent: %s (Agent 面按设计只读)", dropped
        )
    if not allowed:
        raise AgentUnavailableError(
            f"ATERag MCP ({url}) 未暴露任何授权工具; 工具名: "
            f"{sorted(t.name for t in tools)}"
        )

    chat = _build_model(model)
    return create_agent(model=chat, tools=allowed, system_prompt=SYSTEM_PROMPT)


def _build_model(model: str = ""):
    """Chat model, from the same OpenAI-compatible config as the rest of app.

    Read from env rather than the app ``Settings`` so the agent can be used
    from a CLI or a test without constructing the whole service.
    """
    from langchain.chat_models import init_chat_model

    name = model or os.getenv("OPENAI_MODEL", "")
    if not name:
        raise AgentUnavailableError(
            "未配置 OPENAI_MODEL。Agent 面需要一个可用的 LLM 端点; "
            "若只需要只读查询能力, 直接用 GET /knowledge/conditions* 即可, 不必起 Agent。"
        )
    kwargs: dict[str, Any] = {"model": name}
    base_url = os.getenv("OPENAI_BASE_URL", "")
    api_key = os.getenv("OPENAI_API_KEY", "")
    if base_url:
        kwargs["base_url"] = base_url
    if api_key:
        kwargs["api_key"] = api_key
    return init_chat_model(**kwargs)


_agent_cache: dict[str, Any] = {}
_lock = asyncio.Lock()


async def get_agent(url: str = DEFAULT_MCP_URL, *, model: str = ""):
    """Cached agent handle.

    Cached because each build opens an MCP session; rebuilt per request it
    would add a round trip to the board for every question.
    """
    key = f"{url}|{model}"
    async with _lock:
        if key not in _agent_cache:
            _agent_cache[key] = await build_agent(url, model=model)
        return _agent_cache[key]


async def ask(question: str, *, url: str = DEFAULT_MCP_URL, model: str = "") -> dict[str, Any]:
    """Ask one question. Returns ``{answer, tools_used, error}``.

    ``tools_used`` is returned so the UI can show which facts the answer rests
    on. Without it, an engineer cannot tell a retrieved answer from a
    generated one — and the whole point of routing through MCP was to make
    that difference visible.
    """
    if not question.strip():
        return {"answer": "", "tools_used": [], "error": "问题为空"}
    try:
        agent = await get_agent(url, model=model)
    except AgentUnavailableError as exc:
        return {"answer": "", "tools_used": [], "error": str(exc)}

    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": question}]}
        )
    except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
        logger.exception("Agent 调用失败")
        return {"answer": "", "tools_used": [], "error": f"调用失败: {exc}"}

    messages = result.get("messages", [])
    answer = ""
    for m in reversed(messages):
        if getattr(m, "type", "") == "ai" and getattr(m, "content", ""):
            answer = str(m.content)
            break

    used: list[str] = []
    for m in messages:
        for call in getattr(m, "tool_calls", None) or []:
            name = (call or {}).get("name")
            if name and name not in used:
                used.append(name)

    return {"answer": answer, "tools_used": used, "error": None}

