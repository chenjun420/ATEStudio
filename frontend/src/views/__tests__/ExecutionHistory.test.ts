/**
 * Tests for ExecutionHistory.vue view component.
 *
 * Verifies:
 * - Filter bar renders with all filter inputs (serial, product type, status, date range).
 * - Search and Reset buttons are present.
 * - Refresh and Export buttons are present.
 * - Execution table renders with correct columns and data.
 * - Loading skeleton renders when loading.
 * - Error alert renders when API fails.
 * - Empty state renders when no executions found.
 * - Pagination renders.
 * - View Details button triggers fetchDetail.
 * - Filter labels and table column headers render.
 *
 * The composable `useExecutionHistory` is mocked to return controlled reactive
 * state, avoiding real API calls. ElTable is stubbed with a native HTML table
 * because el-table doesn't render body rows in jsdom.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref, reactive, nextTick, defineComponent, h } from 'vue'
import ElementPlus from 'element-plus'
import ExecutionHistory from '../ExecutionHistory.vue'
import type { ExecutionListItem, Execution } from '@/api/executions'

// ─── jsdom polyfills for Element Plus table ──────────────────────────────────

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver

// ─── ElTable stub ────────────────────────────────────────────────────────────
// el-table doesn't render body rows in jsdom (requires real layout/ResizeObserver).
// Stub it with a native HTML div structure that renders slots directly.

interface StubColumnProps {
  prop?: string
  label?: string
  width?: number | string
  type?: string
  sortable?: boolean
  fixed?: string
  align?: string
}

const ElTableStub = defineComponent({
  name: 'ElTable',
  props: {
    data: { type: Array, default: () => [] },
    rowKey: { type: [String, Function], default: '' },
    stripe: { type: Boolean, default: false },
    size: { type: String, default: '' },
  },
  emits: ['row-click'],
  setup(props, { slots, emit }) {
    return () => {
      const columns: any[] = []
      const defaultSlot = slots.default?.()
      if (defaultSlot) {
        const flat = Array.isArray(defaultSlot) ? defaultSlot : [defaultSlot]
        flat.forEach((node) => {
          if (node && typeof node === 'object' && 'props' in node) {
            columns.push(node)
          }
        })
      }

      const rows = props.data as ExecutionListItem[]

      return h('div', { class: 'el-table-stub', 'data-testid': 'exec-table' }, [
        // Column headers
        h('div', { class: 'el-table-header' },
          columns.map((node: any, i: number) =>
            h('span', { key: i, class: 'el-table-col-header' }, node?.props?.label || '')
          )
        ),
        // Table body
        ...rows.map((row: ExecutionListItem, rowIndex: number) => {
          const rowKey = typeof props.rowKey === 'function' ? '' : ''
          return h('div', {
            key: rowKey || rowIndex,
            class: 'el-table-row',
            onClick: () => emit('row-click', row),
          }, [
            ...columns.map((node: any, colIndex: number) => {
              if (node?.children?.default) {
                const cellContent = node.children.default({ row, $index: rowIndex })
                return h('div', { key: colIndex, class: 'el-table-cell' }, cellContent)
              }
              const prop = node?.props?.prop
              const value = prop ? String((row as any)[prop] ?? '') : ''
              return h('span', { key: colIndex, class: 'el-table-cell' }, value)
            }),
          ])
        }),
      ])
    }
  },
})

const ElTableColumnStub = defineComponent({
  name: 'ElTableColumn',
  props: {
    prop: { type: String, default: '' },
    label: { type: String, default: '' },
    width: { type: [Number, String], default: '' },
    type: { type: String, default: '' },
    sortable: { type: Boolean, default: false },
    fixed: { type: String, default: '' },
    align: { type: String, default: '' },
    'min-width': { type: [Number, String], default: '' },
  },
  setup(_props, { slots }) {
    return () => slots.default?.()
  },
})

// ─── Mock state container ────────────────────────────────────────────────────

interface MockState {
  serialNumber: string
  productType: string
  statusFilter: string
  dateRange: [string, string] | null
  currentPage: number
  pageSize: number
  total: number
  executions: ExecutionListItem[]
  detail: Execution | null
  loading: boolean
  error: string | null
  detailLoading: boolean
  detailError: string | null
}

function createMockState(overrides: Partial<MockState> = {}): MockState {
  return {
    serialNumber: '',
    productType: '',
    statusFilter: '',
    dateRange: null,
    currentPage: 1,
    pageSize: 20,
    total: 0,
    executions: [],
    detail: null,
    loading: false,
    error: null,
    detailLoading: false,
    detailError: null,
    ...overrides,
  }
}

let mockState: ReturnType<typeof reactive<MockState>>
const fetchListMock = vi.fn()
const fetchDetailMock = vi.fn()
const clearDetailMock = vi.fn()
const applyFiltersMock = vi.fn()
const onPaginationChangeMock = vi.fn()
const resetFiltersMock = vi.fn()
const exportCsvMock = vi.fn()
const buildSearchRequestMock = vi.fn()

function stateRef<K extends keyof MockState>(key: K) {
  return ref(mockState[key])
}

// Mock the composable module
vi.mock('@/composables/useExecutionHistory', () => ({
  useExecutionHistory: () => ({
    serialNumber: stateRef('serialNumber'),
    productType: stateRef('productType'),
    statusFilter: stateRef('statusFilter'),
    dateRange: stateRef('dateRange'),
    hasActiveFilters: ref(false),
    currentPage: stateRef('currentPage'),
    pageSize: stateRef('pageSize'),
    total: stateRef('total'),
    executions: stateRef('executions'),
    detail: stateRef('detail'),
    loading: stateRef('loading'),
    error: stateRef('error'),
    detailLoading: stateRef('detailLoading'),
    detailError: stateRef('detailError'),
    fetchList: fetchListMock,
    fetchDetail: fetchDetailMock,
    clearDetail: clearDetailMock,
    applyFilters: applyFiltersMock,
    onPaginationChange: onPaginationChangeMock,
    resetFilters: resetFiltersMock,
    exportCsv: exportCsvMock,
    buildSearchRequest: buildSearchRequestMock,
  }),
}))

// ─── Test helpers ────────────────────────────────────────────────────────────

function createExecutionItem(overrides: Partial<ExecutionListItem> = {}): ExecutionListItem {
  return {
    id: 'run-001',
    sequence_id: 'seq-1',
    status: 'COMPLETED',
    dut_serial: 'DUT-001',
    product_type: 'BoardA',
    started_at: '2026-08-01T10:00:00Z',
    completed_at: '2026-08-01T10:05:00Z',
    pass_rate: 95.0,
    error: null,
    ...overrides,
  }
}

function mountComponent() {
  return mount(ExecutionHistory, {
    global: {
      plugins: [ElementPlus],
      stubs: {
        ElTable: ElTableStub,
        ElTableColumn: ElTableColumnStub,
        // Keep ElDrawer as-is; it renders to body via teleport
        transition: false,
      },
    },
  })
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('ExecutionHistory', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockState = reactive(createMockState())
  })

  // ── Filter bar rendering ──

  it('test_renders_filter_bar', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="filter-bar"]').exists()).toBe(true)
  })

  it('test_renders_serial_number_filter', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="filter-serial"]').exists()).toBe(true)
  })

  it('test_renders_product_type_filter', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="filter-product-type"]').exists()).toBe(true)
  })

  it('test_renders_status_filter', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="filter-status"]').exists()).toBe(true)
  })

  it('test_renders_date_range_filter_label', () => {
    const wrapper = mountComponent()
    const labels = wrapper.findAll('.eh-filter-label')
    const hasDateRangeLabel = labels.some(l => l.text().includes('Date Range'))
    expect(hasDateRangeLabel).toBe(true)
  })

  it('test_renders_search_button', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="btn-search"]').exists()).toBe(true)
  })

  it('test_renders_reset_button', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="btn-reset"]').exists()).toBe(true)
  })

  it('test_search_button_triggers_apply_filters', async () => {
    const wrapper = mountComponent()
    const btn = wrapper.find('[data-testid="btn-search"]')
    await btn.trigger('click')
    expect(applyFiltersMock).toHaveBeenCalled()
  })

  it('test_reset_button_triggers_reset_filters', async () => {
    const wrapper = mountComponent()
    const btn = wrapper.find('[data-testid="btn-reset"]')
    await btn.trigger('click')
    expect(resetFiltersMock).toHaveBeenCalled()
  })

  // ── Header buttons ──

  it('test_renders_refresh_button', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="btn-refresh"]').exists()).toBe(true)
  })

  it('test_refresh_button_triggers_fetch_list', async () => {
    const wrapper = mountComponent()
    const btn = wrapper.find('[data-testid="btn-refresh"]')
    await btn.trigger('click')
    expect(fetchListMock).toHaveBeenCalled()
  })

  it('test_renders_export_button', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="btn-export"]').exists()).toBe(true)
  })

  it('test_export_button_triggers_export_csv_when_enabled', async () => {
    mockState.executions = [createExecutionItem()]
    const wrapper = mountComponent()
    const btn = wrapper.find('[data-testid="btn-export"]')
    await btn.trigger('click')
    expect(exportCsvMock).toHaveBeenCalled()
  })

  it('test_export_button_disabled_when_no_data', () => {
    mockState.executions = []
    const wrapper = mountComponent()
    const btn = wrapper.find('[data-testid="btn-export"]')
    expect(btn.attributes('disabled')).toBeDefined()
  })

  // ── Loading state ──

  it('test_renders_loading_skeleton_when_loading', () => {
    mockState.loading = true
    mockState.executions = []
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="loading-skeleton"]').exists()).toBe(true)
  })

  // ── Error state ──

  it('test_renders_error_alert_when_error', () => {
    mockState.error = 'Network timeout'
    const wrapper = mountComponent()
    const alert = wrapper.find('[data-testid="error-alert"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('Network timeout')
  })

  // ── Empty state ──

  it('test_renders_empty_state_when_no_executions', () => {
    mockState.executions = []
    mockState.loading = false
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(true)
  })

  // ── Table rendering ──

  it('test_renders_table_card_with_data', () => {
    mockState.executions = [
      createExecutionItem({ id: 'r1', dut_serial: 'SN-001', status: 'COMPLETED', pass_rate: 100.0 }),
      createExecutionItem({ id: 'r2', dut_serial: 'SN-002', status: 'FAILED', pass_rate: 50.0 }),
    ]
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="table-card"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="exec-table"]').exists()).toBe(true)
  })

  it('test_renders_status_tags_in_table', async () => {
    mockState.executions = [
      createExecutionItem({ id: 'r1', status: 'COMPLETED' }),
      createExecutionItem({ id: 'r2', status: 'FAILED' }),
    ]
    const wrapper = mountComponent()
    await nextTick()
    const tableText = wrapper.find('[data-testid="exec-table"]').text()
    expect(tableText).toContain('COMPLETED')
    expect(tableText).toContain('FAILED')
  })

  it('test_renders_pass_rate_in_table', async () => {
    mockState.executions = [
      createExecutionItem({ pass_rate: 95.0 }),
    ]
    const wrapper = mountComponent()
    await nextTick()
    const tableText = wrapper.find('[data-testid="exec-table"]').text()
    expect(tableText).toContain('95.0%')
  })

  it('test_renders_serial_numbers_in_table', async () => {
    mockState.executions = [
      createExecutionItem({ id: 'r1', dut_serial: 'SN-001' }),
      createExecutionItem({ id: 'r2', dut_serial: 'SN-002' }),
    ]
    const wrapper = mountComponent()
    await nextTick()
    const tableText = wrapper.find('[data-testid="exec-table"]').text()
    expect(tableText).toContain('SN-001')
    expect(tableText).toContain('SN-002')
  })

  // ── Table column headers ──

  it('test_renders_table_column_headers', async () => {
    mockState.executions = [createExecutionItem()]
    const wrapper = mountComponent()
    await nextTick()
    const tableText = wrapper.find('[data-testid="exec-table"]').text()
    expect(tableText).toContain('Serial Number')
    expect(tableText).toContain('Product Type')
    expect(tableText).toContain('Start Time')
    expect(tableText).toContain('Result')
    expect(tableText).toContain('Pass Rate')
    expect(tableText).toContain('Actions')
  })

  // ── Pagination ──

  it('test_renders_pagination_when_data_present', () => {
    mockState.executions = [createExecutionItem()]
    mockState.total = 100
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="pagination"]').exists()).toBe(true)
  })

  it('test_does_not_render_pagination_when_empty', () => {
    mockState.executions = []
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="pagination"]').exists()).toBe(false)
  })

  // ── Title ──

  it('test_renders_page_title', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('.eh-title').text()).toContain('Execution History')
  })

  // ── Filter labels ──

  it('test_renders_filter_labels', () => {
    const wrapper = mountComponent()
    const html = wrapper.html()
    expect(html).toContain('Serial Number')
    expect(html).toContain('Product Type')
    expect(html).toContain('Status')
    expect(html).toContain('Date Range')
  })

  // ── View detail button ──

  it('test_renders_view_detail_button_in_table', async () => {
    mockState.executions = [createExecutionItem()]
    const wrapper = mountComponent()
    await nextTick()
    expect(wrapper.find('[data-testid="btn-view-detail"]').exists()).toBe(true)
  })

  it('test_view_detail_button_triggers_fetch_detail', async () => {
    mockState.executions = [createExecutionItem()]
    const wrapper = mountComponent()
    await nextTick()
    const btn = wrapper.find('[data-testid="btn-view-detail"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    expect(fetchDetailMock).toHaveBeenCalledWith('run-001')
  })
})
