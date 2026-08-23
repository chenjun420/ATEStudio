/**
 * T39 (v41-gap-analysis #39) — typed breakpoint CRUD + BREAKPOINT_HIT SSE
 * handling for the SimulationConsole (§8.4).
 *
 * State transitions are driven by `handleHit` so vitest can exercise the
 * paused/toast flow without a real EventSource; `connect` wires the live
 * SSE stream (`event: breakpoint`, additive mapping in sse_bridge).
 */
import { onBeforeUnmount, ref, type Ref } from 'vue'

import {
  createTypedBreakpoint,
  deleteTypedBreakpoint,
  listTypedBreakpoints,
  resumeExecution,
  type TypedBreakpoint,
} from '@/api/executions'
import {
  buildBreakpointPayload,
  validateBreakpoint,
  type BreakpointForm,
  type BreakpointHitPayload,
  type ValidationResult,
} from '@/utils/breakpointTypes'

export interface UseSimulationBreakpointsReturn {
  items: Ref<TypedBreakpoint[]>
  paused: Ref<boolean>
  lastHit: Ref<BreakpointHitPayload | null>
  load: (runId: string) => Promise<void>
  add: (runId: string, form: BreakpointForm) => Promise<ValidationResult>
  remove: (runId: string, bpId: string) => Promise<void>
  resume: (runId: string) => Promise<void>
  handleHit: (hit: BreakpointHitPayload) => void
  connect: (runId: string) => void
  disconnect: () => void
}

export function useSimulationBreakpoints(
  notify?: (message: string, type: 'success' | 'warning' | 'info') => void,
): UseSimulationBreakpointsReturn {
  const items = ref<TypedBreakpoint[]>([])
  const paused = ref(false)
  const lastHit = ref<BreakpointHitPayload | null>(null)
  let eventSource: EventSource | null = null

  async function load(runId: string): Promise<void> {
    if (!runId) return
    const res = await listTypedBreakpoints(runId)
    items.value = res.items
  }

  function handleHit(hit: BreakpointHitPayload): void {
    paused.value = true
    lastHit.value = hit
    notify?.(`断点命中 [${hit.kind}] ${hit.target}，仿真已暂停`, 'warning')
  }

  function connect(runId: string): void {
    disconnect()
    if (!runId || typeof EventSource === 'undefined') return
    const es = new EventSource(`/api/v1/executions/${runId}/events`)
    es.addEventListener('breakpoint', (e: MessageEvent<string>) => {
      try {
        handleHit(JSON.parse(e.data) as BreakpointHitPayload)
      } catch {
        /* malformed payload — ignore */
      }
    })
    eventSource = es
  }

  function disconnect(): void {
    eventSource?.close()
    eventSource = null
  }

  async function add(runId: string, form: BreakpointForm): Promise<ValidationResult> {
    const validation = validateBreakpoint(form)
    if (!validation.valid || !runId) return validation
    const payload = buildBreakpointPayload(form)
    if (!payload) return { valid: false, error: '无效的断点配置' }
    await createTypedBreakpoint(runId, payload)
    await load(runId)
    return { valid: true }
  }

  async function remove(runId: string, bpId: string): Promise<void> {
    await deleteTypedBreakpoint(runId, bpId)
    await load(runId)
  }

  async function resume(runId: string): Promise<void> {
    await resumeExecution(runId)
    paused.value = false
    lastHit.value = null
    notify?.('已恢复执行', 'success')
  }

  onBeforeUnmount(disconnect)

  return { items, paused, lastHit, load, add, remove, resume, handleHit, connect, disconnect }
}
