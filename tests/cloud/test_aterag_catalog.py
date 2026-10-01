"""型号目录经只读 MCP 读取的行为。

这个模块是 P2 的「产品类型 → 型号」两级上下文里那一级的唯一来源, 所以它需要自己
的测试, 而不是靠 ``/api/v1/models`` 间接覆盖 —— 间接覆盖看不见"工具不存在"
和"返回的不是 JSON"这两种情况, 而这两种恰好是最容易在板子上才暴露的。

关键约束
--------
* 只读。走的是 ``list_models``, 没有写入路径, 符合 D2/D6。
* 失败一律转成 ``AgentUnavailableError``。三种失败(连不上 / 没有这个工具 /
  返回不可解析)在调用方看来是同一件事: 目录不可用, 原因进日志而不是变成一个
  静悄悄的空选择器。
* 工具结果是字符串还是内容块列表, 取决于版本。两种都见过, 猜一种就是把这个变成
  运行时的 JSON 解码错误。
"""

from __future__ import annotations

import json

import pytest

from ate_cloud.services.aterag_agent import AgentUnavailableError
from ate_cloud.services.aterag_catalog import (
    LIST_MODELS_TOOL,
    _tool_text,
    fetch_domain_status,
    fetch_model_catalog,
)

#: Exactly what ATERag's ``list_models`` returns
#: (``src/aterag/mcp_server/server.py``).
SERVER_PAYLOAD = {
    "products": {"MDL-A": "空调", "MDL-B": "空调", "MDL-C": "连接器"},
    "domains": {"空调": "populated", "连接器": "empty"},
}


class _FakeTool:
    """Stands in for the ``BaseTool`` that ``get_tools()`` returns."""

    def __init__(self, name: str, result: object) -> None:
        self.name = name
        self._result = result
        self.calls: list[dict] = []

    async def ainvoke(self, args: dict) -> object:
        self.calls.append(args)
        return self._result


def _patch_tools(monkeypatch: pytest.MonkeyPatch, tools: list[_FakeTool]) -> None:
    async def fake_fetch(url: str) -> list:
        return tools

    monkeypatch.setattr(
        "ate_cloud.services.aterag_catalog._fetch_tools", fake_fetch
    )


class TestToolText:
    """Both shapes a LangChain MCP tool result arrives in."""

    def test_plain_string(self) -> None:
        assert _tool_text("hello") == "hello"

    def test_content_block_list(self) -> None:
        assert _tool_text([{"type": "text", "text": "he"}]) == "he"
        assert _tool_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"

    def test_mixed_list(self) -> None:
        # A bare string inside the list is seen in practice; dropping it would
        # silently truncate the JSON.
        assert _tool_text(["a", {"type": "text", "text": "b"}]) == "ab"

    def test_non_text_blocks_are_ignored_not_stringified(self) -> None:
        # An image block stringified into the middle of JSON breaks parsing.
        assert _tool_text([{"type": "image", "data": "..."}]) == ""

    def test_unknown_shape_falls_back_to_str(self) -> None:
        assert _tool_text(42) == "42"


class TestFetchModelCatalog:
    @pytest.mark.asyncio
    async def test_returns_the_model_to_type_mapping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool = _FakeTool(LIST_MODELS_TOOL, json.dumps(SERVER_PAYLOAD, ensure_ascii=False))
        _patch_tools(monkeypatch, [tool])

        assert await fetch_model_catalog() == {
            "MDL-A": "空调",
            "MDL-B": "空调",
            "MDL-C": "连接器",
        }
        assert tool.calls == [{}], "这个工具不接受参数, 也不该被喂参数"

    @pytest.mark.asyncio
    async def test_accepts_the_content_block_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other result shape, so the version difference cannot bite us."""
        payload = json.dumps(SERVER_PAYLOAD, ensure_ascii=False)
        _patch_tools(
            monkeypatch,
            [_FakeTool(LIST_MODELS_TOOL, [{"type": "text", "text": payload}])],
        )

        assert (await fetch_model_catalog())["MDL-A"] == "空调"

    @pytest.mark.asyncio
    async def test_a_model_with_an_empty_type_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty domain is not a product type.

        Keeping it would put an empty option in the selector's first level, which
        then filters to nothing — a level that looks real and never matches.
        """
        _patch_tools(
            monkeypatch,
            [
                _FakeTool(
                    LIST_MODELS_TOOL,
                    json.dumps({"products": {"MDL-A": "空调", "MDL-X": ""}}),
                )
            ],
        )
        assert await fetch_model_catalog() == {"MDL-A": "空调"}

    @pytest.mark.asyncio
    async def test_an_empty_catalogue_is_empty_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing registered yet is a real state, not a failure."""
        _patch_tools(
            monkeypatch,
            [_FakeTool(LIST_MODELS_TOOL, json.dumps({"products": {}, "domains": {}}))],
        )
        assert await fetch_model_catalog() == {}

    @pytest.mark.asyncio
    async def test_missing_tool_names_what_is_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A server that stopped exposing it must say so, listing what it has.

        Silently returning {} here would render as "no models yet" — telling an
        engineer their specification import failed to register anything, when the
        real problem is a tool rename.
        """
        _patch_tools(monkeypatch, [_FakeTool("health", "{}"), _FakeTool("calculate", "{}")])

        with pytest.raises(AgentUnavailableError) as exc:
            await fetch_model_catalog()
        assert LIST_MODELS_TOOL in str(exc.value)
        assert "calculate" in str(exc.value)

    @pytest.mark.asyncio
    async def test_unparseable_output_is_reported_as_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_tools(monkeypatch, [_FakeTool(LIST_MODELS_TOOL, "not json at all")])

        with pytest.raises(AgentUnavailableError) as exc:
            await fetch_model_catalog()
        assert "JSON" in str(exc.value)

    @pytest.mark.asyncio
    async def test_missing_products_key_is_not_treated_as_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`{}` means "nothing registered"; `{"domains": ...}` means the shape changed.

        Collapsing the second into the first is how a contract break turns into a
        selector that quietly offers nothing.
        """
        _patch_tools(
            monkeypatch,
            [_FakeTool(LIST_MODELS_TOOL, json.dumps({"domains": {"空调": "empty"}}))],
        )
        with pytest.raises(AgentUnavailableError) as exc:
            await fetch_model_catalog()
        assert "products" in str(exc.value)

    @pytest.mark.asyncio
    async def test_domain_status_comes_from_the_same_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_tools(
            monkeypatch,
            [_FakeTool(LIST_MODELS_TOOL, json.dumps(SERVER_PAYLOAD, ensure_ascii=False))],
        )
        assert await fetch_domain_status() == {"空调": "populated", "连接器": "empty"}


class TestAllowedToolsContract:
    """The catalogue must stay on the allow-list.

    D2/D6: the agent reads ATERag over MCP and within an explicit allow-list. A
    tool that works but is not allowlisted is a tool the next person will "just
    add", and this is the place where that gets caught.
    """

    def test_list_models_is_allowlisted(self) -> None:
        from ate_cloud.services.aterag_agent import ALLOWED_TOOLS

        assert LIST_MODELS_TOOL in ALLOWED_TOOLS, (
            f"{LIST_MODELS_TOOL} 不在只读白名单里 —— 目录读得到但 Agent 用不了"
        )
