"""PA601-D54A「23 步全量 commissioning 计划」虚拟设备端到端运行器。

它做什么
--------
在**无硬件**的条件下，用 mat-device-control 的虚拟设备驱动（``MAT.Virtual*``）
跑通 ``data/commission/pa601_full_commission.yaml`` 的 23 个 TC，验证：

    ATEStudio edge → MatDeviceHttpDriver → mat-device-control
      → Virtual* 驱动 → 判定（_judge）→ outputs.result_measured_value

即整条驱动链 + 判据路径在无真板时可用，且 **故障注入时判据链路能报 FAIL 并
fail-fast 中止**。这是给 CI / 桌面验证用的可执行 runner，不是上板 runbook
（后者见 ``scripts/commission_real_board.py``）。

三种模式
--------
1. **live（默认）**：建临时 workdir，生成 ``config.toml`` + ``devices.toml``
   （9 台虚拟设备），启动 mat-device-control，复制 23 脚本，逐 step 合并 overlay
   限值并注入 device_id/base_url，经 ``ProcessExecutor`` + ``V32PlanDispatcher``
   执行；断言 23/23 PASS 且每步 outputs 含 measured_value；写报告；退出码 0。
2. ``--inject <tc_id>=<value>``：给该 step 注入 ``params["force_measured"]``，
   断言该步 FAIL 且 fail-fast 中止（本运行以非零退出码收尾；0 仅用于 23/23 全通过），
   证明判据/中止链路活性。
3. ``--check``：不起服务，静态自检（23 脚本存在且可编译 + 与计划一一对应 +
   overlay 覆盖占位步 + 计划解析 23 步），退出码 0/非 0。

跨仓冻结接口
------------
设备 9 台（device_id / manufacturer.model）见 :data:`DEVICE_SPECS`；连接字段
``connection_type="virtual"`` / ``protocol="custom"``（若 mat-device-control 回退
到 tcpip/custom，改 :data:`VIRTUAL_CONNECTION_TYPE` / :data:`VIRTUAL_PROTOCOL`
即可，运行时以生成的 devices.toml 为准）。

纪律
----
- 不改 23 步计划 YAML 本体：占位限值用外置 overlay（``pa601_virtual_limits.yaml``）。
- 不改 ``scripts/commission_real_board.py`` / ``scripts/fill_in_stub.py`` 既有行为。
- fail-fast：任一步失败即中止并返回非零退出码，不静默降级。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import py_compile
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

#: mat-device-control 仓库（含 venv 与 main:app），可用环境变量覆盖。
DEFAULT_MDC_REPO = Path(
    os.environ.get("MAT_DEVICE_CONTROL_REPO", r"F:\Workspace\ATETest\mat-device-control")
)

#: 23 步计划与 overlay。
PLAN_PATH = REPO_ROOT / "data" / "commission" / "pa601_full_commission.yaml"
OVERLAY_PATH = REPO_ROOT / "data" / "commission" / "pa601_virtual_limits.yaml"
SCRIPTS_DIR = REPO_ROOT / "data" / "commission" / "scripts"
REPORTS_DIR = REPO_ROOT / "data" / "reports"

#: 运行期临时工作目录（可丢弃）。
WORKDIR = REPO_ROOT / "tests" / "tmp" / "commission_virtual_run"

#: 冻结连接字段（mat-device-control 虚拟设备；回退时集中改这里）。
VIRTUAL_CONNECTION_TYPE = "virtual"
VIRTUAL_PROTOCOL = "custom"

#: 计划应执行的 TC 数量（23 步全量）。
EXPECTED_STEP_COUNT = 23
#: overlay 必须覆盖的占位步骤（基准表未给数值）。
OVERLAY_REQUIRED = ("tc05", "tc06", "tc08", "tc21", "tc22")

#: 子进程启动/停机超时（秒）。
STARTUP_TIMEOUT = 60.0
SHUTDOWN_TIMEOUT = 10.0
#: 单步脚本默认超时（秒；step.timeout>0 时以 step 为准）。
DEFAULT_SCRIPT_TIMEOUT = 600.0

#: 计划超时=0 表示长时老化（无超时）→ 运行器用该兜底值。
BURN_IN_TIMEOUT = 600.0

#: 每 step 的 tc 前缀 → 设备 ID（冻结接口：见计划 YAML step 顺序）。
DEVICE_BY_TC: dict[str, str] = {
    "tc01": "safety001",
    "tc02": "safety001",
    "tc03": "safety001",
    "tc04": "safety001",
    "tc05": "acsrc001",
    "tc06": "scope001",
    "tc07": "acsrc001",
    "tc08": "acsrc001",
    "tc09": "pmeter001",
    "tc10": "psu001",
    "tc11": "pmeter001",
    "tc12": "dmm001",
    "tc13": "scope001",
    "tc14": "scope001",
    "tc15": "scope001",
    "tc16": "scope001",
    "tc17": "pmeter001",
    "tc18": "eload001",
    "tc19": "eload001",
    "tc20": "eload001",
    "tc21": "eload001",
    "tc22": "thermo001",
    "tc23": "aging001",
}

#: 每 step 的 tc 前缀 → 展示用方法名（写报告用；判定以脚本实际调用为准）。
METHOD_BY_TC: dict[str, str] = {
    "tc01": "measure_hipot_leakage_ma",
    "tc02": "measure_insulation_resistance_mohm",
    "tc03": "measure_ground_bond_resistance_ohm",
    "tc04": "measure_touch_current_ma",
    "tc05": "set_voltage+output_on→measure_turn_on_delay_ms",
    "tc06": "capture_inrush_peak_a",
    "tc07": "measure_holdup_time_ms",
    "tc08": "measure_uvlo_threshold_v",
    "tc09": "measure_power_factor",
    "tc10": "set_voltage→measure_voltage",
    "tc11": "measure_regulation_pct",
    "tc12": "measure_cross_regulation_pct",
    "tc13": "measure_ripple_mvpp",
    "tc14": "measure_transient_recovery_us",
    "tc15": "measure_startup_time_ms",
    "tc16": "measure_overshoot_pct",
    "tc17": "measure_efficiency_pct",
    "tc18": "measure_ocp_trip_pct",
    "tc19": "measure_ovp_trip_pct",
    "tc20": "measure_uvp_trip_pct",
    "tc21": "measure_scp_recovery_s",
    "tc22": "measure_otp_trip_c",
    "tc23": "run_burn_in",
}


@dataclasses.dataclass(frozen=True, slots=True)
class DeviceSpec:
    """一台虚拟设备的冻结元数据（生成 devices.toml 用）。"""

    device_id: str
    manufacturer: str
    model: str
    category: str
    allowed_methods: tuple[str, ...]


#: 9 台虚拟设备的冻结接口（manufacturer.model 与 mat-device-control 注册键一致）。
DEVICE_SPECS: tuple[DeviceSpec, ...] = (
    DeviceSpec(
        "safety001",
        "MAT",
        "VirtualSafety",
        "generic",
        (
            "measure_hipot_leakage_ma",
            "measure_insulation_resistance_mohm",
            "measure_ground_bond_resistance_ohm",
            "measure_touch_current_ma",
        ),
    ),
    DeviceSpec(
        "dmm001",
        "MAT",
        "VirtualDMM",
        "multimeter",
        ("measure_cross_regulation_pct",),
    ),
    DeviceSpec(
        "eload001",
        "MAT",
        "VirtualELoad",
        "electronic_load",
        (
            "set_cc",
            "measure_ocp_trip_pct",
            "measure_ovp_trip_pct",
            "measure_uvp_trip_pct",
            "measure_scp_recovery_s",
        ),
    ),
    DeviceSpec(
        "scope001",
        "MAT",
        "VirtualScope",
        "oscilloscope",
        (
            "capture_inrush_peak_a",
            "measure_ripple_mvpp",
            "measure_transient_recovery_us",
            "measure_startup_time_ms",
            "measure_overshoot_pct",
        ),
    ),
    DeviceSpec(
        "acsrc001",
        "MAT",
        "VirtualACSource",
        "power_supply",
        (
            "set_voltage",
            "output_on",
            "output_off",
            "measure_turn_on_delay_ms",
            "measure_holdup_time_ms",
            "measure_uvlo_threshold_v",
        ),
    ),
    DeviceSpec(
        "pmeter001",
        "MAT",
        "VirtualPowerMeter",
        "power_meter",
        (
            "measure_power_factor",
            "measure_regulation_pct",
            "measure_efficiency_pct",
        ),
    ),
    DeviceSpec(
        "psu001",
        "MAT",
        "VirtualPSU",
        "power_supply",
        ("set_voltage", "measure_voltage"),
    ),
    DeviceSpec(
        "thermo001",
        "MAT",
        "VirtualThermo",
        "generic",
        ("measure_otp_trip_c",),
    ),
    DeviceSpec(
        "aging001",
        "MAT",
        "VirtualAging",
        "generic",
        ("run_burn_in",),
    ),
)


class CommissionError(Exception):
    """运行器 fail-fast 异常（中止并返回非零退出码）。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """申请一个空闲 TCP 端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """判断 host:port 是否可建立 TCP 连接。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_http(url: str, timeout: float = STARTUP_TIMEOUT) -> None:
    """轮询等待 HTTP 端点返回 2xx（超时 fail-fast）。"""
    import time

    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = str(exc)
            time.sleep(0.2)
    raise TimeoutError(f"HTTP {url} 在 {timeout}s 内未就绪: {last_error}")


