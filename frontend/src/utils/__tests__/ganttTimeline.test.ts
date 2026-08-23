/**
 * Tests for the instrument gantt timeline pure layer
 * (T36, v41-gap-analysis #36, 设计文档 §7.6 / §8.4).
 *
 * Contract under test (`utils/ganttTimeline.ts`):
 * - `buildGanttTimeline(events)`: instrument_call events → per-resource call
 *   intervals (start = t, end = t + elapsed_ms/1000), sorted by start with a
 *   stable original-index tiebreak; overlapping same-resource calls packed
 *   into distinct lanes (greedy interval partitioning); malformed events
 *   dropped without fabricating spans; missing elapsed_ms → zero-width.
 * - `generateTicks(totalDuration)`: nice-step ascending ticks from 0.
 *
 * Spec guards covered here:
 * - Must NOT fabricate spans when data missing → malformed events skipped,
 *   empty input yields an empty timeline.
 * - Zero DOM/X6 imports in the pure layer.
 */
import { describe, it, expect } from 'vitest'
import {
  buildGanttTimeline,
  generateTicks,
  type GanttEventInput,
} from '../ganttTimeline'

// ─── Factories ───────────────────────────────────────────────────────────────

/** Console shape: SimulationResultEvent with instrument_call payload in data. */
function consoleEvent(
  idx: number,
  overrides: {
    resource?: string
    method?: string
    t?: number
    elapsed_ms?: number | null
    error?: string | null
  } = {},
): GanttEventInput {
  const { resource = 'DMM1', method = 'read', t = 0, elapsed_ms = 100, error = null } = overrides
  return {
    event_type: 'instrument_call',
    step_id: `step-${idx}`,
    timestamp: new Date(1700000000000 + t * 1000).toISOString(),
    data: { resource, method, t, elapsed_ms, ...(error ? { error } : {}) },
  }
}

/** Flat recording shape: recording.py record_instrument_call output. */
function flatEvent(overrides: {
  resource?: string
  method?: string
  t?: number
  elapsed_ms?: number | null
} = {}): GanttEventInput {
  const { resource = 'DMM1', method = 'read', t = 0, elapsed_ms = 100 } = overrides
  return { kind: 'instrument_call', resource, method, t, elapsed_ms }
}

// ─── Interval derivation ─────────────────────────────────────────────────────

describe('buildGanttTimeline — interval derivation', () => {
  it('derives start/end from console-shape events and sorts calls by start', () => {
    const tl = buildGanttTimeline([
      consoleEvent(0, { resource: 'DMM1', method: 'read', t: 1.5, elapsed_ms: 200 }),
      consoleEvent(1, { resource: 'PSU1', method: 'set_voltage', t: 0.25, elapsed_ms: 50 }),
      consoleEvent(2, { resource: 'DMM1', method: 'conf', t: 0.5, elapsed_ms: 10 }),
    ])
    expect(tl.instruments.map((i) => i.resource)).toEqual(['PSU1', 'DMM1']) // by first start
    const dmm = tl.instruments[1]!
    expect(dmm.calls.map((c) => [c.start, c.end])).toEqual([
      [0.5, 0.51],
      [1.5, 1.7],
    ])
    expect(tl.totalDuration).toBeCloseTo(1.7, 10)
  })

  it('accepts the flat recording.py shape (kind/t/resource at top level)', () => {
    const tl = buildGanttTimeline([flatEvent({ resource: 'PSU1', t: 2, elapsed_ms: 500 })])
    expect(tl.instruments).toHaveLength(1)
    expect(tl.instruments[0]!.calls[0]).toMatchObject({
      start: 2,
      end: 2.5,
      method: 'read',
      lane: 0,
      idx: 0,
    })
  })

  it('missing/invalid elapsed_ms → zero-width interval (end === start)', () => {
    const tl = buildGanttTimeline([
      consoleEvent(0, { elapsed_ms: null }),
      consoleEvent(1, { t: 3, elapsed_ms: -5 }), // negative is invalid too
      consoleEvent(2, { t: 6, elapsed_ms: Number.NaN }),
    ])
    for (const call of tl.instruments[0]!.calls) {
      expect(call.end).toBe(call.start)
    }
    expect(tl.totalDuration).toBeCloseTo(6, 10)
  })

  it('drops malformed events without fabricating spans (no resource / no time)', () => {
    const tl = buildGanttTimeline([
      { event_type: 'instrument_call', data: { method: 'read' } }, // no resource
      { event_type: 'instrument_call', data: { resource: 'X', t: Number.NaN } }, // no valid time
      { event_type: 'decision', data: { resource: 'Y', t: 1 } }, // wrong kind
      consoleEvent(3),
    ] as GanttEventInput[])
    expect(tl.instruments).toHaveLength(1)
    expect(tl.instruments[0]!.resource).toBe('DMM1')
    expect(tl.instruments[0]!.calls).toHaveLength(1)
  })
})

