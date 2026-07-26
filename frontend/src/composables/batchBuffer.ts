import { ref, type Ref } from 'vue'

/**
 * Partial node data to be merged into a node during batch flush.
 * Keyed by nodeId (step_id from SSE events).
 */
export interface BatchEntry {
  /** Merged partial data to apply to the node */
  data: Record<string, unknown>
}

/**
 * Debug statistics for the batch buffer.
 */
export interface BatchStats {
  /** Total number of events received since creation */
  eventsReceived: number
  /** Total number of flush operations executed */
  batchesFlushed: number
  /** Rolling average batch size across all flushes */
  avgBatchSize: number
}

/**
 * Flush callback signature — decoupled from graph implementation.
 * Receives the full accumulated map and is responsible for applying updates.
 *
 * @param updates - Map of nodeId → merged data to apply
 */
export type FlushCallback = (updates: Map<string, Record<string, unknown>>) => void

/**
 * Configuration for the BatchBuffer.
 */
export interface BatchBufferConfig {
  /** Batching window in milliseconds (default: 50) */
  windowMs?: number
  /** Maximum buffer size before forced flush (default: 200) */
  maxBatchSize?: number
}

const DEFAULT_WINDOW_MS = 50
const DEFAULT_MAX_BATCH_SIZE = 200

/**
 * BatchBuffer accumulates node data updates and flushes them in batches
 * aligned with requestAnimationFrame for optimal rendering performance.
 *
 * ## Behavior
 * - `push(nodeId, data)` merges data into the accumulated buffer (latest wins per nodeId)
 * - Every `windowMs` (default 50ms), `flush()` fires the callback with all accumulated changes
 * - If buffer exceeds `maxBatchSize` (default 200) before the window elapses, flushes immediately
 * - Uses `requestAnimationFrame` for render-frame-aligned batching
 * - `flush()` is a no-op if the buffer is empty
 *
 * ## Usage
 * ```ts
 * const buffer = new BatchBuffer((updates) => {
 *   // Apply all accumulated updates to graph nodes
 *   for (const [nodeId, data] of updates) {
 *     const node = graph.getCellById(nodeId)
 *     if (node) node.setData(data, { silent: true })
 *   }
 * })
 *
 * buffer.push('step-1', { status: 'running' })
 * buffer.push('step-2', { status: 'passed' })
 * // After 50ms (or 200 entries), flushCallback fires once with both updates
 * ```
 */
export class BatchBuffer {
  /** Accumulated updates: nodeId → merged data */
  private _buffer: Map<string, Record<string, unknown>> = new Map()

  /** Callback invoked on each flush */
  private _flushCallback: FlushCallback

  /** Batching window in ms */
  private _windowMs: number

  /** Maximum buffer size before forced flush */
  private _maxBatchSize: number

  /** rAF handle for the pending flush timer */
  private _rafId: number | null = null

  /** Timestamp when the current batch window started (ms since epoch) */
  private _windowStart: number = 0

  /** Whether the buffer has been destroyed */
  private _destroyed = false

  /** Reactive stats for debugging */
  readonly stats: Ref<BatchStats>

  /** Internal mutable stats (to avoid recreating the ref object) */
  private _statsValue: BatchStats

  constructor(
    flushCallback: FlushCallback,
    config: BatchBufferConfig = {},
  ) {
    this._flushCallback = flushCallback
    this._windowMs = config.windowMs ?? DEFAULT_WINDOW_MS
    this._maxBatchSize = config.maxBatchSize ?? DEFAULT_MAX_BATCH_SIZE
    this._windowStart = performance.now()

    this._statsValue = {
      eventsReceived: 0,
      batchesFlushed: 0,
      avgBatchSize: 0,
    }
    this.stats = ref<BatchStats>({ ...this._statsValue })
  }

