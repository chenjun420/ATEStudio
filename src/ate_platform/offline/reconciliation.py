"""T21 重连对账（设计文档 §10.5 reconciliation after reconnect）。

断网恢复后的单趟本地对账流程（``Reconciler.reconcile()``），按序四个阶段：

1. **清空上传队列**（T20）：按时间序（= ``list_pending()`` 插入序）重放全部
   pending 记录，经注入的 :class:`ReconcileUploader` 传输缝上传；幂等键
   ``(station_id, execution_id, seq_no)`` 由队列 UNIQUE 约束兜底，服务器接受即
   ``mark_uploaded`` + ``mark_acked``（ACK 是权威终态，见 T20）。被拒绝/传输
   异常的记录 ``record_retry`` 计数后进入 quarantine 视图——数据保留在队列中
   绝不静默丢弃，下一趟对账自动重试；
2. **未上报缓存版本对账**：遍历缓存中仍处 ``cached``（未 ACK = 未上报）的
   序列/工装拓扑版本，经 ``resolve_version`` 缝询问服务器：
   *confirmed* → 本地 ``mark_acked``；*conflict* → 以服务器指定的新版本号把
   本地载荷经公开 store API 重新落库并直接 ACK（服务器已预先同意新版本号），
   被取代的旧版本行随之删除（载荷已完整承接，防每趟重复冲突、保证收敛）；
   无新版本号可用的冲突进 quarantine——绝不静默丢弃；
3. **脚本完整性巡检**（T19）：intact 脚本经 ``report_script`` 缝向服务器报备
   库存；损坏脚本（读时校验失败）进 quarantine 并告警——绝不静默吞掉；
4. **清空版本锁**（T26 对账释放）：``release_all()`` 幂等返回释放数。

最后发出 reconciled status：冻结的 :class:`ReconciliationReport` 计数 +
structlog ``reconciliation_completed`` 事件。

幂等性：整趟对账可安全重复执行——第二遍零上传（队列已空）、零 ACK（缓存
终态 acked 不再进入扫描）、零冲突（已解决项不再 cached）、零释放（锁表已空）。
队列 bool 翻转与缓存 ``mark_acked`` 均为重放安全设计（T18/T20 契约）。

非阻塞契约：本模块不做任何忙等/长持锁——每个兄弟模块操作各自持有短临界区
RLock，阶段间无全局锁跨越；唯一可能阻塞点是注入的传输回调，其延迟属 T24
真实 NATS/HTTP 接线的职责。生产接线应在连接恢复处理器中以后台线程调用。

本模块只定义传输缝（Protocol），不含任何真实 NATS/HTTP 实现（T24 的职责）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

import structlog

from ate_platform.offline.cache_store import (
    KIND_SEQUENCE,
    KIND_TOPOLOGY,
    EntryStatus,
    OfflineCacheError,
    OfflineCacheStore,
)
from ate_platform.offline.script_cache import OfflineScriptCache
from ate_platform.offline.upload_queue import UploadQueue, UploadRecord
from ate_platform.offline.version_lock import VersionLockManager

logger = structlog.get_logger(__name__)

#: resolve_version 缝的结果：服务器确认该版本（本地可安全 ACK）
RESOLVE_CONFIRMED = "confirmed"
#: resolve_version 缝的结果：与服务器冲突（需以 new_version 重新版本化）
RESOLVE_CONFLICT = "conflict"

__all__ = [
    "RESOLVE_CONFLICT",
    "RESOLVE_CONFIRMED",
    "QuarantinedItem",
    "ReconcileReport",
    "ReconcileUploader",
    "Reconciler",
    "VersionResolution",
]


@dataclass(frozen=True)
class VersionResolution:
    """``resolve_version`` 缝的应答。

    Attributes:
        outcome: :data:`RESOLVE_CONFIRMED` 或 :data:`RESOLVE_CONFLICT`。
        new_version: conflict 时服务器指定的新版本号；``None`` 表示本轮无法
            解决（进 quarantine，数据保留待人工/下轮处理）。
    """

    outcome: str
    new_version: str | None = None


@runtime_checkable
class ReconcileUploader(Protocol):
    """传输缝协议（结构化类型；真实 NATS/HTTP 实现属 T24）。

    全部方法必须非阻塞快速返回或抛异常；对账层把任何异常视为该条目失败，
    绝不让单条故障中断整趟对账。
    """

    def upload_record(self, record: UploadRecord) -> bool:
        """上传一条执行记录。True = 服务器接受并已确认（等价 ACK）。"""
        ...

    def resolve_version(
        self, kind: str, entry_id: str, version: str, checksum: str
    ) -> VersionResolution:
        """询问服务器一个未上报缓存版本的处置（确认 / 冲突+新版本号）。"""
        ...

    def report_script(self, script_id: str, version: str, checksum: str) -> bool:
        """向服务器报备一份 intact 脚本库存。True = 报备成功。"""
        ...


@dataclass(frozen=True)
class QuarantinedItem:
    """一条被隔离的条目视图（数据本体保留在原存储中，绝不删除）。"""

    reason: str  # 'upload_rejected' | 'upload_transport_error' | 'version_conflict' | 'script_corrupt'
    station_id: str | None = None
    execution_id: str | None = None
    seq_no: int | None = None
    kind: str | None = None
    entry_id: str | None = None
    version: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class ReconcileReport:
    """reconciled status（§10.5 对账结果视图，供 UI/日志/T24 消费）。"""

    started_at: float
    finished_at: float
    uploaded: int = 0  # 本趟 pending→uploaded→acked 成功推进的记录数
    acked: int = 0  # 队列 mark_acked 翻转成功数（含历史 uploaded 行被补 ACK）
    confirmed_entries: int = 0  # 服务器确认的未上报缓存版本数
    conflicts_resolved: int = 0  # 冲突后以新版本重新落库并 ACK 的条目数
    quarantined: int = 0  # 隔离条目数（数据全部保留）
    locks_released: int = 0  # 清空的版本锁数量
    ok: bool = True  # False 仅当阶段级故障导致后续阶段跳过
    quarantine: list[QuarantinedItem] = field(default_factory=list)

    @property
    def duration(self) -> float:
        """对账耗时（秒，注入时钟口径）。"""
        return self.finished_at - self.started_at


_STORE_METHODS = {
    KIND_SEQUENCE: "store_sequence",
    KIND_TOPOLOGY: "store_topology",
}


class Reconciler:
    """断网恢复后的端侧对账器（消费 T18/T19/T20/T26 公开 API + 传输缝）。

    Args:
        queue: T20 上传队列。
        cache: T18 ACK 门控缓存（序列/工装拓扑）。
        scripts: T19 脚本磁盘缓存。
        locks: T26 版本锁管理器。
        uploader: 传输缝（:class:`ReconcileUploader` 完整协议对象，或任意
            同形对象；测试中亦可赋值 :attr:`upload_record_fn` 为单个
            ``Callable[[UploadRecord], bool]`` 回调，仅覆盖 Phase 1 上传缝）。
        clock: 可注入时钟（测试用）；生产省略即取 ``time.time``。
    """

    def __init__(
        self,
        *,
        queue: UploadQueue,
        cache: OfflineCacheStore,
        scripts: OfflineScriptCache,
        locks: VersionLockManager,
        uploader: ReconcileUploader,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._queue = queue
        self._cache = cache
        self._scripts = scripts
        self._locks = locks
        # 两个传输角色显式分离：完整协议缝（Phase 2/3 的 resolve_version /
        # report_script 必须存在）与 Phase 1 可选的裸回调覆盖。测试可直接赋值
        # ``rec._uploader`` 为同形最小对象或注入 ``upload_record_fn``。
        self._uploader: ReconcileUploader = uploader
        self.upload_record_fn: Callable[[UploadRecord], bool] | None = None
        self._clock: Callable[[], float] = time.time if clock is None else clock
        self.last_report: ReconcileReport | None = None

    # ------------------------------------------------------------------
    # 对账主流程
    # ------------------------------------------------------------------
    def reconcile(self) -> ReconcileReport:
        """执行一趟完整对账并返回 reconciled status。幂等：重复调用安全。"""
        report = ReconcileReport(started_at=self._clock(), finished_at=self._clock())
        try:
            report = self._flush_upload_queue(report)
            report = self._resolve_cache_versions(report)
            report = self._sweep_scripts(report)
            released = self._locks.release_all()
            report = self._advance(report, finished_at=self._clock(), locks_released=released)
        except Exception as exc:  # 阶段级兜底；单条故障已在各阶段内消化
            logger.error("reconciliation_stage_failure", error=str(exc))
            report = self._advance(report, finished_at=self._clock(), ok=False)

        self.last_report = report
        logger.info(
            "reconciliation_completed",
            uploaded=report.uploaded,
            acked=report.acked,
            confirmed_entries=report.confirmed_entries,
            conflicts_resolved=report.conflicts_resolved,
            quarantined=report.quarantined,
            locks_released=report.locks_released,
            ok=report.ok,
            duration=round(report.duration, 6),
        )
        return report

    # ------------------------------------------------------------------
    # Phase 1 — 上传队列清空（时间序重放，幂等键 UNIQUE 兜底）
    # ------------------------------------------------------------------
    def _flush_upload_queue(self, report: ReconcileReport) -> ReconcileReport:
        uploaded = report.uploaded
        acked = report.acked
        quarantined = report.quarantined
        quarantine: list[QuarantinedItem] = list(report.quarantine)
        for record in self._queue.list_pending():  # 插入序 = 时间序（ORDER BY id）
            accepted = False
            reason = "upload_rejected"
            try:
                accepted = bool(self._call_uploader(record))
            except Exception as exc:
                reason = "upload_transport_error"
                logger.warning(
                    "reconciliation_upload_error",
                    station_id=record.station_id,
                    execution_id=record.execution_id,
                    seq_no=record.seq_no,
                    error=str(exc),
                )
            if accepted:
                # bool 翻转重放安全：False（行不存在/已越过状态）不影响正确性
                self._queue.mark_uploaded(record.id)
                if self._queue.mark_acked(record.id):
                    acked += 1
                uploaded += 1
                continue
            self._queue.record_retry(record.id)  # 计数退避参考；状态保持 pending
            quarantine.append(
                QuarantinedItem(
                    reason=reason,
                    station_id=record.station_id,
                    execution_id=record.execution_id,
                    seq_no=record.seq_no,
                    detail=f"payload preserved at {record.payload_path}",
                )
            )
            quarantined += 1
            logger.warning(
                "reconciliation_record_quarantined",
                reason=reason,
                station_id=record.station_id,
                execution_id=record.execution_id,
                seq_no=record.seq_no,
            )
        return self._advance(
            report, uploaded=uploaded, acked=acked, quarantined=quarantined, quarantine=quarantine
        )

    def _call_uploader(self, record: UploadRecord) -> bool:
        """Phase 1 上传缝：可选裸回调覆盖优先，否则走完整协议的 ``upload_record``。"""
        upload_fn = self.upload_record_fn
        if upload_fn is not None:
            return upload_fn(record)
        return self._uploader.upload_record(record)

    # ------------------------------------------------------------------
    # Phase 2 — 未上报缓存版本对账（confirmed → ACK；conflict → 新版本化）
    # ------------------------------------------------------------------
    def _resolve_cache_versions(self, report: ReconcileReport) -> ReconcileReport:
        confirmed = report.confirmed_entries
        conflicts = report.conflicts_resolved
        quarantined = report.quarantined
        quarantine: list[QuarantinedItem] = list(report.quarantine)
        for status in self._cache.list_cached():
            if status.state != "cached":
                continue  # 已 ACK 的不再上报（幂等第二遍零扫描的关键）
            try:
                resolution = self._uploader.resolve_version(
                    status.kind, status.id, status.version, status.checksum
                )
            except Exception as exc:
                logger.warning(
                    "reconciliation_resolve_error",
                    kind=status.kind,
                    id=status.id,
                    version=status.version,
                    error=str(exc),
                )
                continue  # 传输故障：保持 cached，下一趟重试
            if resolution.outcome == RESOLVE_CONFIRMED:
                # mark_acked 幂等友好（行不存在返回 False），重放安全
                self._cache.mark_acked(status.kind, status.id, status.version)
                confirmed += 1
                continue
            if resolution.outcome != RESOLVE_CONFLICT:
                logger.warning(
                    "reconciliation_unknown_resolution",
                    outcome=resolution.outcome,
                    kind=status.kind,
                    id=status.id,
                )
                continue
            resolved = self._apply_conflict_new_version(status, resolution.new_version)
            if resolved:
                conflicts += 1
            else:
                quarantine.append(
                    QuarantinedItem(
                        reason="version_conflict",
                        kind=status.kind,
                        entry_id=status.id,
                        version=status.version,
                        detail="server conflict without usable new_version; local row preserved",
                    )
                )
                quarantined += 1
                logger.warning(
                    "reconciliation_version_conflict_quarantined",
                    kind=status.kind,
                    id=status.id,
                    version=status.version,
                )
        return self._advance(
            report,
            confirmed_entries=confirmed,
            conflicts_resolved=conflicts,
            quarantined=quarantined,
            quarantine=quarantine,
        )

    def _apply_conflict_new_version(self, status: EntryStatus, new_version: str | None) -> bool:
        """冲突 → 以服务器指定的新版本号重新落库并直接 ACK。

        经公开 API 操作：``get_usable(require_acked=False)`` 读回本地载荷
        （在线诊断例外路径，对账即在线场景），``store_sequence/store_topology``
        写新版本行，``mark_acked(new_version)`` 落终态（服务器已预先同意该
        版本号）。新版本落定后删除被取代的本地旧版本行——载荷已由新版本
        完整承接，保留旧行会让每一趟对账都重复报同一冲突、永不收敛；
        删除动作日志点名新旧版本号，绝不静默。
        """
        if not new_version or new_version == status.version:
            return False
        method_name = _STORE_METHODS.get(status.kind)
        if method_name is None:
            return False
        try:
            payload = self._cache.get_usable(
                status.kind, status.id, status.version, require_acked=False
            )
            getattr(self._cache, method_name)(status.id, new_version, payload)
            self._cache.mark_acked(status.kind, status.id, new_version)
            self._cache.delete(status.kind, status.id, status.version)
        except OfflineCacheError as exc:
            logger.warning(
                "reconciliation_conflict_apply_failed",
                kind=status.kind,
                id=status.id,
                version=status.version,
                error=str(exc),
            )
            return False
        logger.info(
            "reconciliation_conflict_reversioned",
            kind=status.kind,
            id=status.id,
            old_version=status.version,
            new_version=new_version,
        )
        return True

    # ------------------------------------------------------------------
    # Phase 3 — 脚本完整性巡检（intact 报备；损坏隔离告警）
    # ------------------------------------------------------------------
    def _sweep_scripts(self, report: ReconcileReport) -> ReconcileReport:
        quarantine: list[QuarantinedItem] = list(report.quarantine)
        quarantined = report.quarantined
        for script in self._scripts.list_scripts():
            if script.intact:
                try:
                    self._uploader.report_script(script.script_id, script.version, script.checksum)
                except Exception as exc:
                    logger.warning(
                        "reconciliation_script_report_error",
                        script_id=script.script_id,
                        version=script.version,
                        error=str(exc),
                    )
                continue
            quarantine.append(
                QuarantinedItem(
                    reason="script_corrupt",
                    kind="script",
                    entry_id=script.script_id,
                    version=script.version,
                    detail="read-time SHA256 verification failed; files preserved on disk",
                )
            )
            quarantined += 1
            logger.warning(
                "reconciliation_script_corrupt", script_id=script.script_id, version=script.version
            )
        return self._advance(report, quarantined=quarantined, quarantine=quarantine)

    # ------------------------------------------------------------------
    # 不可变报告推进助手
    # ------------------------------------------------------------------
    @staticmethod
    def _advance(
        report: ReconcileReport,
        *,
        finished_at: float | None = None,
        uploaded: int | None = None,
        acked: int | None = None,
        confirmed_entries: int | None = None,
        conflicts_resolved: int | None = None,
        quarantined: int | None = None,
        locks_released: int | None = None,
        ok: bool | None = None,
        quarantine: list[QuarantinedItem] | None = None,
    ) -> ReconcileReport:
        """以显式关键字段生成推进后的报告副本（frozen dataclass 的演进方式）；

        未传入（None）的字段保持 ``report`` 现值。注意 ``ok=False`` / 计数 0
        是有效更新值，故以 ``is not None`` 而非真值判断取舍。
        """
        return replace(
            report,
            finished_at=report.finished_at if finished_at is None else finished_at,
            uploaded=report.uploaded if uploaded is None else uploaded,
            acked=report.acked if acked is None else acked,
            confirmed_entries=report.confirmed_entries if confirmed_entries is None else confirmed_entries,
            conflicts_resolved=report.conflicts_resolved if conflicts_resolved is None else conflicts_resolved,
            quarantined=report.quarantined if quarantined is None else quarantined,
            locks_released=report.locks_released if locks_released is None else locks_released,
            ok=report.ok if ok is None else ok,
            quarantine=report.quarantine if quarantine is None else quarantine,
        )
