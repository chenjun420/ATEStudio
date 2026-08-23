/**
 * T40 (v41-gap-analysis #40) — debugger step modes for SimulationConsole (§8.4).
 *
 * PURE module: no Vue / DOM / API imports, so validation and payload
 * building are unit-testable in isolation. The four modes mirror the
 * backend StepMode contract (ScannerScheduler.arm_step_mode):
 *
 * - over          → 步过：下一个同级兄弟步骤派发前暂停
 * - into          → 步入：进入容器/子序列（顶层无子时退化为步过）
 * - out           → 步出：跑完当前容器，回到父层级再暂停
 * - run_to_cursor → 运行至光标：目标步骤派发前暂停（target 必填）
 */

export const STEP_MODES = ['over', 'into', 'out', 'run_to_cursor'] as const

export type StepMode = (typeof STEP_MODES)[number]

/** Toolbar metadata per mode (label/icon/tooltip + target requirement). */
export interface StepModeMeta {
  mode: StepMode
  label: string
  icon: string
  tooltip: string
  requiresTarget: boolean
}

export const STEP_MODE_META: Record<StepMode, StepModeMeta> = {
  over: {
    mode: 'over',
    label: '步过',
    icon: 'DCaret',
    tooltip: '执行到下一个同级步骤前暂停',
    requiresTarget: false,
  },
  into: {
    mode: 'into',
    label: '步入',
    icon: 'Right',
    tooltip: '进入容器/子序列的第一个子步骤',
    requiresTarget: false,
  },
  out: {
    mode: 'out',
    label: '步出',
    icon: 'Back',
    tooltip: '跑完当前容器，回到父层级暂停',
    requiresTarget: false,
  },
  run_to_cursor: {
    mode: 'run_to_cursor',
    label: '运行至光标',
    icon: 'Aim',
    tooltip: '恢复执行，直到目标步骤开始前暂停',
    requiresTarget: true,
  },
}

/** Canonical toolbar order (over → into → out → run_to_cursor). */
export const STEP_MODE_TOOLBAR: readonly StepModeMeta[] = STEP_MODES.map(
  (mode) => STEP_MODE_META[mode],
)

export interface ValidationResult {
  valid: boolean
  error?: string
}

/** Whether the mode needs a target step id (run_to_cursor only). */
export function requiresTarget(mode: StepMode): boolean {
  return STEP_MODE_META[mode].requiresTarget
}

/**
 * Validate a step-control form:
 * - mode must be one of the four §8.4 modes;
 * - run_to_cursor requires a non-empty target step id.
 */
export function validateStepControl(form: {
  mode: StepMode
  target: string
}): ValidationResult {
  if (!STEP_MODES.includes(form.mode)) {
    return { valid: false, error: `未知步进模式: ${String(form.mode)}` }
  }
  if (requiresTarget(form.mode) && !form.target.trim()) {
    return { valid: false, error: '运行至光标需要填写目标步骤 ID' }
  }
  return { valid: true }
}

/** Payload sent to POST /executions/{runId}/step-control. */
export interface StepControlPayload {
  mode: StepMode
  target_step_id?: string
}

/** Build the wire payload; omits target unless run_to_cursor provides one. */
export function buildStepControlPayload(form: {
  mode: StepMode
  target: string
}): StepControlPayload {
  const target = form.target.trim()
  if (requiresTarget(form.mode) && target) {
    return { mode: form.mode, target_step_id: target }
  }
  return { mode: form.mode }
}