  /**
   * Push a data update for a node into the buffer.
   *
   * If the same nodeId is pushed multiple times within the same window,
   * the data is merged (shallow merge — latest value wins per key).
   *
   * Triggers immediate flush if maxBatchSize is exceeded.
   *
   * @param nodeId - Unique identifier for the node (typically step_id from SSE)
   * @param data - Partial data to merge into the node's current data
   */
  push(nodeId: string, data: Record<string, unknown>): void {
    if (this._destroyed) return

    // Merge: shallow merge — latest value for each key wins
    const existing = this._buffer.get(nodeId)
    this._buffer.set(nodeId, { ...existing, ...data })

    this._statsValue.eventsReceived++

    // Start the flush timer if this is the first entry in the window
    if (this._rafId === null) {
      this._windowStart = performance.now()
      this._scheduleFlush()
    }

    // Force immediate flush if buffer exceeds max size
    if (this._buffer.size >= this._maxBatchSize) {
      this.flush()
    }
  }

  /**
   * Flush all accumulated updates immediately.
   *
   * Calls the flush callback with the full buffer, then clears the buffer.
   * Cancels any pending rAF timer.
   * No-op if the buffer is empty or the instance is destroyed.
   *
   * @param _isDestroy - Internal flag: when true, bypasses the destroyed check
   *   so destroy() can flush remaining data.
   */
  flush(_isDestroy = false): void {
    if (this._destroyed && !_isDestroy) return

    this._cancelTimer()

    if (this._buffer.size === 0) return

    const updates = new Map(this._buffer)
    this._buffer.clear()

    // Update stats
    this._statsValue.batchesFlushed++
    const batchSize = updates.size
    if (this._statsValue.batchesFlushed === 1) {
      this._statsValue.avgBatchSize = batchSize
    } else {
      // Rolling average: (old_avg * (n-1) + new) / n
      this._statsValue.avgBatchSize =
        (this._statsValue.avgBatchSize * (this._statsValue.batchesFlushed - 1) + batchSize) /
        this._statsValue.batchesFlushed
    }
    this.stats.value = { ...this._statsValue }

    // Fire callback synchronously — caller is responsible for any async work
    try {
      this._flushCallback(updates)
    } catch (err) {
      console.error('[BatchBuffer] flush callback error:', err)
    }
  }

  /**
   * Destroy the buffer — cancels pending timers, flushes remaining data,
   * and prevents further pushes.
   */
  destroy(): void {
    if (this._destroyed) return
    this._destroyed = true
    this.flush(true)
  }

  /**
   * Get the current buffer size (for testing/debugging).
   */
  get size(): number {
    return this._buffer.size
  }

  // ── Private helpers ────────────────────────────────────────────

  /**
   * Schedule a flush after the batching window, aligned with rAF.
   */
  private _scheduleFlush(): void {
    if (this._destroyed) return

    // Use requestAnimationFrame for render-frame alignment
    this._rafId = requestAnimationFrame(() => {
      this._rafId = null
      if (this._destroyed) return

      const elapsed = performance.now() - this._windowStart
      if (elapsed >= this._windowMs) {
        // Window elapsed — flush now
        this.flush()
      } else {
        // Still within window — wait until window completes
        // Schedule another rAF aligned to the remaining time
        this._rafId = requestAnimationFrame(() => {
          this._rafId = null
          if (this._destroyed) return

          const totalElapsed = performance.now() - this._windowStart
          if (totalElapsed >= this._windowMs) {
            this.flush()
          } else {
            // Still too early — use setTimeout for precise remaining time
            const remaining = this._windowMs - totalElapsed
            this._rafId = window.setTimeout(() => {
              this._rafId = null
              if (this._destroyed) return
              this.flush()
            }, remaining) as unknown as number
          }
        })
      }
    })
  }

  /**
   * Cancel the pending flush timer (rAF or setTimeout).
   */
  private _cancelTimer(): void {
    if (this._rafId !== null) {
      // Could be rAF or setTimeout — try both cancellation methods
      cancelAnimationFrame(this._rafId)
      clearTimeout(this._rafId)
      this._rafId = null
    }
  }
}

/**
 * Create a BatchBuffer with the given flush callback and optional config.
 *
 * Convenience factory — identical to `new BatchBuffer(callback, config)`.
 */
export function createBatchBuffer(
  flushCallback: FlushCallback,
  config?: BatchBufferConfig,
): BatchBuffer {
  return new BatchBuffer(flushCallback, config)
}