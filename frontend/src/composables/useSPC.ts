/**
 * useSPC — composable wrapping SPC API calls with reactive state.
 *
 * Provides:
 * - Reactive refs for statistics, chart, and alerts data.
 * - `loading` and `error` refs for UI state.
 * - `refresh()` — fetches statistics + chart + alerts in parallel.
 * - `load()` — fetch data for a specific product/measurement stream.
 * - Auto-refresh every 30s via `setInterval`, cleaned up on unmount.
 */
import { onMounted, onUnmounted, ref } from 'vue'
import {
  getSPCStatistics,
  getSPCChart,
  getSPCAlerts,
  type SPCStatistics,
  type SPCChart,
  type SPCAlert,
} from '@/api/spc'

/** Refresh interval in milliseconds (30s). */
const REFRESH_INTERVAL_MS = 30_000

export function useSPC() {
  // ─── State ────────────────────────────────────────────────────────────────
  const statistics = ref<SPCStatistics | null>(null)
  const chart = ref<SPCChart | null>(null)
  const alerts = ref<SPCAlert[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  /** Currently selected product type. */
  const productType = ref<string>('')
  /** Currently selected measurement name. */
  const measurementName = ref<string>('')

  let refreshTimer: ReturnType<typeof setInterval> | null = null

  // ─── Actions ──────────────────────────────────────────────────────────────

  /** Fetch statistics, chart, and alerts in parallel for the current selection. */
  async function refresh(): Promise<void> {
    if (!productType.value || !measurementName.value) return
    loading.value = true
    error.value = null
    try {
      const [stats, chrt, alrts] = await Promise.all([
        getSPCStatistics(productType.value, measurementName.value),
        getSPCChart(productType.value, measurementName.value),
        getSPCAlerts(),
      ])
      statistics.value = stats
      chart.value = chrt
      alerts.value = alrts
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  /** Load SPC data for a specific product type and measurement name. */
  async function load(pt: string, mn: string): Promise<void> {
    productType.value = pt
    measurementName.value = mn
    await refresh()
  }

  /** Fetch alerts only (lightweight, for dashboard widget). */
  async function refreshAlerts(): Promise<void> {
    try {
      alerts.value = await getSPCAlerts()
    } catch {
      // Silently skip — alerts are non-critical
    }
  }

  /** Start auto-refresh loop. Safe to call multiple times. */
  function startAutoRefresh(): void {
    if (refreshTimer !== null) return
    refreshTimer = setInterval(() => {
      void refresh()
    }, REFRESH_INTERVAL_MS)
  }

  /** Stop auto-refresh loop. Safe to call when not running. */
  function stopAutoRefresh(): void {
    if (refreshTimer !== null) {
      clearInterval(refreshTimer)
      refreshTimer = null
    }
  }

  // ─── Lifecycle ────────────────────────────────────────────────────────────
  onMounted(() => {
    if (productType.value && measurementName.value) {
      void refresh()
    }
    startAutoRefresh()
  })

  onUnmounted(() => {
    stopAutoRefresh()
  })

  return {
    statistics,
    chart,
    alerts,
    loading,
    error,
    productType,
    measurementName,
    refresh,
    load,
    refreshAlerts,
    startAutoRefresh,
    stopAutoRefresh,
  }
}
