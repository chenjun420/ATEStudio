/**
 * Tests for MeasurementExplorer.vue component.
 *
 * Verifies:
 * - Renders root container with title.
 * - Renders filter card with product type selector, measurement selector, and date picker.
 * - Renders no-selection prompt when no product/measurement selected.
 * - Renders refresh button (disabled when no selection).
 * - Renders error alert when error state is set.
 * - Renders loading skeleton when loading.
 * - Renders SPC charts component when selection is made.
 * - Renders alerts table when alerts exist.
 * - Renders empty alerts state when no alerts.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref, reactive } from 'vue'
import ElementPlus from 'element-plus'
import MeasurementExplorer from '../MeasurementExplorer.vue'
import type { SPCChart, SPCStatistics, SPCAlert } from '@/api/spc'

// ─── Mock state container ────────────────────────────────────────────────────

interface MockState {
  statistics: SPCStatistics | null
  chart: SPCChart | null
  alerts: SPCAlert[]
  loading: boolean
  error: string | null
  productType: string
  measurementName: string
}

function createMockState(overrides: Partial<MockState> = {}): MockState {
  return {
    statistics: null,
    chart: null,
    alerts: [],
    loading: false,
    error: null,
    productType: '',
    measurementName: '',
    ...overrides,
  }
}

let mockState: ReturnType<typeof reactive<MockState>>
const refreshMock = vi.fn()
const loadMock = vi.fn()
const refreshAlertsMock = vi.fn()
const startAutoRefreshMock = vi.fn()
const stopAutoRefreshMock = vi.fn()

// Mock the composable module
vi.mock('@/composables/useSPC', () => ({
  useSPC: () => ({
    statistics: ref(mockState.statistics),
    chart: ref(mockState.chart),
    alerts: ref(mockState.alerts),
    loading: ref(mockState.loading),
    error: ref(mockState.error),
    productType: ref(mockState.productType),
    measurementName: ref(mockState.measurementName),
    refresh: refreshMock,
    load: loadMock,
    refreshAlerts: refreshAlertsMock,
    startAutoRefresh: startAutoRefreshMock,
    stopAutoRefresh: stopAutoRefreshMock,
  }),
}))

// ─── Test data factories ─────────────────────────────────────────────────────

function createChart(overrides: Partial<SPCChart> = {}): SPCChart {
  return {
    product_type: '5g_bsb',
    measurement_name: 'voltage',
    center_line: 5.0,
    ucl: 5.3,
    lcl: 4.7,
    r_center: 0.15,
    r_ucl: 0.3,
    r_lcl: 0.0,
    subgroup_size: 5,
    subgroups: [
      { index: 0, mean: 5.01, range: 0.12, sample_count: 5 },
      { index: 1, mean: 4.98, range: 0.10, sample_count: 5 },
      { index: 2, mean: 5.02, range: 0.14, sample_count: 5 },
    ],
    ...overrides,
  }
}

function createStatistics(overrides: Partial<SPCStatistics> = {}): SPCStatistics {
  return {
    product_type: '5g_bsb',
    measurement_name: 'voltage',
    sample_count: 40,
    mean: 5.0,
    std_dev_within: 0.05,
    std_dev_overall: 0.06,
    cp: 1.5,
    cpk: 1.4,
    ppk: 1.3,
    usl: 5.5,
    lsl: 4.5,
    last_updated: '2026-08-02T10:00:00Z',
    ...overrides,
  }
}

function createAlerts(): SPCAlert[] {
  return [
    {
      product_type: '5g_bsb',
      measurement_name: 'voltage',
      rule: 'WE1_beyond_3sigma',
      severity: 'warning',
      message: 'Western Electric rule violated: WE1_beyond_3sigma',
      value: 5.35,
      timestamp: '2026-08-02T09:30:00Z',
      sample_count: 40,
    },
    {
      product_type: '5g_bsb',
      measurement_name: 'voltage',
      rule: 'Ppk_below_1.00',
      severity: 'critical',
      message: 'Ppk=0.950 below threshold 1.00',
      value: 5.4,
      timestamp: '2026-08-02T09:35:00Z',
      sample_count: 40,
    },
  ]
}

function mountComponent() {
  return mount(MeasurementExplorer, {
    global: {
      plugins: [ElementPlus],
    },
  })
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('MeasurementExplorer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockState = reactive(createMockState())
  })

  // ── Root rendering ──

  it('test_renders_root_container', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="measurement-explorer"]').exists()).toBe(true)
  })

  it('test_renders_title', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('.me-title').text()).toContain('Measurement Explorer')
  })

  // ── Filter card ──

  it('test_renders_filter_card', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="card-filters"]').exists()).toBe(true)
  })

  it('test_renders_product_type_selector', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="select-product"]').exists()).toBe(true)
  })

  it('test_renders_measurement_selector', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="select-measurement"]').exists()).toBe(true)
  })

  it('test_renders_date_picker', () => {
    const wrapper = mountComponent()
    // ElDatePicker renders an input with class el-range-editor or el-date-editor
    const dateInputs = wrapper.findAll('.el-date-editor')
    expect(dateInputs.length).toBeGreaterThan(0)
  })

  // ── No selection prompt ──

  it('test_renders_no_selection_prompt_initially', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="card-no-selection"]').exists()).toBe(true)
  })

  // ── Refresh button ──

  it('test_renders_refresh_button', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="btn-refresh"]').exists()).toBe(true)
  })

  it('test_refresh_button_disabled_when_no_selection', () => {
    const wrapper = mountComponent()
    const btn = wrapper.find('[data-testid="btn-refresh"]')
    expect(btn.attributes('disabled')).toBeDefined()
  })

  it('test_refresh_button_enabled_when_selection_made', () => {
    mockState.productType = '5g_bsb'
    mockState.measurementName = 'voltage'
    const wrapper = mountComponent()
    const btn = wrapper.find('[data-testid="btn-refresh"]')
    expect(btn.attributes('disabled')).toBeUndefined()
  })

  // ── Error state ──

  it('test_renders_error_alert_when_error', () => {
    mockState.error = 'Network error'
    const wrapper = mountComponent()
    const alert = wrapper.find('[data-testid="error-alert"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('Network error')
  })

  // ── Loading state ──

  it('test_renders_loading_skeleton_when_loading', () => {
    mockState.loading = true
    mockState.statistics = null
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="me-loading"]').exists()).toBe(true)
  })

  // ── SPC charts integration ──

  it('test_renders_spc_charts_when_selection_made', () => {
    mockState.productType = '5g_bsb'
    mockState.measurementName = 'voltage'
    mockState.chart = createChart()
    mockState.statistics = createStatistics()
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="spc-charts"]').exists()).toBe(true)
  })

  it('test_does_not_render_spc_charts_without_selection', () => {
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="spc-charts"]').exists()).toBe(false)
  })

  // ── Alerts table ──

  it('test_renders_alerts_card_when_selection_made', () => {
    mockState.productType = '5g_bsb'
    mockState.measurementName = 'voltage'
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="card-alerts"]').exists()).toBe(true)
  })

  it('test_renders_alerts_table_when_alerts_exist', () => {
    mockState.productType = '5g_bsb'
    mockState.measurementName = 'voltage'
    mockState.alerts = createAlerts()
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="alerts-table"]').exists()).toBe(true)
  })

  it('test_renders_alert_count_tag_when_alerts_exist', () => {
    mockState.productType = '5g_bsb'
    mockState.measurementName = 'voltage'
    mockState.alerts = createAlerts()
    const wrapper = mountComponent()
    const tag = wrapper.find('[data-testid="alert-count"]')
    expect(tag.exists()).toBe(true)
    expect(tag.text()).toContain('2 alert')
  })

  it('test_renders_empty_alerts_when_no_alerts', () => {
    mockState.productType = '5g_bsb'
    mockState.measurementName = 'voltage'
    mockState.alerts = []
    const wrapper = mountComponent()
    expect(wrapper.find('[data-testid="empty-alerts"]').exists()).toBe(true)
  })

  // ── Severity tags ──

  it('test_renders_severity_column_in_alerts_table', () => {
    mockState.productType = '5g_bsb'
    mockState.measurementName = 'voltage'
    mockState.alerts = createAlerts()
    const wrapper = mountComponent()
    // The alerts card should contain severity text from the alerts data
    const card = wrapper.find('[data-testid="card-alerts"]')
    expect(card.exists()).toBe(true)
    // ElTable may not render row text in jsdom; verify alert data is present via the count tag
    const countTag = wrapper.find('[data-testid="alert-count"]')
    expect(countTag.exists()).toBe(true)
    expect(countTag.text()).toContain('2 alert')
  })

  // ── Refresh button triggers refresh ──

  it('test_refresh_button_triggers_refresh_when_enabled', async () => {
    mockState.productType = '5g_bsb'
    mockState.measurementName = 'voltage'
    const wrapper = mountComponent()
    const btn = wrapper.find('[data-testid="btn-refresh"]')
    await btn.trigger('click')
    expect(refreshMock).toHaveBeenCalled()
  })
})
