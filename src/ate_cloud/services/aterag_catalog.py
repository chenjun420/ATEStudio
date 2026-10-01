"""ATERag 的型号目录,经**只读 MCP** 读取。

为什么在这里而不是从 ATERag 的库或文件里读
------------------------------------------
D2/D6: Agent 只读 MCP, 写回走管理面。ATEStudio 读 ATERag 的状态只有一条路 ——
它的 MCP server。绕开它去读 ATERag 的 `registry.yaml` 或 `power_specs` 库, 会在
两个仓库之间造出一条没有鉴权、没有审计、也没有版本约束的私线。

这个模块存在的理由, 是一句被写错的话
------------------------------------
P2 给 ``/api/v1/models`` 写文档时断言「型号与产品类型在数据上连不起来, 真正的映射在
ATERag 的 registry 里, 要等 P4 的 bundle 契约才到得了」。前半句对, 后半句错。

ATERag 的 ``src/aterag/registry.py`` 里早就有这个映射:
``ProductEntry{domain, doc_number, doc_version}`` 按 ``model_id`` 建索引, 持久化成
一个 YAML。而它的 MCP server 暴露成 ``list_models``, 返回
``{"products": {型号: 产品类型}, "domains": {产品类型: 状态}}`` —— 并且这个工具**已经
在** ``aterag_agent.ALLOWED_TOOLS`` 里(注释就写着 "catalogue / spec lookup")。

所以缺的不是数据, 是这一侧的接线。接上它, 规格 §4.2 的「产品类型 → 型号」两级上下文
才是真的两级。

复用已有的 ``_fetch_tools`` 而不是另建客户端
-------------------------------------------
``MultiServerMCPClient`` 没有 ``call_tool`` 方法, 但 ``get_tools()`` 返回的
``BaseTool`` 有 ``ainvoke``。走 ``_fetch_tools`` 同时白拿它对 MCP
``BaseExceptionGroup`` 的处理 —— 那是 ``anyio`` TaskGroup 的失败形态, 漏掉它会让
一块不通的板子变成 traceback 而不是调用方分支的错误。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ate_cloud.services.aterag_agent import (
    DEFAULT_MCP_URL,
    AgentUnavailableError,
    _fetch_tools,
)

logger = logging.getLogger(__name__)

#: The tool this module exists for. Named rather than discovered so that a server
#: that stops exposing it fails loudly instead of silently returning an empty
#: catalogue that the selector renders as "no models yet".
LIST_MODELS_TOOL = "list_models"


def _tool_text(result: Any) -> str:
    """Reduce a LangChain tool result to text.

    A ``BaseTool.ainvoke`` on an MCP tool returns either a plain string or a
    content-block list depending on version. Both are seen in the wild; guessing
    one is how this ends up as a JSON decode error at runtime.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts: list[str] = []
        for block in result:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(result)


async def fetch_model_catalog(url: str = DEFAULT_MCP_URL) -> dict[str, str]:
    """型号 -> 产品类型, from ATERag's read-only ``list_models``.

    Args:
        url: ATERag MCP endpoint. Defaults to :data:`DEFAULT_MCP_URL`.

    Returns:
        ``{product_code: product_type}``. Empty when ATERag has registered no
        models — that is a real state (nothing ingested yet), not an error.

    Raises:
        AgentUnavailableError: ATERag is unreachable, does not expose
            ``list_models``, or returned something unparseable. All three are
            reported the same way, because from the caller's side they mean the
            same thing: the catalogue is not available, and the reason belongs in
            a log line rather than in a silently empty selector.
    """
    tools = await _fetch_tools(url)
    tool = next((t for t in tools if t.name == LIST_MODELS_TOOL), None)
    if tool is None:
        available = sorted(t.name for t in tools)
        raise AgentUnavailableError(
            f"ATERag MCP 未暴露 {LIST_MODELS_TOOL}; 实际可用工具: {available}"
        )

    try:
        raw = _tool_text(await tool.ainvoke({}))
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise AgentUnavailableError(
            f"{LIST_MODELS_TOOL} 返回的不是可解析的 JSON: {exc}"
        ) from exc

    products = data.get("products") if isinstance(data, dict) else None
    if not isinstance(products, dict):
        raise AgentUnavailableError(
            f"{LIST_MODELS_TOOL} 返回结构不含 products: {type(products).__name__}"
        )

    return {str(k): str(v) for k, v in products.items() if v}


async def fetch_domain_status(url: str = DEFAULT_MCP_URL) -> dict[str, str]:
    """产品类型 -> 知识库状态 (``empty`` / ``populated``).

    Separate from :func:`fetch_model_catalog` because the selector does not need
    it, and a second round trip for data nothing displays would be paid on every
    mode switch.
    """
    tools = await _fetch_tools(url)
    tool = next((t for t in tools if t.name == LIST_MODELS_TOOL), None)
    if tool is None:
        raise AgentUnavailableError(f"ATERag MCP 未暴露 {LIST_MODELS_TOOL}")
    try:
        data = json.loads(_tool_text(await tool.ainvoke({})))
    except Exception as exc:  # noqa: BLE001
        raise AgentUnavailableError(f"{LIST_MODELS_TOOL} 解析失败: {exc}") from exc
    domains = data.get("domains") if isinstance(data, dict) else None
    if not isinstance(domains, dict):
        return {}
    return {str(k): str(v) for k, v in domains.items()}


__all__ = ["AgentUnavailableError", "fetch_domain_status", "fetch_model_catalog"]
