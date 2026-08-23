/**
 * Component tests for ExecutionDiffPanel.vue (T37, v41-gap-analysis #37).
 *
 * Covered:
 * - match flag rendering decision: green badge for match=true, red for false
 * - empty/loading states
 * - sectioned tables render with violation-row highlighting
 * - truncation notice appears only when a section is truncated
 */
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import ExecutionDiffPanel from '../ExecutionDiffPanel.vue'
import { buildDiffSections, MAX_ROWS, type DiffSummary } from '@/utils/diffView'

function diffSummary(overrides: Partial<DiffSummary> = {}): DiffSummary {
  return {
    match: false,
    meta: { events_a: 5, events_b: 5 },
    steps: { added: [], removed: [], status_changed: [{ step_id: 's2', a: 'passed', b: 'failed' }] },
    measurements: [{ key: 'DMM1.read#0', a: 3.3, b: 4.9, delta: 1.6 }],
    timing: { total: null, steps: [] },
    resources: [],
    variables: { changed: [] },
    ...overrides,
  }
}

describe('ExecutionDiffPanel.vue', () => {
  it('shows the empty-state guide when summary is null', () => {
    const w = mount(ExecutionDiffPanel, { props: { summary: null } })
    expect(w.find('.diff-empty').exists()).toBe(true)
    expect(w.find('.diff-badge').exists()).toBe(false)
  })

  it('shows loading state while fetching', () => {
    const w = mount(ExecutionDiffPanel, { props: { summary: null, loading: true } })
    expect(w.find('.diff-empty').text()).toContain('对比中')
  })

  it('match=true renders green badge and per-section 无差异 placeholders', () => {
    const w = mount(ExecutionDiffPanel, {
      props: { summary: diffSummary({ match: true, steps: { added: [], removed: [], status_changed: [] }, measurements: [] }) },
    })
    const badge = w.find('.diff-badge')
    expect(badge.classes()).toContain('is-match')
    expect(badge.classes()).not.toContain('is-violation')
    expect(w.findAll('.diff-none')).toHaveLength(5)
    expect(w.findAll('tr.row-violation')).toHaveLength(0)
  })

  it('match=false renders red badge and highlights violation rows', () => {
    const w = mount(ExecutionDiffPanel, { props: { summary: diffSummary() } })
    const badge = w.find('.diff-badge')
    expect(badge.classes()).toContain('is-violation')
    expect(badge.text()).toContain('存在差异')
    // one highlighted row in steps + one in measurements
    expect(w.findAll('tr.row-violation')).toHaveLength(2)
    expect(w.find('[data-section="steps"]').text()).toContain('s2')
    expect(w.find('[data-section="measurements"]').text()).toContain('+1.6')
  })

  it('renders all five sections with headers when diffs exist', () => {
    const w = mount(ExecutionDiffPanel, { props: { summary: diffSummary() } })
    const ids = w.findAll('.diff-section').map((n) => n.attributes('data-section'))
    expect(ids).toEqual(['steps', 'measurements', 'timing', 'resources', 'variables'])
    expect(w.findAll('.diff-table')).toHaveLength(2) // only non-empty sections get tables
  })

  it('shows meta event counts next to the badge', () => {
    const w = mount(ExecutionDiffPanel, { props: { summary: diffSummary() } })
    expect(w.find('.diff-meta').text()).toContain('A=5')
    expect(w.find('.diff-meta').text()).toContain('B=5')
  })

  it('truncation notice renders only for truncated sections', () => {
    const many = Array.from({ length: MAX_ROWS + 1 }, (_, i) => `step_${i}`)
    const summary = diffSummary({
      steps: { added: many, removed: [], status_changed: [] },
    })
    const expectedTruncated = buildDiffSections(summary).filter((s) => s.truncated).length
    const w = mount(ExecutionDiffPanel, { props: { summary } })
    expect(expectedTruncated).toBeGreaterThan(0)
    expect(w.findAll('.diff-truncated')).toHaveLength(expectedTruncated)
  })

  it('reacts to prop changes (new summary swaps rendered rows)', async () => {
    const w = mount(ExecutionDiffPanel, {
      props: { summary: diffSummary({ measurements: [] }) },
    })
    expect(w.findAll('tr.row-violation')).toHaveLength(1)
    await w.setProps({ summary: diffSummary() })
    expect(w.findAll('tr.row-violation')).toHaveLength(2)
  })
})
