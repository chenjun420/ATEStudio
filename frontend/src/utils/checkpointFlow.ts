/**
 * 操作员检查点流程纯函数（T42，v41-gap-analysis #42，设计文档 §10.5）。
 *
 * 状态机：pending → acked（单向；acked 项不可再变更）。
 *
 * 语义：
 *   - 待办检查点来自 GET /executions/{run_id}/checkpoint/pending 轮询，
 *     upsertPending 按 step_id 合并（同一 step 的新 pending 替换旧条目）。
 *   - sortCheckpoints 按 created_at 升序（最早创建的先处理，引导式流程）。
 *   - buildAckPayload 构建 POST /executions/{run_id}/checkpoint/ack 请求体；
 *     操作员姓名必填（签署留痕），note 可选（空串视为未填，省略键）。
 *   - canStartRun 实施检查点门控：存在未确认检查点时禁止开始/继续运行
 *     （服务端同样强制，UI 仅展示原因，绝不绕过）。
 *
 * 所有函数均为纯函数（不修改入参、零 DOM/API 依赖），模式对齐
 * utils/manualFault.ts。绝不进行纯客户端模拟——ack 仅交由后端端点转发。
 */

/** 检查点交互类型（与后端 OperatorInteractionType 对齐）。 */
export type CheckpointInteractionType = 'scan' | 'manual_input' | 'visual_check' | 'confirm'

/** 后端 OperatorCheckpoint 定义（shared/operator_checkpoint.py）。 */
export interface CheckpointDef {
  type: CheckpointInteractionType
  prompt: string
  timeout_sec: number
  validation_regex?: string | null
}

/** GET /executions/{run_id}/checkpoint/pending 响应。 */
export interface PendingCheckpointResponse {
  run_id: string
  pending: boolean
  step_id?: string | null
  checkpoint?: CheckpointDef | null
  created_at?: string | null
}

/** 检查点状态：pending（待确认）→ acked（已确认）。 */
export type CheckpointStatus = 'pending' | 'acked'

/** 操作员控制台中的单个检查点条目（含流程状态）。 */
export interface CheckpointItem {
  runId: string
  stepId: string
  checkpoint: CheckpointDef
  createdAt: string
  status: CheckpointStatus
  /** 已确认时的签署人。 */
  ackedBy?: string
  /** 已确认时间（ISO 字符串）。 */
  ackedAt?: string
  /** 已确认时的可选备注。 */
  note?: string
}

/** POST /executions/{run_id}/checkpoint/ack 请求体。 */
export interface AckPayload {
  step_id: string
  operator: string
  note?: string
}

/** 构建结果：ok=false 时 error 携带中文原因。 */
export type PayloadResult<T> = { ok: true; payload: T } | { ok: false; error: string }

/**
 * 将轮询到的待处理检查点合并进列表（按 stepId upsert）。
 * - payload.pending=false 或字段缺失 → 原列表原样返回（无变更）。
 * - 同 stepId 已存在 pending 条目 → 用新数据替换（保留位置）。
 * - 新 stepId → 追加到末尾。
 * 纯函数——不修改入参。
 */
export function upsertPending(
  items: readonly CheckpointItem[],
  payload: PendingCheckpointResponse,
): CheckpointItem[] {
  if (
    !payload.pending ||
    !payload.step_id ||
    !payload.checkpoint ||
    !payload.created_at
  ) {
    return [...items]
  }
  const next: CheckpointItem = {
    runId: payload.run_id,
    stepId: payload.step_id,
    checkpoint: payload.checkpoint,
    createdAt: payload.created_at,
    status: 'pending',
  }
  const idx = items.findIndex((it) => it.stepId === next.stepId)
  if (idx === -1) return [...items, next]
  const copy = [...items]
  copy[idx] = next
  return copy
}

/**
 * 按 created_at 升序排序（最早的检查点最先处理，引导式流程）。
 * created_at 缺失/非法的条目排在最后；同刻保持原有顺序（稳定排序）。
 */
