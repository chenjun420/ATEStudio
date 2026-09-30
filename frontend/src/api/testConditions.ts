/**
 * Test-condition API module (P5 review wizard).
 *
 * Typed client for the condition read/review endpoints:
 *
 * - `GET  /knowledge/conditions`          — paged, filterable list.
 * - `GET  /knowledge/conditions/summary`  — per-requirement work list.
 * - `POST /knowledge/conditions/approve`  — sign off one requirement's drafts.
 *
 * The approval call requires a signature (`by`). That is not ceremony: an
 * approval with no name on it cannot be audited, which makes it
 * indistinguishable from no approval — the state every condition starts in.
 * The UI therefore prompts for the name, and this module refuses to send a
 * blank one.
 *
 * Backend contracts: src/ate_cloud/api/v1/knowledge_conditions.py,
 * src/ate_cloud/schemas/test_conditions.py.
 */
import http from './interceptor'

const api = http

/** Review state of a condition. `draft` = awaiting a human decision. */
export type ConditionStatus = 'draft' | 'approved'

/** Which side of the UUT a condition describes. */
export type ConditionSide = 'input' | 'output'

/** A test condition clause as the review UI renders it. */
export interface TestCondition {
  id: string
  owner_type: string
  owner_id: string
  side: ConditionSide
  kind: string
  text: string
  value: Record<string, unknown> | null
  source: string
  confidence: string
  status: ConditionStatus
  method_ref: string | null
  cond_fingerprint: string
  created_at: string
  updated_at: string
  /** Denormalized for display so the table needs no per-row request. */
  requirement_code: string | null
  requirement_title: string | null
  section_path: string | null
}

/** Paged list envelope ({items,total}), matching the knowledge list shape. */
export interface ConditionPage {
  items: TestCondition[]
  total: number
}

/** One requirement's condition counts — a row in the review work list. */
export interface ConditionSummaryRow {
  requirement_id: string
  requirement_code: string
  title: string
  section_path: string | null
  /** `stale` = dropped from a newer spec revision; kept for traceability only. */
  requirement_status: string
  draft: number
  approved: number
  total: number
}

/** The review work list. */
export interface ConditionSummary {
  items: ConditionSummaryRow[]
  total: number
  /** Conditions across all requirements still awaiting a decision. */
  pending_total: number
}

/** Query params for GET /knowledge/conditions. */
export interface ConditionListParams {
  requirement_id?: string
  product_code?: string
  side?: ConditionSide
  kind?: string
  status?: ConditionStatus
  skip?: number
  limit?: number
}

/** Result of one approve call. */
export interface ConditionReviewResult {
  requirement_id: string
  approved: number
  skipped: number
  by: string
  message: string
}

/** Fetch a paged, filterable list of conditions. */
export async function fetchConditions(
  params: ConditionListParams = {},
): Promise<ConditionPage> {
  const query: Record<string, string | number> = {}
  if (params.requirement_id) query.requirement_id = params.requirement_id
  if (params.product_code) query.product_code = params.product_code
  if (params.side) query.side = params.side
  if (params.kind) query.kind = params.kind
  if (params.status) query.status = params.status
  if (params.skip !== undefined) query.skip = params.skip
  if (params.limit !== undefined) query.limit = params.limit
  const response = await api.get<ConditionPage>('/knowledge/conditions', { params: query })
  return response.data
}

/** Fetch the per-requirement review work list. */
export async function fetchConditionSummary(
  productCode?: string,
): Promise<ConditionSummary> {
  const params: Record<string, string> = {}
  if (productCode) params.product_code = productCode
  const response = await api.get<ConditionSummary>('/knowledge/conditions/summary', { params })
  return response.data
}

/**
 * Sign off the draft conditions of one requirement.
 *
 * `by` is required and must be non-blank — the caller collects the signature,
 * and this guard exists so a UI bug cannot quietly send an anonymous approval.
 */
export async function approveConditions(payload: {
  requirement_id: string
  by: string
  condition_ids?: string[]
  note?: string
}): Promise<ConditionReviewResult> {
  if (!payload.by || !payload.by.trim()) {
    throw new Error('批准必须署名: 无署名的批准无法审计, 与未签字不可区分')
  }
  const response = await api.post<ConditionReviewResult>('/knowledge/conditions/approve', {
    requirement_id: payload.requirement_id,
    by: payload.by.trim(),
    condition_ids: payload.condition_ids ?? null,
    note: payload.note ?? '',
  })
  return response.data
}