def _tc_prefix(step_id: str) -> str:
    """从 step.id 取 ``tcNN`` 前缀（如 ``tc05_power_sequence`` → ``tc05``）。"""
    return step_id[:4]


# ---------------------------------------------------------------------------
# 计划 / overlay
# ---------------------------------------------------------------------------


def load_plan() -> Any:
    """解析 23 步全量 commissioning 计划。"""
    from ate_platform.dsl.parser import YamlParser

    if not PLAN_PATH.exists():
        raise CommissionError(f"计划文件不存在: {PLAN_PATH}")
    return YamlParser().parse(PLAN_PATH)


def load_overlay() -> dict[str, dict[str, Any]]:
    """读取 overlay YAML,返回 {tc_id: entry}（键同时支持 step.id 与 tcNN）。"""
    import yaml

    if not OVERLAY_PATH.exists():
        raise CommissionError(f"overlay 文件不存在: {OVERLAY_PATH}")
    data = yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("version"):
        raise CommissionError(f"overlay schema 非法（缺 version）: {OVERLAY_PATH}")
    steps = data.get("steps") or {}
    if not isinstance(steps, dict):
        raise CommissionError(f"overlay.steps 非映射: {OVERLAY_PATH}")
    table: dict[str, dict[str, Any]] = {}
    for key, entry in steps.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("value"), dict):
            raise CommissionError(f"overlay 条目非法（缺 value）: {key}")
        table[str(key)] = entry
    return table