export function sortCheckpoints(items: readonly CheckpointItem[]): CheckpointItem[] {
  const time = (it: CheckpointItem): number => {
    const t = Date.parse(it.createdAt)
    return Number.isNaN(t) ? Number.POSITIVE_INFINITY : t
  }
  return [...items].sort((a, b) => time(a) - time(b))
}

/** 分区为 { pending, completed }；completed 即 status==='acked' 的历史记录。 */
export function partitionCheckpoints(items: readonly CheckpointItem[]): {
  pending: CheckpointItem[]
  completed: CheckpointItem[]
} {
  const pending: CheckpointItem[] = []
  const completed: CheckpointItem[] = []
  for (const it of items) {
    if (it.status === 'acked') completed.push(it)
    else pending.push(it)
  }
  return { pending, completed }
}

/**
 * 状态机转移：pending → acked（纯函数，返回新对象）。
 * 已是 acked 的条目原样返回（不可逆转移，防止覆盖签署信息）。
 */
export function acknowledgeCheckpoint(
  item: CheckpointItem,
  operator: string,
  note?: string,
  at: string = new Date().toISOString(),
): CheckpointItem {
  if (item.status === 'acked') return item
  return {
    ...item,
    status: 'acked',
    ackedBy: operator,
    ackedAt: at,
    note: note || undefined,
  }
}

/**
 * 由检查点 + 表单输入构建 ack 请求体。
 * 校验：操作员姓名必填（trim 后非空）；stepId 必须存在。
 * note 为空串时省略键（后端 note 可选）。
 */
export function buildAckPayload(
  item: Pick<CheckpointItem, 'stepId'>,
  operatorInput: string,
  noteInput?: string,
): PayloadResult<AckPayload> {
  if (!item.stepId) {
    return { ok: false, error: '缺少检查点步骤 ID' }
  }
  const operator = operatorInput.trim()
  if (!operator) {
    return { ok: false, error: '请填写操作员姓名' }
  }
  const note = (noteInput ?? '').trim()
  const payload: AckPayload = { step_id: item.stepId, operator }
  if (note) payload.note = note
  return { ok: true, payload }
}

/**
 * 检查点门控：存在未确认检查点时禁止开始运行，返回原因。
 * （服务端同样强制——UI 仅展示原因，不提供绕过入口。）
 */
export function canStartRun(pendingItems: readonly CheckpointItem[]): {
  allowed: boolean
  reason?: string
} {
  if (pendingItems.length === 0) return { allowed: true }
  return {
    allowed: false,
    reason: `存在 ${pendingItems.length} 个未确认检查点，请先完成确认`,
  }
}

/**
 * DUT 更换提示：上一发运行已结束且出现了新的运行 → 提示更换被测件。
 */
export function needsDutSwap(
  previousRunId: string | null,
  currentRunId: string | null,
  hasCompletedRun: boolean,
): boolean {
  if (!previousRunId || !currentRunId) return false
  if (previousRunId === currentRunId) return false
  return hasCompletedRun
}

/** 运行终态判定（COMPLETED / FAILED / ABORTED）。 */
export function isTerminalStatus(status: string): boolean {
  return status === 'COMPLETED' || status === 'FAILED' || status === 'ABORTED'
}

/** 完成签名载荷（运行结束后操作员签收）。 */
export interface CompletionSignature {
  run_id: string
  operator: string
  signed_at: string
}

/**
 * 构建完成签名；操作员姓名必填（trim 后非空）。
 */
export function buildCompletionSignature(
  runId: string,
  operatorInput: string,
  at: string = new Date().toISOString(),
): PayloadResult<CompletionSignature> {
  if (!runId) {
    return { ok: false, error: '缺少运行 ID' }
  }
  const operator = operatorInput.trim()
  if (!operator) {
    return { ok: false, error: '请填写操作员姓名以完成签名' }
  }
  return { ok: true, payload: { run_id: runId, operator, signed_at: at } }
}

/**
 * 离线横幅判定：连续轮询失败达到阈值（默认 3 次）→ 显示离线提示。
 * 显示型离线即可（§10.5 offline note，display-only acceptable now）。
 */
export function shouldShowOfflineBanner(
  consecutiveFailures: number,
  threshold: number = 3,
): boolean {
  return consecutiveFailures >= threshold
}
