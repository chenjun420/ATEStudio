/**
 * Tests for useSimulationBreakpoints composable (T39, v41-gap-analysis #39).
 *
 * Covers: CRUD wiring, BREAKPOINT_HIT state transitions (paused flag +
 * lastHit + warning toast), resume clearing the paused state, and SSE
 * connect parsing of `event: breakpoint` payloads.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const {
  createTypedBreakpointMock,
  listTypedBreakpointsMock,
  deleteTypedBreakpointMock,
  resumeExecutionMock,
} = vi.hoisted(() => ({
  createTypedBreakpointMock: vi.fn(),
  listTypedBreakpointsMock: vi.fn(),
  deleteTypedBreakpointMock: vi.fn(),
  resumeExecutionMock: vi.fn(),
}))

vi.mock('@/api/executions', () => ({
  createTypedBreakpoint: createTypedBreakpointMock,
  listTypedBreakpoints: listTypedBreakpointsMock,
  deleteTypedBreakpoint: deleteTypedBreakpointMock,
  resumeExecution: resumeExecutionMock,
}))

import { useSimulationBreakpoints } from '../useSimulationBreakpoints'
import type { BreakpointHitPayload } from '@/utils/breakpointTypes'

function hit(overrides: Partial<BreakpointHitPayload> = {}): BreakpointHitPayload {
  return { breakpoint_id: 'bp-1', kind: 'variable_change', target: 'bench.voltage', ...overrides }
}

describe('useSimulationBreakpoints', () => {
  beforeEach(() => {
    ;[createTypedBreakpointMock, listTypedBreakpointsMock, deleteTypedBreakpointMock, resumeExecutionMock]
      .forEach((m) => m.mockReset())
  })

  it('handleHit flips paused=true and records lastHit', () => {
    const notify = vi.fn()
    const bp = useSimulationBreakpoints(notify)
    expect(bp.paused.value).toBe(false)
    bp.handleHit(hit())
    expect(bp.paused.value).toBe(true)
    expect(bp.lastHit.value?.breakpoint_id).toBe('bp-1')
    expect(bp.lastHit.value?.kind).toBe('variable_change')
  })

  it('handleHit emits a warning toast describing the hit', () => {
    const notify = vi.fn()
    const bp = useSimulationBreakpoints(notify)
    bp.handleHit(hit({ kind: 'step', target: 'dmm_read' }))
    expect(notify).toHaveBeenCalledTimes(1)
    const [message, type] = notify.mock.calls[0]
    expect(type).toBe('warning')
    expect(message).toContain('step')
    expect(message).toContain('dmm_read')
  })

  it('resume calls the API and clears paused/lastHit', async () => {
    resumeExecutionMock.mockResolvedValue({ id: 'run-1', status: 'RESUMING' })
    const notify = vi.fn()
    const bp = useSimulationBreakpoints(notify)
    bp.handleHit(hit())
    await bp.resume('run-1')
    expect(resumeExecutionMock).toHaveBeenCalledWith('run-1')
    expect(bp.paused.value).toBe(false)
    expect(bp.lastHit.value).toBeNull()
    expect(notify).toHaveBeenCalledWith('已恢复执行', 'success')
  })

  it('add validates before calling the API and reloads items', async () => {
    createTypedBreakpointMock.mockResolvedValue({ id: 'bp-9' })
    listTypedBreakpointsMock.mockResolvedValue({ items: [{ id: 'bp-9' }], total: 1 })
    const bp = useSimulationBreakpoints()

    const bad = await bp.add('run-1', { kind: 'condition', target: '*', condition: '' })
    expect(bad.valid).toBe(false)
    expect(createTypedBreakpointMock).not.toHaveBeenCalled()

    const ok = await bp.add('run-1', { kind: 'step', target: 's1', condition: '' })
    expect(ok.valid).toBe(true)
    expect(createTypedBreakpointMock).toHaveBeenCalledWith('run-1', { kind: 'step', target: 's1' })
    expect(listTypedBreakpointsMock).toHaveBeenCalledWith('run-1')
    expect(bp.items.value).toHaveLength(1)
  })

  it('remove deletes by run+id then refreshes the list', async () => {
    deleteTypedBreakpointMock.mockResolvedValue(undefined)
    listTypedBreakpointsMock.mockResolvedValue({ items: [], total: 0 })
    const bp = useSimulationBreakpoints()
    await bp.remove('run-1', 'bp-3')
    expect(deleteTypedBreakpointMock).toHaveBeenCalledWith('run-1', 'bp-3')
    expect(bp.items.value).toHaveLength(0)
  })

  it('load skips empty run ids without touching the API', async () => {
    const bp = useSimulationBreakpoints()
    await bp.load('')
    expect(listTypedBreakpointsMock).not.toHaveBeenCalled()
  })

  it('connect parses a breakpoint SSE event into handleHit', async () => {
    listTypedBreakpointsMock.mockResolvedValue({ items: [], total: 0 })
    const listeners = new Map<string, (e: MessageEvent<string>) => void>()
    class FakeEventSource {
      addEventListener(name: string, cb: (e: MessageEvent<string>) => void) {
        listeners.set(name, cb)
      }
      close() {}
    }
    vi.stubGlobal('EventSource', FakeEventSource)

    const notify = vi.fn()
    const bp = useSimulationBreakpoints(notify)
    bp.connect('run-sse')
    listeners.get('breakpoint')!(
      new MessageEvent<string>('breakpoint', {
        data: JSON.stringify(hit({ kind: 'instrument_call', target: 'PSU_MAIN.set_voltage' })),
      }),
    )
    expect(bp.paused.value).toBe(true)
    expect(bp.lastHit.value?.target).toBe('PSU_MAIN.set_voltage')

    // Malformed payload must not throw nor change state.
    expect(() =>
      listeners.get('breakpoint')!(new MessageEvent<string>('breakpoint', { data: '{oops' })),
    ).not.toThrow()
    expect(bp.lastHit.value?.target).toBe('PSU_MAIN.set_voltage')
    vi.unstubAllGlobals()
  })

  it('connect is a no-op for empty run ids and missing EventSource', () => {
    const bp = useSimulationBreakpoints()
    expect(() => bp.connect('')).not.toThrow()
    vi.stubGlobal('EventSource', undefined)
    expect(() => bp.connect('run-x')).not.toThrow()
    vi.unstubAllGlobals()
  })
})
