/**
 * useStations — composable wrapping worker/station API calls with reactive state.
 *
 * Provides:
 * - Reactive refs for workers list, loading, and error states.
 * - `refresh()` — fetches the workers list.
 * - `fetchWorkerDetail(workerId)` — fetches health + history for a worker.
 * - `syncWorker(workerId)` / `restartWorker(workerId)` — action wrappers.
 * - Auto-refresh every 15s via `setInterval`, cleaned up on unmount.
 */
import { onMounted, onUnmounted, ref } from 'vue'
import {
  getWorkers,
  getWorkerHealth,
  getWorkerHistory,
  syncWorker as apiSyncWorker,
  restartWorker as apiRestartWorker,
  type WorkerInfo,
  type WorkerHealthResponse,
  type HeartbeatHistoryResponse,
  type WorkerSyncResponse,
} from '@/api/stations'

/** Refresh interval in milliseconds (15s). */
const REFRESH_INTERVAL_MS = 15_000

/** Heartbeat TTL in milliseconds (30s). Workers expiring within this window are "expiring". */
const HEARTBEAT_TTL_MS = 30_000

/** Threshold (ms) before TTL expiry to consider a worker "expiring". */
const EXPIRING_THRESHOLD_MS = 10_000

export type WorkerStatus = 'online' | 'offline' | 'expiring'

export interface WorkerDetail {
  health: WorkerHealthResponse | null
  history: HeartbeatHistoryResponse | null
  loading: boolean
  error: string | null
}

export function useStations() {
  // ─── State ────────────────────────────────────────────────────────────────
  const workers = ref<WorkerInfo[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const lastUpdated = ref<Date | null>(null)

  /** Per-worker detail cache (keyed by worker_id). */
  const workerDetails = ref<Map<string, WorkerDetail>>(new Map())

  let refreshTimer: ReturnType<typeof setInterval> | null = null

  // ─── Derived helpers ──────────────────────────────────────────────────────

  /**
   * Determine a worker's display status from its last_heartbeat timestamp.
   * - `online`: heartbeat within (TTL - threshold) ms
   * - `expiring`: heartbeat between (TTL - threshold) and TTL
   * - `offline`: heartbeat older than TTL, or null
   */
  function computeStatus(worker: WorkerInfo): WorkerStatus {
    if (!worker.last_heartbeat) return 'offline'
    const heartbeatTime = new Date(worker.last_heartbeat).getTime()
    if (isNaN(heartbeatTime)) return 'offline'
    const elapsed = Date.now() - heartbeatTime
    if (elapsed >= HEARTBEAT_TTL_MS) return 'offline'
    if (elapsed >= HEARTBEAT_TTL_MS - EXPIRING_THRESHOLD_MS) return 'expiring'
    return 'online'
  }

  // ─── Actions ──────────────────────────────────────────────────────────────

  /** Fetch the workers list. */
  async function refresh(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const response = await getWorkers()
      workers.value = response.workers
      lastUpdated.value = new Date()
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  /** Fetch detailed health + history for a specific worker. */
  async function fetchWorkerDetail(workerId: string): Promise<void> {
    const existing = workerDetails.value.get(workerId)
    if (existing) {
      existing.loading = true
      existing.error = null
    } else {
      workerDetails.value.set(workerId, {
        health: null,
        history: null,
        loading: true,
        error: null,
      })
    }
    // Trigger reactivity
    workerDetails.value = new Map(workerDetails.value)

    try {
      const [health, history] = await Promise.all([
        getWorkerHealth(workerId),
        getWorkerHistory(workerId, 50),
      ])
      const detail = workerDetails.value.get(workerId)
      if (detail) {
        detail.health = health
        detail.history = history
        detail.loading = false
        detail.error = null
      }
    } catch (e: unknown) {
      const detail = workerDetails.value.get(workerId)
      if (detail) {
        detail.loading = false
        detail.error = e instanceof Error ? e.message : String(e)
      }
    }
    workerDetails.value = new Map(workerDetails.value)
  }

  /** Trigger version sync for a worker. */
  async function syncWorkerAction(workerId: string): Promise<WorkerSyncResponse> {
    const result = await apiSyncWorker(workerId)
    // Refresh the workers list after sync
    void refresh()
    return result
  }

  /** Trigger a worker restart (via sync endpoint). */
  async function restartWorkerAction(workerId: string): Promise<WorkerSyncResponse> {
    const result = await apiRestartWorker(workerId)
    void refresh()
    return result
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
    workers,
    loading,
    error,
    lastUpdated,
    workerDetails,
    computeStatus,
    refresh,
    fetchWorkerDetail,
    syncWorker: syncWorkerAction,
    restartWorker: restartWorkerAction,
    startAutoRefresh,
    stopAutoRefresh,
  }
}
