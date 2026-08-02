"""Operator checkpoint schema for human-in-the-loop test flow pauses.

Defines the ``operator_checkpoint`` field attachable to a sequence step
in YAML test plans. When the executor reaches a step declaring an
operator checkpoint, it pauses execution and emits a pending checkpoint
event; the operator UI presents a modal dialog tailored to the
checkpoint ``type`` and submits a response that resumes execution.

检查点类型（OperatorInteractionType）：
- scan: 操作员扫码（条码/二维码），el-input autofocus
- manual_input: 操作员手动输入文本
- visual_check: 操作员目视检查后按 pass/fail
- confirm: 操作员确认（仅一个确认按钮）

All models use ``extra='forbid'`` for strict validation -- unknown YAML
keys are rejected rather than silently ignored, preventing configuration
drift. Mirrors the ProductConfig schema style (Pydantic v2).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "OperatorInteractionType",
    "OperatorCheckpoint",
]


class OperatorInteractionType(StrEnum):
    """Enumeration of operator interaction types.

    继承 ``str`` 以便 YAML 序列化/JSON 传输时直接使用字符串值，
    且 ``Enum`` 成员可直接与字符串比较。

    Attributes:
        SCAN: 操作员扫码 -- el-input autofocus, 期望条码/二维码字符串
        MANUAL_INPUT: 操作员手动输入文本 -- el-input
        VISUAL_CHECK: 操作员目视检查后按 pass/fail -- el-button pass/fail
        CONFIRM: 操作员确认 -- el-button confirm
    """

    SCAN = "scan"
    MANUAL_INPUT = "manual_input"
    VISUAL_CHECK = "visual_check"
    CONFIRM = "confirm"


class OperatorCheckpoint(BaseModel):
    """Operator checkpoint definition attachable to a sequence step.

    序列步骤检查点定义 -- 当执行器到达声明了 ``operator_checkpoint``
    的步骤时，暂停执行并发布待处理检查点事件；操作员 UI 展示模态
    对话框（类型由 ``type`` 决定），操作员提交响应后恢复执行；
    超时未响应则该步骤以 TimeoutError 失败。

    Attributes:
        type: 交互类型（scan|manual_input|visual_check|confirm）
        prompt: 展示给操作员的提示文本
        timeout_sec: 超时秒数，超过则步骤以 TimeoutError 失败
        validation_regex: 可选正则表达式，用于校验操作员输入
            （scan/manual_input 类型）。匹配失败时 UI 提示重新输入，
            不提交响应。
    """

    model_config = ConfigDict(extra="forbid")

    type: OperatorInteractionType = Field(
        ...,
        description="交互类型: scan | manual_input | visual_check | confirm",
    )
    prompt: str = Field(
        ...,
        min_length=1,
        description="展示给操作员的提示文本",
    )
    timeout_sec: float = Field(
        ...,
        gt=0,
        description="超时秒数，超过则步骤以 TimeoutError 失败",
    )
    validation_regex: str | None = Field(
        default=None,
        description="可选正则表达式，用于校验操作员输入 (scan/manual_input)",
    )
