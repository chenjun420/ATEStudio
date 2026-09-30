/**
 * ATERag bundle import / plan API (P5 review wizard).
 *
 * Two calls, both on the import router:
 *
 * - `POST /imports/aterag?dry_run=true`   — preview. Writes nothing.
 * - `POST /imports/aterag?dry_run=false`  — commit.
 *
 * The commit path is a separate function rather than a flag on one call. A
 * reviewer clicking "commit" while a preview is still loading would otherwise
 * write a bundle they never saw previewed — and the preview is the only thing
 * that surfaces the conflicts about to be overwritten.
 *
 * Planning runs server-side rather than in the browser: the flow planner needs
 * the binding table, and shipping that table to the client to reproduce the
 * sequence client-side would mean two implementations that can disagree about
 * the sequence that runs on the line.
 */
import http from './interceptor'

const api = http

/** One change that would overwrite a human edit. */
export interface ImportConflict {
  entity: string
  identifier: string
  field: string
  current_value: unknown
  incoming_value: unknown
  last_modified_by: string
}

/** Result of a dry-run or a commit. Same shape for both, so the UI renders one. */
export interface ImportResult {
  product_code: string
  contract_hash: string
  dry_run: boolean
  changed: boolean
  requirements: { created: number; updated: number }
  conditions: { created: number; updated: number }
  cases: { created: number; updated: number }
  limits: { created: number; updated: number }
  cases_reset_to_draft: string[]
  requirements_staled: string[]
  conflicts: ImportConflict[]
  unmapped: { entity: string; identifier: string; reason: string }[]
}

/** A scenario whose conditions no binding could turn into steps. */
export interface PlanUnmapped {
  requirement_code: string
  scenario_seq: string
  scenario_name: string
  missing: string
}

/** A measurement the plan cannot judge. */
export interface PlanGap {
  requirement_code: string
  condition_kind: string
  reason: string
}

/** An unapproved condition held out of the executable sequence. */
export interface PlanPending {
  requirement_code: string
  side: string
  kind: string
  status: string
  confidence: string
  method_ref: string
  text: string
}

/** Planned sequence summary, enough to decide whether to proceed. */
export interface PlanPreview {
  product_code: string
  segments: number
  steps: number
  settle_s: number
  batch_setups: { action: string; value: unknown; settle_s: number }[]
  unmapped: PlanUnmapped[]
  gaps: PlanGap[]
  pending: PlanPending[]
  warnings: string[]
  /** Full DSL v3.2 plan text, downloadable. */
  plan_yaml: string
}

/** Result of an import, normalised for the wizard. */
export interface ImportPreview extends ImportResult {
  message: string
}

/** Human-readable summary of an import result. */
function describe(r: ImportResult): string {
  const parts = [
    `需求 +${r.requirements.created}/~${r.requirements.updated}`,
    `条件 +${r.conditions.created}/~${r.conditions.updated}`,
    `用例 +${r.cases.created}/~${r.cases.updated}`,
    `限值 +${r.limits.created}/~${r.limits.updated}`,
  ]
  if (r.cases_reset_to_draft.length) parts.push(`${r.cases_reset_to_draft.length} 用例回退待审`)
  if (r.requirements_staled.length) parts.push(`${r.requirements_staled.length} 需求标 stale`)
  if (r.conflicts.length) parts.push(`${r.conflicts.length} 处冲突`)
  return parts.join('  ')
}

/**
 * Preview or commit an ATERag bundle.
 *
 * `dryRun` defaults to true on purpose — the caller has to pass `false`
 * explicitly to write anything.
 */
export async function importBundle(
  bundleJson: string,
  dryRun: boolean,
  confirmOverwrite = false,
): Promise<ImportPreview> {
  const response = await api.post<ImportResult>('/imports/aterag', JSON.parse(bundleJson), {
    params: { dry_run: dryRun, confirm_overwrite: confirmOverwrite },
  })
  return { ...response.data, message: describe(response.data) }
}

/** Plan the test sequence for a bundle (server-side, no writes). */
export async function planBundle(bundleJson: string): Promise<PlanPreview> {
  const response = await api.post<PlanPreview>('/imports/aterag/plan', JSON.parse(bundleJson))
  return response.data
}
