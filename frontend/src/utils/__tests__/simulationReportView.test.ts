/**
 * Unit tests for utils/simulationReportView.ts (T41, v41-gap-analysis #41).
 *
 * Covered:
 * - coverage bar math (steps/branches percent + covered/total)
 * - empty-plan and clamp edge cases
 * - branch table row mapping with full-coverage flag
 * - contention top-N sorting + empty state
 * - deadlock alert extraction
 * - fault record alias normalization + severity tag mapping
 * - section meta mapping incl. degraded (unavailable) sections
 */
import { describe, expect, it } from 'vitest'

import {
  barWidthPercent,
  buildBranchRows,
  buildContentionRows,
  buildCoverageBars,
  buildDeadlockAlerts,
  buildFaultRows,
  buildReportSections,
  severityTagType,
  type ContentionReport,
  type CoverageReport,
  type FaultRecord,
  type SimulationReportResponse,
} from '../simulationReportView'

function coverage(overrides: Partial<CoverageReport> = {}): CoverageReport {
  return {
    plan: { total_steps: 4, total_branches: 1, total_branch_arms: 2, all_step_ids: [] },
    step_coverage: {
      planned: 4,
      executed: 3,
      skipped: [],
      unexecuted: ['step-4'],
      unknown_executed: [],
      percent: 75,
    },
    branch_coverage: {
      branches: {},
      arms_total: 2,
      arms_covered: 1,
      percent: 50,
      both_sides_seen: [],
    },
    by_source_step: {},
    summary: { step_percent: 75, branch_percent: 50, quality: 'partial' },
    ...overrides,
  }
}

function contention(overrides: Partial<ContentionReport> = {}): ContentionReport {
  const stats = (c: number, w: number) => ({
    acquire_count: c,
    release_count: c,
    contention_count: c,
    max_concurrent_waiters: w,
    wait: { count: c, total: c * 0.5, min: 0.5, max: 0.5, mean: 0.5, histogram: [] },
    hold: { count: c, total: 0, min: 0, max: 0, mean: 0, histogram: [] },
  })
  return {
    generated_from: { events: 6, resources: 2, owners: 2 },
    resources: {
      R1: stats(1, 1),
      R2: stats(3, 2),
    },
    gantt: [],
    deadlocks: [],
    unresolved_waits: [],
    ...overrides,
  }
}

function report(overrides: Partial<SimulationReportResponse> = {}): SimulationReportResponse {
  return {
    run_id: 'run-1',
    run_status: 'COMPLETED',
    generated_at: '2026-08-24T00:00:00+00:00',
    warnings: [],
    coverage: { available: true, reason: null, report: coverage() },
    contention: { available: true, reason: null, report: contention() },
    faults: { records: [], total: 0 },
    ...overrides,
  }
}

describe('buildCoverageBars', () => {
  it('computes steps + branches bars from coverage report math', () => {
    const bars = buildCoverageBars(coverage())
    expect(bars).toHaveLength(2)
    expect(bars[0]).toMatchObject({ key: 'steps', label: '步骤覆盖', percent: 75, covered: 3, total: 4 })
    expect(bars[1]).toMatchObject({ key: 'branches', label: '分支覆盖', percent: 50, covered: 1, total: 2 })
  })

  it('empty plan yields zeroed bars without NaN', () => {
    const bars = buildCoverageBars(
      coverage({
        plan: { total_steps: 0, total_branches: 0, total_branch_arms: 0, all_step_ids: [] },
        step_coverage: {
          planned: 0, executed: 0, skipped: [], unexecuted: [], unknown_executed: [], percent: 0,
        },
        branch_coverage: { branches: {}, arms_total: 0, arms_covered: 0, percent: 100, both_sides_seen: [] },
      }),
    )
    expect(bars[0]).toMatchObject({ covered: 0, total: 0, percent: 0 })
    expect(Number.isNaN(bars[0].percent)).toBe(false)
  })

  it('barWidthPercent clamps to [0, 100]', () => {
    expect(barWidthPercent(150)).toBe(100)
    expect(barWidthPercent(-5)).toBe(0)
    expect(barWidthPercent(42.5)).toBeCloseTo(42.5)
  })
})

