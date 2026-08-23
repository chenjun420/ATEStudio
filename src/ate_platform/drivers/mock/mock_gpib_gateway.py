"""MockGPIBGateway 虚拟 GPIB 网关 — 设计文档 §7.5 AC-4。

五类虚拟仪器之一：模拟一块 GPIB 板卡（board id）上的总线与挂接设备，使
上层驱动路径（寻址 → 写/查询 → 串行查询/SRQ）与真实硬件 API 兼容。纯内存
实现，无 socket/端口；客户端经 :meth:`MockGPIBGateway.execute` 收发 SCPI。

设计要点：

- **总线互斥**：``asyncio.Lock`` 按板号（board id）键控，且注册表为模块级
  —— 同板的不同网关实例同样互斥（对应真实 GPIB 板上多控制器争用总线的
  语义）；不同板号完全并行。选 asyncio 而非 threading.Lock 是因为整个
  mock 栈基于 asyncio（与 MockTCPDevice 一致），单线程事件循环内无跨线程
  争用。
- **寻址**：方法式 :meth:`MockGPIBGateway.select` 与 SCPI 风格 ``ADR <n>`` /
  ``ADR?``（含 ``*ADR`` 变体）双通道；命令路由到当前选址设备的处理器。
- **SCPI 分发**：IEEE-488.2 公共命令（``*IDN?`` / ``*RST`` / ``*CLS`` /
  ``*STB?`` / ``*OPC?``）由网关统一处理；仪器专属命令委托给
  :class:`VirtualGPIBDevice` 子类。未知命令按 SCPI 惯例入错误队列并应答
  ``-113,"Undefined header"``，经 ``SYST:ERR?`` 弹出。
- **串行查询与 SRQ**：每设备一个状态字节（bit6 = RQS/MSS 汇总位）；
  设备置位即触发服务请求事件 —— FIFO 地址队列 + 监听器回调 +
  :meth:`MockGPIBGateway.wait_srq(timeout)` 等待入口。
- **故障钩子**：可选注入既有 :class:`FaultInjector`（network 层 timeout →
  抛 :class:`GPIBTimeoutError`；protocol 层 scpi_error → 错误应答），另提供
  手动一次性 ``inject_timeout_once()`` / ``inject_error_once()``。

注册：模块级 :func:`register` 以 ``'gpib_gateway'`` 键接入集中工厂
（工厂本体由 T9 统一接线，本模块不改动它）。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Protocol
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from ate_platform.simulation.fault_injector import FaultInjector

FACTORY_KEY = "gpib_gateway"

# 状态字节位定义（IEEE-488.2）：bit6 = RQS/MSS 服务请求汇总位
STB_SRQ_BIT = 0x40

# SCPI 错误码（IEEE-488.2 / SCPI 惯例）
SCPI_ERR_UNDEFINED_HEADER = -113
_SCPI_UNDEFINED_HEADER_MSG = "Undefined header"
_SCPI_NO_ERROR = '0,"No error"'


class GPIBError(RuntimeError):
    """GPIB 网关错误基类。"""


class GPIBTimeoutError(GPIBError, TimeoutError):
    """通信超时（故障注入或 wait_srq 超时）。"""


class GPIBAddressError(GPIBError):
    """寻址错误：未选址、地址无设备、或重复挂载。"""


class UnknownSCPICommandError(LookupError):
    """设备处理器不认识的 SCPI 命令（网关转成 -113 错误路径）。"""


# ---------------------------------------------------------------------------
# 总线锁注册表 — 模块级，按 (板号, 事件循环) 键控（同板跨实例互斥）
# ---------------------------------------------------------------------------

# asyncio 原语绑定创建时的事件循环：pytest 等每测试新建循环的场景下，跨循环
# 复用同一把锁会抛 "bound to a different event loop"。故按运行中循环弱引用
# 分桶 —— 同一循环内同板号共享一把锁（跨网关实例互斥），循环销毁自动回收。
_BUS_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Lock]] = WeakKeyDictionary()


def _bus_lock(board_id: int) -> asyncio.Lock:
    """取（或建）当前事件循环上指定板号的总线锁。"""
    loop = asyncio.get_running_loop()
    board_locks = _BUS_LOCKS.get(loop)
    if board_locks is None:
        board_locks = {}
        _BUS_LOCKS[loop] = board_locks
    lock = board_locks.get(board_id)
    if lock is None:
        lock = asyncio.Lock()
        board_locks[board_id] = lock
    return lock


# ---------------------------------------------------------------------------
# 注册钩子（与 mock_tcp_device 完全一致的模式）
# ---------------------------------------------------------------------------


class MockRegistryLike(Protocol):
    """集中注册目标的最小接口（MockDriverFactory 稍后接线）。"""

    def register(self, key: str, driver_cls: type) -> None: ...


def register(factory: MockRegistryLike) -> None:
    """把 MockGPIBGateway 以 ``'gpib_gateway'`` 键注册到 factory。"""
    factory.register(FACTORY_KEY, MockGPIBGateway)


# ---------------------------------------------------------------------------
# 虚拟设备基类
# ---------------------------------------------------------------------------


class VirtualGPIBDevice:
    """挂在网关总线上的虚拟仪器基类。

    子类实现 :meth:`handle_scpi` 处理仪器专属命令；IEEE-488.2 公共命令
    （*IDN?/*RST/*CLS/*STB?/*OPC?）由网关代管，无需子类关心。状态字节低
    位由子类经 :meth:`set_status_bits` / :meth:`clear_status_bits` 维护，
    bit6 由 :meth:`request_service` / :meth:`clear_service_request` 管理。
    """

    def __init__(self, identity: str) -> None:
        self._identity = identity
        self._gateway: MockGPIBGateway | None = None
        self._address: int | None = None
        self._status_bits = 0

    @property
    def identity(self) -> str:
        """"*IDN?" 应答（厂商,型号,序列号 惯例）。"""
        return self._identity

    @property
    def address(self) -> int | None:
        """GPIB 主地址（attach 后非 None）。"""
        return self._address

    @property
    def status_byte(self) -> int:
        """当前状态字节（含 bit6 SRQ 汇总位）。"""
        return self._status_bits

    def set_status_bits(self, bits: int) -> None:
        """置低位状态位（不得用于 bit6 — 请用 request_service）。"""
        self._status_bits |= bits & ~STB_SRQ_BIT

    def clear_status_bits(self, bits: int) -> None:
        """清低位状态位。"""
        self._status_bits &= ~(bits & ~STB_SRQ_BIT)

    def request_service(self) -> None:
        """置 bit6 并向网关发起 SRQ（同步触发监听器 + 唤醒 wait_srq）。"""
        self._status_bits |= STB_SRQ_BIT
        if self._gateway is not None:
            self._gateway._on_service_request(self._address)

    def clear_service_request(self) -> None:
        """清 bit6（不影响已排队的 SRQ 通知）。"""
        self._status_bits &= ~STB_SRQ_BIT

    def reset(self) -> None:
        """*RST 钩子：恢复上电默认态（子类按需覆盖）。"""

    def handle_scpi(self, command: str) -> str | None:
        """处理仪器专属 SCPI 命令。

        Returns:
            查询应答字符串；写命令返回 None。

        Raises:
            UnknownSCPICommandError: 不认识的命令（网关转 -113 错误路径）。
        """
        raise UnknownSCPICommandError(command)

    def _attach(self, gateway: MockGPIBGateway, address: int) -> None:
        self._gateway = gateway
        self._address = address


# ---------------------------------------------------------------------------
# 内置通用测量设备（DMM 风格，响应格式与 MockDriverFactory 一致：E 记数法）
# ---------------------------------------------------------------------------


class MeasurementDevice(VirtualGPIBDevice):
    """通用测量仪器：MEAS:VOLT:DC? / MEAS:CURR:DC? / MEAS:RES?。

    默认值可经构造参数配置；``voltage`` 属性可变以模拟状态漂移，
    ``*RST`` 恢复构造默认（供 *RST 行为验证与测试夹具复位）。
    """

    def __init__(
        self,
        identity: str,
        *,
        voltage: float = 5.0,
        current: float = 0.5,
        resistance: float = 1000.0,
    ) -> None:
        super().__init__(identity)
        self._defaults = (voltage, current, resistance)
        self.voltage = voltage
        self.current = current
        self.resistance = resistance

    def reset(self) -> None:
        self.voltage, self.current, self.resistance = self._defaults

    def handle_scpi(self, command: str) -> str | None:
        cmd = command.upper().strip()
        if cmd == "MEAS:VOLT:DC?":
            return f"{self.voltage:.6E}"
        if cmd == "MEAS:CURR:DC?":
            return f"{self.current:.6E}"
        if cmd == "MEAS:RES?":
            return f"{self.resistance:.6E}"
        raise UnknownSCPICommandError(command)


# ---------------------------------------------------------------------------
# 网关
# ---------------------------------------------------------------------------


class MockGPIBGateway:
    """虚拟 GPIB 网关：一块板卡的总线仲裁 + 寻址 + SCPI 分发 + SRQ。

    Attributes:
        board_id: GPIB 板号（同板号的网关实例共享模块级总线锁）。
    """

    def __init__(
        self,
        board_id: int = 0,
        *,
        fault_injector: FaultInjector | None = None,
        command_latency_s: float = 0.0,
    ) -> None:
        self.board_id = board_id
        self._faults = fault_injector
        self._latency = command_latency_s
        self._devices: dict[int, VirtualGPIBDevice] = {}
        self._selected: int | None = None
        # 故障钩子：手动一次性注入 + FaultInjector 传输计数（count 触发语义）
        self._timeout_once = False
        self._error_once: tuple[int, str] | None = None
        self._transfer_count = 0
        # SRQ：FIFO 地址队列 + 唤醒事件 + 同步监听器
        self._srq_queue: deque[int] = deque()
        self._srq_event = asyncio.Event()
        self._srq_listeners: list[Callable[[int], None]] = []
        # 每设备独立错误队列（SYST:ERR? 弹出）
        self._error_queues: dict[int, deque[str]] = {}

    # ------------------------------------------------------------------
    # 设备管理
    # ------------------------------------------------------------------

    def attach(self, device: VirtualGPIBDevice, primary_address: int) -> None:
        """把设备挂到主地址（0..30，GPIB 地址空间；地址须空闲）。"""
        if not 0 <= primary_address <= 30:
            raise GPIBAddressError(f"primary address out of range [0,30]: {primary_address}")
        if primary_address in self._devices:
            raise GPIBAddressError(f"address {primary_address} already attached")
        device._attach(self, primary_address)
        self._devices[primary_address] = device
        self._error_queues[primary_address] = deque()

    def detach(self, primary_address: int) -> None:
        """摘除设备（不存在则 GPIBAddressError）。"""
        if primary_address not in self._devices:
            raise GPIBAddressError(f"address {primary_address} not attached")
        del self._devices[primary_address]
        self._error_queues.pop(primary_address, None)
        if self._selected == primary_address:
            self._selected = None

    @property
    def addresses(self) -> tuple[int, ...]:
        """已挂载地址（升序）。"""
        return tuple(sorted(self._devices))

    def __iter__(self) -> Iterator[tuple[int, VirtualGPIBDevice]]:
        return iter(sorted(self._devices.items()))

    # ------------------------------------------------------------------
    # 寻址
    # ------------------------------------------------------------------

    def select(self, primary_address: int) -> None:
        """方法式选址（后续 execute 路由到该地址）。"""
        if primary_address not in self._devices:
            raise GPIBAddressError(f"no device at address {primary_address}")
        self._selected = primary_address

    @property
    def selected_address(self) -> int | None:
        """当前选址（未选址为 None）。"""
        return self._selected

    # ------------------------------------------------------------------
    # 命令收发
    # ------------------------------------------------------------------

    async def execute(self, command: str, address: int | None = None) -> str | None:
        """向（显式或当前选址的）设备发送一条 SCPI 命令。

        总线互斥下执行：同板所有命令（含其他网关实例）串行化；
        ``command_latency_s`` 模拟总线传输时延（持锁期间休眠）。

        Args:
            command: SCPI 命令字符串。
            address: 显式目标地址；缺省用 :meth:`select` 选定的地址。

        Returns:
            查询应答字符串；写命令为 None。未知查询应答
            ``-113,"Undefined header"``（同时入错误队列）。

        Raises:
            GPIBAddressError: 未选址 / 地址无设备。
            GPIBTimeoutError: 故障钩子命中 timeout 动作。
        """
        cmd = command.upper().strip()
        body = cmd[1:] if cmd.startswith("*") else cmd
        is_adr = body.startswith("ADR")

        target = address if address is not None else self._selected
        if target is None:
            # 寻址引导：ADR 命令先于设备上下文执行（对应真实 GPIB 的 ATN
            # 总线管理）；其余命令无选址即报错。
            if not is_adr:
                raise GPIBAddressError("no device addressed: select() or pass address explicitly")
            error = self._check_faults()
            async with _bus_lock(self.board_id):
                if self._latency:
                    await asyncio.sleep(self._latency)
                if error is not None:
                    return f'{error[0]},"{error[1]}"'
                try:
                    return self._handle_adr(body)
                except (ValueError, GPIBAddressError) as exc:
                    raise GPIBAddressError(f"malformed ADR command: {command!r}") from exc

        device = self._devices.get(target)
        if device is None:
            raise GPIBAddressError(f"no device at address {target}")

        error = self._check_faults()
        async with _bus_lock(self.board_id):
            if self._latency:
                await asyncio.sleep(self._latency)
            if error is not None:
                code, message = error
                return self._push_error(target, code, message)
            return self._dispatch(device, target, command)

    def _handle_adr(self, body: str) -> str | None:
        """网关级寻址伪命令：``ADR <n>`` 选址 / ``ADR?`` 查询当前地址。

        真实 GPIB 经 ATN 总线管理寻址，此处以 SCPI 风格近似；``*ADR``
        为任务书变体写法（调用前已剥前导 ``*``）。
        """
        if body == "ADR?" or body == "ADR":
            return str(self._selected) if self._selected is not None else ""
        if body.startswith("ADR "):
            self.select(int(body.split(None, 1)[1]))
            return None
        raise GPIBAddressError(f"not an ADR command: {body!r}")

    def _dispatch(self, device: VirtualGPIBDevice, target: int, command: str) -> str | None:
        """SCPI 分发：ADR 寻址 → IEEE 公共命令 → SYST:ERR? → 设备处理器。"""
        cmd = command.upper().strip()

        adr = cmd[1:] if cmd.startswith("*") else cmd
        if adr == "ADR" or adr == "ADR?" or adr.startswith("ADR "):
            return self._handle_adr(adr)

        # IEEE-488.2 公共命令（网关代管）
        if cmd == "*IDN?":
            return device.identity
        if cmd == "*RST":
            device.reset()
            return None
        if cmd == "*CLS":
            device.clear_status_bits(0xFF)
            device.clear_service_request()
            self._error_queues[target].clear()
            return None
        if cmd == "*STB?":
            return str(device.status_byte)
        if cmd == "*OPC?":
            return "1"

        # SCPI 状态寄存器错误队列
        if cmd == "SYST:ERR?":
            queue = self._error_queues[target]
            return queue.popleft() if queue else _SCPI_NO_ERROR

        # 仪器专属命令 → 设备处理器
        try:
            return device.handle_scpi(cmd)
        except UnknownSCPICommandError:
            # SCPI 惯例：未定义头 → -113 入错误队列；本 mock 对查询与写命令
            # 统一以错误串应答（调用方无轮询 SYST:ERR? 也能感知失败）。
            return self._push_error(target, SCPI_ERR_UNDEFINED_HEADER, _SCPI_UNDEFINED_HEADER_MSG)

    # ------------------------------------------------------------------
    # 串行查询与 SRQ
    # ------------------------------------------------------------------

    async def read_status_byte(self, address: int | None = None) -> int:
        """串行查询：经总线锁读取设备状态字节。"""
        target = address if address is not None else self._selected
        if target is None:
            raise GPIBAddressError("no device addressed: select() or pass address explicitly")
        device = self._devices.get(target)
        if device is None:
            raise GPIBAddressError(f"no device at address {target}")
        async with _bus_lock(self.board_id):
            return device.status_byte

    def add_srq_listener(self, callback: Callable[[int], None]) -> None:
        """注册 SRQ 监听器（发起设备的 assert 任务内同步调用，参数为主地址）。"""
        self._srq_listeners.append(callback)

    def remove_srq_listener(self, callback: Callable[[int], None]) -> None:
        """移除监听器（未注册时静默）。"""
        try:
            self._srq_listeners.remove(callback)
        except ValueError:
            pass

    @property
    def srq_pending(self) -> bool:
        """是否有待处理的 SRQ 通知。"""
        return bool(self._srq_queue)

    async def wait_srq(self, timeout: float | None = None) -> int:
        """等待任一设备的服务请求，返回发起设备的主地址。

        已排队的 SRQ 立即返回（锁存语义）；否则阻塞至新 SRQ 或超时。

        Raises:
            GPIBTimeoutError: timeout 秒内无 SRQ。
        """
        if not self._srq_queue:
            try:
                await asyncio.wait_for(self._srq_event.wait(), timeout)
            except TimeoutError as exc:
                msg = f"no service request within {timeout}s"
                raise GPIBTimeoutError(msg) from exc
            self._srq_event.clear()
        return self._srq_queue.popleft()

    def _on_service_request(self, address: int | None) -> None:
        """设备 request_service 回调：排队 + 唤醒等待者 + 通知监听器。"""
        if address is None:  # 未挂载的设备不应到达这里；防御性忽略
            return
        self._srq_queue.append(address)
        self._srq_event.set()
        for listener in list(self._srq_listeners):
            listener(address)

    # ------------------------------------------------------------------
    # 故障钩子
    # ------------------------------------------------------------------

    def inject_timeout_once(self) -> None:
        """手动注入：下一条 execute 抛 GPIBTimeoutError（一次性）。"""
        self._timeout_once = True

    def inject_error_once(self, code: int = SCPI_ERR_UNDEFINED_HEADER, message: str = "Injected comm error") -> None:
        """手动注入：下一条 execute 得到 ``<code>,"<message>"`` 错误应答并入队。"""
        self._error_once = (code, message)

    def _check_faults(self) -> tuple[int, str] | None:
        """执行前故障检查：手动钩子优先，其次 FaultInjector 规则。

        count 触发按“第 n 次总线传输”语义命中（与 check_scheduler_raise
        的派发计数一致）：每次 execute 递增传输计数并作为 call_count 上下文。

        Returns:
            应答型错误的 (code, message)；timeout 类直接抛出；None 正常放行。
        """
        if self._timeout_once:
            self._timeout_once = False
            raise GPIBTimeoutError("injected GPIB bus timeout")
        if self._error_once is not None:
            code, message = self._error_once
            self._error_once = None
            return code, message
        if self._faults is None:
            return None
        resource = f"gpib{self.board_id}"
        self._transfer_count += 1
        context = {"call_count": self._transfer_count}
        action = self._faults.check_network(resource, "transfer", context)
        if action is not None and action.fault_type == "timeout":
            raise GPIBTimeoutError(f"Fault '{action.fault_id}' injected at network layer (bus timeout)")
        action = self._faults.check_protocol(resource, "transfer", context)
        if action is not None and action.fault_type in ("scpi_error", "truncated_data"):
            params = action.params
            return int(params.get("code", SCPI_ERR_UNDEFINED_HEADER)), str(
                params.get("message", f"Fault '{action.fault_id}' injected at protocol layer")
            )
        return None

    def _push_error(self, target: int, code: int, message: str) -> str:
        """错误入队并返回 SCPI 标准格式应答 ``<code>,"<message>"``。"""
        response = f'{code},"{message}"'
        self._error_queues[target].append(response)
        return response


__all__ = [
    "FACTORY_KEY",
    "STB_SRQ_BIT",
    "GPIBAddressError",
    "GPIBError",
    "GPIBTimeoutError",
    "MeasurementDevice",
    "MockGPIBGateway",
    "MockRegistryLike",
    "SCPI_ERR_UNDEFINED_HEADER",
    "UnknownSCPICommandError",
    "VirtualGPIBDevice",
    "register",
]
