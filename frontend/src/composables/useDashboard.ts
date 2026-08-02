/**
 * useDashboard — composable wrapping dashboard API calls with reactive state.
 *
 * Provides:
 * - Reactive refs for summary, stations, faults, and executions data.
 * - `loading` and `error` refs for UI state.
 * - `refresh()` — fetches all 4 endpoints in parallel.
 * - Auto-refresh every 30s via `setInterval`, cleaned up on unmount.
 */
import { onMounted, onUnmounted, ref } from 'vue'
import {
  getSummary,
  getStations,
  getFaults,
  getExecutions,
  type DashboardSummary,
  type StationsResponse,
  type FaultsResponse,
  type ExecutionsResponse,
} from '@/api/dashboard'

/** Refresh interval in milliseconds (30s). */
const REFRESH_INTERVAL_MS = 30_000

export function useDashboard() {
  // ─── State ────────────────────────────────────────────────────────────────
  const summary = ref<DashboardSummary | null>(null)
  const stations = ref<StationsResponse | null>(null)
  const faults = ref<FaultsResponse | null>(null)
  const executions = ref<ExecutionsResponse | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  let refreshTimer: ReturnType<typeof setInterval> | null = null

  // ─── Actions ──────────────────────────────────────────────────────────────

  /** Fetch all 4 dashboard endpoints in parallel. */
  async function refresh(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const [s, st, f, e] = await Promise.all([
        getSummary(),
        getStations(),
        getFaults(),
        getExecutions(),
      ])
      summary.value = s
      stations.value = st
      faults.value = f
      executions.value = e
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
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
    void refresh()
    startAutoRefresh()
  })

  onUnmounted(() => {
    stopAutoRefresh()
  })

  return {
    summary,
    stations,
    faults,
    executions,
    loading,
    error,
    refresh,
    startAutoRefresh,
    stopAutoRefresh,
  }
}