describe('buildBranchRows', () => {
  it('maps branch entries with full flag when both arms covered', () => {
    const rows = buildBranchRows(
      coverage({
        branch_coverage: {
          branches: {
            'branch-a': {
              then_ids: ['s1'], else_ids: ['s2'],
              decisions: ['then', 'else'], arms_covered: ['else', 'then'], arms_total: 2,
            },
            'branch-b': {
              then_ids: ['s3'], else_ids: [],
              decisions: ['then'], arms_covered: ['then'], arms_total: 1,
            },
          },
          arms_total: 3,
          arms_covered: 3,
          percent: 100,
          both_sides_seen: ['branch-a'],
        },
      }),
    )
    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({ branchId: 'branch-a', armsTotal: 2, full: true })
    expect(rows[0].armsCovered).toEqual(['else', 'then'])
    expect(rows[1]).toMatchObject({ branchId: 'branch-b', armsTotal: 1, full: false })
  })

  it('returns no rows when plan declares no branches', () => {
    expect(buildBranchRows(coverage())).toEqual([])
  })
})

describe('buildContentionRows', () => {
  it('sorts top-N by contention count descending', () => {
    const rows = buildContentionRows(contention(), 5)
    expect(rows).toHaveLength(2)
    expect(rows[0].resource).toBe('R2')
    expect(rows[0].contentionCount).toBe(3)
    expect(rows[0].maxWaiters).toBe(2)
    expect(rows[1].resource).toBe('R1')
  })

  it('truncates to N and converts mean wait seconds to ms', () => {
    const rep = contention()
    rep.resources['R3'] = { ...rep.resources['R1'], contention_count: 9 }
    const rows = buildContentionRows(rep, 2)
    expect(rows).toHaveLength(2)
    expect(rows[0].resource).toBe('R3')
    expect(rows[0].meanWaitMs).toBe(500)
  })

  it('empty resources degrade to an empty row list', () => {
    expect(buildContentionRows(contention({ resources: {} }))).toEqual([])
  })
})

describe('buildDeadlockAlerts', () => {
  it('passes through detected cycles for alert rendering', () => {
    const deadlocks = [
      {
        detected_at_ts: 3,
        cycle_owners: ['uut-a', 'uut-b'],
        involved_resources: ['R1', 'R2'],
        edges: [{ waiter: 'uut-a', waits_for: 'R2', held_by: 'uut-b' }],
      },
    ]
    expect(buildDeadlockAlerts(contention({ deadlocks }))).toEqual(deadlocks)
  })

  it('no cycles → empty alert list', () => {
    expect(buildDeadlockAlerts(contention())).toEqual([])
  })
})

describe('buildFaultRows + severityTagType', () => {
  it('normalizes alias fields to canonical display keys', () => {
    const records: FaultRecord[] = [
      { id: 'fx-9', fault_type: 'short_circuit', level: 'critical', link_id: 'L3' },
      { fault_id: 'f-2', type: 'noise' },
    ]
    const rows = buildFaultRows(records)
    expect(rows[0]).toEqual({
      faultId: 'fx-9', type: 'short_circuit', severity: 'critical', timestamp: null, target: 'L3',
    })
    expect(rows[1].faultId).toBe('f-2')
    expect(rows[1].severity).toBe('warning')
  })

  it('severity maps to Element Plus tag types', () => {
    expect(severityTagType('critical')).toBe('danger')
    expect(severityTagType('error')).toBe('danger')
    expect(severityTagType('warning')).toBe('warning')
    expect(severityTagType('info')).toBe('info')
    expect(severityTagType('whatever')).toBe('info')
  })
})

describe('buildReportSections', () => {
  it('marks unavailable sections with their degrade reason', () => {
    const sections = buildReportSections(
      report({
        warnings: ['覆盖率: no recording for this run'],
        coverage: { available: false, reason: 'no recording for this run', report: null },
      }),
    )
    const cov = sections.find((s) => s.id === 'coverage')
    expect(cov?.available).toBe(false)
    expect(cov?.emptyReason).toBe('no recording for this run')
    expect(cov?.hasContent).toBe(false)
  })

  it('populated report → all sections available; faults content follows total', () => {
    const sections = buildReportSections(
      report({ faults: { records: [{ fault_id: 'f-1' }], total: 1 } }),
    )
    expect(sections.map((s) => s.id)).toEqual(['coverage', 'contention', 'faults'])
    for (const sec of sections) {
      expect(sec.available).toBe(true)
      expect(sec.hasContent).toBe(true)
    }
  })

  it('available-but-empty contention (zero resources) has no content', () => {
    const sections = buildReportSections(
      report({ contention: { available: true, reason: null, report: contention({ resources: {} }) } }),
    )
    expect(sections.find((s) => s.id === 'contention')?.hasContent).toBe(false)
  })
})