def _overlay_entry(
    overlay: dict[str, dict[str, Any]], step_id: str
) -> dict[str, Any] | None:
    """按 step.id 或 tcNN 前缀查 overlay 条目。"""
    return overlay.get(step_id) or overlay.get(_tc_prefix(step_id))


def _overlay_keys(overlay: dict[str, dict[str, Any]]) -> set[str]:
    """overlay 覆盖的 tc 前缀集合。"""
    return {k[:4] for k in overlay}


# ---------------------------------------------------------------------------
# 静态自检（--check）
# ---------------------------------------------------------------------------


def static_check() -> int:
    """静态自检:脚本齐全可编译 + 与计划一一对应 + overlay 覆盖占位步。

    Returns:
        退出码（0=通过）。
    """
    print("[check] 计划解析…")
    plan = load_plan()
    if len(plan.steps) != EXPECTED_STEP_COUNT:
        print(f"[check] FAIL: 计划 step 数={len(plan.steps)}, 期望 {EXPECTED_STEP_COUNT}")
        return 1

    print("[check] 23 脚本存在且可编译…")
    errors: list[str] = []
    plan_scripts = [s.script for s in plan.steps]
    for step in plan.steps:
        script_path = SCRIPTS_DIR / step.script
        if not script_path.exists():
            errors.append(f"脚本缺失: {step.script} (step={step.id})")
            continue
        try:
            py_compile.compile(str(script_path), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"编译失败: {step.script}: {exc}")
    if errors:
        for e in errors:
            print(f"[check] FAIL: {e}")
        return 1

    print("[check] 脚本与计划一一对应…")
    dir_scripts = {p.name for p in SCRIPTS_DIR.glob("*.py")}
    plan_set = set(plan_scripts)
    if dir_scripts != plan_set:
        extra = sorted(dir_scripts - plan_set)
        missing = sorted(plan_set - dir_scripts)
        print(f"[check] FAIL: overlay/脚本不一一对应; extra={extra}, missing={missing}")
        return 1
    if len(plan_set) != EXPECTED_STEP_COUNT:
        print(f"[check] FAIL: 脚本数={len(plan_set)}, 期望 {EXPECTED_STEP_COUNT}")
        return 1

    print("[check] overlay 覆盖占位步…")
    overlay = load_overlay()
    covered = _overlay_keys(overlay)
    for tc in OVERLAY_REQUIRED:
        if tc not in covered:
            print(f"[check] FAIL: overlay 未覆盖占位步 {tc}")
            return 1
    for key, entry in overlay.items():
        value = entry["value"]
        has_bound = any(k in value for k in ("min", "max"))
        if not has_bound:
            print(f"[check] FAIL: overlay {key} 无 min/max 限值")
            return 1
        if not str(entry.get("note", "")).strip():
            print(f"[check] FAIL: overlay {key} 缺 note 说明")
            return 1

    print("[check] PASS: 23 脚本 + overlay + 计划一致")
    return 0


