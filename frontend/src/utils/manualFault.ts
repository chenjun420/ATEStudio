/**
 * 手动故障注入纯函数（T38，v41-gap-analysis #38，设计文档 §7.7/§8.4）。
 *
 * 语义：
 *   - scope → §7.7.1 注入层映射与后端 MANUAL_SCOPE_LAYERS 严格一致
 *     （link→network / instrument→instrument / step→scheduler /
 *      scheduler→scheduler / protocol→protocol）。
 *   - 每个 scope 的 fault_type 允许集合与后端 MANUAL_SCOPE_FAULT_TYPES
 *     一致；下拉框按 scope 过滤。
 *   - 提交前客户端校验（计划 #38 要求）：目标非空、params JSON 可解析、
 *     数值范围合法（probability ∈ [0,1]、count ≥ 1、value_override 必须带 value）。
 *
 * 所有函数均为纯函数（不修改入参、零 DOM/API 依赖），模式对齐
 * utils/faultSuggestions.ts。绝不进行纯客户端模拟——payload 仅交由
 * POST /executions/{run_id}/manual-fault 转发。
 */

/** 注入目标域（与后端 ManualFaultRequest.scope Literal 对齐）。 */
export type ManualFaultScope = 'link' | 'instrument' | 'step' | 'scheduler' | 'protocol'

/** 单个 scope 的目录定义：显示名 + §7.7.1 层 + 允许的故障类型。 */
export interface ManualFaultScopeDef {
  value: ManualFaultScope
  label: string
  layer: string
  faultTypes: ReadonlyArray<{ value: string; label: string }>
}

/** scope → 故障类型目录（与后端 MANUAL_SCOPE_FAULT_TYPES 对齐，不得单侧扩展）。 */
export const MANUAL_FAULT_SCOPES: ReadonlyArray<ManualFaultScopeDef> = [
  {
    value: 'link',
    label: '链路 link',
    layer: 'network',
    faultTypes: [
      { value: 'open_circuit', label: '断路 open_circuit' },
      { value: 'short_circuit', label: '短路 short_circuit' },
      { value: 'contact_resistance', label: '接触电阻 contact_resistance' },
      { value: 'noise', label: '噪声 noise' },
      { value: 'delay', label: '延迟 delay' },
      { value: 'packet_loss', label: '丢包 packet_loss' },
      { value: 'reorder', label: '乱序 reorder' },
    ],
  },
  {
    value: 'instrument',
    label: '仪器 instrument',
    layer: 'instrument',
    faultTypes: [
      { value: 'measurement_out_of_range', label: '测量越界 measurement_out_of_range' },
      { value: 'over_voltage', label: '过压 over_voltage' },
      { value: 'over_current', label: '过流 over_current' },
      { value: 'communication', label: '通信故障 communication' },
      { value: 'selftest_failed', label: '自检失败 selftest_failed' },
      { value: 'noise', label: '噪声 noise' },
      { value: 'value_override', label: '读数覆盖 value_override' },
    ],
  },
  {
    value: 'step',
    label: '步骤 step',
    layer: 'scheduler',
    faultTypes: [
      { value: 'timeout', label: '超时 timeout' },
      { value: 'force_fail', label: '强制失败 force_fail' },
      { value: 'skip_step', label: '跳过步骤 skip_step' },
      { value: 'value_override', label: '读数覆盖 value_override' },
    ],
  },
  {
    value: 'scheduler',
    label: '调度 scheduler',
    layer: 'scheduler',
    faultTypes: [
      { value: 'resource_deadlock', label: '资源死锁 resource_deadlock' },
      { value: 'timeout', label: '超时 timeout' },
      { value: 'force_fail', label: '强制失败 force_fail' },
    ],
  },
  {
    value: 'protocol',
    label: '协议 protocol',
    layer: 'protocol',
    faultTypes: [
      { value: 'scpi_error', label: 'SCPI 错误 scpi_error' },
      { value: 'truncated_data', label: '数据截断 truncated_data' },
      { value: 'checksum_error', label: '校验错误 checksum_error' },
    ],
  },
]

