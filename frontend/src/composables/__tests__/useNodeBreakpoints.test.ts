/**
 * Tests for useNodeBreakpoints composable (task 23 — SequenceEditor
 * breakpoint toggles).
 *
 * Mirrors useSimulationBreakpoints.test.ts: the typed-breakpoint REST API
 * (@/api/executions) and the ticketed SSE helper (@/utils/sseTicket) are
 * mocked so the tests exercise CRUD wiring, step-target indexing, toggle
 * on/off, BREAKPOINT_HIT state, and SSE `breakpoint` event parsing — the
 * SAME API functions and SSE stream the SimulationConsole uses.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const {
  createTypedBreakpointMock,
  listTypedBreakpointsMock,
  deleteTypedBreakpointMock,
} = vi.hoisted(() => ({
  createTypedBreakpointMock: vi.fn(),
  listTypedBreakpointsMock: vi.fn(),
  deleteTypedBreakpointMock: vi.fn(),
}))

vi.mock('@/api/executions', () => ({
  createTypedBreakpoint: createTypedBreakpointMock,
  listTypedBreakpoints: listTypedBreakpointsMock,
  deleteTypedBreakpoint: deleteTypedBreakpointMock,
}))

// Ticket mechanics are covered by utils/sseTicket.test.ts; stub the seam so
// connect() synchronously opens the (fake) global EventSource and wires the
// breakpoint listener.
vi.mock('@/utils/sseTicket', () => ({
  openTicketedEventSource: (
    path: string,
    handlers: {
      onOpen?: () => void
      onError?: () => void
      listeners?: Record<string, (e: MessageEvent<string>) => void>
    },
  ) => {
    const es = new EventSource(path) as unknown as {
      addEventListener: (t: string, cb: (e: MessageEvent<string>) => void) => void
      close: () => void
    }
    for (const [name, cb] of Object.entries(handlers.listeners ?? {})) {
      es.addEventListener(name, cb)
    }
    return { close: () => es.close() }
  },
}))

import { useNodeBreakpoints } from '../useNodeBreakpoints'
import type { TypedBreakpoint } from '@/api/executions'

function bp(overrides: Partial<TypedBreakpoint> = {}): TypedBreakpoint {
  return {
    id: 'bp-1',
    run_id: 'run-1',
    kind: 'step',
    target: 'step-001',
    condition: null,
    enabled: true,
    ...overrides,
  }
}

describe('useNodeBreakpoints', () => {
  beforeEach(() => {
    ;[createTypedBreakpointMock, listTypedBreakpointsMock, deleteTypedBreakpointMock].forEach(
      (m) => m.mockReset(),
    )
  })

  it('load indexes enabled step breakpoints by target and ignores others', async () => {
    listTypedBreakpointsMock.mockResolvedValue({
      items: [
        bp({ id: 'bp-a', target: 'step-001' }),
        bp({ id: 'bp-b', target: 'step-002', enabled: false }),
        bp({ id: 'bp-c', kind: 'variable_change', target: 'scope.voltage' }),
      ],
      total: 3,
    })
    const nb = useNodeBreakpoints()
    await nb.load('run-1')
    expect(listTypedBreakpointsMock).toHaveBeenCalledWith('run-1')
    expect(nb.isArmed('step-001')).toBe(true)
    expect(nb.armedSteps['step-001']).toBe('bp-a')
    // disabled step breakpoint and non-step kinds are not armed
    expect(nb.isArmed('step-002')).toBe(false)
    expect(nb.isArmed('scope.voltage')).toBe(false)
  })

  it('load skips empty run ids without touching the API', async () => {
    const nb = useNodeBreakpoints()
    await nb.load('')
    expect(listTypedBreakpointsMock).not.toHaveBeenCalled()
  })

  it('toggleStep creates a step breakpoint when absent and arms it', async () => {
    createTypedBreakpointMock.mockResolvedValue(bp({ id: 'bp-new', target: 'step-009' }))
    const notify = vi.fn()
    const nb = useNodeBreakpoints(notify)

    const armed = await nb.toggleStep('run-1', 'step-009')

    expect(armed).toBe(true)
    expect(createTypedBreakpointMock).toHaveBeenCalledWith('run-1', {
      kind: 'step',
      target: 'step-009',
    })
    expect(nb.isArmed('step-009')).toBe(true)
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('step-009'), 'success')
  })

  it('toggleStep deletes the breakpoint when armed and disarms the step', async () => {
    listTypedBreakpointsMock.mockResolvedValue({
      items: [bp({ id: 'bp-1', target: 'step-001' })],
      total: 1,
    })
    deleteTypedBreakpointMock.mockResolvedValue(undefined)
    const nb = useNodeBreakpoints()
    await nb.load('run-1')
    expect(nb.isArmed('step-001')).toBe(true)

    const armed = await nb.toggleStep('run-1', 'step-001')

    expect(armed).toBe(false)
    expect(deleteTypedBreakpointMock).toHaveBeenCalledWith('run-1', 'bp-1')
    expect(nb.isArmed('step-001')).toBe(false)
  })

  it('toggleStep without a run id warns and never calls the API', async () => {
    const notify = vi.fn()
    const nb = useNodeBreakpoints(notify)
    const armed = await nb.toggleStep('', 'step-001')
    expect(armed).toBe(false)
    expect(createTypedBreakpointMock).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('运行'), 'warning')
  })

  it('toggleStep surfaces API failures as an error toast without flipping state', async () => {
    createTypedBreakpointMock.mockRejectedValue(new Error('409 terminal'))
    const notify = vi.fn()
    const nb = useNodeBreakpoints(notify)
    const armed = await nb.toggleStep('run-1', 'step-001')
    expect(armed).toBe(false)
    expect(nb.isArmed('step-001')).toBe(false)
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('409 terminal'), 'error')
  })

  it('handleHit records the hit step for step-kind hits only', () => {
    const notify = vi.fn()
    const nb = useNodeBreakpoints(notify)

    nb.handleHit({ breakpoint_id: 'bp-1', kind: 'step', target: 'step-002' })
    expect(nb.hitStep.value).toBe('step-002')
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('step-002'), 'warning')

    // Non-step hits still notify (pause happened) but do not highlight a node.
    nb.clearHit()
    nb.handleHit({ breakpoint_id: 'bp-2', kind: 'variable_change', target: 'scope.v' })
    expect(nb.hitStep.value).toBeNull()

    nb.clearHit()
    expect(nb.hitStep.value).toBeNull()
  })

  it('connect parses a breakpoint SSE event into handleHit', () => {
    const listeners = new Map<string, (e: MessageEvent<string>) => void>()
    class FakeEventSource {
      addEventListener(name: string, cb: (e: MessageEvent<string>) => void) {
        listeners.set(name, cb)
      }
      close() {}
    }
    vi.stubGlobal('EventSource', FakeEventSource)

    const nb = useNodeBreakpoints()
    nb.connect('run-sse')
    expect(listeners.has('breakpoint')).toBe(true)

    listeners.get('breakpoint')!(
      new MessageEvent<string>('breakpoint', {
        data: JSON.stringify({ breakpoint_id: 'bp-9', kind: 'step', target: 'step-007' }),
      }),
    )
    expect(nb.hitStep.value).toBe('step-007')

    // Malformed payload must not throw nor change state.
    expect(() =>
      listeners.get('breakpoint')!(new MessageEvent<string>('breakpoint', { data: '{oops' })),
    ).not.toThrow()
    expect(nb.hitStep.value).toBe('step-007')

    nb.disconnect()
    vi.unstubAllGlobals()
  })

  it('connect is a no-op for empty run ids and missing EventSource', () => {
    const nb = useNodeBreakpoints()
    expect(() => nb.connect('')).not.toThrow()
    vi.stubGlobal('EventSource', undefined)
    expect(() => nb.connect('run-x')).not.toThrow()
    vi.unstubAllGlobals()
  })
})
