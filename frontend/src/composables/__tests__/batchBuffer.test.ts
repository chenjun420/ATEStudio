/**
 * Unit tests for BatchBuffer — state update batching with 50ms window + dedup.
 *
 * Tests cover:
 * - Single push → flush after window
 * - Multiple pushes to same nodeId → dedup (only latest applied)
 * - Multiple pushes to different nodeIds → all in one flush
 * - Immediate flush on maxBatchSize exceed
 * - Flush before window (explicit)
 * - destroy() flushes remaining and prevents further pushes
 * - Stats tracking (eventsReceived, batchesFlushed, avgBatchSize)
 * - rAF-aligned batching (using fake timers)
 * - Empty flush is a no-op
 * - Pushing after destroy is a no-op
 * - Configurable windowMs and maxBatchSize
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { BatchBuffer, createBatchBuffer, type FlushCallback } from '@/composables/batchBuffer'

describe('BatchBuffer', () => {
  let flushCallback: ReturnType<typeof vi.fn<FlushCallback>>
  let rafCallbacks: Array<FrameRequestCallback>

  beforeEach(() => {
    flushCallback = vi.fn<FlushCallback>()
    rafCallbacks = []

    // Mock requestAnimationFrame / cancelAnimationFrame
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb: FrameRequestCallback): number => {
      rafCallbacks.push(cb)
      return rafCallbacks.length // return a non-zero handle
    })

    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation((_handle: number) => {
      // no-op
    })

    // Mock performance.now
    vi.spyOn(performance, 'now').mockReturnValue(0)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  /**
   * Flush all pending rAF callbacks, advancing time by `ms` each call.
   */
  function flushRaf(msPerTick: number = 50): void {
    while (rafCallbacks.length > 0) {
      const cb = rafCallbacks.shift()!
      // Advance mock time
      vi.mocked(performance.now).mockReturnValue(
        (performance.now() || 0) + msPerTick,
      )
      cb(performance.now())
    }
  }

  // ─────────────────────────────────────────────
  // Construction and basic properties
  // ─────────────────────────────────────────────

  it('creates with default config (50ms window, 200 maxBatchSize)', () => {
    const buffer = new BatchBuffer(flushCallback)
    expect(buffer.size).toBe(0)
    expect(buffer.stats.value).toEqual({
      eventsReceived: 0,
      batchesFlushed: 0,
      avgBatchSize: 0,
    })
    buffer.destroy()
  })

  it('creates with custom windowMs and maxBatchSize', () => {
    const buffer = new BatchBuffer(flushCallback, {
      windowMs: 100,
      maxBatchSize: 50,
    })
    // Push 1 event — should not flush immediately
    buffer.push('node-1', { status: 'running' })
    expect(flushCallback).not.toHaveBeenCalled()
    buffer.destroy()
  })

  it('createBatchBuffer factory works identically', () => {
    const buffer = createBatchBuffer(flushCallback, { windowMs: 30 })
    expect(buffer.size).toBe(0)
    buffer.destroy()
  })

  // ─────────────────────────────────────────
  // Dedup: same nodeId within window
  // ─────────────────────────────────────────

  it('deduplicates multiple pushes to same nodeId within window', () => {
    const buffer = new BatchBuffer(flushCallback, { windowMs: 50 })

    // 10 rapid updates to the same node
    buffer.push('step-1', { status: 'running' })
    buffer.push('step-1', { status: 'running' })
    buffer.push('step-1', { status: 'passed' })
    buffer.push('step-1', { status: 'running' })
    buffer.push('step-1', { status: 'failed' }) // last one wins
    buffer.push('step-1', { status: 'running' })
    buffer.push('step-1', { status: 'running' })
    buffer.push('step-1', { status: 'passed' })
    buffer.push('step-1', { status: 'running' })
    buffer.push('step-1', { status: 'passed' }) // latest

    // Advance time past window
    vi.mocked(performance.now).mockReturnValue(50)
    flushRaf(50)

    expect(flushCallback).toHaveBeenCalledTimes(1)
    const updates: Map<string, Record<string, unknown>> = flushCallback.mock.calls[0][0]
    expect(updates.size).toBe(1)
    expect(updates.get('step-1')).toEqual({ status: 'passed' })

    expect(buffer.stats.value.eventsReceived).toBe(10)
    expect(buffer.stats.value.batchesFlushed).toBe(1)
    expect(buffer.stats.value.avgBatchSize).toBe(1)

    buffer.destroy()
  })

  it('deduplicates when multiple nodeIds updated', () => {
    const buffer = new BatchBuffer(flushCallback, { windowMs: 50 })

    buffer.push('step-1', { status: 'running' })
    buffer.push('step-2', { status: 'passed' })
    buffer.push('step-1', { status: 'failed' }) // overwrites step-1
    buffer.push('step-3', { status: 'skipped' })
    buffer.push('step-2', { status: 'running' }) // overwrites step-2

    vi.mocked(performance.now).mockReturnValue(50)
    flushRaf(50)

    expect(flushCallback).toHaveBeenCalledTimes(1)
    const updates: Map<string, Record<string, unknown>> = flushCallback.mock.calls[0][0]
    expect(updates.size).toBe(3)
    expect(updates.get('step-1')).toEqual({ status: 'failed' })
    expect(updates.get('step-2')).toEqual({ status: 'running' })
    expect(updates.get('step-3')).toEqual({ status: 'skipped' })

    buffer.destroy()
  })

  // ─────────────────────────────────────────
  // 10 nodes, 1 flush
  // ─────────────────────────────────────────

  it('batches 10 updates to 10 different nodes into 1 flush', () => {
    const buffer = new BatchBuffer(flushCallback, { windowMs: 50 })

    for (let i = 0; i < 10; i++) {
      buffer.push(`node-${i}`, { status: 'passed' })
    }

    expect(flushCallback).not.toHaveBeenCalled()
    expect(buffer.size).toBe(10)

    vi.mocked(performance.now).mockReturnValue(50)
    flushRaf(50)

    expect(flushCallback).toHaveBeenCalledTimes(1)
    const updates: Map<string, Record<string, unknown>> = flushCallback.mock.calls[0][0]
    expect(updates.size).toBe(10)
    for (let i = 0; i < 10; i++) {
      expect(updates.get(`node-${i}`)).toEqual({ status: 'passed' })
    }

    expect(buffer.stats.value.eventsReceived).toBe(10)
    expect(buffer.stats.value.batchesFlushed).toBe(1)
    expect(buffer.stats.value.avgBatchSize).toBe(10)

    buffer.destroy()
  })

  // ─────────────────────────────────────────
  // maxBatchSize: immediate flush
  // ─────────────────────────────────────────

  it('flushes immediately when buffer exceeds maxBatchSize', () => {
    const buffer = new BatchBuffer(flushCallback, {
      windowMs: 50,
      maxBatchSize: 5,
    })

    // Push 4 — should not flush
    for (let i = 0; i < 4; i++) {
      buffer.push(`node-${i}`, { status: 'running' })
    }
    expect(flushCallback).not.toHaveBeenCalled()

    // Push 5th — should trigger immediate flush (size >= maxBatchSize)
    buffer.push('node-4', { status: 'passed' })
    expect(flushCallback).toHaveBeenCalledTimes(1)

    const updates: Map<string, Record<string, unknown>> = flushCallback.mock.calls[0][0]
    expect(updates.size).toBe(5)

    buffer.destroy()
  })

  it('flushes immediately on the exact maxBatchSize boundary', () => {
    const buffer = new BatchBuffer(flushCallback, {
      windowMs: 50,
      maxBatchSize: 3,
    })

    buffer.push('a', { x: 1 })
    buffer.push('b', { x: 2 })
    expect(flushCallback).not.toHaveBeenCalled()

    buffer.push('c', { x: 3 })
    expect(flushCallback).toHaveBeenCalledTimes(1)

    buffer.destroy()
  })

  // ─────────────────────────────────────────
  // Multiple batch windows
  // ─────────────────────────────────────────

  it('creates multiple batches across multiple windows', () => {
    const buffer = new BatchBuffer(flushCallback, { windowMs: 50 })

    // First window: 3 nodes
    buffer.push('a', { v: 1 })
    buffer.push('b', { v: 2 })
    buffer.push('c', { v: 3 })

    vi.mocked(performance.now).mockReturnValue(50)
    flushRaf(50)
    expect(flushCallback).toHaveBeenCalledTimes(1)
    expect(flushCallback.mock.calls[0][0].size).toBe(3)

    // Second window: 2 nodes
    buffer.push('d', { v: 4 })
    buffer.push('e', { v: 5 })

    vi.mocked(performance.now).mockReturnValue(100)
    flushRaf(50)
    expect(flushCallback).toHaveBeenCalledTimes(2)
    expect(flushCallback.mock.calls[1][0].size).toBe(2)

    expect(buffer.stats.value.batchesFlushed).toBe(2)
    expect(buffer.stats.value.eventsReceived).toBe(5)
    expect(buffer.stats.value.avgBatchSize).toBe(2.5)

    buffer.destroy()
  })

  // ─────────────────────────────────────────
  // Explicit flush()
  // ─────────────────────────────────────────

  it('flush() immediately fires callback with accumulated data', () => {
    const buffer = new BatchBuffer(flushCallback, { windowMs: 50 })

    buffer.push('n1', { a: 1 })
    buffer.push('n2', { b: 2 })
    buffer.push('n1', { a: 10 }) // dedup n1

    buffer.flush()

    expect(flushCallback).toHaveBeenCalledTimes(1)
    const updates: Map<string, Record<string, unknown>> = flushCallback.mock.calls[0][0]
    expect(updates.size).toBe(2)
    expect(updates.get('n1')).toEqual({ a: 10 })
    expect(updates.get('n2')).toEqual({ b: 2 })

    expect(buffer.size).toBe(0)

    buffer.destroy()
  })

  it('flush() is a no-op when buffer is empty', () => {
    const buffer = new BatchBuffer(flushCallback, { windowMs: 50 })
    buffer.flush()
    expect(flushCallback).not.toHaveBeenCalled()
    buffer.destroy()
  })

  it('flush() cancels pending rAF timer', () => {
    const buffer = new BatchBuffer(flushCallback, { windowMs: 50 })
    buffer.push('n1', { x: 1 })

    // Flush before timer fires
    buffer.flush()
    expect(flushCallback).toHaveBeenCalledTimes(1)

    // Now advance time — should NOT fire again
    vi.mocked(performance.now).mockReturnValue(50)
    flushRaf(50)
    expect(flushCallback).toHaveBeenCalledTimes(1)

    buffer.destroy()
  })

  // ─────────────────────────────────────────
  // destroy()
  // ─────────────────────────────────────────

  it('destroy() flushes remaining data and prevents further pushes', () => {
    const buffer = new BatchBuffer(flushCallback, { windowMs: 50 })

    buffer.push('a', { x: 1 })
    buffer.push('b', { x: 2 })

    buffer.destroy()

    expect(flushCallback).toHaveBeenCalledTimes(1)
    expect(flushCallback.mock.calls[0][0].size).toBe(2)

    // Further pushes are no-ops
    buffer.push('c', { x: 3 })
    expect(flushCallback).toHaveBeenCalledTimes(1)
    expect(buffer.size).toBe(0)

    // Further flushes are no-ops
    buffer.flush()
    expect(flushCallback).toHaveBeenCalledTimes(1)
  })

  // ─────────────────────────────────────────
  // Stats tracking
  // ─────────────────────────────────────────

  it('tracks stats correctly across multiple flushes', () => {
    const buffer = new BatchBuffer(flushCallback, { windowMs: 50 })

    // Batch 1: 3 events
    buffer.push('a', {})
    buffer.push('b', {})
    buffer.push('c', {})
    vi.mocked(performance.now).mockReturnValue(50)
    flushRaf(50)

    // Batch 2: 1 event (deduped from 2 pushes)
    buffer.push('d', {})
    buffer.push('d', {})
    vi.mocked(performance.now).mockReturnValue(100)
    flushRaf(50)

    // Batch 3: 5 events
    buffer.push('e', {})
    buffer.push('f', {})
    buffer.push('g', {})
    buffer.push('h', {})
    buffer.push('i', {})
    vi.mocked(performance.now).mockReturnValue(150)
    flushRaf(50)

    expect(buffer.stats.value.eventsReceived).toBe(10)
    expect(buffer.stats.value.batchesFlushed).toBe(3)
    // avgBatchSize = (3 + 1 + 5) / 3 ≈ 3
    expect(buffer.stats.value.avgBatchSize).toBeCloseTo(3, 0)

    buffer.destroy()
  })

  it('stats ref is reactive (new object on each update)', () => {
    const buffer = new BatchBuffer(flushCallback, { windowMs: 50 })
    const statsRef = buffer.stats

    const firstValue = statsRef.value
    buffer.push('a', {})
    buffer.flush()

    // After flush, stats should be a new object
    expect(statsRef.value).not.toBe(firstValue)
    expect(statsRef.value.eventsReceived).toBe(1)
    expect(statsRef.value.batchesFlushed).toBe(1)

    buffer.destroy()
  })

  // ─────────────────────────────────────────
  // Error handling in flush callback
  // ─────────────────────────────────────────

  it('handles errors in flush callback gracefully', () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const throwingCallback: FlushCallback = () => {
      throw new Error('flush error')
    }

    const buffer = new BatchBuffer(throwingCallback, { windowMs: 50 })
    buffer.push('a', { x: 1 })
    buffer.flush()

    // Should not throw
    expect(consoleSpy).toHaveBeenCalledWith(
      '[BatchBuffer] flush callback error:',
      expect.any(Error),
    )
    expect(buffer.size).toBe(0) // Buffer cleared despite error

    consoleSpy.mockRestore()
    buffer.destroy()
  })

  // ─────────────────────────────────────────
  // Merge behavior (shallow merge)
  // ─────────────────────────────────────────

  it('shallow-merges data for the same nodeId', () => {
    const buffer = new BatchBuffer(flushCallback, { windowMs: 50 })

    buffer.push('n1', { status: 'running', count: 1 })
    buffer.push('n1', { status: 'passed' }) // overwrites status, count preserved via shallow merge
    buffer.push('n1', { count: 5 }) // overwrites count, status preserved

    vi.mocked(performance.now).mockReturnValue(50)
    flushRaf(50)

    const updates = flushCallback.mock.calls[0][0] as Map<string, Record<string, unknown>>
    // Shallow merge: { ...existing, ...data } — latest value for each key wins
    expect(updates.get('n1')).toEqual({ status: 'passed', count: 5 })

    buffer.destroy()
  })

  it('different nodeIds maintain independent data', () => {
    const buffer = new BatchBuffer(flushCallback, { windowMs: 50 })

    buffer.push('n1', { a: 1 })
    buffer.push('n2', { b: 2 })
    buffer.push('n1', { c: 3 }) // shallow merge: { a: 1, c: 3 }

    vi.mocked(performance.now).mockReturnValue(50)
    flushRaf(50)

    const updates = flushCallback.mock.calls[0][0] as Map<string, Record<string, unknown>>
    // Shallow merge preserves existing keys
    expect(updates.get('n1')).toEqual({ a: 1, c: 3 })
    expect(updates.get('n2')).toEqual({ b: 2 })

    buffer.destroy()
  })

  // ─────────────────────────────────────────
  // Configurable parameters
  // ─────────────────────────────────────────

  it('respects custom windowMs', () => {
    const buffer = new BatchBuffer(flushCallback, { windowMs: 100 })

    buffer.push('a', {})

    // Fire one rAF tick at 50ms — should NOT flush (50 < 100 window)
    vi.mocked(performance.now).mockReturnValue(50)
    // Only fire the first rAF callback (don't process chained callbacks)
    const firstCallback = rafCallbacks.shift()
    if (firstCallback) firstCallback(50)
    expect(flushCallback).not.toHaveBeenCalled()

    // Fire the chained rAF (scheduled by the first) at 100ms
    vi.mocked(performance.now).mockReturnValue(100)
    const secondCallback = rafCallbacks.shift()
    if (secondCallback) secondCallback(100)
    expect(flushCallback).toHaveBeenCalledTimes(1)

    buffer.destroy()
  })

  it('respects custom maxBatchSize', () => {
    const buffer = new BatchBuffer(flushCallback, { maxBatchSize: 10 })

    for (let i = 0; i < 9; i++) {
      buffer.push(`n-${i}`, {})
    }
    expect(flushCallback).not.toHaveBeenCalled()

    buffer.push('n-9', {})
    expect(flushCallback).toHaveBeenCalledTimes(1)

    buffer.destroy()
  })
})