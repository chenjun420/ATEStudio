/**
 * diffView — ExecutionDiff 摘要 → 分节行模型（T37，v41-gap-analysis #37）。
 *
 * 纯函数层：把后端 `GET /executions/{run_id}/diff` 返回的
 * ExecutionDiff.compare() 摘要（schema 见
 * src/ate_platform/simulation/diff.py 模块 docstring）映射为 5 个固定分节
 * （steps / measurements / timing / resources / variables）的表格行模型，
 * 供 ExecutionDiffPanel.vue 直接渲染。零 DOM、零框架依赖。
 *
 * 行状态约定：每行带 `status: 'match' | 'violation'`——本视图只展示差异，
 * 因此所有生成的行均为 violation（绿色 match 态由"空行 + 总徽标"表达）。
 */

/** 后端 ExecutionDiff.compare() 摘要（透传 schema）。 */
export interface DiffSummary {
  match: boolean
  meta: { events_a: number; events_b: number }
  steps: {
    added: string[]
    removed: string[]
    status_changed: Array<{ step_id: string; a: string; b: string }>
  }
  measurements: Array<{ key: string; a: number | string; b: number | string; delta: number | null }>
  timing: {
    total: { a_ms: number; b_ms: number; delta_ms: number } | null
    steps: Array<{ step_id: string; a_ms: number; b_ms: number; delta_ms: number }>
  }
  resources: Array<{ resource: string; method: string; a_count: number; b_count: number }>
  variables: {
    changed: Array<{ scope: string; key: string; old: unknown; new: unknown }>
  }
}

export type DiffSectionId = 'steps' | 'measurements' | 'timing' | 'resources' | 'variables'

export type DiffRowStatus = 'match' | 'violation'

/** 一行：cells 与所属 section 的 headers 一一对齐。 */
export interface DiffRow {
  key: string
  cells: string[]
  status: DiffRowStatus
}

export interface DiffSection {
  id: DiffSectionId
  title: string
  headers: string[]
  rows: DiffRow[]
  /** 超过 MAX_ROWS 被截断时为 true（面板据此提示，避免大 diff 卡死渲染）。 */
  truncated: boolean
}

/** 单节最大渲染行数——超出截断并提示（计划约束：>500 行不得阻塞渲染）。 */
export const MAX_ROWS = 500

const DASH = '—'

/** 任意值 → 展示字符串；null/undefined 显示占位符。 */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return DASH
  if (typeof value === 'number') return String(Number(value.toFixed(6)))
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

/** 数值差 → 带符号展示；null（非数值对）显示占位符。 */
export function formatDelta(delta: number | null): string {
  if (delta === null || delta === undefined) return DASH
  const rounded = Number(delta.toFixed(4))
  return rounded > 0 ? `+${rounded}` : String(rounded)
}

function truncate(rows: DiffRow[]): { rows: DiffRow[]; truncated: boolean } {
  return { rows: rows.slice(0, MAX_ROWS), truncated: rows.length > MAX_ROWS }
}

/** steps 节：新增 / 移除 / 状态变更 三类差异行。 */
export function buildStepsSection(summary: DiffSummary): DiffSection {
  const rows: DiffRow[] = []
  for (const sid of summary.steps.added) {
    rows.push({ key: `added:${sid}`, cells: [sid, DASH, DASH, '新增'], status: 'violation' })
  }
  for (const sid of summary.steps.removed) {
    rows.push({ key: `removed:${sid}`, cells: [sid, DASH, DASH, '移除'], status: 'violation' })
  }
  for (const ch of summary.steps.status_changed) {
    rows.push({
      key: `status:${ch.step_id}`,
      cells: [ch.step_id, ch.a, ch.b, '状态变更'],
      status: 'violation',
    })
  }
  return { id: 'steps', title: '步骤', headers: ['步骤', '基线状态', '候选状态', '变化'], ...truncate(rows) }
}

/** measurements 节：仅容差外违规对。 */
export function buildMeasurementsSection(summary: DiffSummary): DiffSection {
  const rows: DiffRow[] = summary.measurements.map((m) => ({
    key: `meas:${m.key}`,
    cells: [m.key, formatValue(m.a), formatValue(m.b), formatDelta(m.delta)],
    status: 'violation',
  }))
  return { id: 'measurements', title: '测量', headers: ['测量项', '基线 A', '候选 B', 'Δ'], ...truncate(rows) }
}

/** timing 节：总时长（可空）+ 有非零 Δ 的公共步骤。 */
export function buildTimingSection(summary: DiffSummary): DiffSection {
  const rows: DiffRow[] = []
  const total = summary.timing.total
  if (total) {
    rows.push({
      key: 'timing:total',
      cells: ['总时长', String(Number(total.a_ms.toFixed(3))), String(Number(total.b_ms.toFixed(3))), formatDelta(total.delta_ms)],
      status: 'violation',
    })
  }
  for (const s of summary.timing.steps) {
    rows.push({
      key: `timing:${s.step_id}`,
      cells: [s.step_id, String(Number(s.a_ms.toFixed(3))), String(Number(s.b_ms.toFixed(3))), formatDelta(s.delta_ms)],
      status: 'violation',
    })
  }
  return { id: 'timing', title: '耗时', headers: ['步骤', 'A (ms)', 'B (ms)', 'Δ (ms)'], ...truncate(rows) }
}

/** resources 节：(resource, method) 调用次数差异。 */
export function buildResourcesSection(summary: DiffSummary): DiffSection {
  const rows: DiffRow[] = summary.resources.map((r) => ({
    key: `res:${r.resource}.${r.method}`,
    cells: [r.resource, r.method, String(r.a_count), String(r.b_count)],
    status: 'violation',
  }))
  return { id: 'resources', title: '资源调用', headers: ['资源', '方法', 'A 次数', 'B 次数'], ...truncate(rows) }
}

/** variables 节：(scope, key) 终值折叠差异。 */
export function buildVariablesSection(summary: DiffSummary): DiffSection {
  const rows: DiffRow[] = summary.variables.changed.map((v) => ({
    key: `var:${v.scope}.${v.key}`,
    cells: [v.scope, v.key, formatValue(v.old), formatValue(v.new)],
    status: 'violation',
  }))
  return { id: 'variables', title: '变量', headers: ['作用域', '键', '旧值', '新值'], ...truncate(rows) }
}

/**
 * 摘要 → 全部 5 节（固定顺序）。完全一致的运行各节 rows 为空数组、
 * truncated 为 false —— 面板以"无差异"空态呈现全绿结果。
 */
export function buildDiffSections(summary: DiffSummary): DiffSection[] {
  return [
    buildStepsSection(summary),
    buildMeasurementsSection(summary),
    buildTimingSection(summary),
    buildResourcesSection(summary),
    buildVariablesSection(summary),
  ]
}
