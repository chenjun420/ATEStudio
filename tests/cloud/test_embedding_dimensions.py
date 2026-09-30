"""向量宽度是模型的属性,不是 API 的属性 —— 两处依赖它的地方都得守住。

背景
----
本部署用的嵌入模型是 ``qwen3.7-text-embedding``,实测返回 **1024** 维,而代码
默认写的是 1536(OpenAI ``text-embedding-3-small`` 的尺寸)。两个后果:

1. **启动时**, ``ensure_collection`` 只检查集合是否存在、从不校验宽度。
2. **运行时**, ``index_failure`` 是后台任务,写入失败只记日志、从不抛出。

于是「不匹配」既不报错也不阻止启动, 只是故障历史**悄悄停止积累**, 而面板总数
看起来一切正常 —— 每一台设备都停在同一个数字上, 没人知道它不再更新了。

LangChain 的 tokenizer 第二个问题
------------------------------
``OpenAIEmbeddings`` 默认在本地分词并发送 token id。那只在模型用 OpenAI 的
tokenizer 时才正确: 对 Qwen 模型, 用 OpenAI 的 token 数去切分长文本是错的, 而
DashScope 的 OpenAI 兼容端点干脆拒收, 报 ``input must be an array of strings``。
所以 ``check_embedding_ctx_length`` 默认关闭, 并记下打开它会失去什么。
"""

from __future__ import annotations

from typing import Any

import pytest

from ate_cloud.config import settings
from ate_cloud.services.failure_indexer import (
    EmbeddingDimensionMismatchError,
    FailureIndexer,
)


class _VectorParams:
    def __init__(self, size: int) -> None:
        self.size = size


class _Config:
    def __init__(self, size: int) -> None:
        self.params = type("P", (), {"vectors": _VectorParams(size)})()


class _CollectionInfo:
    def __init__(self, size: int) -> None:
        self.config = _Config(size)


class _FakeQdrant:
    """Minimal Qdrant double: enough surface for ``ensure_collection``."""

    def __init__(self, existing: dict[str, int] | None = None) -> None:
        self.existing = dict(existing or {})
        self.created: dict[str, int] = {}

    def get_collections(self) -> Any:
        names = type("N", (), {"collections": [type("C", (), {"name": n})() for n in self.existing]})()
        return type("R", (), {"collections": names.collections})()

    def create_collection(self, collection_name: str, vectors_config: Any) -> None:
        self.created[collection_name] = vectors_config.size

    def get_collection(self, name: str) -> _CollectionInfo:
        return _CollectionInfo(self.existing[name])


def _indexer(client: _FakeQdrant, dim: int = 1024) -> FailureIndexer:
    return FailureIndexer(
        qdrant_client=client,  # type: ignore[arg-type]
        embedding_service=None,  # type: ignore[arg-type]
        embedding_dim=dim,
        collection_name="ate_failures",
    )


class TestACollectionOfTheWrongWidthIsRefusedNotAdopted:
    async def test_mismatch_raises_rather_than_proceeding(self) -> None:
        """The one behaviour that matters: refuse, loudly.

        Recreating the collection would be the tempting alternative and it is
        wrong — it deletes the history that *does* match, to fix a problem one
        environment variable away.
        """
        client = _FakeQdrant({"ate_failures": 1536})
        with pytest.raises(EmbeddingDimensionMismatchError) as ei:
            await _indexer(client, dim=1024).ensure_collection()

        assert "1536" in str(ei.value) and "1024" in str(ei.value)

    async def test_the_message_names_the_setting_to_change(self) -> None:
        """A message that does not say what to change is just an error."""
        client = _FakeQdrant({"ate_failures": 1536})
        with pytest.raises(EmbeddingDimensionMismatchError) as ei:
            await _indexer(client, dim=1024).ensure_collection()

        message = str(ei.value)
        assert "ATE_CLOUD_EMBEDDING_DIMENSIONS" in message
        # And it must say what the consequence is, or an operator reads a config
        # mismatch and assumes it is cosmetic.
        assert "silently" in message

    async def test_a_matching_collection_is_left_alone(self) -> None:
        client = _FakeQdrant({"ate_failures": 1024})
        await _indexer(client, dim=1024).ensure_collection()
        assert client.created == {}, "宽度相符时不该重建集合"

    async def test_an_absent_collection_is_created_at_the_configured_width(self) -> None:
        client = _FakeQdrant()
        await _indexer(client, dim=1024).ensure_collection()
        assert client.created == {"ate_failures": 1024}

    async def test_a_transient_qdrant_error_is_still_swallowed(self) -> None:
        """The mismatch guard must not turn Qdrant being down into a boot failure.

        Being unable to reach Qdrant has always degraded to a logged skip and
        should keep doing so — that is a retryable condition. A wrong width is
        not, and is the only one that now propagates.
        """

        class _Down(_FakeQdrant):
            def get_collections(self) -> Any:
                raise ConnectionError("qdrant unreachable")

        await _indexer(_Down(), dim=1024).ensure_collection()  # does not raise


class TestLocalTokenizationIsOffByDefault:
    """Sending token ids is only correct for OpenAI's own tokenizer."""

    def test_default_is_off(self) -> None:
        assert settings.embedding_check_ctx_length is False

    def test_it_reaches_the_langchain_client(self, monkeypatch) -> None:
        """Not just a config value that nothing reads.

        A setting declared but never passed through is the same class of bug as
        a key present in ``.env`` with an empty value.
        """
        captured: dict[str, Any] = {}

        class _FakeEmbeddings:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        class _FakeSecretStr(str):
            pass

        import sys
        import types

        fake_lc = types.ModuleType("langchain_openai")
        fake_lc.OpenAIEmbeddings = _FakeEmbeddings
        fake_pyd = types.ModuleType("pydantic")
        fake_pyd.SecretStr = _FakeSecretStr
        monkeypatch.setitem(sys.modules, "langchain_openai", fake_lc)
        monkeypatch.setitem(sys.modules, "pydantic", fake_pyd)
        monkeypatch.setattr(settings, "embedding_check_ctx_length", False, raising=False)
        monkeypatch.setattr(settings, "openai_base_url", "https://example.invalid/v1", raising=False)

        from ate_cloud.services.embedding_service import EmbeddingService

        EmbeddingService(api_key="k", model="m", dimensions=1024)

        assert captured["check_embedding_ctx_length"] is False
        assert captured["dimensions"] == 1024

    def test_an_explicit_argument_overrides_the_setting(self, monkeypatch) -> None:
        captured: dict[str, Any] = {}

        class _FakeEmbeddings:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        import sys
        import types

        fake_lc = types.ModuleType("langchain_openai")
        fake_lc.OpenAIEmbeddings = _FakeEmbeddings
        fake_pyd = types.ModuleType("pydantic")
        fake_pyd.SecretStr = str
        monkeypatch.setitem(sys.modules, "langchain_openai", fake_lc)
        monkeypatch.setitem(sys.modules, "pydantic", fake_pyd)
        monkeypatch.setattr(settings, "openai_base_url", "", raising=False)

        from ate_cloud.services.embedding_service import EmbeddingService

        EmbeddingService(api_key="k", model="m", dimensions=1024, check_ctx_length=True)

        assert captured["check_embedding_ctx_length"] is True
