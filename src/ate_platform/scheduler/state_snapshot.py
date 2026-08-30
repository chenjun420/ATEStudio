"""持久化状态快照与崩溃恢复（设计文档 §6.6）。

崩溃恢复的关键：在进程崩溃后，仅凭内存状态无法得知执行到哪一步。
本模块提供 :class:`StateSnapshot`，将步骤状态、变量、UUT 状态原子写入
本地 JSON 文件；下次启动时 :meth:`can_resume` 判定可恢复，:meth:`load`
还原状态，完成后 :meth:`cleanup` 清理快照。

设计要点：
- 原子写：先写临时文件再 ``os.replace``，避免写一半崩溃留下损坏文件；
- 损坏容错：加载时 JSON 解析失败只记录 warning 并返回 ``None``（视为不可恢复，
  从零开始），而不是让调度器带着残缺状态启动；
- 恢复流程（§6.6）由调用方（ScannerScheduler）编排：恢复步骤状态 → 恢复变量
  → 恢复 UUT 状态 → 重置仪器（*RST）→ 重建夹具状态。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: 快照中必须存在且非空才视为可恢复的键
_RESUME_MARKER = "step_states"


class StateSnapshot:
    """执行状态快照：原子保存 / 加载 / 可恢复判定 / 清理。

    Attributes:
        snapshot_path: 快照文件路径（默认 ``<dir>/ate_scheduler_snapshot.json``）。
    """

    def __init__(self, snapshot_dir: str | os.PathLike[str]) -> None:
        """初始化快照管理器。

        Args:
            snapshot_dir: 快照存放目录（自动创建）。
        """
        self._dir = Path(snapshot_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_path: Path = self._dir / "ate_scheduler_snapshot.json"

    # ------------------------------------------------------------------
    # 存取
    # ------------------------------------------------------------------
    def save(self, state: dict[str, Any]) -> None:
        """原子写入快照。

        先写同目录临时文件再 ``os.replace``：若写入中途崩溃，原快照保持
        完整；``os.replace`` 在同一文件系统内是原子的。

        Args:
            state: 待持久化的完整状态（step_states/variables/uut_states 等）。
        """
        # 临时文件必须与目标同目录，os.replace 跨文件系统会失败
        fd, tmp_path = tempfile.mkstemp(
            prefix=".snapshot-", suffix=".tmp", dir=str(self._dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())  # 确保落到磁盘，崩溃不丢
            os.replace(tmp_path, self.snapshot_path)
        except Exception:
            # 清理临时文件，避免残留
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            logger.error("state_snapshot_save_failed", path=str(self.snapshot_path))
            raise

    def load(self) -> dict[str, Any] | None:
        """读取快照。

        Returns:
            状态字典；文件不存在或内容损坏（JSON 解析失败/非 dict）返回 None。
        """
        if not self.snapshot_path.exists():
            return None
        try:
            with open(self.snapshot_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                logger.warning("state_snapshot_invalid", path=str(self.snapshot_path))
                return None
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "state_snapshot_load_failed", path=str(self.snapshot_path), error=str(e)
            )
            return None

    # ------------------------------------------------------------------
    # 判定 / 清理
    # ------------------------------------------------------------------
    def can_resume(self) -> bool:
        """是否存在可恢复的快照。

        Returns:
            快照文件存在且包含非空 ``step_states`` 才为 True。
        """
        if not self.snapshot_path.exists():
            return False
        data = self.load()
        if not data:
            return False
        steps = data.get(_RESUME_MARKER)
        return isinstance(steps, dict) and bool(steps)

    def cleanup(self) -> None:
        """删除快照文件（正常完成后调用）。

        幂等：文件不存在时静默返回。
        """
        try:
            if self.snapshot_path.exists():
                self.snapshot_path.unlink()
                logger.info("state_snapshot_cleaned", path=str(self.snapshot_path))
        except OSError as e:
            logger.warning(
                "state_snapshot_cleanup_failed",
                path=str(self.snapshot_path),
                error=str(e),
            )
