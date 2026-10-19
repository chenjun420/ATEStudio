"""TC-21 短路保护恢复（tc21）—— PA601-D54A commissioning 步骤脚本。

本脚本由 23-TC 全量 commissioning 计划（pa601_full_commission.yaml）驱动，在
虚拟设备（mat-device-control Virtual* 驱动）或真板上执行：经 MatDeviceHttpDriver
→ mat-device-control → 设备方法，读回 scp_recovery 读数并按 params["value"] 判据判定。

执行协议（ate_platform/executor/process_executor.py）：
  * 正常返回              -> PASSED（executor 收集模块命名空间里的 result_* 变量）
  * raise AssertionError  -> FAILED（判据不满足 / 无条件判据 / 无读数）
  * 其它异常              -> ERROR

模块级执行体放在 ``if __name__ == "__main__":`` 之下：executor 以
``__name__="__main__"`` 执行该脚本 → 会运行执行体；importlib 导入（测试用）→
只加载 ``_driver`` / ``_judge``，不触发真实设备调用。
"""

from __future__ import annotations

import os

#: executor 在 exec 前把步骤 params 注入脚本全局命名空间；importlib 导入时回落为空 dict。
#: 用 ``globals().get`` 而非裸引用 ``params``，既避免 F821 又保证两种入口都可用。
params = globals().get("params", {})


def _driver():
    """取本站设备实例（真链路：HTTP → mat-device-control → 设备）。

    Returns:
        已 ``connect()`` 的 :class:`MatDeviceHttpDriver`。

    Raises:
        Exception: 远端不可达/连接失败时原样抛出（fail-fast，不静默降级）。
    """
    from ate_platform.drivers.mat_device.http_driver import MatDeviceHttpDriver

    drv = MatDeviceHttpDriver(
        str(params.get("device_id", "eload001")),
        base_url=params.get("base_url") or os.environ.get("MAT_DEVICE_BASE_URL"),
    )
    drv.connect()
    return drv


def _judge(measured, value):
    """按归一化后的判据区间判定 scp_recovery。

    边界归一化：``min`` 与 ``max`` 皆非空且 ``min > max`` 时自动交换为 lo/hi
    （负轨幅值序上游可能写反）；单边/无边按语义判定（只有 min 视为下限、
    只有 max 视为上限）。通过即返回 True，越限/无条件判据/无读数抛
    ``AssertionError`` → executor 记为 FAILED。

    成功路径写入 ``result_*`` 模块全局，供 executor 收集为 outputs。

    Args:
        measured: 实测读数。
        value: 判据字典，可含 ``min`` / ``typ`` / ``max`` / ``unit``。

    Returns:
        True（判定通过）。
    """
    global result_measured_value, result_passed, result_cond_fingerprint, result_detail

    limits = dict(value or {})
    lo, hi = limits.get("min"), limits.get("max")
    if (
        isinstance(lo, (int, float))
        and not isinstance(lo, bool)
        and isinstance(hi, (int, float))
        and not isinstance(hi, bool)
        and lo > hi
    ):
        lo, hi = hi, lo  # 边界归一化：交换反向区间
    unit = str(limits.get("unit", ""))
    cond_kind = str(params.get("condition_kind", "scp_recovery"))

    if lo is None and hi is None:
        raise AssertionError(
            f"无条件判据，无法判定 {cond_kind}: value={limits}。"
            "请补 min/max 或改为定性确认，不得默认通过。"
        )
    if measured is None:
        raise AssertionError(f"仪表未返回读数，无法判定 {cond_kind}: value={limits}")

    if (lo is not None and measured < lo) or (hi is not None and measured > hi):
        raise AssertionError(
            f"实测 {measured}{unit} 超出归一化区间 [{lo}, {hi}]{unit}"
            f"（{cond_kind}）"
        )

    result_measured_value = float(measured)
    result_passed = True
    result_cond_fingerprint = str(params.get("cond_fingerprint", ""))
    result_detail = (
        f"实测 {measured}{unit} 在归一化区间 [{lo}, {hi}]{unit} 内"
        f"（{cond_kind}）"
    )
    return True


def _run():
    """执行体：取设备 → 调用冻结方法读回 →（可选故障注入覆盖）→ 判定。"""
    drv = _driver()
    measured = drv.query("measure_scp_recovery_s")
    forced = params.get("force_measured")
    if forced is not None:
        measured = float(forced)  # 故障注入：覆盖实测值，用于证明判据/中止链路活性
    _judge(measured, params.get("value") or {})


if __name__ == "__main__":
    _run()
