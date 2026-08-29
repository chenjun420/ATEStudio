/**
 * One-time SSE ticket auth for native EventSource streams (RH-3).
 *
 * Native ``EventSource`` cannot set an ``Authorization`` header, so the
 * JWT bearer guard that protects every REST endpoint cannot ride an SSE
 * request. The cloud instead issues a short-lived, single-consume ticket:
 *
 *   1. ``fetchSseTicket()`` POSTs ``/api/v1/auth/sse-ticket`` with the
 *      app's normal axios client (which attaches the bearer token via the
 *      request interceptor) and returns the opaque ticket string.
 *   2. ``buildSseUrl(path, ticket)`` appends ``?ticket=<value>`` to the
 *      stream path, preserving any query params already present.
 *   3. ``openTicketedEventSource(path, handlers)`` opens the EventSource
 *      with that URL. A ticket that is missing / garbage / expired /
 *      already-consumed is answered 401 by the server; EventSource hides
 *      the HTTP status, so auth failure is detected heuristically — an
 *      ``error`` that fires BEFORE the stream ever opens and leaves the
 *      readyState ``CLOSED`` (a definitive 4xx, as opposed to a transient
 *      network drop which leaves it ``CONNECTING`` for native retry). On
 *      that signal the ticket is refetched ONCE and the stream reconnected
 *      ONCE; a second failure is reported via ``onError`` and never retried
 *      again (no infinite reconnect loop).
 */
import http from '@/api/interceptor'

/** Response body of POST /api/v1/auth/sse-ticket (mirrors SseTicketResponse). */
interface SseTicketResponse {
  ticket: string
  expires_in: number
}

/**
 * POST /api/v1/auth/sse-ticket with the app's Authorization header.
 *
 * @returns The opaque one-time ticket string.
 * @throws If the request fails (e.g. 401 — the bearer session is gone) or
 *   the response carries no ticket.
 */
export async function fetchSseTicket(): Promise<string> {
  const response = await http.post<SseTicketResponse>('/auth/sse-ticket')
  const ticket = response.data?.ticket
  if (!ticket) {
    throw new Error('SSE ticket issue returned no ticket')
  }
  return ticket
}

/**
 * Append ``?ticket=<value>`` to a stream path, preserving existing query
 * params (e.g. ``/replay/stream?speed=100`` keeps ``speed``).
 *
 * @param path Absolute API path, optionally with an existing query string.
 * @param ticket The one-time ticket from {@link fetchSseTicket}.
 * @returns The path with a ``ticket`` query parameter set.
 */
export function buildSseUrl(path: string, ticket: string): string {
  const queryIndex = path.indexOf('?')
  const pathOnly = queryIndex === -1 ? path : path.slice(0, queryIndex)
  const existingQuery = queryIndex === -1 ? '' : path.slice(queryIndex + 1)
  const params = new URLSearchParams(existingQuery)
  params.set('ticket', ticket)
  return `${pathOnly}?${params.toString()}`
}

/** Named ``event:`` listeners plus lifecycle hooks for a ticketed stream. */
export interface SseEventHandlers {
  /** Map of SSE event name -> handler (registered via addEventListener). */
  listeners?: Record<string, (event: MessageEvent<string>) => void>
  /** Fired when the stream opens (also after a successful reconnect). */
  onOpen?: () => void
  /**
   * Fired on a transient error (stream had opened, then dropped — native
   * auto-reconnect continues) or after a failed reconnect/refetch.
   */
  onError?: () => void
}

/** Handle for a ticketed EventSource; ``close`` tears the stream down. */
export interface TicketedEventSource {
  close: () => void
}

/**
 * Open an EventSource against a ticket-protected SSE endpoint.
 *
 * Fetches a one-time ticket, opens the stream, and — on the closed-before-
 * open auth-failure heuristic — refetches the ticket and reconnects exactly
 * once. Never loops: a second auth failure (or a failed refetch) is reported
 * through ``handlers.onError``.
 *
 * @param path Absolute API path of the SSE endpoint (query params allowed).
 * @param handlers Named event listeners + open/error lifecycle hooks.
 * @returns A handle whose ``close()`` closes the active stream and cancels
 *   any in-flight (re)connect.
 */
export function openTicketedEventSource(
  path: string,
  handlers: SseEventHandlers = {},
): TicketedEventSource {
  // jsdom / old environments without EventSource — silently no-op, matching
  // the pre-RH3 call sites' guard.
  if (typeof EventSource === 'undefined') {
    return { close() {} }
  }

  let closed = false
  let current: EventSource | null = null
  let everOpened = false
  let retried = false

  const wire = (source: EventSource): void => {
    source.onopen = () => {
      everOpened = true
      handlers.onOpen?.()
    }
    source.onerror = () => {
      // Auth-failure heuristic: a definitive 4xx (bad/consumed/expired
      // ticket) closes the connection before it ever opens. A transient
      // network drop leaves readyState CONNECTING while EventSource retries,
      // and a post-open drop is not an auth problem. Only the former triggers
      // the single ticket refetch + reconnect.
      const authFailure =
        !everOpened && source.readyState === EventSource.CLOSED && !retried
      if (authFailure) {
        retried = true
        source.close()
        current = null
        void reconnectWithFreshTicket()
        return
      }
      handlers.onError?.()
    }
    for (const [name, callback] of Object.entries(handlers.listeners ?? {})) {
      source.addEventListener(name, callback as EventListener)
    }
  }

  const openWithTicket = (ticket: string): void => {
    if (closed) return
    current = new EventSource(buildSseUrl(path, ticket))
    wire(current)
  }

  const reconnectWithFreshTicket = async (): Promise<void> => {
    try {
      const ticket = await fetchSseTicket()
      openWithTicket(ticket)
    } catch {
      // Refetch failed (session expired / offline) — surface once; no retry.
      if (!closed) handlers.onError?.()
    }
  }

  void (async () => {
    try {
      const ticket = await fetchSseTicket()
      openWithTicket(ticket)
    } catch {
      if (!closed) handlers.onError?.()
    }
  })()

  return {
    close() {
      closed = true
      current?.close()
      current = null
    },
  }
}
