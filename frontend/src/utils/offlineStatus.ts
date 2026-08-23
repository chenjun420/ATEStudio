/**
 * Offline status pure logic (T43, v41-gap-analysis #43, doc §10.5).
 *
 * Types mirror the T24 wire contract (src/ate_cloud/api/v1/offline.py):
 *   GET  /api/v1/offline/status        -> OfflineStatusSnapshot
 *   GET  /api/v1/offline/status/stream -> SSE `offline_status` frames
 *                                         (frame 0 = instant snapshot)
 *   POST /api/v1/offline/reconcile     -> ReconcileReport (202)
 *
 * ZERO DOM / ZERO Vue in this module — formatters, thresholds and the SSE
 * frame reducer are pure so vitest exercises them without a component.
 */

// ─── Wire types (T24 contract) ──────────────────────────────────────────────

/** GET /offline/status → cache_health{} */
export interface OfflineCacheHealth {
  /** Total cached bytes on disk. */
  size_bytes: number
  /** Age of the oldest cached record in hours (null when cache empty). */
  oldest_record_age_h: number | null
  /** size_bytes as percent of the configured capacity budget. */
  capacity_pct: number
  /** True when the hard threshold paused new downloads. */
  downloads_paused: boolean
}

/** GET /offline/status and every SSE `offline_status` frame. */
export interface OfflineStatusSnapshot {
  /** Station connectivity per heartbeat monitor. */
  online: boolean
  /** Records awaiting upload+ACK. */
  pending_upload_count: number
  cache_health: OfflineCacheHealth
}

/** POST /offline/reconcile → 202 report summary (path-free). */
export interface ReconcileQuarantineItem {
  reason: string
  station_id: string | null
  execution_id: string | null
  seq_no: number | null
  kind: string | null
  entry_id: string | null
  version: string | null
}

export interface ReconcileReport {
  ok: boolean
  uploaded: number
  acked: number
  confirmed_entries: number
  conflicts_resolved: number
  quarantined: number
  locks_released: number
  duration: number
  quarantine: ReconcileQuarantineItem[]
}

// ─── UI state ───────────────────────────────────────────────────────────────

/** Capacity bar level: ok < 70% ≤ warn < 90% ≤ full. */
export type CapacityLevel = 'ok' | 'warn' | 'full'

export const CAPACITY_WARN_PCT = 70
export const CAPACITY_FULL_PCT = 90

/**
 * Reducer state for the offline badge: last known snapshot plus stream
 * health. `connected === false` means the SSE stream dropped — the UI falls
 * back to slow polling and shows a subtle hint, never an error toast.
 */
export interface OfflineStatusState {
  status: OfflineStatusSnapshot | null
  connected: boolean
  lastUpdateAt: number | null
}

export function initialOfflineStatusState(): OfflineStatusState {
  return { status: null, connected: false, lastUpdateAt: null }
}

// ─── Formatters ─────────────────────────────────────────────────────────────

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'] as const

/**
 * Human-readable bytes (binary 1024 base). Negative/NaN sizes clamp to
 * "0 B" — degraded data must never render as a scary value.
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  let value = bytes
  let unitIdx = 0
  while (value >= 1024 && unitIdx < BYTE_UNITS.length - 1) {
    value /= 1024
    unitIdx += 1
  }
  if (unitIdx === 0) return `${Math.round(value)} B`
  return `${value.toFixed(1)} ${BYTE_UNITS[unitIdx]}`
}

function trimNum(n: number): string {
  const rounded = Math.round(n * 10) / 10
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1)
}

/**
 * Oldest-record age: minutes below 1h, hours up to 48h, days beyond.
 * `null` (empty cache) renders as an em dash.
 */
export function formatAgeHours(hours: number | null): string {
  if (hours == null || !Number.isFinite(hours)) return '—'
  if (hours < 1) return `${Math.max(0, Math.round(hours * 60))} 分钟`
  if (hours < 48) return `${trimNum(hours)} 小时`
  return `${trimNum(hours / 24)} 天`
}

/**
 * capacity_pct → level. Boundaries: <70 ok, 70–<90 warn, ≥90 full.
 * Negative and non-finite percentages degrade to 'ok' (never scary).
 */
export function capacityLevel(pct: number): CapacityLevel {
  if (!Number.isFinite(pct) || pct < CAPACITY_WARN_PCT) return 'ok'
  if (pct < CAPACITY_FULL_PCT) return 'warn'
  return 'full'
}

// ─── SSE frame reducer ──────────────────────────────────────────────────────

function isCacheHealth(v: unknown): v is OfflineCacheHealth {
  if (typeof v !== 'object' || v === null) return false
  const h = v as Record<string, unknown>
  return (
    typeof h.size_bytes === 'number' &&
    (typeof h.oldest_record_age_h === 'number' || h.oldest_record_age_h === null) &&
    typeof h.capacity_pct === 'number' &&
    typeof h.downloads_paused === 'boolean'
  )
}

export function isOfflineStatusFrame(v: unknown): v is OfflineStatusSnapshot {
  if (typeof v !== 'object' || v === null) return false
  const f = v as Record<string, unknown>
  return (
    typeof f.online === 'boolean' &&
    typeof f.pending_upload_count === 'number' &&
    isCacheHealth(f.cache_health)
  )
}

/**
 * Fold one SSE `offline_status` frame into the UI state. Malformed frames
 * are ignored (state returned untouched) so a bad payload never blanks the
 * badge mid-shift. Frame 0 (instant snapshot) and later updates share this
 * path — both are complete snapshots per the T24 contract.
 *
 * `now` is injectable for deterministic tests.
 */
export function reduceOfflineStatusFrame(
  state: OfflineStatusState,
  frame: unknown,
  now: number = Date.now(),
): OfflineStatusState {
  if (!isOfflineStatusFrame(frame)) return state
  return { status: frame, connected: true, lastUpdateAt: now }
}

/**
 * Mark the SSE stream as disconnected (onerror) without touching the last
 * known snapshot — the badge keeps its last honest values while the UI
 * degrades to fallback polling.
 */
export function setStreamConnected(
  state: OfflineStatusState,
  connected: boolean,
  now: number = Date.now(),
): OfflineStatusState {
  return { ...state, connected, lastUpdateAt: now }
}
