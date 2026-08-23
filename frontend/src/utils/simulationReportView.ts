/**
 * simulationReportView — 仿真报告响应 → 分节视图模型（T41，v41-gap-analysis #41）。
 *
 * 纯函数层：把后端 `GET /executions/{run_id}/simulation-report` 的组合报告
 * （coverage=T14 SimulationCoverage.report / contention=T13
 * ResourceContentionAnalyzer.analyze / faults=执行记录的故障）映射为三个
 * 固定分节的展示模型，供 SimulationReportPanel.vue 直接渲染。
 * 零 DOM、零框架依赖、零图表库——覆盖率条 = div 宽度百分比。
 */

// ─── 后端 schema（透传类型）─────────────────────────────────────────────────

/** T14 SimulationCoverage.report() 输出。 */
export interface CoverageReport {
  plan: { total_steps: number; total_branches: number; total_branch_arms: number; all_step_ids: string[] }
  step_coverage: {
    planned: number
    executed: number
    skipped: string[]
    unexecuted: string[]
    unknown_executed: string[]
    percent: number
  }
  branch_coverage: {
    branches: Record<
      string,
      { then_ids: string[]; else_ids: string[]; decisions: string[]; arms_covered: string[]; arms_total: number }
    >
    arms_total: number
    arms_covered: number
    percent: number
    both_sides_seen: string[]
  }
  by_source_step: Record<string, { planned: number; executed: number }>
  summary: { step_percent: number; branch_percent: number; quality: 'full' | 'partial' | 'empty' }
}

/** T13 单资源区间统计。 */
export interface ContentionIntervalStats {
  count: number
  total: number
  min: number
  max: number
  mean: number
  histogram: Array<{ bucket: string; count: number }>
}

/** T13 单资源竞争统计。 */
export interface ContentionResourceStats {
  acquire_count: number
  release_count: number
  contention_count: number
  max_concurrent_waiters: number
  wait: ContentionIntervalStats
  hold: ContentionIntervalStats
}

/** T13 死锁事件（等待环）。 */
export interface DeadlockAlert {
  detected_at_ts: number
  cycle_owners: string[]
  involved_resources: string[]
  edges: Array<{ waiter: string; waits_for: string; held_by: string }>
}

/** T13 ResourceContentionAnalyzer.analyze() 输出（gantt 行 end:null=未闭合）。 */
export interface ContentionReport {
  generated_from: { events: number; resources: number; owners: number }
  resources: Record<string, ContentionResourceStats>
  gantt: Array<{ resource: string; owner: string; start: number; end: number | null; kind: string }>
  deadlocks: DeadlockAlert[]
  unresolved_waits: Array<{ owner: string; resource: string; since_ts: number }>
}

/** 执行记录的故障条目（后端已归一化；保留别名兼容）。 */
export interface FaultRecord {
  fault_id?: string
  id?: string
  type?: string
  fault_type?: string
  severity?: string
  level?: string
  timestamp?: string | null
  target?: string | null
  target_id?: string
  link_id?: string
  [key: string]: unknown
}

/** 分节信封：available=false 时 reason 说明降级原因。 */
export interface ReportSectionEnvelope<T> {
  available: boolean
  reason: string | null
  report: T | null
}

/** 后端报告信封（与 ate_cloud.services.simulation_report 对齐）。 */
export interface SimulationReportResponse {
  run_id: string
  run_status: string
  generated_at: string
  warnings: string[]
  coverage: ReportSectionEnvelope<CoverageReport>
  contention: ReportSectionEnvelope<ContentionReport>
  faults: { records: FaultRecord[]; total: number }
}

// ─── 视图模型 ───────────────────────────────────────────────────────────────

export type CoverageBarKey = 'steps' | 'branches'

/** 覆盖率条模型（div 宽度 = percent%，无图表库）。 */
export interface CoverageBarModel {
  key: CoverageBarKey
  label: string
  percent: number
  covered: number
  total: number
}

/** 分支表行模型。 */
export interface BranchRowModel {
  key: string
  branchId: string
  thenIds: string[]
  elseIds: string[]
  armsCovered: string[]
  armsTotal: number
  /** 双臂分支且两臂均覆盖 → full（单臂分支永远不为 full）。 */
  full: boolean
}

/** 竞争 Top-N 行模型（wait 均值已换算为 ms）。 */
export interface ContentionRowModel {
  resource: string
  contentionCount: number
  maxWaiters: number
  meanWaitMs: number
}

/** 故障记录行模型（别名已折叠为规范键）。 */
export interface FaultRowModel {
  faultId: string
  type: string
  severity: string
  timestamp: string | null
  target: string | null
}

