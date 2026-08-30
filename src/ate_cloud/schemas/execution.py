"""Pydantic schemas for Execution resources.

Defines request/response models for execution CRUD and SSE streaming:
- ExecutionCreate: Schema for starting a new execution
- ExecutionUpdate: Schema for partial updates (internal use)
- ExecutionResponse: Schema for execution API responses
- ExecutionAbortResponse: Schema for abort endpoint response
"""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ExecutionCreate(BaseModel):
    """Schema for creating a new execution.

    Attributes:
        sequence_id: The sequence to execute (required).
        config: Optional execution configuration (max_concurrency, etc.).
    """

    sequence_id: str = Field(..., min_length=1, max_length=36)
    config: dict[str, Any] | None = None


class ExecutionUpdate(BaseModel):
    """Schema for updating an existing execution.

    All fields are optional to support partial updates.

    Attributes:
        status: Updated execution status.
        result: Updated result summary.
        error: Updated error message.
        completed_at: Timestamp when execution completed.
    """

    status: str | None = Field(None, pattern=r"^(PENDING|RUNNING|COMPLETED|FAILED|ABORTED)$")
    result: dict[str, Any] | None = None
    error: str | None = None
    completed_at: datetime | None = None


class ExecutionResponse(BaseModel):
    """Schema for execution API responses.

    Attributes:
        id: Unique execution identifier (= run_id).
        sequence_id: Reference to the sequence being executed.
        status: Current execution state.
        config: Execution configuration.
        result: Final result summary.
        step_results: Per-step results (JSON list).
        error: Error message on failure.
        started_at: Timestamp when execution started running.
        completed_at: Timestamp when execution completed.
        dut_serial: Device-under-test serial number.
        station_id: Station that ran the execution.
        instrument_ids: List of instrument IDs used.
        created_at: Timestamp of record creation.
        updated_at: Timestamp of last update.
    """

    id: str
    sequence_id: str | None = None
    status: str = "PENDING"
    config: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    step_results: list[dict[str, Any]] | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    dut_serial: str | None = None
    station_id: str | None = None
    instrument_ids: list[str] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = {"from_attributes": True}


class ExecutionAbortResponse(BaseModel):
    """Schema for abort execution response.

    Attributes:
        id: The execution run_id.
        status: Confirmation status (ABORTING).
    """

    id: str
    status: str = "ABORTING"


class ExecutionControlResponse(BaseModel):
    """Schema for execution control (pause/resume/force_next) response.

    Attributes:
        id: The execution run_id.
        action: The control action performed (pause/resume/force_next).
        status: Confirmation status (PAUSING/RESUMING/FORCE_NEXT).
    """

    id: str
    action: str
    status: str


class ExecutionSearchRequest(BaseModel):
    """Schema for searching executions with advanced filters.

    All fields are optional. When multiple filters are provided they are
    combined with AND logic.

    Attributes:
        serial_number: Filter by DUT serial number (partial match).
        product_type: Filter by product type (exact match on config.product_type).
        status: Filter by execution status (PENDING/RUNNING/COMPLETED/FAILED/ABORTED).
        date_from: Filter executions started at or after this ISO datetime.
        date_to: Filter executions started at or before this ISO datetime.
        skip: Pagination offset.
        limit: Maximum number of results (default 50, max 500).
    """

    serial_number: str | None = None
    product_type: str | None = None
    status: str | None = Field(None, pattern=r"^(PENDING|RUNNING|COMPLETED|FAILED|ABORTED)$")
    date_from: datetime | None = None
    date_to: datetime | None = None
    skip: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=500)


class ExecutionListItem(BaseModel):
    """Compact execution item for list/search responses.

    Attributes:
        id: Execution identifier.
        sequence_id: Sequence reference.
        status: Execution status.
        dut_serial: DUT serial number.
        product_type: Product type from config.
        started_at: Execution start timestamp.
        completed_at: Execution completion timestamp.
        pass_rate: Pass rate percentage from result.
        error: Error message if failed.
    """

    id: str
    sequence_id: str | None = None
    status: str = "PENDING"
    dut_serial: str | None = None
    product_type: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    pass_rate: float | None = None
    error: str | None = None

    model_config = {"from_attributes": True}


