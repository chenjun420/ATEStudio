"""Agent face tests (P4).

Run without an LLM and without ``aterag-mcp``: the point of most of these is
that the safety properties hold *before* any model is involved.

The three that matter:

- Only the allow-listed tools are bound. An agent that can approve a condition
  can launder an unreviewed criterion into a production bound — the one thing
  the red line exists to stop, and the one thing an allow-list prevents
  structurally rather than by instruction.
- No LLM configured → a hard error, never a tool-less agent. A tool-less agent
  answers from its training data, and a plausible wrong current limit looks
  exactly like a right one.
- MCP unreachable → an honest failure, not a degraded agent.
"""

from __future__ import annotations

from typing import Any

import pytest

from ate_cloud.services import aterag_agent as agent_mod


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeClient:
    """Stands in for MultiServerMCPClient."""

    def __init__(self, tools: list[str], boom: bool = False) -> None:
        self._tools = [FakeTool(n) for n in tools]
        self._boom = boom

    async def get_tools(self, server_name: str = "") -> list[FakeTool]:
        if self._boom:
            raise ConnectionError("connection refused")
        return self._tools


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    agent_mod._agent_cache.clear()
    yield
    agent_mod._agent_cache.clear()


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _install(tools: list[str], boom: bool = False) -> None:
        monkeypatch.setattr(
            agent_mod, "_build_client", lambda url: FakeClient(tools, boom=boom)
        )

    return _install


# ── the read-only boundary ──────────────────────────────────────────────────