/** 分节元信息：面板据此渲染折叠头/空态/降级原因。 */
export interface ReportSectionMeta {
  id: 'coverage' | 'contention' | 'faults'
  title: string
  available: boolean
  emptyReason: string | null
  hasContent: boolean
}

// ─── 映射函数 ───────────────────────────────────────────────────────────────

const DASH = null

/** 百分比 → 条宽（clamp 到 [0,100]，防脏数据溢出）。 */
export function barWidthPercent(percent: number): number {
  if (!Number.isFinite(percent)) return 0
  return Math.min(100, Math.max(0, percent))
}

/** 覆盖率报告 → 步骤/分支两条 % 条。 */
export function buildCoverageBars(cov: CoverageReport): CoverageBarModel[] {
  return [
    {
      key: 'steps',
      label: '步骤覆盖',
      percent: cov.step_coverage.percent,
      covered: cov.step_coverage.executed,
      total: cov.step_coverage.planned,
    },
    {
      key: 'branches',
      label: '分支覆盖',
      percent: cov.branch_coverage.percent,
      covered: cov.branch_coverage.arms_covered,
      total: cov.branch_coverage.arms_total,
    },
  ]
}

/** 分支覆盖表行（保持 branches 键序，full = 双臂全见）。 */
export function buildBranchRows(cov: CoverageReport): BranchRowModel[] {
  return Object.entries(cov.branch_coverage.branches).map(([branchId, b]) => ({
    key: `branch:${branchId}`,
    branchId,
    thenIds: b.then_ids,
    elseIds: b.else_ids,
    armsCovered: b.arms_covered,
    armsTotal: b.arms_total,
    full: b.then_ids.length > 0 && b.else_ids.length > 0 && b.arms_covered.length === 2,
  }))
}

/** 竞争资源 → 按 contention_count 降序的 Top-N 行（并列按资源名升序）。 */
export function buildContentionRows(rep: ContentionReport, n = 5): ContentionRowModel[] {
  return Object.entries(rep.resources)
    .map(([resource, stats]) => ({
      resource,
      contentionCount: stats.contention_count,
      maxWaiters: stats.max_concurrent_waiters,
      meanWaitMs: Math.round(stats.wait.mean * 1000),
    }))
    .sort((a, b) => b.contentionCount - a.contentionCount || a.resource.localeCompare(b.resource))
    .slice(0, Math.max(0, n))
}

/** 死锁告警透传（空数组 = 无环）。 */
export function buildDeadlockAlerts(rep: ContentionReport): DeadlockAlert[] {
  return rep.deadlocks
}

/** 故障记录 → 展示行（后端已归一化，这里兜底折叠别名）。 */
export function buildFaultRows(records: FaultRecord[]): FaultRowModel[] {
  return records.map((raw, i) => ({
    faultId: String(raw.fault_id ?? raw.id ?? `fault-${i}`),
    type: String(raw.type ?? raw.fault_type ?? 'unknown'),
    severity: String(raw.severity ?? raw.level ?? 'warning'),
    timestamp: typeof raw.timestamp === 'string' ? raw.timestamp : DASH,
    target: (typeof raw.target === 'string' ? raw.target : null) ?? (typeof raw.link_id === 'string' ? raw.link_id : null),
  }))
}

/** severity → Element Plus tag 类型（复用既有严重度配色约定）。 */
export function severityTagType(severity: string): 'danger' | 'warning' | 'info' {
  const s = severity.toLowerCase()
  if (s === 'critical' || s === 'error') return 'danger'
  if (s === 'warning') return 'warning'
  return 'info'
}

/** 报告信封 → 三分节元信息（固定顺序；降级节带原因供空态/横幅渲染）。 */
export function buildReportSections(resp: SimulationReportResponse): ReportSectionMeta[] {
  const coverageHasContent =
    resp.coverage.available && resp.coverage.report !== null && resp.coverage.report.plan.total_steps > 0
  const contentionHasContent =
    resp.contention.available && resp.contention.report !== null && Object.keys(resp.contention.report.resources).length > 0
  return [
    {
      id: 'coverage',
      title: '覆盖率',
      available: resp.coverage.available,
      emptyReason: resp.coverage.reason,
      hasContent: coverageHasContent,
    },
    {
      id: 'contention',
      title: '资源竞争',
      available: resp.contention.available,
      emptyReason: resp.contention.reason,
      hasContent: contentionHasContent,
    },
    {
      id: 'faults',
      title: '故障记录',
      available: true,
      emptyReason: null,
      hasContent: resp.faults.total > 0,
    },
  ]
}