// ─── Lane packing ────────────────────────────────────────────────────────────

describe('buildGanttTimeline — multi-lane packing', () => {
  it('packs overlapping same-resource calls into distinct lanes; sequential reuses lane 0', () => {
    // DMM1: A[0,1] B[0.5,1.5] overlap → lanes 0,1; C[2,2.5] after both → lane 0.
    const tl = buildGanttTimeline([
      consoleEvent(0, { resource: 'DMM1', method: 'a', t: 0, elapsed_ms: 1000 }),
      consoleEvent(1, { resource: 'DMM1', method: 'b', t: 0.5, elapsed_ms: 1000 }),
      consoleEvent(2, { resource: 'DMM1', method: 'c', t: 2, elapsed_ms: 500 }),
    ])
    const dmm = tl.instruments[0]!
    expect(dmm.calls.map((c) => c.lane)).toEqual([0, 1, 0])
    expect(dmm.laneCount).toBe(2)
    expect(tl.lanes).toBe(2)
  })

  it('lane budget is global max across instruments', () => {
    const tl = buildGanttTimeline([
      // PSU1 never overlaps itself → 1 lane; DMM1 overlaps → 2 lanes.
      consoleEvent(0, { resource: 'PSU1', t: 0, elapsed_ms: 100 }),
      consoleEvent(1, { resource: 'DMM1', t: 0, elapsed_ms: 100 }),
      consoleEvent(2, { resource: 'DMM1', t: 0.05, elapsed_ms: 100 }),
    ])
    expect(tl.lanes).toBe(2)
  })
})

// ─── Ordering & empty state ──────────────────────────────────────────────────

describe('buildGanttTimeline — sort stability & empty state', () => {
  it('equal starts keep original input order (stable idx tiebreak)', () => {
    const tl = buildGanttTimeline([
      consoleEvent(0, { resource: 'A', method: 'first', t: 1 }),
      consoleEvent(1, { resource: 'B', method: 'second', t: 1 }),
      consoleEvent(2, { resource: 'C', method: 'third', t: 1 }),
    ])
    expect(tl.instruments.map((i) => i.resource)).toEqual(['A', 'B', 'C'])
    expect(tl.instruments.every((i) => i.calls[0]!.start === 1)).toBe(true)
  })

  it('empty / all-malformed input → empty timeline (no fabricated rows)', () => {
    expect(buildGanttTimeline([])).toEqual({
      instruments: [],
      totalDuration: 0,
      lanes: 0,
    })
    expect(buildGanttTimeline([{ event_type: 'decision' } as GanttEventInput]).instruments).toEqual([])
  })
})

// ─── Tick generation ─────────────────────────────────────────────────────────

describe('generateTicks', () => {
  it('produces ascending uniform nice-step ticks including 0', () => {
    const ticks = generateTicks(1.0)
    expect(ticks[0]).toBe(0)
    expect([...ticks].sort((a, b) => a - b)).toEqual(ticks) // ascending
    const step = ticks[1]! - ticks[0]!
    for (let i = 1; i < ticks.length; i++) {
      expect(ticks[i]! - ticks[i - 1]!).toBeCloseTo(step, 10)
    }
    expect(step).toBeGreaterThan(0)
    expect(ticks.length).toBeLessThanOrEqual(9)
    expect(ticks.length).toBeGreaterThanOrEqual(4)
  })

  it('zero duration collapses to a single tick; float durations get nice steps', () => {
    expect(generateTicks(0)).toEqual([0])
    const ticks = generateTicks(0.35)
    expect(ticks[0]).toBe(0)
    expect(ticks[ticks.length - 1]!).toBeLessThanOrEqual(0.35 + 1e-9)
    // Nice steps only: multiples of 1/2/5 × 10^k.
    const step = ticks[1]! - ticks[0]!
    const mantissa = Number((step / Math.pow(10, Math.floor(Math.log10(step)))).toPrecision(6))
    expect([1, 2, 5]).toContain(mantissa)
  })
})