# ---------------------------------------------------------------------------
# mat-device-control
# ---------------------------------------------------------------------------


def _write_runtime_config(workdir: Path) -> None:
    """生成运行期 config.toml + devices.toml（9 台虚拟设备，不改仓内文件）。"""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "config.toml").write_text(
        "\n".join(
            [
                "[app]",
                'name = "mat-device-control"',
                "debug = false",
                "",
                "[log]",
                'level = "INFO"',
                'log_dir = "logs"',
                "max_bytes = 1048576",
                "backup_count = 3",
                "retention_days = 1",
                'format = "json"',
                "",
                "[mqtt]",
                "enabled = false",
                "",
                "[devices]",
                "auto_load = true",
                'config_file = "devices.toml"',
                "devices = []",
                "",
            ]
        ),
        encoding="utf-8",
    )

    lines: list[str] = ["# 临时虚拟设备配置（commission virtual run，勿提交）", ""]
    for spec in DEVICE_SPECS:
        methods = ", ".join(f'"{m}"' for m in spec.allowed_methods)
        lines += [
            "[[devices]]",
            f'device_id = "{spec.device_id}"',
            f'model = "{spec.model}"',
            f'manufacturer = "{spec.manufacturer}"',
            f'category = "{spec.category}"',
            f'connection_type = "{VIRTUAL_CONNECTION_TYPE}"',
            f'protocol = "{VIRTUAL_PROTOCOL}"',
            'resource_string = ""',
            "auto_connect = true",
            "enabled = true",
            "timeout = 5.0",
            "retry_count = 3",
            "retry_delay = 0.2",
            f"allowed_methods = [{methods}]",
            "",
        ]
    (workdir / "devices.toml").write_text("\n".join(lines), encoding="utf-8")


def virtual_drivers_registered(mdc_repo: Path) -> bool:
    """探测 M 仓是否已注册 9 个 ``MAT.Virtual*`` 驱动键。"""
    factory = mdc_repo / "modules" / "device" / "factory.py"
    if not factory.exists():
        return False
    text = factory.read_text(encoding="utf-8", errors="replace")
    return all(f"MAT.{spec.model}" in text for spec in DEVICE_SPECS)


class MdcServer:
    """mat-device-control 子进程（fail-safe 停机，参考 commission_real_board）。"""

    def __init__(self, mdc_repo: Path, workdir: Path) -> None:
        """记录启动参数（未启动）。"""
        self._repo = mdc_repo
        self._workdir = workdir
        self._procs: list[subprocess.Popen[bytes]] = []
        self.port = 0

    @property
    def base_url(self) -> str:
        """mat-device-control 基址。"""
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> str:
        """生成配置、启动服务并等待 /health 就绪。

        Returns:
            服务基址。

        Raises:
            FileNotFoundError: M 仓 venv 不可用。
            TimeoutError: 服务未在超时内就绪。
        """
        mdc_python = self._repo / ".venv" / "Scripts" / "python.exe"
        if not mdc_python.exists():
            raise FileNotFoundError(
                f"mat-device-control venv 不可用: {mdc_python}"
                "（可用环境变量 MAT_DEVICE_CONTROL_REPO 覆盖）"
            )
        _write_runtime_config(self._workdir)
        self.port = _free_port()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self._repo)
        out = (self._workdir / "app.out").open("wb")
        err = (self._workdir / "app.err").open("wb")
        self._procs.append(
            subprocess.Popen(
                [
                    str(mdc_python),
                    "-m",
                    "uvicorn",
                    "main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--log-level",
                    "warning",
                ],
                cwd=str(self._workdir),
                env=env,
                stdout=out,
                stderr=err,
            )
        )
        try:
            _wait_for_http(f"{self.base_url}/health")
        except Exception:
            self.stop()
            raise
        return self.base_url

    def stop(self) -> None:
        """终止子进程（幂等、带超时兜底 kill）。"""
        for proc in self._procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in self._procs:
            try:
                proc.wait(timeout=SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=SHUTDOWN_TIMEOUT)
        self._procs.clear()


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class StepReport:
    """单步运行报告行。"""

    index: int
    tc_id: str
    step_id: str
    device_id: str
    method: str
    measured: Any
    limits: dict[str, Any]
    status: str
    detail: str


