/**
 * T40 (v41-gap-analysis #40) — stepModes util tests (§8.4 debugger toolbar).
 */
import { describe, expect, it } from 'vitest'

import {
  STEP_MODES,
  STEP_MODE_META,
  STEP_MODE_TOOLBAR,
  buildStepControlPayload,
  requiresTarget,
  validateStepControl,
  type StepMode,
} from '../stepModes'

describe('stepModes', () => {
  it('exposes exactly the four §8.4 modes in canonical order', () => {
    expect(STEP_MODES).toEqual(['over', 'into', 'out', 'run_to_cursor'])
    expect(STEP_MODE_TOOLBAR.map((m) => m.mode)).toEqual([...STEP_MODES])
  })

  it('maps every mode to a non-empty Chinese label and icon', () => {
    for (const meta of STEP_MODE_TOOLBAR) {
      expect(meta.label.length).toBeGreaterThan(0)
      expect(meta.icon.length).toBeGreaterThan(0)
      expect(meta.tooltip.length).toBeGreaterThan(0)
      expect(STEP_MODE_META[meta.mode]).toBe(meta)
    }
  })

  it('requires a target only for run_to_cursor', () => {
    expect(requiresTarget('over')).toBe(false)
    expect(requiresTarget('into')).toBe(false)
    expect(requiresTarget('out')).toBe(false)
    expect(requiresTarget('run_to_cursor')).toBe(true)
  })

  it('rejects run_to_cursor with an empty target', () => {
    const result = validateStepControl({ mode: 'run_to_cursor', target: '   ' })
    expect(result.valid).toBe(false)
    expect(result.error).toContain('目标步骤')
  })

  it('accepts targetless modes without a target and rejects unknown modes', () => {
    expect(validateStepControl({ mode: 'over', target: '' }).valid).toBe(true)
    expect(validateStepControl({ mode: 'out', target: '' }).valid).toBe(true)
    const bad = validateStepControl({
      mode: 'teleport' as unknown as StepMode,
      target: '',
    })
    expect(bad.valid).toBe(false)
    expect(bad.error).toContain('未知步进模式')
  })

  it('builds payloads that omit target unless run_to_cursor provides one', () => {
    expect(buildStepControlPayload({ mode: 'into', target: '' })).toEqual({
      mode: 'into',
    })
    expect(
      buildStepControlPayload({ mode: 'over', target: 'step-1' }),
    ).toEqual({ mode: 'over' })
    expect(
      buildStepControlPayload({ mode: 'run_to_cursor', target: ' step-9 ' }),
    ).toEqual({ mode: 'run_to_cursor', target_step_id: 'step-9' })
  })
})
