/**
 * Tests for SPCCharts.vue component.
 *
 * Verifies:
 * - Renders chart cards when data is provided.
 * - Renders loading skeleton when loading.
 * - Renders empty state when no data.
 * - Canvas elements exist for X-bar, R chart, and Cpk gauge.
 * - Outlier count tag renders when outliers are detected.
 * - Cpk stats row renders correct values.
 * - Cpk color zones computed correctly for different Cpk values.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import SPCCharts from '../SPCCharts.vue'
import type { SPCChart, SPCStatistics } from '@/api/spc'

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
      { index: 3, mean: 4.99, range: 0.11, sample_count: 5 },
      { index: 4, mean: 5.35, range: 0.13, sample_count: 5 }, // outlier: beyond UCL
      { index: 5, mean: 5.00, range: 0.15, sample_count: 5 },
      { index: 6, mean: 4.97, range: 0.09, sample_count: 5 },
      { index: 7, mean: 5.03, range: 0.16, sample_count: 5 },
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

function mountComponent(props: Record<string, unknown>) {
  return mount(SPCCharts, {
    props,
    global: {
      plugins: [ElementPlus],
    },
  })
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('SPCCharts', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  // ── Chart card rendering ──

  it('test_renders_xbar_chart_card', () => {
    const wrapper = mountComponent({
      chart: createChart(),
      statistics: createStatistics(),
    })
    expect(wrapper.find('[data-testid="card-xbar-chart"]').exists()).toBe(true)
  })

  it('test_renders_r_chart_card', () => {
    const wrapper = mountComponent({
      chart: createChart(),
      statistics: createStatistics(),
    })
    expect(wrapper.find('[data-testid="card-r-chart"]').exists()).toBe(true)
  })

  it('test_renders_cpk_gauge_card', () => {
    const wrapper = mountComponent({
      chart: createChart(),
      statistics: createStatistics(),
    })
    expect(wrapper.find('[data-testid="card-cpk-gauge"]').exists()).toBe(true)
  })

  // ── Canvas elements ──

  it('test_renders_canvas_elements_for_all_charts', () => {
    const wrapper = mountComponent({
      chart: createChart(),
      statistics: createStatistics(),
    })
    expect(wrapper.find('[data-testid="canvas-xbar-chart"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="canvas-r-chart"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="canvas-cpk-gauge"]').exists()).toBe(true)
  })

  // ── Loading state ──

  it('test_renders_loading_skeleton_when_loading', () => {
    const wrapper = mountComponent({
      chart: null,
      statistics: null,
      loading: true,
    })
    expect(wrapper.find('[data-testid="spc-loading"]').exists()).toBe(true)
  })

  // ── Empty state ──

  it('test_renders_empty_state_when_no_data', () => {
    const wrapper = mountComponent({
      chart: null,
      statistics: null,
    })
    expect(wrapper.find('[data-testid="spc-empty"]').exists()).toBe(true)
  })

  // ── Outlier detection ──

  it('test_renders_outlier_count_tag_when_outliers_exist', () => {
    const wrapper = mountComponent({
      chart: createChart(), // has one outlier at index 4 (mean=5.35 > ucl=5.3)
      statistics: createStatistics(),
    })
    const tag = wrapper.find('[data-testid="xbar-outlier-count"]')
    expect(tag.exists()).toBe(true)
    expect(tag.text()).toContain('outlier')
  })

  it('test_does_not_render_outlier_tag_when_no_outliers', () => {
    const chart = createChart({
      subgroups: [
        { index: 0, mean: 5.0, range: 0.12, sample_count: 5 },
        { index: 1, mean: 5.01, range: 0.10, sample_count: 5 },
        { index: 2, mean: 4.99, range: 0.14, sample_count: 5 },
        { index: 3, mean: 5.0, range: 0.11, sample_count: 5 },
        { index: 4, mean: 5.01, range: 0.13, sample_count: 5 },
        { index: 5, mean: 5.0, range: 0.15, sample_count: 5 },
        { index: 6, mean: 4.99, range: 0.09, sample_count: 5 },
        { index: 7, mean: 5.0, range: 0.16, sample_count: 5 },
      ],
    })
    const wrapper = mountComponent({
      chart,
      statistics: createStatistics(),
    })
    expect(wrapper.find('[data-testid="xbar-outlier-count"]').exists()).toBe(false)
  })

  // ── Cpk stats row ──

  it('test_renders_cpk_stats_row_with_values', () => {
    const wrapper = mountComponent({
      chart: createChart(),
      statistics: createStatistics({ cpk: 1.5, cp: 1.6, ppk: 1.4, mean: 5.0, sample_count: 40 }),
    })
    const statsRow = wrapper.find('[data-testid="spc-stats-row"]')
    expect(statsRow.exists()).toBe(true)
    expect(statsRow.text()).toContain('1.500')
    expect(statsRow.text()).toContain('1.600')
    expect(statsRow.text()).toContain('1.400')
    expect(statsRow.text()).toContain('5.0000')
    expect(statsRow.text()).toContain('40')
  })

  // ── Cpk color zones ──

  it('test_cpk_red_zone_when_below_1', () => {
    const wrapper = mountComponent({
      chart: createChart(),
      statistics: createStatistics({ cpk: 0.8 }),
    })
    const statsRow = wrapper.find('[data-testid="spc-stats-row"]')
    expect(statsRow.html()).toContain('rgb(239, 68, 68)')
    expect(statsRow.text()).toContain('Incapable')
  })

  it('test_cpk_yellow_zone_when_between_1_and_1_33', () => {
    const wrapper = mountComponent({
      chart: createChart(),
      statistics: createStatistics({ cpk: 1.2 }),
    })
    const statsRow = wrapper.find('[data-testid="spc-stats-row"]')
    expect(statsRow.html()).toContain('rgb(245, 158, 11)')
    expect(statsRow.text()).toContain('Marginal')
  })

  it('test_cpk_green_zone_when_above_1_33', () => {
    const wrapper = mountComponent({
      chart: createChart(),
      statistics: createStatistics({ cpk: 1.5 }),
    })
    const statsRow = wrapper.find('[data-testid="spc-stats-row"]')
    expect(statsRow.html()).toContain('rgb(16, 185, 129)')
    expect(statsRow.text()).toContain('Capable')
  })

  // ── Cpk N/A handling ──

  it('test_cpk_displays_na_when_null', () => {
    const wrapper = mountComponent({
      chart: createChart(),
      statistics: createStatistics({ cpk: null }),
    })
    const statsRow = wrapper.find('[data-testid="spc-stats-row"]')
    expect(statsRow.text()).toContain('N/A')
  })

  // ── Charts grid rendering ──

  it('test_renders_charts_grid_when_data_available', () => {
    const wrapper = mountComponent({
      chart: createChart(),
      statistics: createStatistics(),
    })
    expect(wrapper.find('[data-testid="spc-charts-grid"]').exists()).toBe(true)
  })

  it('test_does_not_render_charts_grid_when_no_data', () => {
    const wrapper = mountComponent({
      chart: null,
      statistics: null,
    })
    expect(wrapper.find('[data-testid="spc-charts-grid"]').exists()).toBe(false)
  })

  // ── Root element ──

  it('test_renders_root_container', () => {
    const wrapper = mountComponent({
      chart: createChart(),
      statistics: createStatistics(),
    })
    expect(wrapper.find('[data-testid="spc-charts"]').exists()).toBe(true)
  })
})