class ExecutionSearchResponse(BaseModel):
    """Paginated response for execution search.

    Attributes:
        items: List of execution items.
        total: Total matching count (for pagination).
        skip: Pagination offset.
        limit: Page size.
    """

    items: list[ExecutionListItem]
    total: int
    skip: int
    limit: int


class FaultInjectionRequest(BaseModel):
    """Request body for POST /api/v1/executions/{run_id}/fault-injection.

    链路故障注入（T44，设计文档 §8.3）：FixtureDesigner 右键菜单把操作员
    选择的故障类型转发给云端虚拟驱动（§8.3 禁止纯客户端模拟）。

    Attributes:
        link_id: 目标链路 ID（拓扑画布 Link.id）。
        fault_type: 故障类型（§8.3 规定集合，不得扩展）。
        params: 可选附加参数（如 contact_resistance 的阻值、noise 的幅度）。
    """

    link_id: str = Field(..., min_length=1)
    fault_type: Literal["open_circuit", "short_circuit", "contact_resistance", "noise"]
    params: dict[str, Any] | None = None


class FaultInjectionResponse(BaseModel):
    """Response for POST /api/v1/executions/{run_id}/fault-injection.

    Attributes:
        ok: 注入受理成功标志。
        run_id: The execution run identifier.
        link_id: 目标链路 ID。
        fault_type: 故障类型。
        fault_id: 生成的故障规则 ID（转发给 worker FaultInjector 的规则）。
    """

    ok: bool = True
    run_id: str
    link_id: str
    fault_type: str
    fault_id: str


class StepControlRequest(BaseModel):
    """Request body for POST /api/v1/executions/{run_id}/step-control.

    调试步进指令（T40，设计文档 §8.4 StepMode）：断点暂停后由
    SimulationConsole 工具栏发出，经 ``ate.control.{run_id}`` 转发给
    边端调度器的单步状态机。

    Attributes:
        mode: 步进模式（over=步过兄弟 / into=步入容器 / out=步出容器 /
            run_to_cursor=运行至指定步骤）。
        target_step_id: run_to_cursor 必填的目标步骤 id；其余模式忽略。
    """

    mode: Literal["over", "into", "out", "run_to_cursor"]
    target_step_id: str | None = None

    @model_validator(mode="after")
    def _require_target_for_run_to_cursor(self) -> "StepControlRequest":
        if self.mode == "run_to_cursor" and not (self.target_step_id or "").strip():
            raise ValueError("target_step_id is required when mode is run_to_cursor")
        return self


class StepControlResponse(BaseModel):
    """Response for POST /api/v1/executions/{run_id}/step-control.

    Attributes:
        ok: 指令受理成功标志（NATS 中断时仍为 true，与控制面约定一致）。
        run_id: The execution run identifier.
        mode: 受理的步进模式。
        target_step_id: run_to_cursor 的目标步骤 id（其余模式为 null）。
    """

    ok: bool = True
    run_id: str
    mode: str
    target_step_id: str | None = None


class ManualFaultRequest(BaseModel):
    """Request body for POST /api/v1/executions/{run_id}/manual-fault.

    手动故障注入面板（T38，v41-gap-analysis #38）：不等待 DSL 规则触发，
    由操作员在 SimulationConsole 直接向运行中的仿真注入故障。scope 决定
    §7.7.1 注入层（link→network / instrument→instrument / step→scheduler /
    scheduler→scheduler / protocol→protocol），fault_type 合法集合按 scope
    在端点内校验（越界 422）。

    Attributes:
        scope: 注入目标域（link/instrument/step/scheduler/protocol）。
        target_id: 目标 ID（链路/仪器/步骤 ID；scheduler 域可为 "*"）。
        fault_type: 故障类型（须属于该 scope 的允许集合）。
        params: 可选附加参数（如 value_override 的 value、noise 的幅度）。
    """

    scope: Literal["link", "instrument", "step", "scheduler", "protocol"]
    target_id: str = Field(..., min_length=1)
    fault_type: str = Field(..., min_length=1)
    params: dict[str, Any] | None = None


