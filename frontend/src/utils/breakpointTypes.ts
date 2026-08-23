/**
 * T39 (v41-gap-analysis #39) — typed simulation breakpoint model (§8.4).
 *
 * PURE module: no Vue / DOM / API imports, so validation and payload
 * building are unit-testable in isolation. The four kinds mirror the
 * backend BreakpointRegistry contract:
 *
 * - step            → target = step id
 * - instrument_call → target = "resource.method"
 * - variable_change → target = "scope.key"
 * - condition       → target = "*" wildcard; `condition` REQUIRED
 *                     (evaluated SERVER-SIDE only — never client-side)
 */

export const BREAKPOINT_KINDS = ['step', 'instrument_call', 'variable_change', 'condition'] as const

export type BreakpointKind = (typeof BREAKPOINT_KINDS)[number]

/** Form state for the add-breakpoint dialog. */
export interface BreakpointForm {
  kind: BreakpointKind
  target: string
  condition: string
}

/** Validated payload sent to POST /executions/{runId}/breakpoints. */
export interface TypedBreakpointPayload {
  kind: BreakpointKind
  target: string
  condition?: string
}

/** SSE BREAKPOINT_HIT event payload ({breakpoint_id, kind, target, context}). */
export interface BreakpointHitPayload {
  breakpoint_id: string
  kind: BreakpointKind | string
  target: string
  context?: Record<string, unknown>
}

export interface ValidationResult {
  valid: boolean
  error?: string
}

function isDottedPair(target: string): boolean {
  const parts = target.split('.')
  return parts.length === 2 && parts[0].length > 0 && parts[1].length > 0
}

/**
 * Validate a breakpoint form per kind:
 * - target non-empty for every kind;
 * - instrument_call / variable_change targets must be dotted pairs
 *   ("resource.method" / "scope.key");
 * - condition non-empty ONLY for the condition kind.
 */
export function validateBreakpoint(form: BreakpointForm): ValidationResult {
  if (!BREAKPOINT_KINDS.includes(form.kind)) {
    return { valid: false, error: `未知断点类型: ${form.kind}` }
  }
  const target = form.target.trim()
  if (!target) {
    return { valid: false, error: '请输入匹配目标' }
  }
  if ((form.kind === 'instrument_call' || form.kind === 'variable_change') && !isDottedPair(target)) {
    return {
      valid: false,
      error: form.kind === 'instrument_call' ? '目标须为 resource.method 格式' : '目标须为 scope.key 格式',
    }
  }
  const condition = form.condition.trim()
  if (form.kind === 'condition' && !condition) {
    return { valid: false, error: '条件类型断点必须填写条件表达式' }
  }
  if (form.kind !== 'condition' && condition) {
    return { valid: false, error: '仅条件类型断点允许填写条件表达式' }
  }
  return { valid: true }
}

/**
 * Build the API payload from a validated form, or null when invalid.
 * For the condition kind the expression rides in `condition`; other kinds
 * never carry one (mirrors backend validate_breakpoint).
 */
export function buildBreakpointPayload(form: BreakpointForm): TypedBreakpointPayload | null {
  if (!validateBreakpoint(form).valid) return null
  const kind = form.kind
  const target = form.target.trim()
  if (kind === 'condition') {
    return { kind, target, condition: form.condition.trim() }
  }
  return { kind, target }
}

/** Human-readable label per kind (console UI). */
export const BREAKPOINT_KIND_LABELS: Record<BreakpointKind, string> = {
  step: '步骤',
  instrument_call: '仪器调用',
  variable_change: '变量变更',
  condition: '条件',
}
