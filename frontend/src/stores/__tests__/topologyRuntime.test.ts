/**
 * Tests for the topologyRuntime Pinia store（设计文档 §8.3.6）。
 *
 * Covers SSE subscription behavior with a mocked global EventSource:
 * - connect() creates EventSource on the topology-stream URL and registers
 *   listeners for instrument/link/relay/measurement/fixture/fault events.
 * - Each SSE event updates the corresponding reactive state.
 * - disconnect() closes the connection; reset() clears runtime state.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

// Ticket mechanics are covered by utils/sseTicket.test.ts; here we stub the
// seam so it synchronously opens the (fake) global EventSource on the raw
// path and wires the store's handlers onto it, keeping this test focused on
// the store's state updates.
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
      onopen: (() => void) | null
      onerror: (() => void) | null
      addEventListener: (t: string, cb: (e: MessageEvent<string>) => void) => void
      close: () => void
    }
    es.onopen = () => handlers.onOpen?.()
    es.onerror = () => handlers.onError?.()
    for (const [name, cb] of Object.entries(handlers.listeners ?? {})) {
      es.addEventListener(name, cb)
    }
    return { close: () => es.close() }
  },
}))

import { useTopologyRuntimeStore } from '../topologyRuntime'

interface MockEventSource {
  url: string
  listeners: Map<string, (e: MessageEvent<string>) => void>
  onopen: (() => void) | null
  onerror: (() => void) | null
  closed: boolean
  addEventListener: (type: string, cb: (e: MessageEvent<string>) => void) => void
  close: () => void
}

let lastMock: MockEventSource | null = null
let instances: MockEventSource[] = []

function installEventSourceMock() {
  class FakeEventSource {
    url: string
    listeners = new Map<string, (e: MessageEvent<string>) => void>()
    onopen: (() => void) | null = null
    onerror: (() => void) | null = null
    closed = false

    constructor(url: string) {
      this.url = url
      instances.push(this as unknown as MockEventSource)
      lastMock = this as unknown as MockEventSource
    }
    addEventListener(type: string, cb: (e: MessageEvent<string>) => void) {
      this.listeners.set(type, cb)
    }
    close() {
      this.closed = true
    }
  }
  vi.stubGlobal('EventSource', FakeEventSource)
}

function fire(type: string, data: unknown) {
  if (!lastMock) throw new Error('no EventSource instance')
  const cb = lastMock.listeners.get(type)
  if (!cb) throw new Error(`no listener for ${type}`)
  cb({ data: JSON.stringify(data) } as MessageEvent<string>)
}

function open() {
  lastMock?.onopen?.()
}

describe('topologyRuntime store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    instances = []
    lastMock = null
    installEventSourceMock()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('connect() creates an EventSource on the topology-stream URL', () => {
    const store = useTopologyRuntimeStore()
    store.connect('run-1')
    expect(instances).toHaveLength(1)
    expect(instances[0].url).toBe('/api/v1/executions/run-1/topology-stream')
    expect(store.activeRunId).toBe('run-1')
  })

  it('connect(null) does not create a connection', () => {
    const store = useTopologyRuntimeStore()
    store.connect(null)
    expect(instances).toHaveLength(0)
    expect(store.activeRunId).toBeNull()
  })

  it('connect() twice closes the previous connection (no leak)', () => {
    const store = useTopologyRuntimeStore()
    store.connect('run-1')
    store.connect('run-2')
    expect(instances).toHaveLength(2)
    expect(instances[0].closed).toBe(true)
    expect(instances[1].url).toContain('run-2')
  })

  it('instrument event updates instrumentStatus', () => {
    const store = useTopologyRuntimeStore()
    store.connect('run-1')
    open()
    fire('instrument', { instrument_id: 'PSU_MAIN', status: 'error' })
    expect(store.connected).toBe(true)
    expect(store.instrumentStatus.PSU_MAIN).toMatchObject({
      instrument_id: 'PSU_MAIN',
      status: 'error',
    })
  })

  it('link event updates linkStatus with active mapping', () => {
    const store = useTopologyRuntimeStore()
    store.connect('run-1')
    fire('link', { link_id: 'L1', active: true, status: 'active' })
    expect(store.linkStatus.L1).toMatchObject({ link_id: 'L1', active: true, status: 'active' })
  })

  it('relay event updates relayState', () => {
    const store = useTopologyRuntimeStore()
    store.connect('run-1')
    fire('relay', { relay_id: 'R1', state: 'closed' })
    expect(store.relayState.R1.state).toBe('closed')
  })

  it('measurement event updates measurementStatus keyed by dut:testpoint', () => {
    const store = useTopologyRuntimeStore()
    store.connect('run-1')
    fire('measurement', { dut_id: 'DUT1', testpoint_id: 'TP1', value: 4.98, status: 'pass' })
    expect(store.measurementStatus['DUT1:TP1']).toMatchObject({
      dut_id: 'DUT1',
      testpoint_id: 'TP1',
      value: 4.98,
      status: 'pass',
    })
  })

  it('fixture event updates fixtureStatus', () => {
    const store = useTopologyRuntimeStore()
    store.connect('run-1')
    fire('fixture', { fixture_id: 'FIX1', status: 'clamped', sensors: { clamp_position: 1 } })
    expect(store.fixtureStatus.FIX1).toMatchObject({ fixture_id: 'FIX1', status: 'clamped' })
  })

  it('fault event appends fault with location preserved', () => {
    const store = useTopologyRuntimeStore()
    store.connect('run-1')
    fire('fault', {
      fault: { type: 'open_circuit', severity: 'critical', message: '开路' },
      location: { suspect_links: ['L1', 'L2'] },
    })
    expect(store.faults).toHaveLength(1)
    expect(store.faults[0].type).toBe('open_circuit')
    expect((store.faults[0] as unknown as { location: { suspect_links: string[] } }).location.suspect_links).toEqual([
      'L1',
      'L2',
    ])
  })

  it('setTopology stores the fixture topology data', () => {
    const store = useTopologyRuntimeStore()
    const data = { instruments: [], fixtures: [], duts: [], links: [], routes: [] }
    store.setTopology(data)
    expect(store.topology).toBe(data)
  })

  it('disconnect() closes EventSource and clears connection state but keeps data', () => {
    const store = useTopologyRuntimeStore()
    store.connect('run-1')
    fire('link', { link_id: 'L1', active: true })
    store.disconnect()
    expect(instances[0].closed).toBe(true)
    expect(store.connected).toBe(false)
    expect(store.activeRunId).toBeNull()
    // runtime data preserved after disconnect
    expect(store.linkStatus.L1).toBeDefined()
  })

  it('reset() clears topology, runtime state and faults', () => {
    const store = useTopologyRuntimeStore()
    store.connect('run-1')
    store.setTopology({ instruments: [], fixtures: [], duts: [], links: [], routes: [] })
    fire('link', { link_id: 'L1', active: true })
    fire('fault', { fault: { type: 'open_circuit', severity: 'error', message: 'x' } })
    store.reset()
    expect(store.topology).toBeNull()
    expect(Object.keys(store.linkStatus)).toHaveLength(0)
    expect(store.faults).toHaveLength(0)
    expect(store.connected).toBe(false)
  })
})