class ManualFaultResponse(BaseModel):
    """Response for POST /api/v1/executions/{run_id}/manual-fault.

    Attributes:
        ok: 注入受理成功标志。
        run_id: The execution run identifier.
        scope: 注入目标域。
        layer: 映射出的 §7.7.1 注入层。
        target_id: 目标 ID。
        fault_type: 故障类型。
        fault_id: 生成的故障规则 ID。
    """

    ok: bool = True
    run_id: str
    scope: str
    layer: str
    target_id: str
    fault_type: str
    fault_id: str


class BreakpointCreateRequest(BaseModel):
    """Request body for POST /api/v1/executions/{run_id}/breakpoints (T39).

    §8.4 typed breakpoint: kind selects the match semantics, target is the
    match key and condition is a simpleeval-subset expression evaluated
    SERVER-SIDE only (never client-side). ``condition`` is non-empty ONLY
    for the ``condition`` kind — enforced in BreakpointRegistry validation.

    Attributes:
        kind: 断点类型（step / instrument_call / variable_change / condition）。
        target: 匹配目标（步骤 ID / resource.method / scope.key / "*"）。
        condition: 条件表达式（仅 condition 类型允许，创建时做语法校验）。
    """

    kind: Literal["step", "instrument_call", "variable_change", "condition"]
    target: str = Field(..., min_length=1)
    condition: str | None = None


class BreakpointResponse(BaseModel):
    """Response for a single typed breakpoint (T39)."""

    ok: bool = True
    id: str
    run_id: str
    kind: str
    target: str
    condition: str | None = None
    enabled: bool = True


class BreakpointListResponse(BaseModel):
    """Response for GET /api/v1/executions/{run_id}/breakpoints (T39)."""

    items: list[BreakpointResponse] = Field(default_factory=list)
    total: int = 0


class BreakpointDeleteResponse(BaseModel):
    """Idempotent DELETE response for typed breakpoints (T39).

    Attributes:
        ok: 请求受理成功标志（幂等，重复删除仍为 true）。
        removed: 是否实际移除了断点（首次 true，重复 false）。
    """

    ok: bool = True
    removed: bool


class SimulationRequest(BaseModel):
    """Request body for POST /api/v1/executions/{run_id}/simulate.

    Attributes:
        tier: 仿真层级（driver/dry_run/full，§7 三层仿真）。
        noise_model: 噪声模型（仅 full 层级有意义，§7.3）。
        noise_sigma: 高斯噪声 sigma。
        drift_rate: 漂移速率（单位/秒）。
        bias: 恒定偏差。
        seed: 随机种子（可复现）。
        fault_config: 故障注入规则列表（§7.7.2 fault_injection 段，传给
            FullChainSimulator.fault_config）。
    """

    tier: Literal["driver", "dry_run", "full"] = "driver"
    noise_model: Literal["GAUSSIAN", "GAUSSIAN_DRIFT", "GAUSSIAN_BIAS", "FULL"] = "GAUSSIAN"
    noise_sigma: float = 0.001
    drift_rate: float = 0.0
    bias: float = 0.0
    seed: int | None = 42
    fault_config: list[dict[str, Any]] | None = None


class SimulationResultEvent(BaseModel):
    """A single simulated measurement/decision event.

    Attributes:
        step_id: 步骤 ID。
        timestamp: 事件时间戳（monotonic）。
        event_type: 事件类型（measurement/decision/fault）。
        data: 事件数据。
    """

    step_id: str
    timestamp: float
    event_type: str
    data: dict[str, Any]


class SimulationResponse(BaseModel):
    """Response from POST /api/v1/executions/{run_id}/simulate.

    Attributes:
        session_id: 仿真会话 ID（= run_id）。
        tier: 执行的仿真层级。
        status: 仿真结果状态（passed/failed）。
        events: 仿真事件列表（决策 + 测量）。
        duration_seconds: 仿真总耗时。
        statistics: 汇总统计（通过/失败/跳过/仪器统计）。
    """

    session_id: str
    tier: str
    status: str
    events: list[SimulationResultEvent]
    duration_seconds: float
    statistics: dict[str, Any]
