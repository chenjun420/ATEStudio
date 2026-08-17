"""ContextProxy.instrument() — 脚本执行与仪器代理的桥接。

验证执行侧脚本上下文（ContextProxy）通过 ProxyManager 拿到
InstrumentClient，并能完成一次真实的代理转发调用。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ate_platform.executor.context_proxy import ContextProxy
from ate_platform.proxy import ProxyManager
from ate_platform.scheduler.resource_manager import ResourceManager
from ate_platform.scheduler.variable_space import VariableSpace

INSTRUMENT_CONFIG = {"instruments": {"DMM_CH1": {"type": "DMM"}}}


def _make_context(tmp_path: Path, proxy_manager: ProxyManager | None) -> ContextProxy:
    """构造一个绑定（或未绑定）代理的 ContextProxy。"""
    ctx = ContextProxy(
        _variable_space=VariableSpace(),
        _resource_manager=ResourceManager(),
        _step_id="step_1",
        _proxy_manager=proxy_manager,
    )
    return ctx


def test_instrument_requires_manager(tmp_path: Path) -> None:
    """未配置代理管理器时 instrument() 明确报错。"""
    ctx = _make_context(tmp_path, None)
    with pytest.raises(RuntimeError, match="proxy manager"):
        ctx.instrument("DMM_CH1")


def test_instrument_through_proxy_manager(tmp_path: Path) -> None:
    """绑定管理器后，脚本侧可通过 instrument() 完成一次代理调用。"""
    manager = ProxyManager(INSTRUMENT_CONFIG, simulation=True, log_dir=str(tmp_path))
    manager.start()
    try:
        ctx = _make_context(tmp_path, manager)
        dmm = ctx.instrument("DMM_CH1", timeout=10.0)
        dmm.connect("MOCK::DMM")
        voltage = dmm.query("MEAS:VOLT:DC?")
        assert float(voltage) > 0
    finally:
        manager.stop()
