/**
 * 继电器触点编辑纯函数（T27，设计文档 §8.3.2 Relay 实体）。
 *
 * 数据形状与 src/shared/fixture_topology.py::Relay 对齐：
 *   { id, type: spst|spdt|dpdt|matrix, control_signal, contacts: dict[str, str|None], state: open|closed }
 *
 * contacts 语义：触点名 → 绑定的夹具端子 id（null = 未绑定）。
 * 触点布局（§8.3.2）：
 *   - spst: common / no
 *   - spdt: common / no / nc
 *   - dpdt: 双刀 1c/1no/1nc + 2c/2no/2nc
 *   - matrix: r{i}c{j} 交叉点网格（尺寸由 contacts 键推导——后端
 *     extra="forbid" 不允许额外字段，故行列信息编码在键名中）
 *
 * 所有变换均为纯函数（返回新数组，不修改入参），便于测试与
 * Vue 响应式回写（父视图将结果赋回 topology_data.fixtures[].relays）。
 */

/** 继电器类型（§8.3.2 RelayType）。 */
export type RelayContactType = 'spst' | 'spdt' | 'dpdt' | 'matrix'

export const RELAY_TYPES: readonly RelayContactType[] = ['spst', 'spdt', 'dpdt', 'matrix'] as const

export function isRelayType(v: unknown): v is RelayContactType {
  return typeof v === 'string' && (RELAY_TYPES as readonly string[]).includes(v)
}

/**
 * 与后端 Relay 模型对齐的最小结构（frontend/src/api/fixtures.ts::Relay 的结构超集，
 * 使 Relay[] 可直接作为 RelayLike[] 使用）。
 */
export interface RelayLike {
  id: string
  type?: string
  control_signal?: string | null
  contacts?: Record<string, unknown>
  state?: string
}

/** 读取触点绑定值：非空字符串视为端子 id，其余（null/undefined）视为未绑定。 */
export function boundTerminal(relay: RelayLike, contact: string): string | null {
  const v = relay.contacts?.[contact]
  return typeof v === 'string' && v.length > 0 ? v : null
}

/** 固定类型（非 matrix）的触点布局。 */
const FIXED_CONTACT_LAYOUTS: Record<Exclude<RelayContactType, 'matrix'>, string[]> = {
  spst: ['common', 'no'],
  spdt: ['common', 'no', 'nc'],
  dpdt: ['1c', '1no', '1nc', '2c', '2no', '2nc'],
}

export const DEFAULT_MATRIX_ROWS = 2
export const DEFAULT_MATRIX_COLS = 2

/** matrix 交叉点键名：r{i}c{j}（1-based）。 */
export function matrixCellName(row: number, col: number): string {
  return `r${row}c${col}`
}

/** 从 matrix contacts 键推导网格尺寸（无有效键时返回默认 2×2）。 */
export function matrixSize(
  contacts: Record<string, unknown> | null | undefined,
): { rows: number; cols: number } {
  let rows = 0
  let cols = 0
  for (const key of Object.keys(contacts ?? {})) {
    const m = /^r(\d+)c(\d+)$/.exec(key)
    if (!m) continue
    rows = Math.max(rows, Number(m[1]))
    cols = Math.max(cols, Number(m[2]))
  }
  if (rows === 0 || cols === 0) return { rows: DEFAULT_MATRIX_ROWS, cols: DEFAULT_MATRIX_COLS }
  return { rows, cols }
}

/**
 * 指定类型的触点名列表（matrix 行优先排序）。
 */
export function contactNamesForType(
  type: string,
  contacts?: Record<string, unknown> | null,
): string[] {
  if (type === 'matrix') {
    const { rows, cols } = matrixSize(contacts)
    const names: string[] = []
    for (let r = 1; r <= rows; r++) {
      for (let c = 1; c <= cols; c++) {
        names.push(matrixCellName(r, c))
      }
    }
    return names
  }
  if (type === 'spst' || type === 'spdt' || type === 'dpdt') {
    return [...FIXED_CONTACT_LAYOUTS[type]]
  }
  // 未知类型按 spst 处理（防御）
  return [...FIXED_CONTACT_LAYOUTS.spst]
}

