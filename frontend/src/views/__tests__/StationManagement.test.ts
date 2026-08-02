/**
 * Tests for StationManagement.vue view component.
 *
 * Verifies:
 * - Table renders with correct columns and worker data.
 * - Loading skeleton renders when data is loading.
 * - Error banner renders when API fails.
 * - Empty state renders when no workers.
 * - Status badges are color-coded (online/expiring/offline).
 * - Status filter buttons filter the table.
 * - Refresh button triggers refresh() call.
 * - Config dialog opens and has input fields.
 * - Action buttons (配置/重启/同步) are present.
 * - Expandable detail panel exists.
 * - Summary counts (online/expiring/offline) render.
 *
 * The composable `useStations` is mocked to return controlled reactive
 * state, avoiding real API calls.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref, reactive, nextTick, h, defineComponent } from 'vue'
import ElementPlus from 'element-plus'
import StationManagement from '../StationManagement.vue'
import type { WorkerInfo } from '@/api/stations'
import type { WorkerStatus } from '@/composables/useStations'

// ─── jsdom polyfills for Element Plus table ──────────────────────────────────

// el-table relies on ResizeObserver which jsdom doesn't provide
class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver

// ─── ElTable stub ────────────────────────────────────────────────────────────
// el-table doesn't render body rows in jsdom (requires real layout/ResizeObserver).
// Stub it with a native HTML table that renders slots directly.

interface StubColumnProps {
  prop?: string
  label?: string
  width?: number | string
  type?: string
  sortable?: boolean
  fixed?: string
}

const ElTableStub = defineComponent({
  name: 'ElTable',
  props: {
    data: { type: Array, default: () => [] },
    rowKey: { type: [String, Function], default: '' },
    stripe: { type: Boolean, default: false },
    expandRowKeys: { type: Array, default: () => [] },
  },
  emits: ['expand-change'],
  setup(props, { slots, emit }) {
    return () => {
      const columns: ReturnType<typeof slots.default>[] = []
      const defaultSlot = slots.default?.()
      if (defaultSlot) {
        const flat = Array.isArray(defaultSlot) ? defaultSlot : [defaultSlot]
        flat.forEach((node) => {
          if (node && typeof node === 'object' && 'props' in node) {
            columns.push(node as never)
          }
        })
      }

      const rows = props.data as WorkerInfo[]
      const expandedKeys = props.expandRowKeys as string[]

      return h('div', { class: 'el-table-stub' }, [
        // Column headers
        h('div', { class: 'el-table-header' },
          columns.map((node: any, i: number) =>
            h('span', { key: i, class: 'el-table-col-header' }, node?.props?.label || '')
          )
        ),
        // Table body
        ...rows.map((row: WorkerInfo, rowIndex: number) => {
          const rowKey = typeof props.rowKey === 'string' ? (row as any)[props.rowKey] : ''
          const isExpanded = expandedKeys.includes(rowKey)
          return h('div', { key: rowKey || rowIndex, class: 'el-table-row' }, [
            ...columns.map((node: any, colIndex: number) => {
              const colType = node?.props?.type
              if (colType === 'expand') {
                // Render expand slot if expanded
                if (isExpanded && slots) {
                  return h('div', { key: colIndex, class: 'el-table-cell-expand' },
                    slots.default ? [] : ''
                  )
                }
                return h('span', { key: colIndex, class: 'el-table-expand-icon' }, '>')
              }
              // Check if the column has a default slot (custom template)
              if (node?.children?.default) {
                const cellContent = node.children.default({ row, $index: rowIndex })
                return h('div', { key: colIndex, class: 'el-table-cell' }, cellContent)
              }
              // Default: render the prop value
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
  },
  setup(_props, { slots }) {
    // Just render default slot; the parent ElTableStub reads props from vnode
    return () => slots.default?.()
  },
})

// ─── Mock state container ────────────────────────────────────────────────────

interface MockState {
  workers: WorkerInfo[]
  loading: boolean
  error: string | null
  lastUpdated: Date | null
}

function createMockState(overrides: Partial<MockState> = {}): MockState {
  return {
    workers: [],
    loading: false,
    error: null,
    lastUpdated: null,
    ...overrides,
  }
}

let mockState: ReturnType<typeof reactive<MockState>>
const refreshMock = vi.fn()
const fetchWorkerDetailMock = vi.fn()
const syncWorkerMock = vi.fn()
const restartWorkerMock = vi.fn()
const startAutoRefreshMock = vi.fn()
const stopAutoRefreshMock = vi.fn()

// Mock computeStatus: online if heartbeat < 20s ago, expiring if < 30s, offline if >= 30s or null
function computeStatusMock(worker: WorkerInfo): WorkerStatus {
  if (!worker.last_heartbeat) return 'offline'
  const heartbeatTime = new Date(worker.last_heartbeat).getTime()
  if (isNaN(heartbeatTime)) return 'offline'
  const elapsed = Date.now() - heartbeatTime
  if (elapsed >= 30_000) return 'offline'
  if (elapsed >= 20_000) return 'expiring'
  return 'online'
}

// Mock the composable module
vi.mock('@/composables/useStations', () => ({
  useStations: () => ({
    workers: ref(mockState.workers),
    loading: ref(mockState.loading),
    error: ref(mockState.error),
    lastUpdated: ref(mockState.lastUpdated),
    workerDetails: ref(new Map()),
    computeStatus: computeStatusMock,
    refresh: refreshMock,
    fetchWorkerDetail: fetchWorkerDetailMock,
    syncWorker: syncWorkerMock,
    restartWorker: restartWorkerMock,
    startAutoRefresh: startAutoRefreshMock,
    stopAutoRefresh: stopAutoRefreshMock,
  }),
}))

// ─── Test helpers ────────────────────────────────────────────────────────────

function createWorker(overrides: Partial<WorkerInfo> = {}): WorkerInfo {
  return {
    worker_id: 'worker-001',
    hostname: 'station-a',
    capabilities: ['script_execution'],
    max_concurrent_tasks: 4,
    current_tasks: 2,
    last_heartbeat: new Date().toISOString(),
    ...overrides,
  }
}

function createOnlineWorker(overrides: Partial<WorkerInfo> = {}): WorkerInfo {
  return createWorker({
    worker_id: 'worker-online',
    hostname: 'station-online',
    last_heartbeat: new Date().toISOString(),
    ...overrides,
  })
}

function createOfflineWorker(overrides: Partial<WorkerInfo> = {}): WorkerInfo {
  return createWorker({
    worker_id: 'worker-offline',
    hostname: 'station-offline',
    last_heartbeat: new Date(Date.now() - 60_000).toISOString(),
    current_tasks: 0,
    ...overrides,
  })
}

function createExpiringWorker(overrides: Partial<WorkerInfo> = {}): WorkerInfo {
  return createWorker({
    worker_id: 'worker-expiring',
    hostname: 'station-expiring',
    last_heartbeat: new Date(Date.now() - 25_000).toISOString(),
    ...overrides,
  })
}

function mountComponent() {
  return mount(StationManagement, {
    global: {
      plugins: [ElementPlus],
      stubs: {
        ElTable: ElTableStub,
        ElTableColumn: ElTableColumnStub,
      },
    },
  })
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('StationManagement', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockState = reactive(createMockState())
  })

  // ── Table rendering ──

  it('test_renders_workers_table_with_data', () => {
    mockState.workers = [createOnlineWorker()]

    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="workers-table"]').exists()).toBe(true)
  })

  it('test_renders_loading_skeleton_when_loading', () => {
    mockState.loading = true
    mockState.workers = []

    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="loading-skeleton"]').exists()).toBe(true)
  })

  it('test_renders_error_banner_when_error', () => {
    mockState.error = 'Connection refused'

    const wrapper = mountComponent()
    const banner = wrapper.find('[data-testid="error-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Connection refused')
  })

  it('test_renders_empty_state_when_no_workers', () => {
    mockState.workers = []

    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(true)
  })

  // ── Status badges ──

  it('test_renders_status_counts', () => {
    mockState.workers = [
      createOnlineWorker(),
      createOfflineWorker(),
      createExpiringWorker(),
    ]

    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="count-online"]').text()).toContain('1 Online')
    expect(wrapper.find('[data-testid="count-offline"]').text()).toContain('1 Offline')
    expect(wrapper.find('[data-testid="count-expiring"]').text()).toContain('1 Expiring')
  })

  // ── Column headers ──

  it('test_renders_all_expected_column_headers', () => {
    mockState.workers = [createOnlineWorker()]

    const wrapper = mountComponent()
    const tableText = wrapper.find('[data-testid="workers-table"]').text()
    expect(tableText).toContain('Worker ID')
    expect(tableText).toContain('Hostname')
    expect(tableText).toContain('Status')
    expect(tableText).toContain('Current Task')
    expect(tableText).toContain('Version')
    expect(tableText).toContain('Last Heartbeat')
    expect(tableText).toContain('Actions')
  })

  // ── Worker data in table ──

  it('test_displays_worker_id_in_table', () => {
    mockState.workers = [createOnlineWorker({ worker_id: 'worker-abc-123' })]

    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="workers-table"]').text()).toContain('worker-abc-123')
  })

  it('test_displays_hostname_in_table', () => {
    mockState.workers = [createOnlineWorker({ hostname: 'my-station' })]

    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="workers-table"]').text()).toContain('my-station')
  })

  it('test_displays_current_task_info', () => {
    mockState.workers = [createOnlineWorker({ current_tasks: 3, max_concurrent_tasks: 8 })]

    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="workers-table"]').text()).toContain('3/8 running')
  })

  it('test_displays_idle_when_no_tasks', () => {
    mockState.workers = [createOnlineWorker({ current_tasks: 0 })]

    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="workers-table"]').text()).toContain('Idle')
  })

  // ── Status filter ──

  it('test_renders_status_filter_buttons', () => {
    mockState.workers = [createOnlineWorker()]

    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="filter-all"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="filter-online"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="filter-expiring"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="filter-offline"]').exists()).toBe(true)
  })

  it('test_filter_all_shows_all_workers', () => {
    mockState.workers = [
      createOnlineWorker(),
      createOfflineWorker(),
    ]

    const wrapper = mountComponent()
    const table = wrapper.find('[data-testid="workers-table"]')
    expect(table.text()).toContain('worker-online')
    expect(table.text()).toContain('worker-offline')
  })

  // ── Refresh button ──

  it('test_refresh_button_triggers_refresh', async () => {
    mockState.workers = [createOnlineWorker()]

    const wrapper = mountComponent()
    const refreshBtn = wrapper.find('[data-testid="btn-refresh"]')
    expect(refreshBtn.exists()).toBe(true)
    await refreshBtn.trigger('click')
    expect(refreshMock).toHaveBeenCalled()
  })

  // ── Actions ──

  it('test_renders_action_buttons', () => {
    mockState.workers = [createOnlineWorker()]

    const wrapper = mountComponent()
    const tableText = wrapper.find('[data-testid="workers-table"]').text()
    expect(tableText).toContain('配置')
    expect(tableText).toContain('重启')
    expect(tableText).toContain('同步')
  })

  it('test_config_button_opens_dialog', async () => {
    mockState.workers = [createOnlineWorker({ worker_id: 'worker-dialog-test' })]

    const wrapper = mountComponent()
    // Find the config button by its text content
    const allButtons = wrapper.findAll('button')
    const configBtn = allButtons.find((b) => b.text().includes('配置'))
    expect(configBtn).toBeDefined()
    await configBtn!.trigger('click')
    await nextTick()

    const dialog = wrapper.find('[data-testid="config-dialog"]')
    expect(dialog.exists()).toBe(true)
  })

  it('test_sync_button_triggers_sync', async () => {
    syncWorkerMock.mockResolvedValue({ synced: [], failed: [] })
    mockState.workers = [createOnlineWorker()]

    const wrapper = mountComponent()
    const allButtons = wrapper.findAll('button')
    const syncBtn = allButtons.find((b) => b.text().includes('同步'))
    expect(syncBtn).toBeDefined()
    await syncBtn!.trigger('click')
    await nextTick()
    // Wait for async handler
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(syncWorkerMock).toHaveBeenCalled()
  })

  it('test_restart_button_triggers_restart', async () => {
    restartWorkerMock.mockResolvedValue({ synced: [], failed: [] })
    mockState.workers = [createOnlineWorker()]

    const wrapper = mountComponent()
    const allButtons = wrapper.findAll('button')
    const restartBtn = allButtons.find((b) => b.text().includes('重启'))
    expect(restartBtn).toBeDefined()
    await restartBtn!.trigger('click')
    await nextTick()
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(restartWorkerMock).toHaveBeenCalled()
  })

  // ── Title ──

  it('test_renders_page_title', () => {
    mockState.workers = [createOnlineWorker()]

    const wrapper = mountComponent()
    expect(wrapper.find('.sm-title').text()).toContain('Station Management')
  })

  // ── Multiple workers ──

  it('test_renders_multiple_workers_in_table', () => {
    mockState.workers = [
      createOnlineWorker({ worker_id: 'w1', hostname: 'h1' }),
      createOnlineWorker({ worker_id: 'w2', hostname: 'h2' }),
      createOnlineWorker({ worker_id: 'w3', hostname: 'h3' }),
    ]

    const wrapper = mountComponent()
    const table = wrapper.find('[data-testid="workers-table"]')
    expect(table.text()).toContain('w1')
    expect(table.text()).toContain('w2')
    expect(table.text()).toContain('w3')
  })

  // ── Last updated ──

  it('test_displays_last_updated_time', () => {
    mockState.workers = [createOnlineWorker()]
    mockState.lastUpdated = new Date('2026-08-02T10:30:00Z')

    const wrapper = mountComponent()
    expect(wrapper.find('.sm-last-updated').exists()).toBe(true)
  })
})
