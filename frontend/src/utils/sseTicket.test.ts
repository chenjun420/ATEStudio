/**
 * Tests for the one-time SSE ticket util (RH-3).
 *
 * Covers:
 * - buildSseUrl appends ?ticket= and preserves existing query params
 * - fetchSseTicket POSTs /auth/sse-ticket with the axios client and throws
 *   on failure / empty ticket
 * - openTicketedEventSource opens the stream with the ticket URL
 * - a consumed/expired/garbage ticket (server 401 -> EventSource CLOSED
 *   before ever opening) triggers exactly ONE ticket refetch + reconnect
 * - a second auth failure does NOT loop (onError, no third connection)
 * - a transient drop (readyState CONNECTING, or after open) does not refetch
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const { httpPostMock } = vi.hoisted(() => ({
  httpPostMock: vi.fn(),
}))

vi.mock('@/api/interceptor', () => ({
  default: { post: httpPostMock },
}))

import { buildSseUrl, fetchSseTicket, openTicketedEventSource } from './sseTicket'

// ─── Fake EventSource ───────────────────────────────────────────────────────

interface FakeES {
  url: string
  readyState: number
  onopen: (() => void) | null
  onerror: (() => void) | null
  listeners: Map<string, EventListener>
  closed: boolean
}

class FakeEventSource {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSED = 2

  url: string
  readyState = FakeEventSource.CONNECTING
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  listeners = new Map<string, EventListener>()
  closed = false

  constructor(url: string) {
    this.url = url
    instances.push(this as unknown as FakeES)
  }
  addEventListener(type: string, cb: EventListener) {
    this.listeners.set(type, cb)
  }
  close() {
    this.closed = true
    this.readyState = FakeEventSource.CLOSED
  }
}

let instances: FakeES[] = []

function installES() {
  instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
}

function ticketResponse(ticket: string) {
  return Promise.resolve({ data: { ticket, expires_in: 60 } })
}

// ─── buildSseUrl ────────────────────────────────────────────────────────────

describe('buildSseUrl', () => {
  it('appends ?ticket= to a path with no query string', () => {
    expect(buildSseUrl('/api/v1/offline/status/stream', 'abc')).toBe(
      '/api/v1/offline/status/stream?ticket=abc',
    )
  })

  it('preserves existing query params and adds ticket', () => {
    const url = buildSseUrl('/api/v1/executions/r1/replay/stream?speed=100.0', 'tk')
    expect(url).toContain('speed=100.0')
    expect(url).toContain('ticket=tk')
    // ticket must not clobber the existing param
    const params = new URLSearchParams(url.slice(url.indexOf('?') + 1))
    expect(params.get('speed')).toBe('100.0')
    expect(params.get('ticket')).toBe('tk')
  })

  it('overwrites a stale ticket param rather than duplicating it', () => {
    const url = buildSseUrl('/api/v1/x/events?ticket=old', 'new')
    const params = new URLSearchParams(url.slice(url.indexOf('?') + 1))
    expect(params.getAll('ticket')).toEqual(['new'])
  })
})

// ─── fetchSseTicket ─────────────────────────────────────────────────────────

describe('fetchSseTicket', () => {
  beforeEach(() => {
    httpPostMock.mockReset()
  })

  it('POSTs /auth/sse-ticket and returns the ticket string', async () => {
    httpPostMock.mockImplementationOnce(() => ticketResponse('t-xyz'))
    const ticket = await fetchSseTicket()
    expect(httpPostMock).toHaveBeenCalledWith('/auth/sse-ticket')
    expect(ticket).toBe('t-xyz')
  })

  it('throws when the issue request fails (e.g. 401 session gone)', async () => {
    httpPostMock.mockImplementationOnce(() => Promise.reject(new Error('401 Unauthorized')))
    await expect(fetchSseTicket()).rejects.toThrow()
  })

  it('throws when the response carries no ticket', async () => {
    httpPostMock.mockImplementationOnce(() => Promise.resolve({ data: {} }))
    await expect(fetchSseTicket()).rejects.toThrow(/no ticket/)
  })
})

// ─── openTicketedEventSource ────────────────────────────────────────────────

describe('openTicketedEventSource', () => {
  beforeEach(() => {
    httpPostMock.mockReset()
    installES()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('opens the stream with a ticketed URL and wires named listeners', async () => {
    httpPostMock.mockImplementationOnce(() => ticketResponse('tk-1'))
    const onOpen = vi.fn()
    const onMessage = vi.fn()
    const handle = openTicketedEventSource('/api/v1/executions/r1/topology-stream', {
      onOpen,
      listeners: { instrument: onMessage },
    })

    // initial ticket fetch is async — let it resolve
    await Promise.resolve()
    await Promise.resolve()

    expect(instances).toHaveLength(1)
    expect(instances[0].url).toBe('/api/v1/executions/r1/topology-stream?ticket=tk-1')

    instances[0].readyState = FakeEventSource.OPEN
    instances[0].onopen?.()
    expect(onOpen).toHaveBeenCalledTimes(1)

    instances[0].listeners.get('instrument')?.(new MessageEvent('instrument', { data: '{}' }))
    expect(onMessage).toHaveBeenCalledTimes(1)

    handle.close()
    expect(instances[0].closed).toBe(true)
  })

  it('refetches the ticket ONCE and reconnects when the first ticket is rejected (consumed/expired)', async () => {
    // first ticket is consumed server-side (401 -> CLOSED before open);
    // refetch returns a fresh ticket
    httpPostMock
      .mockImplementationOnce(() => ticketResponse('consumed-ticket'))
      .mockImplementationOnce(() => ticketResponse('fresh-ticket'))
    const onError = vi.fn()
    openTicketedEventSource('/api/v1/offline/status/stream', { onError })

    await Promise.resolve()
    await Promise.resolve()
    expect(instances).toHaveLength(1)
    expect(instances[0].url).toContain('ticket=consumed-ticket')

    // Simulate the 401: EventSource gives up -> CLOSED, error before open.
    instances[0].readyState = FakeEventSource.CLOSED
    instances[0].onerror?.()

    // refetch + reconnect
    await Promise.resolve()
    await Promise.resolve()

    expect(httpPostMock).toHaveBeenCalledTimes(2)
    expect(instances).toHaveLength(2)
    expect(instances[0].closed).toBe(true)
    expect(instances[1].url).toContain('ticket=fresh-ticket')
    // A successful reconnect is not an error condition.
    expect(onError).not.toHaveBeenCalled()
  })

  it('does NOT loop infinitely: a second auth failure reports onError and stops', async () => {
    httpPostMock
      .mockImplementationOnce(() => ticketResponse('tk-1'))
      .mockImplementationOnce(() => ticketResponse('tk-2'))
    const onError = vi.fn()
    openTicketedEventSource('/api/v1/executions/r1/events', { onError })

    await Promise.resolve()
    await Promise.resolve()

    // First connection rejected -> one refetch/reconnect.
    instances[0].readyState = FakeEventSource.CLOSED
    instances[0].onerror?.()
    await Promise.resolve()
    await Promise.resolve()
    expect(httpPostMock).toHaveBeenCalledTimes(2)
    expect(instances).toHaveLength(2)

    // Second connection ALSO rejected -> give up: onError, no third fetch.
    instances[1].readyState = FakeEventSource.CLOSED
    instances[1].onerror?.()
    await Promise.resolve()
    await Promise.resolve()

    expect(httpPostMock).toHaveBeenCalledTimes(2) // still exactly two
    expect(instances).toHaveLength(2) // no third EventSource
    expect(onError).toHaveBeenCalledTimes(1)
  })

  it('does not refetch on a transient drop (readyState CONNECTING) or after the stream opened', async () => {
    httpPostMock.mockImplementationOnce(() => ticketResponse('tk-1'))
    const onError = vi.fn()
    openTicketedEventSource('/api/v1/executions/r1/topology-stream', { onError })

    await Promise.resolve()
    await Promise.resolve()

    // Transient network blip: EventSource stays CONNECTING and will retry itself.
    instances[0].readyState = FakeEventSource.CONNECTING
    instances[0].onerror?.()
    expect(httpPostMock).toHaveBeenCalledTimes(1)
    expect(instances).toHaveLength(1)
    expect(onError).toHaveBeenCalledTimes(1)

    // After a successful open, a later error is a drop, not an auth failure.
    instances[0].readyState = FakeEventSource.OPEN
    instances[0].onopen?.()
    onError.mockClear()
    instances[0].readyState = FakeEventSource.CLOSED
    instances[0].onerror?.()
    expect(httpPostMock).toHaveBeenCalledTimes(1) // no refetch
    expect(onError).toHaveBeenCalledTimes(1)
  })

  it('reports onError (no loop) when the initial ticket fetch fails', async () => {
    httpPostMock.mockImplementationOnce(() => Promise.reject(new Error('401')))
    const onError = vi.fn()
    const handle = openTicketedEventSource('/api/v1/offline/status/stream', { onError })

    await Promise.resolve()
    await Promise.resolve()

    expect(instances).toHaveLength(0)
    expect(onError).toHaveBeenCalledTimes(1)
    handle.close() // must not throw
  })
})