class VirtualCommissionRunner:
    """虚拟设备 23 步全量 commissioning 运行器。"""

    def __init__(self, args: argparse.Namespace) -> None:
        """初始化（解析 overlay 与注入表）。"""
        self._args = args
        self._mdc_repo = Path(args.mdc_repo)
        self._overlay = load_overlay()
        self._inject: dict[str, float] = {}
        for raw in args.inject or []:
            if "=" not in raw:
                raise CommissionError(f"--inject 需形如 tc06=100.0,得到: {raw!r}")
            tc, _, val = raw.partition("=")
            tc = tc.strip()
            if tc not in DEVICE_BY_TC:
                raise CommissionError(f"--inject 未知 tc_id: {tc!r}")
            self._inject[tc] = float(val)
        self._rows: list[StepReport] = []

    # -- 主流程 ------------------------------------------------------------

    def run(self) -> int:
        """执行 live（或注入）运行；返回退出码（0=23/23 PASS）。"""
        os.environ.setdefault("ATE_DEV_MODE", "true")

        if not virtual_drivers_registered(self._mdc_repo):
            print(
                "[commission-virtual] M 仓未见 9 台 MAT.Virtual* 驱动注册"
                f"（{self._mdc_repo / 'modules' / 'device' / 'factory.py'}）："
                "live run 待 M lane 完成后由主会话执行。",
                file=sys.stderr,
            )
            return 3

        self._prepare_workdir()
        server = MdcServer(self._mdc_repo, WORKDIR)
        try:
            base_url = server.start()
            print(f"[commission-virtual] mat-device-control 就绪: {base_url}")
            self._copy_scripts()
            plan = self._prepare_plan(base_url)
            outcomes, outputs_by_step = asyncio.run(self._execute(plan))
        finally:
            server.stop()

        return self._evaluate(outcomes, outputs_by_step, base_url)

    def _prepare_workdir(self) -> None:
        """重置临时工作目录。"""
        if WORKDIR.exists():
            shutil.rmtree(WORKDIR)
        WORKDIR.mkdir(parents=True, exist_ok=True)

    def _copy_scripts(self) -> None:
        """把 23 脚本复制进工作目录（执行器按 step.script 相对解析）。"""
        for path in SCRIPTS_DIR.glob("*.py"):
            shutil.copy2(path, WORKDIR / path.name)

    def _prepare_plan(self, base_url: str) -> Any:
        """解析计划，逐 step 合并 overlay + 注入 device_id/base_url/force_measured。"""
        plan = load_plan()
        if len(plan.steps) != EXPECTED_STEP_COUNT:
            raise CommissionError(
                f"计划 step 数={len(plan.steps)}, 期望 {EXPECTED_STEP_COUNT}"
            )
        for step in plan.steps:
            tc = _tc_prefix(step.id)
            device_id = DEVICE_BY_TC.get(tc)
            if device_id is None:
                raise CommissionError(f"未映射设备: step={step.id}")
            entry = _overlay_entry(self._overlay, step.id)
            if entry is not None:
                step.params["value"] = dict(entry["value"])
            step.params["device_id"] = device_id
            step.params["base_url"] = base_url
            if tc in self._inject:
                step.params["force_measured"] = self._inject[tc]
        return plan

    async def _execute(self, plan: Any) -> tuple[list[Any], dict[str, dict[str, Any]]]:
        """经 ProcessExecutor + V32PlanDispatcher 顺序执行 23 步。"""
        from ate_platform.executor.process_executor import ProcessExecutor
        from ate_platform.executor.v32_dispatcher import V32PlanDispatcher

        outputs_by_step: dict[str, dict[str, Any]] = {}
        errors_by_step: dict[str, str | None] = {}
        executor = ProcessExecutor(max_workers=1, script_timeout=DEFAULT_SCRIPT_TIMEOUT)

        async def _exec(step: Any) -> bool:
            """把一步交给 ProcessExecutor 在独立线程跑（真链路驱动）。"""
            timeout = float(step.timeout) if (step.timeout or 0) > 0 else BURN_IN_TIMEOUT
            res = await executor.execute_async(
                str(WORKDIR / step.script),
                dict(step.params),
                step_id=step.id,
                timeout=timeout,
            )
            outputs_by_step[step.id] = dict(getattr(res, "outputs", {}) or {})
            errors_by_step[step.id] = getattr(res, "error", None)
            status = str(getattr(res.status, "value", res.status))
            return status.upper() in ("PASSED", "PASS")

        try:
            outcomes = await V32PlanDispatcher(
                plan, simulation=False, script_executor=_exec
            ).run()
        finally:
            executor.shutdown()

        # 收集报告行。
        step_by_id = {s.id: s for s in plan.steps}
        for outcome in outcomes:
            step = step_by_id.get(outcome.step_id)
            outputs = outputs_by_step.get(outcome.step_id, {})
            err = errors_by_step.get(outcome.step_id)
            tc = _tc_prefix(outcome.step_id)
            params = dict(getattr(step, "params", {}) or {})
            self._rows.append(
                StepReport(
                    index=len(self._rows) + 1,
                    tc_id=tc,
                    step_id=outcome.step_id,
                    device_id=str(params.get("device_id", DEVICE_BY_TC.get(tc, "?"))),
                    method=METHOD_BY_TC.get(tc, "?"),
                    measured=outputs.get("result_measured_value"),
                    limits=dict(params.get("value") or {}),
                    status=outcome.status,
                    detail=(err or outcome.detail or ""),
                )
            )
        return outcomes, outputs_by_step

    def _evaluate(
        self,
        outcomes: list[Any],
        outputs_by_step: dict[str, dict[str, Any]],
        base_url: str,
    ) -> int:
        """判定 23/23 或注入失败；写报告并返回退出码。"""
        passed = [o for o in outcomes if o.status == "PASS"]
        failed = [o for o in outcomes if o.status != "PASS"]

        # 注入模式：断言注入步 FAIL 且 fail-fast 中止（本运行按规范以非零退出码收尾）。
        if self._inject:
            injected_tc = next(iter(self._inject))
            executed = [o for o in outcomes if o.status in ("PASS", "FAIL")]
            hit = [o for o in outcomes if _tc_prefix(o.step_id) == injected_tc]
            if not hit or hit[0].status != "FAIL":
                statuses = [(_tc_prefix(o.step_id), o.status) for o in outcomes]
                print(
                    f"[commission-virtual] FAIL: 注入 {injected_tc} 未按预期 FAIL "
                    f"(statuses={statuses})",
                    file=sys.stderr,
                )
                return 2  # 判据链路异常（未按预期报 FAIL）
            if len(executed) >= EXPECTED_STEP_COUNT:
                print(
                    "[commission-virtual] FAIL: 注入失败未触发 fail-fast 中止"
                    f"（仍执行了 {len(executed)} 步）",
                    file=sys.stderr,
                )
                return 2  # 中止链路异常
            print(
                f"[commission-virtual] 注入证明: {injected_tc} 判据报 FAIL 并 fail-fast "
                f"中止（已执行 {len(executed)}/{EXPECTED_STEP_COUNT} 步，其余 BLOCKED/未调度；"
                f"注入值={self._inject[injected_tc]}）—— 判据/中止链路活性成立"
            )
            # 注入即故意制造一次失败：按规范以非零退出码收尾。
            return 1

        # 正常模式：断言 23/23 PASS 且每步 outputs 含 measured_value。
        ok = True
        if len(outcomes) != EXPECTED_STEP_COUNT or failed:
            ok = False
        missing_measured = [
            sid
            for sid, out in outputs_by_step.items()
            if not isinstance(out.get("result_measured_value"), (int, float))
            or isinstance(out.get("result_measured_value"), bool)
        ]
        if missing_measured:
            ok = False
        if len(outputs_by_step) != EXPECTED_STEP_COUNT:
            ok = False

        report_path = self._write_report(base_url, ok)
        print("\n" + "=" * 72)
        print("PA601-D54A 23-TC 虚拟 commissioning 结果")
        print("=" * 72)
        for row in self._rows:
            print(
                f"[{row.status}] {row.tc_id} {row.device_id} "
                f"{row.method} measured={row.measured} limits={row.limits}"
            )
        print(f"\n报告: {report_path}")
        if ok:
            print(f"结论: 23/23 PASS —— 驱动链 + 判据路径在虚拟设备上贯通（{len(passed)} 步）")
            return 0
        print(
            f"结论: FAIL —— PASS {len(passed)}/{EXPECTED_STEP_COUNT}, "
            f"失败={[o.step_id for o in failed]}, 缺读数={missing_measured}",
            file=sys.stderr,
        )
        return 1

    # -- 报告 --------------------------------------------------------------

    def _report_path(self) -> Path:
        """报告文件路径（data/reports/pa601-23tc-virtual-<YYYY-MM-DD>.md）。"""
        return REPORTS_DIR / f"pa601-23tc-virtual-{date.today().isoformat()}.md"

    def _write_report(self, base_url: str, ok: bool) -> Path:
        """写 Markdown 报告并返回路径。"""
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = self._report_path()
        passed = sum(1 for r in self._rows if r.status == "PASS")
        lines: list[str] = [
            f"# PA601-D54A 23-TC 全量 commissioning 虚拟运行报告（{date.today().isoformat()}）",
            "",
            "- 模式: virtual（mat-device-control 虚拟设备，无真板）",
            f"- mat-device-control base_url: {base_url}",
            f"- 计划: `{PLAN_PATH.relative_to(REPO_ROOT).as_posix()}`",
            f"- overlay: `{OVERLAY_PATH.relative_to(REPO_ROOT).as_posix()}`",
            f"- 结果: {passed}/{EXPECTED_STEP_COUNT} PASS"
            if ok
            else f"- 结果: FAIL（{passed}/{EXPECTED_STEP_COUNT} PASS）",
            "",
            "## 逐步结果",
            "",
            "| # | TC | device | method | measured | limits | status |",
            "|---|----|--------|--------|----------|--------|--------|",
        ]
        for row in self._rows:
            limits = ", ".join(
                f"{k}={v}" for k, v in row.limits.items()
            )
            lines.append(
                f"| {row.index} | {row.tc_id} | {row.device_id} | {row.method} | "
                f"{row.measured} | {limits} | {row.status} |"
            )
        lines += [
            "",
            "## 结论",
            "",
            (
                "23/23 PASS：ATEStudio 驱动链（MatDeviceHttpDriver → mat-device-control → "
                "Virtual* 设备）与判据归因路径在无硬件条件下贯通；每步 outputs 均含"
                " `result_measured_value`。"
                if ok
                else f"未全通过：{passed}/{EXPECTED_STEP_COUNT} 步 PASS，详见逐步结果。"
            ),
            "",
            "## 免责声明",
            "",
            "本报告由虚拟行为模型产生的**确定性合格值**，仅用于验证工具链与判据路径"
            "（含故障注入下的 FAIL/中止链路），**不替代真板与规格书验证**。上板前须以"
            "PA601 规格书核定全部限值（尤其 overlay 覆盖的 tc05/06/08/21/22）。",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """构造 CLI 解析器。"""
    parser = argparse.ArgumentParser(
        description="PA601-D54A 23 步全量 commissioning 虚拟设备运行器"
    )
    parser.add_argument(
        "--check", action="store_true", help="静态自检（不起服务）"
    )
    parser.add_argument(
        "--inject",
        action="append",
        default=[],
        metavar="TC=VALUE",
        help="给该 step 注入 force_measured（断言该步 FAIL 并 fail-fast），可重复",
    )
    parser.add_argument(
        "--mdc-repo",
        default=str(DEFAULT_MDC_REPO),
        help="mat-device-control 仓库路径（可用 MAT_DEVICE_CONTROL_REPO 覆盖）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    args = _build_parser().parse_args(argv)
    if args.check:
        return static_check()
    try:
        return VirtualCommissionRunner(args).run()
    except CommissionError as exc:
        print(f"[commission-virtual] FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
