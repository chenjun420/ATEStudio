/**
 * Tests for TraceabilityMatrix.vue (task 26).
 *
 * Verifies:
 * - Requirement → case → DSL-step rows render from the mocked
 *   GET /knowledge/traceability response (case codes + step ids appear).
 * - Header coverage indicators use the paged requirements/cases totals
 *   (requirements count, cases count, covered/uncovered, coverage %).
 * - Uncovered requirements are flagged; linked cases show their DSL step id.
 * - Unlinked cases (traceability gaps) render in their own section.
 * - A product filter calls the APIs with product_code.
 * - A failure renders the error banner.
 *
 * @/api/knowledge is mocked. ElTable/ElTableColumn are stubbed (jsdom has no
 * layout) with a stub that renders each row's column default slots.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { h, defineComponent, nextTick } from 'vue'
import ElementPlus from 'element-plus'
import TraceabilityMatrix from '../TraceabilityMatrix.vue'

const { traceMock, reqMock, casesMock } = vi.hoisted(() => ({
  traceMock: vi.fn(),
  reqMock: vi.fn(),
  casesMock: vi.fn(),
}))

vi.mock('@/api/knowledge', () => ({
  fetchTraceability: traceMock,
  fetchRequirements: reqMock,
  fetchCases: casesMock,
}))

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver

// ─── ElTable stub (renders each row's column default slots) ──────────────────

interface VNodeLike {
  props?: Record<string, unknown>
  children?: { default?: (scope: Record<string, unknown>) => unknown }
}

const ElTableStub = defineComponent({
  name: 'ElTable',
  props: { data: { type: Array, default: () => [] } },
  setup(props, { slots }) {
    return () => {
      const cols: VNodeLike[] = []
      const defaultSlot = slots.default?.()
      const flat = Array.isArray(defaultSlot) ? defaultSlot : [defaultSlot]
      for (const node of flat ?? []) {
        if (node && typeof node === 'object' && 'props' in node) cols.push(node as VNodeLike)
      }
      const rows = props.data as Record<string, unknown>[]
      return h('div', { class: 'el-table-stub' }, [
        h(
          'div',
          { class: 'el-table-header' },
          cols.map((c, i) =>
            h('span', { key: i, class: 'el-table-col-header' }, String(c.props?.label ?? '')),
          ),
        ),
        ...rows.map((row, ri) =>
          h(
            'div',
            { key: ri, class: 'el-table-row' },
            cols.map((c, ci) => {
              const cellSlot = c.children?.default
              if (cellSlot) {
                return h(
                  'span',
                  { key: ci, class: 'el-table-cell' },
                  cellSlot({ row, $index: ri }) as never,
                )
              }
              const prop = c.props?.prop as string | undefined
              return h('span', { key: ci, class: 'el-table-cell' }, prop ? String(row[prop] ?? '') : '')
            }),
          ),
        ),
      ])
    }
  },
})

const ElTableColumnStub = defineComponent({
  name: 'ElTableColumn',
  props: { prop: { type: String, default: '' }, label: { type: String, default: '' } },
  setup(_props, { slots }) {
    return () => slots.default?.()
  },
})

// ─── Fixtures ────────────────────────────────────────────────────────────────

function makeTree() {
  return {
    product_code: null,
    requirements: [
      {
        id: 'r1',
        requirement_code: 'REQ-001',
        title: '5V rail within tolerance',
        source: 'dsl',
        cases: [
          { id: 'c1', case_code: 'TC-001', title: 'Measure 5V', sequence_id: 'seq-1', step_id: 'step-measure-5v', atml_ref: null, status: 'active' },
          { id: 'c2', case_code: 'TC-002', title: 'Ripple check', sequence_id: null, step_id: '', atml_ref: null, status: 'draft' },
        ],
      },
      {
        id: 'r2',
        requirement_code: 'REQ-002',
        title: 'Uncovered requirement',
        source: 'manual',
        cases: [],
      },
    ],
    unlinked_cases: [
      { id: 'c9', case_code: 'TC-099', title: 'Orphan case', sequence_id: null, step_id: 'step-x', atml_ref: null, status: 'draft' },
    ],
  }
}

async function mountView() {
  const wrapper = mount(TraceabilityMatrix, {
    global: {
      plugins: [ElementPlus],
      stubs: { ElTable: ElTableStub, ElTableColumn: ElTableColumnStub },
    },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

describe('TraceabilityMatrix', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    reqMock.mockResolvedValue({ items: [], total: 2 })
    casesMock.mockResolvedValue({ items: [], total: 3 })
  })

  it('renders requirement → case → DSL-step rows from the traceability tree', async () => {
    traceMock.mockResolvedValue(makeTree())
    const wrapper = await mountView()

    const table = wrapper.find('[data-testid="traceability-table"]')
    expect(table.exists()).toBe(true)
    const text = table.text()
    // Requirement rows.
    expect(text).toContain('REQ-001')
    expect(text).toContain('5V rail within tolerance')
    expect(text).toContain('REQ-002')
    // Linked cases + DSL step ids.
    expect(text).toContain('TC-001')
    expect(text).toContain('step-measure-5v')
    expect(text).toContain('TC-002')
    // Uncovered requirement shows the "no linked cases" gap.
    expect(text).toContain('No linked cases')
  })

  it('shows coverage indicators derived from the tree', async () => {
    traceMock.mockResolvedValue(makeTree())
    const wrapper = await mountView()

    // 2 requirements total (from paged endpoint), 3 cases total.
    expect(wrapper.find('[data-testid="count-requirements"]').text()).toContain('2')
    expect(wrapper.find('[data-testid="count-cases"]').text()).toContain('3')
    // 1 covered (r1), 1 uncovered (r2) → 50%.
    expect(wrapper.find('[data-testid="count-covered"]').text()).toContain('1')
    expect(wrapper.find('[data-testid="count-uncovered"]').text()).toContain('1')
    expect(wrapper.find('[data-testid="coverage-percent"]').text()).toContain('50%')
  })

  it('renders unlinked (orphan) cases in the gap section', async () => {
    traceMock.mockResolvedValue(makeTree())
    const wrapper = await mountView()

    const unlinked = wrapper.find('[data-testid="unlinked-card"]')
    expect(unlinked.exists()).toBe(true)
    expect(unlinked.text()).toContain('TC-099')
    expect(unlinked.text()).toContain('step-x')
  })

  it('renders an empty state when there are no requirements', async () => {
    traceMock.mockResolvedValue({ product_code: null, requirements: [], unlinked_cases: [] })
    reqMock.mockResolvedValue({ items: [], total: 0 })
    casesMock.mockResolvedValue({ items: [], total: 0 })
    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="empty-matrix"]').exists()).toBe(true)
  })

  it('forwards the product_code filter to all three APIs', async () => {
    traceMock.mockResolvedValue(makeTree())
    const wrapper = await mountView()

    await wrapper.find('input[data-testid="filter-product"]').setValue('PRD-42')
    await wrapper.find('[data-testid="btn-filter"]').trigger('click')
    await flushPromises()

    expect(traceMock).toHaveBeenLastCalledWith('PRD-42')
    expect(reqMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ product_code: 'PRD-42' }),
    )
    expect(casesMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ product_code: 'PRD-42' }),
    )
  })

  it('renders an error banner when loading fails', async () => {
    traceMock.mockRejectedValue(new Error('Network down'))
    const wrapper = await mountView()

    const banner = wrapper.find('[data-testid="error-alert"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Network down')
  })
})
