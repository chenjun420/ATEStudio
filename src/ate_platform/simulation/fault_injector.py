"""四层故障注入引擎（设计文档 §7.7，任务 #6）。

在虚拟仿真中注入仪表异常、通信超时、测量越界、DUT 故障等，验证序列的
容错与重试逻辑。四层注入点（§7.7.1）：

- 网络层 L1：延迟、丢包、断连、乱序、校验错误
- 协议层 L1：SCPI 错误码、截断数据
- 仪器层 L2：测量越界、读数漂移、模式切换失败、自检失败
- 调度层 L3/L4：步骤异常退出、变量污染、资源死锁

故障规则经 YAML ``fault_injection:`` 段声明（§7.7.2），由 :class:`FaultInjector`
统一注册；触发方式支持 count / probability / time / condition / state 五种。

设计要点：
- 规则匹配是纯函数（:meth:`FaultRule.matches`），无副作用，便于测试；
- 命中动作由 :meth:`FaultInjector.intercept` 返回结构化结果，动作执行
  （抛异常 / 覆盖值 / 延迟）与匹配解耦；
- condition/state 表达式用 ``simpleeval`` 安全求值（与 ConditionEvaluator
  一致），不落 ``eval()``；
- 规则可选 ``once``：命中一次后自动失效（如 ``dmm_timeout_once``）。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

from simpleeval import SimpleEval

# 四层注入点（§7.7.1）
LAYER_NETWORK = "network"
LAYER_PROTOCOL = "protocol"
LAYER_INSTRUMENT = "instrument"
LAYER_SCHEDULER = "scheduler"
ALL_LAYERS = (
    LAYER_NETWORK,
    LAYER_PROTOCOL,
    LAYER_INSTRUMENT,
    LAYER_SCHEDULER,
)

# 触发类型（§7.7.2）
TRIGGER_TYPES = ("count", "probability", "time", "condition", "state")


class FaultInjectionError(RuntimeError):
    """故障注入触发后抛出的统一基类异常。

    Attributes:
        fault_id: 命中的规则 ID。
        layer: 注入层。
        target: 目标资源/步骤。
    """

    def __init__(
        self,
        message: str,
        fault_id: str | None = None,
        layer: str | None = None,
        target: str | None = None,
    ) -> None:
        super().__init__(message)
        self.fault_id = fault_id
        self.layer = layer
        self.target = target


class NetworkFaultError(FaultInjectionError):
    """网络层故障：断连、校验错误等。"""


class ProtocolFaultError(FaultInjectionError):
    """协议层故障：SCPI 错误码、截断数据。

    Attributes:
        code: SCPI 错误码（如 -113）。
        message: 错误文本。
    """

    def __init__(
        self,
        message: str,
        code: int | None = None,
        fault_id: str | None = None,
        layer: str | None = None,
        target: str | None = None,
    ) -> None:
        super().__init__(message, fault_id=fault_id, layer=layer, target=target)
        self.code = code


class InstrumentFaultError(FaultInjectionError):
    """仪器层故障：测量越界、自检失败等。"""


class SchedulerFaultError(FaultInjectionError):
    """调度层故障：步骤异常退出、资源死锁等。"""


@dataclass
class FaultAction:
    """一次故障命中的执行结果（匹配与执行解耦）。"""

    fault_id: str
    layer: str
    fault_type: str
    params: dict[str, Any] = field(default_factory=dict)
    # value_override 时为覆盖值；packet_loss/delay 时不抛异常
    value: Any = None

    @property
    def is_exception(self) -> bool:
        """该动作是否应抛出异常。"""
        return self.fault_type not in (
            "value_override",
            "delay",
            "packet_loss",
            "reorder",
        )


class FaultRule:
    """故障规则定义（§7.7.3）。

    Attributes:
        fault_id: 规则唯一 ID。
        layer: 注入层（network/protocol/instrument/scheduler）。
        target: 目标 resource_id 或 step_id。
        method: 目标方法名（默认 "*" 匹配任意）。
        trigger: 触发配置 dict（type: count/probability/time/condition/state）。
        action: 动作配置 dict（type + 参数）。
        once: 命中一次后失效。
    """

    def __init__(
        self,
        fault_id: str,
        layer: str,
        target: str,
        method: str = "*",
        trigger: dict[str, Any] | None = None,
        action: dict[str, Any] | None = None,
        once: bool = False,
    ) -> None:
        if layer not in ALL_LAYERS:
            raise ValueError(
                f"Invalid layer '{layer}'. Must be one of {ALL_LAYERS}"
            )
        trigger = trigger or {}
        action = action or {}
        # 缺省触发：首次调用即命中（等价的 count=1）
        if "type" not in trigger:
            trigger = {"type": "count", "value": 1}
        if trigger.get("type") not in TRIGGER_TYPES:
            raise ValueError(
                f"Invalid trigger type '{trigger.get('type')}'. "
                f"Must be one of {TRIGGER_TYPES}"
            )
        if not action.get("type"):
            raise ValueError("action.type is required")

        self.fault_id = fault_id
        self.layer = layer
        self.target = target
        self.method = method or "*"
        self.trigger: dict[str, Any] = trigger
        self.action: dict[str, Any] = action
        self.once = once
        self._triggered_count = 0
        # 无随机种子时按时间播种（测试可传固定 seed 保证可复现）
        self._rng = random.Random(action.get("seed"))

    def matches(self, context: dict[str, Any]) -> bool:
        """判断规则是否在当前上下文中命中（§7.7.3 matches）。

        上下文键（由调用方提供）：
            call_count: 该方法被调用次数。
            elapsed_s: 注入器启动以来的秒数。
            其他自定义变量（condition/state 表达式求值）。

        Args:
            context: 匹配上下文。

        Returns:
            True 表示命中。
        """
        if self.once and self._triggered_count > 0:
            return False

        t = self.trigger
        trigger_type = t["type"]
        hit = False

        if trigger_type == "count":
            hit = int(context.get("call_count", 0)) >= int(t.get("value", 0))
        elif trigger_type == "probability":
            hit = self._rng.random() < float(t.get("value", 0))
        elif trigger_type == "time":
            hit = float(context.get("elapsed_s", 0.0)) >= float(t.get("after_s", 0))
        elif trigger_type in ("condition", "state"):
            expr = t.get("expression") or t.get("condition")
            if expr:
                try:
                    evaluator = SimpleEval()
                    evaluator.names = dict(context)
                    hit = bool(evaluator.eval(expr))
                except Exception:
                    hit = False

        if hit:
            self._triggered_count += 1
        return hit

    def build_action(self) -> FaultAction:
        """由命中规则构造 FaultAction。"""
        act = self.action
        fault_type = act["type"]
        params = {k: v for k, v in act.items() if k != "type"}
        return FaultAction(
            fault_id=self.fault_id,
            layer=self.layer,
            fault_type=fault_type,
            params=params,
            value=act.get("value"),
        )

    @property
    def triggered_count(self) -> int:
        """已触发次数（测试/统计用）。"""
        return self._triggered_count


class FaultInjector:
    """四层故障注入引擎。

    提供统一的 :meth:`intercept` 入口：给定层、目标、方法、上下文，
    返回首个命中的 :class:`FaultAction`（无命中返回 None）。网络/协议/
    仪器/调度四层均可注入（§7.7.1）。调度层检查通过 :meth:`check_scheduler`
    暴露给执行器/仿真上层调用。

    Example:
        >>> injector = FaultInjector(seed=42)
        >>> injector.load([
        ...     {"id": "dmm_timeout_once", "target": "DMM_CH1",
        ...      "trigger": {"type": "count", "value": 3},
        ...      "action": {"type": "timeout", "timeout_ms": 5000}},
        ... ])
        >>> injector.intercept("network", "DMM_CH1", "measure_voltage",
        ...                    {"call_count": 3})
        FaultAction(...)
    """

    def __init__(self, seed: int | None = None) -> None:
        """初始化注入器。

        Args:
            seed: 概率触发用的随机种子（None 用系统随机）。
        """
        self._rules: list[FaultRule] = []
        self._rng = random.Random(seed)
        self._start_time = time.monotonic()

    # ------------------------------------------------------------------
    # 规则注册
    # ------------------------------------------------------------------
    def load(self, config: list[dict[str, Any]] | None) -> None:
        """从配置列表加载规则（YAML ``fault_injection:`` 段解析后）。

        Args:
            config: 规则 dict 列表（§7.7.2）。None/空则无操作。
        """
        if not config:
            return
        for rule_cfg in config:
            self.add_rule(self._parse_rule(rule_cfg))

    def _parse_rule(self, cfg: dict[str, Any]) -> FaultRule:
        """把 DSL dict 转成 FaultRule。

        键名兼容两种约定：``fault_id``/``id``、``action``/``fault``
        （§7.7.2 文档示例用 ``fault:`` 键描述动作）。
        """
        action = cfg.get("action")
        if action is None:
            action = cfg.get("fault")
        return FaultRule(
            fault_id=str(cfg.get("fault_id") or cfg.get("id")),
            layer=str(cfg.get("layer", LAYER_INSTRUMENT)),
            target=str(cfg.get("target", "*")),
            method=str(cfg.get("method", "*")),
            trigger=cfg.get("trigger") or {},
            action=action or {},
            once=bool(cfg.get("once", False)),
        )

    def add_rule(self, rule: FaultRule) -> None:
        """注册一条规则。"""
        self._rules.append(rule)

    @property
    def rules(self) -> list[FaultRule]:
        """已注册规则（只读视图）。"""
        return list(self._rules)

    def clear(self) -> None:
        """清空全部规则（多轮仿真间复用）。"""
        self._rules.clear()
        self._start_time = time.monotonic()

    # ------------------------------------------------------------------
    # 匹配与执行
    # ------------------------------------------------------------------
    def _base_context(self, target: str, method: str, context: dict[str, Any]) -> dict:
        """合并注入器级上下文（elapsed_s）与调用方上下文。"""
        merged = {
            "elapsed_s": time.monotonic() - self._start_time,
            "target": target,
            "method": method,
            "call_count": 0,
        }
        merged.update(context)
        return merged

    def intercept(
        self,
        layer: str,
        target: str,
        method: str = "*",
        context: dict[str, Any] | None = None,
    ) -> FaultAction | None:
        """查询指定层/目标/方法的首条命中规则。

        Args:
            layer: 注入层。
            target: 目标资源/步骤 ID（* 匹配任意）。
            method: 目标方法名（* 匹配任意）。
            context: 匹配上下文（call_count/elapsed_s/自定义变量）。

        Returns:
            命中的 FaultAction；无命中返回 None。
        """
        ctx = self._base_context(target, method, context or {})
        for rule in self._rules:
            if rule.layer != layer:
                continue
            if rule.target != "*" and rule.target != target:
                continue
            if rule.method != "*" and rule.method != method:
                continue
            if rule.matches(ctx):
                return rule.build_action()
        return None

    # 语义化入口 —— 供不同层调用方使用
    def check_network(
        self,
        resource_id: str,
        method: str = "*",
        context: dict[str, Any] | None = None,
    ) -> FaultAction | None:
        """网络层检查（§7.7.3 check_network）。"""
        return self.intercept(LAYER_NETWORK, resource_id, method, context)

    def check_protocol(
        self,
        resource_id: str,
        method: str = "*",
        context: dict[str, Any] | None = None,
    ) -> FaultAction | None:
        """协议层检查（SCPI 错误码/截断）。"""
        return self.intercept(LAYER_PROTOCOL, resource_id, method, context)

    def check_instrument(
        self,
        resource_id: str,
        method: str = "*",
        context: dict[str, Any] | None = None,
    ) -> FaultAction | None:
        """仪器层检查（§7.7.3 check_instrument）。"""
        return self.intercept(LAYER_INSTRUMENT, resource_id, method, context)

    def check_scheduler(
        self,
        step_id: str,
        method: str = "*",
        context: dict[str, Any] | None = None,
    ) -> FaultAction | None:
        """调度层检查（步骤异常/变量污染/资源死锁）。"""
        return self.intercept(LAYER_SCHEDULER, step_id, method, context)

    # ------------------------------------------------------------------
    # 动作执行
    # ------------------------------------------------------------------
    def raise_for(self, action: FaultAction | None) -> None:
        """把命中的异常类动作抛出（§7.7.1 故障类型映射）。

        非异常类动作（value_override/delay/packet_loss/reorder）不抛。

        Args:
            action: intercept 返回值；None 或非异常动作时为空操作。

        Raises:
            NetworkFaultError / ProtocolFaultError / InstrumentFaultError /
            SchedulerFaultError: 按 fault_type 映射。
        """
        if action is None or not action.is_exception:
            return

        fault_type = action.fault_type
        params = action.params
        msg = f"Fault '{action.fault_id}' injected at {action.layer} layer"

        if fault_type in ("connection_drop", "checksum_error"):
            raise NetworkFaultError(
                msg, fault_id=action.fault_id, layer=action.layer, target=params.get("target")
            )
        if fault_type in ("scpi_error", "truncated_data"):
            raise ProtocolFaultError(
                msg,
                code=params.get("code"),
                fault_id=action.fault_id,
                layer=action.layer,
                target=params.get("target"),
            )
        if fault_type in (
            "instrument_error",
            "out_of_range",
            "selftest_fail",
            "mode_switch_fail",
        ):
            raise InstrumentFaultError(
                msg, fault_id=action.fault_id, layer=action.layer, target=params.get("target")
            )
        # scheduler_error / timeout / bus_error 等默认归类
        if fault_type == "scheduler_error":
            raise SchedulerFaultError(
                msg, fault_id=action.fault_id, layer=action.layer, target=params.get("target")
            )
        # 兜底：统一 FaultInjectionError
        raise FaultInjectionError(
            msg, fault_id=action.fault_id, layer=action.layer, target=params.get("target")
        )
