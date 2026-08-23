/**
 * Tests for breakpointTypes.ts (T39, v41-gap-analysis #39).
 *
 * Pure util: per-kind validation, payload building, and the
 * "condition non-empty only for condition kind" rule.
 */
import { describe, it, expect } from 'vitest'

import {
  BREAKPOINT_KINDS,
  buildBreakpointPayload,
  validateBreakpoint,
  type BreakpointForm,
} from '../breakpointTypes'

function form(overrides: Partial<BreakpointForm> = {}): BreakpointForm {
  return { kind: 'step', target: 'dmm_read', condition: '', ...overrides }
}

describe('BREAKPOINT_KINDS', () => {
  it('exposes exactly the four §8.4 kinds', () => {
    expect([...BREAKPOINT_KINDS]).toEqual([
      'step',
      'instrument_call',
      'variable_change',
      'condition',
    ])
  })
})

describe('validateBreakpoint', () => {
  it('accepts a step-kind breakpoint with a plain target', () => {
    expect(validateBreakpoint(form())).toEqual({ valid: true })
  })

  it('rejects empty target for every kind', () => {
    for (const kind of BREAKPOINT_KINDS) {
      expect(validateBreakpoint(form({ kind, target: '   ' })).valid).toBe(false)
    }
  })

  it('requires resource.method dotted pair for instrument_call', () => {
    expect(validateBreakpoint(form({ kind: 'instrument_call', target: 'PSU_MAIN.set_voltage' })).valid).toBe(true)
    const bad = validateBreakpoint(form({ kind: 'instrument_call', target: 'PSU_MAIN' }))
    expect(bad.valid).toBe(false)
    expect(bad.error).toContain('resource.method')
  })

  it('requires scope.key dotted pair for variable_change', () => {
    expect(validateBreakpoint(form({ kind: 'variable_change', target: 'bench.voltage' })).valid).toBe(true)
    const bad = validateBreakpoint(form({ kind: 'variable_change', target: 'voltage' }))
    expect(bad.valid).toBe(false)
    expect(bad.error).toContain('scope.key')
  })

  it('requires a non-empty condition for the condition kind', () => {
    const missing = validateBreakpoint(form({ kind: 'condition', target: '*', condition: '' }))
    expect(missing.valid).toBe(false)
    expect(missing.error).toContain('条件表达式')
  })

  it('forbids condition on non-condition kinds', () => {
    const res = validateBreakpoint(form({ kind: 'step', condition: 'x > 1' }))
    expect(res.valid).toBe(false)
    expect(res.error).toContain('仅条件类型')
  })
})

describe('buildBreakpointPayload', () => {
  it('builds a step payload without condition field', () => {
    expect(buildBreakpointPayload(form())).toEqual({ kind: 'step', target: 'dmm_read' })
  })

  it('rides the expression in condition for the condition kind', () => {
    expect(buildBreakpointPayload(form({ kind: 'condition', target: '*', condition: ' voltage > 3.0 ' }))).toEqual({
      kind: 'condition',
      target: '*',
      condition: 'voltage > 3.0',
    })
  })

  it('returns null for invalid forms (malformed condition rejected client-side too)', () => {
    expect(buildBreakpointPayload(form({ kind: 'condition', target: '*', condition: '  ' }))).toBeNull()
    expect(buildBreakpointPayload(form({ kind: 'instrument_call', target: 'nodot' }))).toBeNull()
  })
})