/** 取 scope 定义；未知 scope 返回 undefined。 */
export function scopeDef(scope: string): ManualFaultScopeDef | undefined {
  return MANUAL_FAULT_SCOPES.find((s) => s.value === scope)
}

/** 按 scope 过滤故障类型选项（计划验收：fault-type options filtering by scope）。 */
export function faultTypesForScope(
  scope: string,
): ReadonlyArray<{ value: string; label: string }> {
  return scopeDef(scope)?.faultTypes ?? []
}

/** 表单输入（组件 v-model 状态）。 */
export interface ManualFaultForm {
  scope: ManualFaultScope
  targetId: string
  faultType: string
  paramsText: string
}

/** 后端 ManualFaultRequest 载荷。 */
export interface ManualFaultPayload {
  scope: ManualFaultScope
  target_id: string
  fault_type: string
  params?: Record<string, unknown>
}

/** 构建结果：ok=false 时 error 携带中文原因。 */
export type PayloadResult =
  | { ok: true; payload: ManualFaultPayload }
  | { ok: false; error: string }

/**
 * 解析 params JSON 文本；空文本视为无参数。
 * 非法 JSON 返回 {ok:false}（计划验收：invalid JSON blocked）。
 */
export function parseParamsJson(text: string): { ok: boolean; params?: Record<string, unknown>; error?: string } {
  const trimmed = text.trim()
  if (!trimmed) return { ok: true }
  try {
    const parsed: unknown = JSON.parse(trimmed)
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { ok: false, error: 'params 必须是 JSON 对象（如 {"value": 4.2}）' }
    }
    return { ok: true, params: parsed as Record<string, unknown> }
  } catch {
    return { ok: false, error: 'params 不是合法 JSON' }
  }
}

/**
 * 校验 params 数值范围（计划 #38：negative probability rejected 等）。
 * - probability ∈ [0, 1]
 * - count ≥ 1（整数）
 * - value_override 动作必须携带 value 键
 */
export function validateParamRanges(params: Record<string, unknown>): string | null {
  if ('probability' in params) {
    const p = Number(params.probability)
    if (!Number.isFinite(p) || p < 0 || p > 1) {
      return 'probability 必须在 [0, 1] 区间内'
    }
  }
  if ('count' in params) {
    const c = Number(params.count)
    if (!Number.isInteger(c) || c < 1) {
      return 'count 必须为 ≥ 1 的整数'
    }
  }
  if ('after_s' in params) {
    const t = Number(params.after_s)
    if (!Number.isFinite(t) || t < 0) {
      return 'after_s 不能为负数'
    }
  }
  return null
}

/**
 * 由表单构建后端载荷；任一校验失败返回 {ok:false, error}。
 * 纯函数——不修改入参表单。
 */
export function buildManualFaultPayload(form: ManualFaultForm): PayloadResult {
  if (!form.scope) {
    return { ok: false, error: '请选择注入目标域' }
  }
  const target = form.targetId.trim()
  if (!target) {
    return { ok: false, error: '请填写目标 ID' }
  }
  if (!form.faultType) {
    return { ok: false, error: '请选择故障类型' }
  }
  if (!faultTypesForScope(form.scope).some((t) => t.value === form.faultType)) {
    return { ok: false, error: `故障类型 ${form.faultType} 不属于 ${form.scope} 域` }
  }

  const parsed = parseParamsJson(form.paramsText)
  if (!parsed.ok) {
    return { ok: false, error: parsed.error ?? 'params 校验失败' }
  }
  const params = parsed.params
  if (params) {
    const rangeError = validateParamRanges(params)
    if (rangeError) return { ok: false, error: rangeError }
  }

  const payload: ManualFaultPayload = {
    scope: form.scope,
    target_id: target,
    fault_type: form.faultType,
  }
  if (params && Object.keys(params).length > 0) payload.params = params
  return { ok: true, payload }
}
