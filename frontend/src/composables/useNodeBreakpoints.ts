/**
 * useNodeBreakpoints — SequenceEditor breakpoint toggles (task 23).
 *
 * Breakpoint controls for graph step nodes in the SequenceEditor. Reuses
 * the SAME typed-breakpoint API and ticketed SSE stream the
 * SimulationConsole uses:
 *
 * - REST: `createTypedBreakpoint` / `listTypedBreakpoints` /
 *   `deleteTypedBreakpoint` from `@/api/executions` (shared
 *   `@/api/interceptor` axios client — JWT attached, no new axios instance).
 * - SSE:  `openTicketedEventSource` from `@/utils/sseTicket` against
 *   `/api/v1/executions/{runId}/events`, listening to the `breakpoint`
 *   event (BREAKPOINT_HIT, category mapped by the cloud sse_bridge).
 *
 * Scope: only `kind: 'step'` breakpoints (target = step id) are owned here;
 * instrument_call / variable_change / condition breakpoints remain console
 * concerns. State is a reactive Map keyed by step target so GraphContainer
 * and SubGraphContainer (each holding their own X6 Graph instance) render
 * identical markers without a second store.
 */
import { onBeforeUnmount, reactive, ref, type InjectionKey, type Ref } from 'vue'

import {
  createTypedBreakpoint,
  deleteTypedBreakpoint,
  listTypedBreakpoints,
  type TypedBreakpoint,
} from '@/api/executions'
import type { BreakpointHitPayload } from '@/utils/breakpointTypes'
import { openTicketedEventSource, type TicketedEventSource } from '@/utils/sseTicket'

/**
 * Injection key for sharing ONE useNodeBreakpoints instance between the
 * SequenceEditor index view and its graph containers (main graph + sub-graph
 * each build their own X6 Graph but must render identical breakpoint markers).
 */
export const NODE_BREAKPOINTS_KEY: InjectionKey<UseNodeBreakpointsReturn> =
  Symbol('nodeBreakpoints')

export interface UseNodeBreakpointsReturn {
  /** Step target -> breakpoint id for enabled step breakpoints. */
  armedSteps: Record<string, string>
  /** Step target of the latest BREAKPOINT_HIT, or null when cleared. */
  hitStep: Ref<string | null>
  /** True while a toggle/list request is in flight (disables menu spam). */
  busy: Ref<boolean>
  /** Load durable breakpoints for a run (call when a run starts). */
  load: (runId: string) => Promise<void>
  /** Open the ticketed BREAKPOINT_HIT SSE stream for a run. */
  connect: (runId: string) => void
  /** Close the SSE stream. */
  disconnect: () => void
  /** True when an enabled step breakpoint targets `stepId`. */
  isArmed: (stepId: string) => boolean
  /**
   * Toggle a step breakpoint for `stepId`: create+enable when absent,
   * delete when present. Returns true when a breakpoint is armed after
   * the toggle.
   */
  toggleStep: (runId: string, stepId: string) => Promise<boolean>
  /** Record a BREAKPOINT_HIT programmatically (drives the hit halo; tests). */
  handleHit: (hit: BreakpointHitPayload) => void
  /** Clear the hit highlight (e.g. after resume / run end). */
  clearHit: () => void
}

/**
 * Composable for step-node breakpoints in the SequenceEditor.
 *
 * @param notify Optional toast sink (mirrors useSimulationBreakpoints).
 */
export function useNodeBreakpoints(
  notify?: (message: string, type: 'success' | 'warning' | 'info' | 'error') => void,
): UseNodeBreakpointsReturn {
  /** step target -> breakpoint id (enabled step breakpoints only). */
  const armed = reactive<Record<string, string>>({})
  const hitStep = ref<string | null>(null)
  const busy = ref(false)
  let eventSource: TicketedEventSource | null = null
  /** Breakpoints owned by this editor (all kinds), for id lookup on delete. */
  let items: TypedBreakpoint[] = []

  function indexItems(list: TypedBreakpoint[]): void {
    items = list
    for (const key of Object.keys(armed)) delete armed[key]
    for (const bp of list) {
      if (bp.kind === 'step' && bp.enabled) {
        armed[bp.target] = bp.id
      }
    }
  }

  async function load(runId: string): Promise<void> {
    if (!runId) return
    busy.value = true
    try {
      const res = await listTypedBreakpoints(runId)
      indexItems(res.items)
      hitStep.value = null
    } catch (e) {
      notify?.(`加载断点失败: ${e instanceof Error ? e.message : String(e)}`, 'error')
    } finally {
      busy.value = false
    }
  }

  function handleHit(hit: BreakpointHitPayload): void {
    if (hit.kind === 'step' && typeof hit.target === 'string' && hit.target) {
      hitStep.value = hit.target
    }
    notify?.(`断点命中 [${hit.kind}] ${hit.target}，仿真已暂停`, 'warning')
  }

  function clearHit(): void {
    hitStep.value = null
  }

  function connect(runId: string): void {
    disconnect()
    if (!runId || typeof EventSource === 'undefined') return
    eventSource = openTicketedEventSource(`/api/v1/executions/${runId}/events`, {
      listeners: {
        breakpoint: (e: MessageEvent<string>) => {
          try {
            handleHit(JSON.parse(e.data) as BreakpointHitPayload)
          } catch {
            /* malformed payload — ignore */
          }
        },
      },
    })
  }

  function disconnect(): void {
    eventSource?.close()
    eventSource = null
  }

  function isArmed(stepId: string): boolean {
    return Boolean(armed[stepId])
  }

  async function toggleStep(runId: string, stepId: string): Promise<boolean> {
    if (!runId) {
      notify?.('请先启动一次仿真运行后再设置断点（断点属于运行）', 'warning')
      return isArmed(stepId)
    }
    if (busy.value) return isArmed(stepId)
    busy.value = true
    try {
      const existingId = armed[stepId]
      if (existingId) {
        await deleteTypedBreakpoint(runId, existingId)
        delete armed[stepId]
        items = items.filter((bp) => bp.id !== existingId)
        if (hitStep.value === stepId) hitStep.value = null
        notify?.(`已移除步骤断点 ${stepId.slice(0, 8)}`, 'info')
        return false
      }
      const created = await createTypedBreakpoint(runId, { kind: 'step', target: stepId })
      items = [...items, created]
      if (created.enabled) armed[stepId] = created.id
      notify?.(`已在步骤 ${stepId.slice(0, 8)} 设置断点`, 'success')
      return true
    } catch (e) {
      notify?.(`切换断点失败: ${e instanceof Error ? e.message : String(e)}`, 'error')
      return isArmed(stepId)
    } finally {
      busy.value = false
    }
  }

  onBeforeUnmount(disconnect)

  return {
    armedSteps: armed,
    hitStep,
    busy,
    load,
    connect,
    disconnect,
    isArmed,
    toggleStep,
    handleHit,
    clearHit,
  }
}