/** 创建带合法默认值的继电器（control_signal 必填：后端 min_length=1）。 */
export function createRelay(index: number, type: RelayContactType): RelayLike {
  const contacts: Record<string, unknown> = {}
  for (const name of contactNamesForType(type)) contacts[name] = null
  return {
    id: `RLY_${index + 1}`,
    type,
    control_signal: `CTRL_${index + 1}`,
    contacts,
    state: 'open',
  }
}

// ─── 端子占用校验 ────────────────────────────────────────────────────────────

/** 端子占用者（哪个继电器的哪个触点绑定了该端子）。 */
export interface Occupant {
  relayId: string
  contact: string
}

/**
 * 在全部继电器中查找绑定到 terminalId 的触点。
 * @param exclude 排除自身触点（重绑场景）。
 */
export function findOccupant(
  relays: RelayLike[],
  terminalId: string,
  exclude?: Occupant,
): Occupant | null {
  for (const relay of relays) {
    for (const contact of Object.keys(relay.contacts ?? {})) {
      if (boundTerminal(relay, contact) !== terminalId) continue
      if (exclude && exclude.relayId === relay.id && exclude.contact === contact) continue
      return { relayId: relay.id, contact }
    }
  }
  return null
}

export type BindCheckResult = { ok: true } | { ok: false; error: string }

/**
 * 校验「触点 → 端子」绑定是否合法：
 * 一个夹具端子同一时刻只能被一个触点占用（非法链路阻断）。
 */
export function checkBind(
  relays: RelayLike[],
  relayId: string,
  contact: string,
  terminalId: string,
): BindCheckResult {
  if (!terminalId) return { ok: false, error: '请先选择夹具端子' }
  const occupant = findOccupant(relays, terminalId, { relayId, contact })
  if (occupant) {
    return {
      ok: false,
      error: `端子 ${terminalId} 已被继电器 ${occupant.relayId}.${occupant.contact} 占用`,
    }
  }
  return { ok: true }
}

export type BindApplyResult =
  | { ok: true; relays: RelayLike[] }
  | { ok: false; error: string }

/** 校验并应用绑定（纯变换；失败时返回错误且不产生新数组）。 */
export function applyBind(
  relays: RelayLike[],
  relayId: string,
  contact: string,
  terminalId: string,
): BindApplyResult {
  const check = checkBind(relays, relayId, contact, terminalId)
  if (!check.ok) return check
  return {
    ok: true,
    relays: applyUpdateRelay(relays, relayId, {
      contacts: { ...(relays.find((r) => r.id === relayId)?.contacts ?? {}), [contact]: terminalId },
    }),
  }
}

/** 解除触点绑定（置 null）。 */
export function applyUnbind(relays: RelayLike[], relayId: string, contact: string): RelayLike[] {
  return applyUpdateRelay(relays, relayId, {
    contacts: { ...(relays.find((r) => r.id === relayId)?.contacts ?? {}), [contact]: null },
  })
}

/** 切换继电器开关状态 open ↔ closed。 */
export function applyToggleState(relays: RelayLike[], relayId: string): RelayLike[] {
  return relays.map((r) =>
    r.id === relayId ? { ...r, state: r.state === 'closed' ? ('open' as const) : ('closed' as const) } : r,
  )
}

/** 局部更新继电器字段（不可变）。 */
export function applyUpdateRelay(
  relays: RelayLike[],
  relayId: string,
  patch: Partial<Omit<RelayLike, 'id'>>,
): RelayLike[] {
  return relays.map((r) => (r.id === relayId ? { ...r, ...patch } : r))
}

/**
 * 调整 matrix 网格尺寸：保留界内既有绑定，界外丢弃，新增格补 null。
 */
export function resizeMatrixContacts(
  contacts: Record<string, unknown> | null | undefined,
  rows: number,
  cols: number,
): Record<string, unknown> {
  const next: Record<string, unknown> = {}
  for (let r = 1; r <= rows; r++) {
    for (let c = 1; c <= cols; c++) {
      const name = matrixCellName(r, c)
      const existing = contacts?.[name]
      next[name] = typeof existing === 'string' && existing.length > 0 ? existing : null
    }
  }
  return next
}
