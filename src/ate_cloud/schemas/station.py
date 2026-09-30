"""厂区 / 工位 / 工位故障案例 的 API 契约。

关于「未验证」的三处约定
-----------------------
1. ``fix_verified`` 默认 False, 且**创建时省略它等于明确声明未验证**。诊断建议
   只能引用已验证的措施 —— 把谁也没验过的措施当成「这么修就好了」, 等于把未签字
   的判断当成已签字的, 与本项目「未批准的条件不得成为产测判据」同源。
2. 空 ``symptom`` 在这里被拒绝。数据库只保证 NOT NULL, 而空串不是 NULL: 一条
   没有观察记录的案例无法被任何检索命中, 却会安静地占一个位置, 让「本工位无
   历史」与「本工位从未出过故障」继续无法区分。
3. S/O/D 按 FMEA 惯例限定 1..10。``rpn`` 存的是**当初评估并签字的那个数**, 不
   重算 —— 重算若与之不符, 那个分歧是信息, 不该被静默覆盖; 所以这里只校验它是
   非负整数, 不校验它等于 S*O*D。

关于分页
--------
列表一律 ``skip`` / ``limit``, ``limit`` 上限 1000 —— 与既有 CRUD 一致。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Strict(BaseModel):
    """Reject unknown fields rather than dropping them silently.

    A dropped ``fix_verified`` would read as ``False`` — safe. A dropped
    ``plant_id`` would produce a row attached to nothing, which is the failure
    this whole layer exists to make impossible.
    """

    model_config = ConfigDict(extra="forbid")


def _require_text(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} 不能为空")
    return cleaned


# ── 厂区 ──────────────────────────────────────────────────────────────────


class PlantCreate(_Strict):
    code: str = Field(..., description="厂区编码, 全局唯一", max_length=64)
    name: str = Field(..., description="厂区名称", max_length=128)
    parent_id: str | None = Field(
        default=None,
        description="上级厂区。本期上下文选择器是两级, 但产线分组(总装/分装)"
        "在实践中几乎必然出现, 预留一列远低于日后改表",
    )

    _check_code = field_validator("code")(_require_text)
    _check_name = field_validator("name")(_require_text)


class PlantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    name: str
    parent_id: str | None
    is_active: bool
    station_count: int = Field(
        default=0, description="该厂区下的工位数 —— 上下文选择器需要它来决定能否删除"
    )
    created_at: datetime
    updated_at: datetime


class PlantListResponse(BaseModel):
    total: int
    items: list[PlantResponse]


# ── 工位 ──────────────────────────────────────────────────────────────────


class StationCreate(_Strict):
    plant_id: str = Field(..., description="所属厂区")
    code: str = Field(..., description="工位编码, 厂区内唯一", max_length=64)
    name: str = Field(..., description="工位名称", max_length=128)
    attributes: str | None = Field(
        default=None,
        description="自由属性(JSON 文本)。工位该带什么因产线而异, 固定 schema "
        "只会带来无尽迁移",
    )

    _check_code = field_validator("code")(_require_text)
    _check_name = field_validator("name")(_require_text)


class StationUpdate(_Strict):
    name: str | None = Field(default=None, max_length=128)
    attributes: str | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str | None) -> str | None:
        return None if v is None else _require_text(v, "name")


class StationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    plant_id: str
    plant_code: str | None = None
    plant_name: str | None = None
    code: str
    name: str
    attributes: str | None
    is_active: bool
    fault_case_count: int = 0
    created_at: datetime
    updated_at: datetime


class StationListResponse(BaseModel):
    total: int
    items: list[StationResponse]


# ── 工位故障案例 ──────────────────────────────────────────────────────────


class FaultCaseCreate(_Strict):
    station_id: str = Field(..., description="所属工位")
    symptom: str = Field(..., description="现象原文。将来检索匹配的就是这段文字, "
                                          "所以按写的原样存, 不归一化到词表")
    cause: str | None = None
    effect: str | None = None
    fix: str | None = Field(
        default=None, description="措施。未经验证时只能作为候选, 不得作为结论呈现"
    )
    fix_verified: bool = Field(
        default=False, description="是否已有人确认该措施有效。默认 False"
    )
    product_code: str | None = Field(
        default=None, description="型号。可空 —— 工装或工装类故障属于工位, 与在产型号无关"
    )
    severity: int | None = Field(default=None, ge=1, le=10)
    occurrence: int | None = Field(default=None, ge=1, le=10)
    detection: int | None = Field(default=None, ge=1, le=10)
    rpn: int | None = Field(
        default=None, ge=0, description="按当初评估的值存储, 不由 S*O*D 重算"
    )
    occurred_at: datetime | None = None

    @field_validator("symptom")
    @classmethod
    def _check_symptom(cls, v: str) -> str:
        return _require_text(v, "symptom")


class FaultCaseUpdate(_Strict):
    symptom: str | None = None
    cause: str | None = None
    effect: str | None = None
    fix: str | None = None
    fix_verified: bool | None = None
    product_code: str | None = None
    severity: int | None = Field(default=None, ge=1, le=10)
    occurrence: int | None = Field(default=None, ge=1, le=10)
    detection: int | None = Field(default=None, ge=1, le=10)
    rpn: int | None = Field(default=None, ge=0)

    @field_validator("symptom")
    @classmethod
    def _check_symptom(cls, v: str | None) -> str | None:
        return None if v is None else _require_text(v, "symptom")


class FaultCaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    station_id: str
    station_code: str | None = None
    station_name: str | None = None
    plant_id: str | None = None
    product_code: str | None
    symptom: str
    cause: str | None
    effect: str | None
    fix: str | None
    fix_verified: bool
    severity: int | None
    occurrence: int | None
    detection: int | None
    rpn: int | None
    source_diagnosis_id: str | None
    occurred_at: datetime
    created_at: datetime
    updated_at: datetime


class FaultCaseListResponse(BaseModel):
    total: int
    items: list[FaultCaseResponse]


class ReindexResponse(BaseModel):
    indexed: int = Field(..., description="成功写入向量库的案例数")
    skipped: int = Field(..., description="因缺 embedding 服务或索引不可用而跳过的数量")
    detail: str


__all__ = [
    "FaultCaseCreate",
    "FaultCaseListResponse",
    "FaultCaseResponse",
    "FaultCaseUpdate",
    "PlantCreate",
    "PlantListResponse",
    "PlantResponse",
    "ReindexResponse",
    "StationCreate",
    "StationListResponse",
    "StationResponse",
    "StationUpdate",
]
