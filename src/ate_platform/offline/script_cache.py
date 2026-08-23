"""离线脚本磁盘缓存（设计文档 §10.5.2 脚本缓存层）。

端侧缓存分层第二层：脚本文件落本地磁盘 + sidecar 元数据（版本 / SHA256 /
时间戳）。核心契约（§10.5.2 管理策略「下发时整包同步；执行前校验文件哈希，
失败则用上一可用版本并告警」）：

- **原子写入**：内容文件先写临时文件、fsync、回读校验 SHA256 通过后才
  ``os.replace`` 就位——新版本验证通过前绝不触碰上一可用版本；
- **读取时校验**：:meth:`OfflineScriptCache.get_script` 每次重算文件 SHA256
  并与 sidecar 比对，不匹配（篡改/半写/文件丢失）视为损坏；
- **回退上一可用版本并告警**：损坏时按版本新旧倒序寻找最近一个可校验通过的
  历史版本返回，并以 ``logging.warning`` 点名 script_id；全部候选均损坏则抛
  :class:`ScriptCorruptionError` —— 绝不静默返回损坏内容；
- **最新版本确定性**：Windows ``time.time()`` 粒度粗（~15ms），sidecar 额外
  记录单调递增写入序号 ``seq``，最新版本以 ``(stored_at, seq)`` 决胜；
- **上传时哈希不旁路**：云端下发若携带校验和，store 时强制比对。

本模块只做纯本地缓存层：上传队列/对账/容量保护属 T20-T23，不在此实现。
与 T18 的 SQLite 层（序列/拓扑）互不影响：脚本按 §10.5.2 走磁盘+元数据层。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

_CONTENT_SUFFIX = ".script"
_META_SUFFIX = ".meta.json"
_CHUNK = 65536


class ScriptCacheError(Exception):
    """离线脚本缓存层异常基类。"""


class ScriptMissError(ScriptCacheError):
    """缓存中不存在该脚本（id 完全未知）。"""


class ScriptVersionMismatchError(ScriptCacheError):
    """脚本存在但请求的版本从未缓存过（§10.5.4.2 版本一致性）。"""


class ScriptCorruptionError(ScriptCacheError):
    """所有候选版本的文件哈希均校验失败 —— 拒绝服务，绝不静默返回损坏内容。"""


def sha256_text(content: str) -> str:
    """计算文本的 SHA256 十六进制摘要（UTF-8 编码）。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    """流式计算文件内容的 SHA256 十六进制摘要。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ScriptStatus:
    """脚本缓存条目状态视图（不含载荷，供列表/UI 与 T21 对账使用）。"""

    script_id: str
    version: str
    checksum: str
    stored_at: float
    size_bytes: int
    intact: bool  # 读时校验结果 —— 列表即缓存健康视图（§10.5.1 状态可感知）


def _safe_name(raw: str) -> str:
    """将 script_id / version 编码为单段安全路径名（保留可读性，转义分隔符）。"""
    encoded = quote(raw, safe="")
    if not encoded or encoded.startswith("."):
        raise ValueError(f"unusable cache name derived from {raw!r}")
    return encoded


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """tmp 写入 + fsync + 回读校验 + os.replace 的原子落盘。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class OfflineScriptCache:
    """SHA256 校验 + 上一可用版本回退的脚本磁盘缓存。

    线程安全：全部操作经 :class:`threading.RLock` 串行化——端侧脚本仅在
    下发/执行前访问，锁开销可忽略。
    """

    def __init__(self, cache_root: str | Path) -> None:
        self._root = Path(cache_root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        logger.info("script_cache_opened: root=%s", self._root)

    # ------------------------------------------------------------------
    # 路径与 sidecar 解析
    # ------------------------------------------------------------------
    def _script_dir(self, script_id: str) -> Path:
        return self._root / _safe_name(script_id)

    def _paths(self, script_id: str, version: str) -> tuple[Path, Path]:
        base = self._script_dir(script_id) / _safe_name(version)
        return base.with_suffix(_CONTENT_SUFFIX), base.with_suffix(_META_SUFFIX)

    def _read_meta(self, meta_path: Path) -> dict | None:
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    def _iter_metas(self, script_id: str) -> list[dict]:
        """该脚本全部 sidecar，按 (stored_at, seq) 新→旧排序（Windows 同秒决胜靠 seq）。"""
        script_dir = self._script_dir(script_id)
        metas: list[dict] = []
        if not script_dir.is_dir():
            return metas
        for meta_path in script_dir.glob(f"*{_META_SUFFIX}"):
            meta = self._read_meta(meta_path)
            if meta is not None and {"script_id", "version", "sha256", "stored_at", "seq"} <= meta.keys():
                metas.append(meta)
        metas.sort(key=lambda m: (m["stored_at"], m["seq"]), reverse=True)
        return metas

    def _next_seq(self, script_id: str) -> int:
        seqs = [int(m["seq"]) for m in self._iter_metas(script_id)]
        return max(seqs, default=-1) + 1

    def _verify(self, content_path: Path, expected_sha: str) -> bool:
        try:
            return content_path.is_file() and sha256_file(content_path) == expected_sha
        except OSError:
            return False

    # ------------------------------------------------------------------
    # 写入（下发即缓存，整包同步）
    # ------------------------------------------------------------------
    def store_script(
        self, script_id: str, version: str, content: str, checksum: str | None = None
    ) -> None:
        """缓存一份脚本：内容文件 + sidecar 元数据，原子写入。

        Args:
            script_id: 脚本标识。
            version: 脚本版本。
            content: 脚本全文（UTF-8 文本）。
            checksum: 云端上传时计算的 SHA256；提供时不匹配即拒绝，
                保证端侧缓存不旁路上传时哈希链。

        Raises:
            TypeError: content 非 str。
            ValueError: id/version 为空，或提供的 checksum 与内容不符。
        """
        if not isinstance(content, str):
            raise TypeError("content must be a str (script text)")
        if not script_id or not version:
            raise ValueError("script_id and version must be non-empty")
        computed = sha256_text(content)
        if checksum is not None and checksum != computed:
            raise ValueError(
                f"checksum mismatch for scripts/{script_id}@{version}: "
                f"provided {checksum[:12]}… != computed {computed[:12]}…"
            )

        content_path, meta_path = self._paths(script_id, version)
        with self._lock:
            existing = self._read_meta(meta_path)
            if (
                existing is not None
                and existing.get("sha256") == computed
                and self._verify(content_path, computed)
            ):
                # 幂等重发（同 id+version+内容）：保留原时间戳/序号，
                # 云端重试不应扰动最新版本解析
                logger.debug("script_cache_store_idempotent: scripts/%s@%s unchanged", script_id, version)
                return

            data = content.encode("utf-8")
            content_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=content_path.parent, prefix=f".{content_path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(tmp_fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                # §10.5.2：新版本验证通过前绝不替换上一可用版本——
                # 回读临时文件校验 SHA256，失败则丢弃 tmp 并原样保留旧版本
                if sha256_file(tmp_name) != computed:
                    raise ScriptCorruptionError(
                        f"scripts/{script_id}@{version}: post-write verification failed; "
                        "last-good version left untouched"
                    )
                os.replace(tmp_name, content_path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

            meta = {
                "script_id": script_id,
                "version": version,
                "sha256": computed,
                "stored_at": time.time(),
                "seq": self._next_seq(script_id),
                "size": len(data),
            }
            _atomic_write_bytes(
                meta_path, json.dumps(meta, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )
        logger.debug("script_cache_stored: scripts/%s@%s", script_id, version)

    # ------------------------------------------------------------------
    # 读取（执行前校验文件哈希 → 失败回退上一可用版本并告警）
    # ------------------------------------------------------------------
    def get_script(self, script_id: str, version: str | None = None) -> str:
        """取回脚本全文；请求版本损坏时自动回退最近可用版本并告警。

        Args:
            script_id: 脚本标识。
            version: 期望版本；``None`` 取最新缓存版本。

        Raises:
            ScriptMissError: id 完全不存在。
            ScriptVersionMismatchError: 请求的版本从未缓存过。
            ScriptCorruptionError: 所有候选版本均校验失败。
        """
        with self._lock:
            metas = self._iter_metas(script_id)
            if not metas:
                raise ScriptMissError(f"scripts/{script_id}: no cached versions")

            if version is None:
                candidates = list(metas)
            else:
                pinned = [m for m in metas if m["version"] == version]
                if not pinned:
                    raise ScriptVersionMismatchError(
                        f"scripts/{script_id}@{version}: version never cached"
                    )
                # §10.5.2 回退无版本条件：锁定版本损坏时同样退回最近可用历史版本
                candidates = pinned + [m for m in metas if m["version"] != version]

            first_bad: str | None = None
            for meta in candidates:
                ver, sha = str(meta["version"]), str(meta["sha256"])
                content_path, _ = self._paths(script_id, ver)
                if self._verify(content_path, sha):
                    if first_bad is not None:
                        logger.info(
                            "script_cache_fallback_served: scripts/%s served %s "
                            "after %s failed verification",
                            script_id,
                            ver,
                            first_bad,
                        )
                    return content_path.read_text(encoding="utf-8")
                if first_bad is None:
                    first_bad = ver
                # §10.5.2：校验失败 → 用上一可用版本并告警（点名 script_id）
                logger.warning(
                    "script_cache_corruption_fallback: scripts/%s@%s failed SHA256 "
                    "verification (%s); falling back to next cached version",
                    script_id,
                    ver,
                    "file missing" if not content_path.exists() else "hash mismatch",
                )
            raise ScriptCorruptionError(
                f"scripts/{script_id}: all cached versions failed verification "
                f"(newest bad: {first_bad if first_bad else '?'}) — refusing to serve"
            )

    # ------------------------------------------------------------------
    # 列表 / 删除 / 清理
    # ------------------------------------------------------------------
    def list_scripts(self, script_id: str | None = None) -> list[ScriptStatus]:
        """列出缓存条目状态视图（含读时完整性标志，不含载荷）。"""
        ids = [script_id] if script_id is not None else self._all_ids()
        entries: list[ScriptStatus] = []
        with self._lock:
            for sid in ids:
                for meta in self._iter_metas(sid):
                    content_path, _ = self._paths(sid, str(meta["version"]))
                    entries.append(
                        ScriptStatus(
                            script_id=sid,
                            version=str(meta["version"]),
                            checksum=str(meta["sha256"]),
                            stored_at=float(meta["stored_at"]),
                            size_bytes=int(meta.get("size", 0)),
                            intact=self._verify(content_path, str(meta["sha256"])),
                        )
                    )
        entries.sort(key=lambda e: (e.script_id, -e.stored_at))
        return entries

    def delete(self, script_id: str, version: str | None = None) -> int:
        """删除指定版本（version 给定）或该脚本全部版本。返回删除条目数。"""
        removed = 0
        with self._lock:
            targets = [str(m["version"]) for m in self._iter_metas(script_id)]
            if version is not None:
                targets = [v for v in targets if v == version]
            for ver in targets:
                content_path, meta_path = self._paths(script_id, ver)
                had_meta = False
                for path in (content_path, meta_path):
                    try:
                        path.unlink()
                        if path == meta_path:
                            had_meta = True
                    except FileNotFoundError:
                        pass
                if had_meta:
                    removed += 1  # 按条目计数（sidecar 为存在性凭据）
            script_dir = self._script_dir(script_id)
            if script_dir.is_dir() and not any(script_dir.iterdir()):
                script_dir.rmdir()
        return removed

    def prune(self, keep_last_n: int = 2) -> int:
        """全库按脚本保留最近 N 个版本（§10.5.2「保留最近 N 个版本」）。

        最新序以 ``(stored_at, seq)`` 决胜，保证 Windows 同秒连存时
        最后写入的版本存活。
        """
        if keep_last_n < 1:
            raise ValueError("keep_last_n must be >= 1")
        removed = 0
        with self._lock:
            for sid in self._all_ids():
                versions = [str(m["version"]) for m in self._iter_metas(sid)]
                for ver in versions[keep_last_n:]:
                    removed += self.delete(sid, ver)
        return removed

    def _all_ids(self) -> list[str]:
        """扫描根目录还原全部 script_id（sidecar 内保存原名）。"""
        ids: set[str] = set()
        if not self._root.is_dir():
            return []
        for meta_path in self._root.glob(f"*/*{_META_SUFFIX}"):
            meta = self._read_meta(meta_path)
            if meta is not None and meta.get("script_id"):
                ids.add(str(meta["script_id"]))
        return sorted(ids)
