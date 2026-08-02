/**
 * Tests for Dashboard.vue view component.
 *
 * Verifies:
 * - All 5 widget cards render with correct data-testid attributes.
 * - Loading skeleton renders when data is loading.
 * - Error alert renders when API fails.
 * - Active station count statistic renders from summary data.
 * - Today's execution count statistic renders from summary data.
 * - Recent executions list renders from executions data.
 * - Empty state for recent executions when no data.
 * - Refresh button triggers refresh() call.
 * - Canvas elements exist for charts (fault trend, yield gauge, top faults).
 *
 * The composable `useDashboard` is mocked to return controlled reactive
 * state, avoiding real API calls.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref, reactive } from 'vue'
import ElementPlus from 'element-plus'
import Dashboard from '../Dashboard.vue'
import type {
  DashboardSummary,
  StationsResponse,
  FaultsResponse,
  ExecutionsResponse,
} from '@/api/dashboard'

// ─── Mock state container ────────────────────────────────────────────────────

interface MockState {
  summary: DashboardSummary | null
  stations: StationsResponse | null
  faults: FaultsResponse | null
  executions: ExecutionsResponse | null
  loading: boolean
  error: string | null
}

function createMockState(overrides: Partial<MockState> = {}): MockState {
  return {
    summary: null,
    stations: null,
    faults: null,
    executions: null,
    loading: false,
    error: null,
    ...overrides,
  }
}

let mockState: ReturnType<typeof reactive<MockState>>
const refreshMock = vi.fn()
const startAutoRefreshMock = vi.fn()
const stopAutoRefreshMock = vi.fn()

// Mock the composable module
vi.mock('@/composables/useDashboard', () => ({
  useDashboard: () => ({
    summary: ref(mockState.summary),
    stations: ref(mockState.stations),
    faults: ref(mockState.faults),
    executions: ref(mockState.executions),
    loading: ref(mockState.loading),
    error: ref(mockState.error),
    refresh: refreshMock,
    startAutoRefresh: startAutoRefreshMock,
    stopAutoRefresh: stopAutoRefreshMock,
  }),
}))

// ─── Test helpers ────────────────────────────────────────────────────────────

function createSummary(overrides: Partial<DashboardSummary> = {}): DashboardSummary {
  return {
    active_workers: 3,
    total_executions_today: 10,
    completed_today: 8,
    failed_today: 2,
    pass_rate: 80.0,
    total_faults: 5,
    ...overrides,
  }
}

function createExecutions(overrides: Partial<ExecutionsResponse> = {}): ExecutionsResponse {
  return {
    total: 10,
    by_status: { COMPLETED: 8, FAILED: 2 },
    recent: [
      { id: 'r1', status: 'COMPLETED', sequence_id: 'seq-1', started_at: null, completed_at: null },
      { id: 'r2', status: 'FAILED', sequence_id: 'seq-2', started_at: null, completed_at: null },
    ],
    ...overrides,
  }
}

function createFaults(overrides: Partial<FaultsResponse> = {}): FaultsResponse {
  return {
    trend: [
      { hour: '2026-08-02T00:00:00+00:00', count: 0 },
      { hour: '2026-08-02T01:00:00+00:00', count: 2 },
      { hour: '2026-08-02T02:00:00+00:00', count: 1 },
    ],
    top_faults: [
      { category: 'voltage_check', count: 5 },
      { category: 'current_test', count: 3 },
    ],
    ...overrides,
  }
}

function createStations(overrides: Partial<StationsResponse> = {}): StationsResponse {
  return {
    stations: [
      {
        worker_id: 'worker-1',
        hostname: 'station-a',
        status: 'online',
        capabilities: ['measure'],
        current_tasks: 2,
        max_concurrent_tasks: 4,
      },
    ],
    total: 1,
    ...overrides,
  }
}

function mountComponent() {
  return mount(Dashboard, {
    global: {
      plugins: [ElementPlus],
    },
  })
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('Dashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockState = reactive(createMockState())
  })

  // ── Widget rendering ──

  it('test_renders_all_5_widget_cards', () => {
    mockState.summary = createSummary()
    mockState.executions = createExecutions()
    mockState.faults = createFaults()
    mockState.stations = createStations()

    const wrapper = mountComponent()

    expect(wrapper.find('[data-testid="widget-active-stations"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="widget-fault-trend"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="widget-yield-gauge"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="widget-executions-today"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="widget-top-faults"]').exists()).toBe(true)
  })

  it('test_renders_loading_skeleton_when_loading', () => {
    mockState.loading = true
    mockState.summary = null

    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="dashboard-skeleton"]').exists()).toBe(true)
  })

  it('test_renders_error_alert_when_error', () => {
    mockState.error = 'Connection refused'

    const wrapper = mountComponent()
    const alert = wrapper.find('[data-testid="error-alert"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('Connection refused')
  })

  // ── Active stations widget ──

  it('test_renders_active_station_count', () => {
    mockState.summary = createSummary({ active_workers: 5 })
    mockState.stations = createStations()

    const wrapper = mountComponent()
    const stat = wrapper.find('[data-testid="stat-active-stations"]')
    expect(stat.exists()).toBe(true)
    expect(stat.text()).toContain('5')
  })

  it('test_renders_station_count_in_detail', () => {
    mockState.summary = createSummary()
    mockState.stations = createStations({ total: 2, stations: [
      { worker_id: 'w1', hostname: 'a', status: 'online', capabilities: [], current_tasks: 0, max_concurrent_tasks: 1 },
      { worker_id: 'w2', hostname: 'b', status: 'online', capabilities: [], current_tasks: 0, max_concurrent_tasks: 1 },
    ] })

    const wrapper = mountComponent()
    const widget = wrapper.find('[data-testid="widget-active-stations"]')
    expect(widget.text()).toContain('2 station(s)')
  })

  // ── Today's executions widget ──

  it('test_renders_today_execution_count', () => {
    mockState.summary = createSummary({ total_executions_today: 15 })
    mockState.executions = createExecutions({ total: 15 })

    const wrapper = mountComponent()
    const stat = wrapper.find('[data-testid="stat-executions-today"]')
    expect(stat.exists()).toBe(true)
    expect(stat.text()).toContain('15')
  })

  it('test_renders_status_tags_in_executions_widget', () => {
    mockState.summary = createSummary()
    mockState.executions = createExecutions({
      by_status: { COMPLETED: 8, FAILED: 2, RUNNING: 1 },
    })

    const wrapper = mountComponent()
    const widget = wrapper.find('[data-testid="widget-executions-today"]')
    expect(widget.text()).toContain('8 done')
    expect(widget.text()).toContain('2 failed')
  })

  // ── Recent executions list ──

  it('test_renders_recent_executions_list', () => {
    mockState.summary = createSummary()
    mockState.executions = createExecutions({
      recent: [
        { id: 'abc123-def456', status: 'COMPLETED', sequence_id: 'seq-1', started_at: null, completed_at: null },
        { id: 'xyz789-abc012', status: 'FAILED', sequence_id: 'seq-2', started_at: null, completed_at: null },
      ],
    })

    const wrapper = mountComponent()
    const list = wrapper.find('[data-testid="exec-list"]')
    expect(list.exists()).toBe(true)
    expect(list.text()).toContain('abc123-d')
    expect(list.text()).toContain('COMPLETED')
    expect(list.text()).toContain('FAILED')
  })

  it('test_renders_empty_state_for_recent_executions', () => {
    mockState.summary = createSummary()
    mockState.executions = createExecutions({ recent: [], total: 0 })

    const wrapper = mountComponent()
    const empty = wrapper.find('[data-testid="empty-recent-executions"]')
    expect(empty.exists()).toBe(true)
  })

  // ── Chart canvases ──

  it('test_renders_canvas_elements_for_charts', () => {
    mockState.summary = createSummary()
    mockState.faults = createFaults()
    mockState.executions = createExecutions()

    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="canvas-fault-trend"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="canvas-yield-gauge"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="canvas-top-faults"]').exists()).toBe(true)
  })

  // ── Refresh button ──

  it('test_refresh_button_triggers_refresh', async () => {
    mockState.summary = createSummary()
    mockState.executions = createExecutions()

    const wrapper = mountComponent()
    const refreshBtn = wrapper.find('[data-testid="btn-refresh"]')
    expect(refreshBtn.exists()).toBe(true)
    await refreshBtn.trigger('click')
    expect(refreshMock).toHaveBeenCalled()
  })

  // ── Title ──

  it('test_renders_dashboard_title', () => {
    mockState.summary = createSummary()
    mockState.executions = createExecutions()

    const wrapper = mountComponent()
    expect(wrapper.find('.dash-title').text()).toContain('Production Dashboard')
  })
})