class TestReadOnlyBoundary:
    def test_allow_list_excludes_every_write_capability(self) -> None:
        """Structural, not advisory: the names simply are not in the set."""
        for forbidden in (
            "approve_conditions",
            "import_bundle",
            "commit",
            "update_requirement",
            "write_annotation",
        ):
            assert forbidden not in agent_mod.ALLOWED_TOOLS

    @pytest.mark.asyncio
    async def test_unlisted_tools_are_dropped_from_the_agent(
        self, fake_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tool ATERag adds later is not reachable until it is listed here.

        Failing closed matters: if unknown tools were passed through, a new
        upstream write tool would silently become agent-callable.
        """
        fake_client(["get_coverage_summary", "approve_conditions_unsafe"])
        bound: list[str] = []

        def _fake_create_agent(model: Any, tools: Any, system_prompt: Any = None) -> str:
            bound.extend(t.name for t in tools)
            return "agent"

        monkeypatch.setattr(agent_mod, "_build_model", lambda model="": "chat")
        import langchain.agents as la

        monkeypatch.setattr(la, "create_agent", _fake_create_agent)
        assert await agent_mod.build_agent(url="http://x/mcp") == "agent"
        assert bound == ["get_coverage_summary"]

    @pytest.mark.asyncio
    async def test_no_tools_at_all_is_an_error(self, fake_client: Any, monkeypatch: Any) -> None:
        """An agent with no tools answers from its training data."""
        fake_client(["some_unexpected_tool"])
        monkeypatch.setattr(agent_mod, "_build_model", lambda model="": "chat")
        with pytest.raises(agent_mod.AgentUnavailableError, match="未暴露任何授权工具"):
            await agent_mod.build_agent(url="http://x/mcp")


# ── degraded-mode refusals ──────────────────────────────────────────────────


class TestRefusesRatherThanDegrades:
    @pytest.mark.asyncio
    async def test_unreachable_mcp_is_an_error(self) -> None:
        with pytest.raises(agent_mod.AgentUnavailableError, match="无法连接"):
            await agent_mod.list_available_tools(url="http://192.0.2.1:1/mcp")

    @pytest.mark.asyncio
    async def test_no_llm_configured_is_an_error(
        self, fake_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hard error, so the caller falls back to the plain read API rather
        than getting an agent that invents answers."""
        fake_client(["get_coverage_summary"])
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        with pytest.raises(agent_mod.AgentUnavailableError, match="OPENAI_MODEL"):
            await agent_mod.build_agent(url="http://x/mcp")

    def test_error_message_points_at_the_read_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The failure must say what to do instead, or an operator hunts for a
        working LLM when all they needed was the REST endpoint."""
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        with pytest.raises(agent_mod.AgentUnavailableError) as exc:
            agent_mod._build_model()
        assert "/knowledge/conditions" in str(exc.value)

    @pytest.mark.asyncio
    async def test_ask_returns_error_not_exception(self, fake_client: Any) -> None:
        """The route layer should get a structured error, not a 500 traceback."""
        out = await agent_mod.ask("SR-1100 测什么", url="http://192.0.2.1:1/mcp")
        assert out["error"]
        assert out["answer"] == ""
        assert out["tools_used"] == []

    @pytest.mark.asyncio
    async def test_blank_question_is_rejected(self) -> None:
        out = await agent_mod.ask("   ")
        assert out["error"] == "问题为空"
        assert out["tools_used"] == []


# ── the prompt encodes the rules that tools cannot ──────────────────────────


class TestSystemPrompt:
    def test_forbids_inventing_numbers(self) -> None:
        """An LLM will otherwise produce a plausible current limit from
        training data, and a plausible wrong limit is indistinguishable from a
        right one once it is printed on a work instruction."""
        assert "不得编造数值" in agent_mod.SYSTEM_PROMPT

    def test_requires_must_pass_through_tool_refusals(self) -> None:
        """ATERag refuses ambiguous lookups; that refusal has to survive the
        round trip through the model or the safety check was theatre."""
        for token in ("requirement_ambiguous", "model_not_registered"):
            assert token in agent_mod.SYSTEM_PROMPT

    def test_marks_draft_conditions_as_not_criteria(self) -> None:
        assert "未经人审" in agent_mod.SYSTEM_PROMPT
        assert "不得作为产测判据" in agent_mod.SYSTEM_PROMPT

    def test_explains_one_sided_limits_are_normal(self) -> None:
        """Without this the model reports a one-sided criterion as an extraction
        failure, which sends engineers hunting for a bug that isn't there."""
        assert "单边限值" in agent_mod.SYSTEM_PROMPT


# ── observability ───────────────────────────────────────────────────────────


class TestToolProvenance:
    @pytest.mark.asyncio
    async def test_answer_reports_which_tools_were_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without this, an engineer cannot tell a retrieved answer from a
        generated one — and routing through MCP existed to make that visible."""

        class Msg:
            def __init__(self, type_: str, content: str, calls: Any = None) -> None:
                self.type = type_
                self.content = content
                self.tool_calls = calls or []

        class FakeAgent:
            async def ainvoke(self, payload: Any) -> dict[str, Any]:
                return {
                    "messages": [
                        Msg("ai", "", [{"name": "get_coverage_summary"}]),
                        Msg("ai", "共 95 条需求", [{"name": "get_coverage_summary"}]),
                    ]
                }

        async def _get_agent(url: str = "", model: str = "") -> Any:
            return FakeAgent()

        monkeypatch.setattr(agent_mod, "get_agent", _get_agent)
        out = await agent_mod.ask("覆盖率?")
        assert out["error"] is None
        assert out["answer"] == "共 95 条需求"
        assert out["tools_used"] == ["get_coverage_summary"]


class TestAgentCaching:
    @pytest.mark.asyncio
    async def test_agent_is_built_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each build opens an MCP session to the board; per-request rebuilds
        would add a round trip for every question."""
        builds = 0

        async def _build(url: str = "", *, model: str = "") -> str:
            nonlocal builds
            builds += 1
            return f"agent-{builds}"

        monkeypatch.setattr(agent_mod, "build_agent", _build)
        await agent_mod.get_agent("http://x/mcp")
        await agent_mod.get_agent("http://x/mcp")
        await agent_mod.get_agent("http://x/mcp")
        assert builds == 1

    @pytest.mark.asyncio
    async def test_different_urls_get_separate_agents(self, monkeypatch: Any) -> None:
        """A cached handle from the factory board must not be served to a
        laptop pointed at a different one."""
        builds: list[str] = []

        async def _build(url: str = "", *, model: str = "") -> str:
            builds.append(url)
            return url

        monkeypatch.setattr(agent_mod, "build_agent", _build)
        await agent_mod.get_agent("http://a/mcp")
        await agent_mod.get_agent("http://b/mcp")
        assert builds == ["http://a/mcp", "http://b/mcp"]


# ── tool-set contract ────────────────────────────────────────────────────────


#: Exactly what `aterag-mcp` advertised over tools/list on 2026-09-30 at
#: http://192.168.5.25:8080/mcp (commit e7b5cc7), captured with the
#: initialize -> initialized -> tools/list handshake rather than read off a
#: docstring.
#:
#: This snapshot exists to catch drift in the direction the existing
#: fail-closed test cannot see. That test proves an *unlisted* tool is not
#: reachable; nothing proved a read tool ATERag newly added was ever noticed.
#: `list_models` and `list_domain_rules` sat in that gap: both are pure reads,
#: both were simply absent, and the agent could not report which model it was
#: reasoning about.
SERVER_TOOLS = frozenset(
    {
        # reads
        "calculate",
        "extract_test_conditions",
        "get_condition_detail",
        "get_coverage_summary",
        "get_fixture_spec",
        "get_test_cases",
        "health",
        "list_domain_rules",
        "list_models",
        "list_pending_review",
        "optimize_process",
        "query_parameters",
        "search_cases",
        "search_requirements",
        "validate_constraints",
        # writes
        "build_domain_kb",
        "ingest_document",
    }
)


class TestToolSetMatchesServer:
    def test_every_server_tool_is_classified(self) -> None:
        """No tool may be neither allowed nor explicitly denied.

        An unclassified tool is the exact failure this file was extended for:
        it is invisible in review, so it stays missing until an engineer
        notices the agent cannot answer something obvious.
        """
        unclassified = SERVER_TOOLS - agent_mod.ALLOWED_TOOLS - set(agent_mod.DENIED_TOOLS)
        assert not unclassified, (
            f"ATERag 新增了未分类的工具: {sorted(unclassified)}。"
            " 逐个判定读/写后, 加入 ALLOWED_TOOLS 或 DENIED_TOOLS。"
        )

    def test_no_tool_is_both_allowed_and_denied(self) -> None:
        assert not agent_mod.ALLOWED_TOOLS & set(agent_mod.DENIED_TOOLS)

    def test_whitelist_has_no_phantom_tools(self) -> None:
        """A name the server does not serve is dead config, not defence.

        A rename upstream would otherwise leave a stale entry that looks like
        coverage in every review of this file.
        """
        phantom = agent_mod.ALLOWED_TOOLS - SERVER_TOOLS
        assert not phantom, f"白名单里有服务器不存在的工具: {sorted(phantom)}"

    def test_denied_tools_are_the_write_ones(self) -> None:
        """Spelled out because the red line is the point of the whole file.

        Approving a condition, or importing a document, would let the model
        launder an unreviewed criterion into a production bound. The denial is
        structural — the tool is never bound to the agent — so this asserts the
        set rather than trusting a policy sentence.
        """
        assert set(agent_mod.DENIED_TOOLS) == {"ingest_document", "build_domain_kb"}
        for name in agent_mod.DENIED_TOOLS:
            assert name not in agent_mod.ALLOWED_TOOLS

    def test_every_denial_states_a_reason(self) -> None:
        """A denial with no recorded reason is the first thing to be 'fixed'
        by someone who assumes it is stale."""
        for name, reason in agent_mod.DENIED_TOOLS.items():
            assert reason.strip(), f"{name} 被拒绝但没写原因"



