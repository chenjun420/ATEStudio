/**
 * Unit tests for utils/diffView.ts (T37, v41-gap-analysis #37).
 *
 * Covered:
 * - per-section row mapping (steps/measurements/timing/resources/variables)
 * - empty/match state → all sections with zero rows
 * - formatValue / formatDelta edge cases
 * - MAX_ROWS truncation flag
 */
import { describe, expect, it } from 'vitest'

import {
  buildDiffSections,
  buildStepsSection,
  buildTimingSection,
  formatDelta,
  formatValue,
  MAX_ROWS,
  type DiffSummary,
} from '../diffView'

function summary(overrides: Partial<DiffSummary> = {}): DiffSummary {
  return {
    match: false,
    meta: { events_a: 5, events_b: 6 },
    steps: { added: [], removed: [], status_changed: [] },
    measurements: [],
    timing: { total: null, steps: [] },
    resources: [],
    variables: { changed: [] },
    ...overrides,
  }
}

describe('buildDiffSections', () => {
  it('returns all five sections in fixed order', () => {
    const sections = buildDiffSections(summary())
    expect(sections.map((s) => s.id)).toEqual([
      'steps',
      'measurements',
      'timing',
      'resources',
      'variables',
    ])
  })

  it('match state (all-empty diff) yields zero rows and no truncation everywhere', () => {
    const all = buildDiffSections(
      summary({ match: true, meta: { events_a: 3, events_b: 3 } }),
    )
    for (const sec of all) {
      expect(sec.rows).toHaveLength(0)
      expect(sec.truncated).toBe(false)
    }
  })

  it('steps section maps added/removed/status_changed to violation rows', () => {
    const sec = buildStepsSection(
      summary({
        steps: {
          added: ['s_new'],
          removed: ['s_old'],
          status_changed: [{ step_id: 's2', a: 'passed', b: 'failed' }],
        },
      }),
    )
    expect(sec.rows).toHaveLength(3)
    expect(sec.rows[0]!.cells).toEqual(['s_new', '—', '—', '新增'])
    expect(sec.rows[1]!.cells).toEqual(['s_old', '—', '—', '移除'])
    expect(sec.rows[2]!.cells).toEqual(['s2', 'passed', 'failed', '状态变更'])
    expect(sec.rows.every((r) => r.status === 'violation')).toBe(true)
  })

  it('measurements section formats numeric deltas and null deltas', () => {
    const sec = buildDiffSections(
      summary({
        measurements: [
          { key: 'DMM1.read#0', a: 3.3, b: 4.9, delta: 1.60000001 },
          { key: 's1:voltage', a: 'hi', b: 'lo', delta: null },
        ],
      }),
    )[1]!
    expect(sec.headers).toEqual(['测量项', '基线 A', '候选 B', 'Δ'])
    expect(sec.rows[0]!.cells).toEqual(['DMM1.read#0', '3.3', '4.9', '+1.6'])
    expect(sec.rows[1]!.cells).toEqual(['s1:voltage', 'hi', 'lo', '—'])
  })

  it('timing section includes total row only when total is non-null', () => {
    const withTotal = buildTimingSection(
      summary({
        timing: {
          total: { a_ms: 100, b_ms: 150.25, delta_ms: 50.25 },
          steps: [{ step_id: 's1', a_ms: 10, b_ms: 12.5, delta_ms: 2.5 }],
        },
      }),
    )
    expect(withTotal.rows.map((r) => r.cells[0])).toEqual(['总时长', 's1'])
    expect(withTotal.rows[0]!.cells[3]).toBe('+50.25')

    const withoutTotal = buildTimingSection(summary({ timing: { total: null, steps: [] } }))
    expect(withoutTotal.rows).toHaveLength(0)
  })

  it('resources and variables sections map their diff entries', () => {
    const [res, vars] = buildDiffSections(
      summary({
        resources: [{ resource: 'DMM1', method: 'read', a_count: 2, b_count: 3 }],
        variables: { changed: [{ scope: 'global', key: 'retries', old: 1, new: 2 }] },
      }),
    ).slice(3)
    expect(res!.rows[0]!.cells).toEqual(['DMM1', 'read', '2', '3'])
    expect(vars!.rows[0]!.cells).toEqual(['global', 'retries', '1', '2'])
  })

  it('flags truncation when a section exceeds MAX_ROWS', () => {
    const many = Array.from({ length: MAX_ROWS + 1 }, (_, i) => `step_${i}`)
    const sec = buildStepsSection(summary({ steps: { added: many, removed: [], status_changed: [] } }))
    expect(sec.truncated).toBe(true)
    expect(sec.rows).toHaveLength(MAX_ROWS)
  })
})

describe('formatters', () => {
  it('formatValue renders dash for nullish and JSON for objects', () => {
    expect(formatValue(null)).toBe('—')
    expect(formatValue(undefined)).toBe('—')
    expect(formatValue(42)).toBe('42')
    expect(formatValue({ a: 1 })).toBe('{"a":1}')
  })

  it('formatDelta signs positive values and passes through negatives', () => {
    expect(formatDelta(1.5)).toBe('+1.5')
    expect(formatDelta(-2)).toBe('-2')
    expect(formatDelta(null)).toBe('—')
  })
})
