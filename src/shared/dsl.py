"""YAML DSL type definitions for ATE Platform.

This module defines data structures for YAML DSL:
- LoopType: Enum for loop types (FOR, WHILE, FOREACH)
- ExecutionMode: Enum for execution modes (SERIAL, PARALLEL)
- StepType: Enum for step types (SCRIPT, LOOP, CALL)
- YamlStep: Represents a single step in the execution plan
- YamlLoop: Represents a loop construct in the execution plan
- YamlPlan: Represents the complete execution plan from YAML
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LoopType(Enum):
    """Enumeration of loop types.

    Attributes:
        FOR: Counted loop with explicit range
        WHILE: Conditional loop with break condition
        FOREACH: Iterate over a collection
    """

    FOR = "FOR"
    WHILE = "WHILE"
    FOREACH = "FOREACH"


class ExecutionMode(Enum):
    """Enumeration of execution modes for steps within a loop.

    Attributes:
        SERIAL: Steps execute one after another
        PARALLEL: Steps execute concurrently
    """

    SERIAL = "SERIAL"
    PARALLEL = "PARALLEL"


class StepType(Enum):
    """Enumeration of step types (DSL v3.2, 设计文档 §6.5).

    枚举值使用小写字符串以匹配 YAML 中的 ``type`` 字段（如
    ``type: fixture_control``）。旧式无 ``type`` 字段的脚本步骤按
    SCRIPT 处理，保持 v3.0 向后兼容。

    Attributes:
        SCRIPT: 旧式脚本步骤（无 type 字段，或 type: script）
        ACTION: 脚本动作步骤（type: action，含 script）
        LOOP: 循环容器（type: loop，编译期展开为 N 次迭代）
        BRANCH: 分支容器（type: branch，运行时按条件选择路径）
        BARRIER: 多 UUT 同步屏障（type: barrier，barrier_name 分组）
        FIXTURE_CONTROL: 夹具控制步骤（type: fixture_control，action+fixture_id）
        CALL: 调用子序列/子计划（type: call）
        SUBSEQUENCE: 内联子序列容器（type: subsequence）
    """

    SCRIPT = "script"
    ACTION = "action"
    LOOP = "loop"
    BRANCH = "branch"
    BARRIER = "barrier"
    FIXTURE_CONTROL = "fixture_control"
    CALL = "call"
    SUBSEQUENCE = "subsequence"


@dataclass
class YamlStep:
    """Represents a single step in the execution plan.

    该数据类同时承载 DSL v3.0 的脚本步骤与 v3.2 的
    barrier / fixture_control / branch / call 等非脚本步骤
    （设计文档 §6.5.4）。非脚本步骤通过 ``type`` 区分，脚本字段
    （``script``/``params``）仅对 SCRIPT/ACTION 步骤有意义。

    Attributes:
        id: Unique identifier for the step
        type: v3.2 步骤类型（SCRIPT/ACTION/BARRIER/FIXTURE_CONTROL/...），
            缺省按 SCRIPT 处理（向后兼容）
        script: Path or name of the script to execute
        params: Parameters passed to the script
        preconditions: List of step IDs that must complete before this step
        depends_on: v3.2 依赖声明（与 preconditions 语义一致，供编译器接线）
        resources: Resource requirements for this step
        timeout: Maximum execution time in seconds
        retry: Number of retry attempts on failure
        on_fail: Action to take on failure (e.g. 'stop', 'skip', 'ignore')
        on_failure: v3.2 on_failure 别名（abort/continue/skip），优先于 on_fail
        uut_affinity: 目标 UUT（any 或指定 UUT ID）
        barrier_name: barrier 步骤的屏障名（type == BARRIER 时必填）
        action: fixture_control 步骤的动作（clamp/release/set_route/read_sensor）
        fixture_id: fixture_control 步骤的目标夹具 ID
        export_outputs: Whether to export step outputs to plan-level scope
        skip_if: Expression that, if True, causes this step to be skipped
        skip_reason: Human-readable reason logged when step is skipped
    """

    id: str
    type: StepType | None = None
    script: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    preconditions: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)
    timeout: int = 60
    retry: int = 0
    on_fail: str | None = None
    on_failure: str | None = None
    uut_affinity: str | None = None
    barrier_name: str | None = None
    action: str | None = None
    fixture_id: str | None = None
    export_outputs: bool = False
    skip_if: str | None = None
    skip_reason: str | None = None


@dataclass
class YamlLoop:
    """Represents a loop construct in the execution plan.

    A loop contains nested steps (and optionally other loops) that are
    executed repeatedly based on the loop type and conditions.

    Attributes:
        id: Unique identifier for the loop
        loop_type: Type of loop (FOR, WHILE, FOREACH)
        steps: Nested steps or loops to execute in each iteration
        count: Number of iterations (for FOR loops)
        condition: Break condition expression (for WHILE loops)
        collection: Variable name holding the collection (for FOREACH loops)
        iterator_var: Variable name for the current item (for FOREACH loops)
        execution_mode: Whether nested steps run serially or in parallel
        max_iterations: Safety limit on iterations (prevents infinite loops)
        skip_if: Expression that, if True, causes this loop to be skipped
        skip_reason: Human-readable reason logged when loop is skipped
        depends_on: v3.2 依赖声明（与步骤级 depends_on 一致）
    """

    id: str
    loop_type: LoopType
    steps: list[YamlStep | YamlLoop] = field(default_factory=list)
    count: int | None = None
    condition: str | None = None
    collection: str | None = None
    iterator_var: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.SERIAL
    max_iterations: int = 1000
    skip_if: str | None = None
    skip_reason: str | None = None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class YamlPlan:
    """Represents the complete execution plan from YAML.

    Attributes:
        name: Name of the test plan
        version: Version of the test plan
        scope: Scope variables as a dictionary (supports both dict and string for backward compat)
        max_concurrency: Maximum number of concurrent steps
        steps: List of steps and/or loops in the plan
    """

    name: str
    version: str
    scope: dict[str, Any] = field(default_factory=dict)
    max_concurrency: int = 1
    steps: list[YamlStep | YamlLoop] = field(default_factory=list)
