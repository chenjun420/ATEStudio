/**
 * Knowledge read API module (tasks 25 & 26).
 *
 * Typed client over the backend knowledge READ endpoints (JWT-protected):
 *
 * - `GET /knowledge/traceability`  — requirement → cases → DSL-step tree.
 * - `GET /knowledge/requirements`  — paged TestRequirement list ({items,total}).
 * - `GET /knowledge/cases`         — paged TestCase list joined to requirement.
 *
 * A `GET /knowledge/graph` client used to live here and was removed with the
 * knowledge-graph subsystem, along with the view that consumed it. It was not
 * a client for something that had been switched off: the endpoint it called
 * returned an honest 503 on every deployment, because the backend it needed was
 * never provisioned.
 *
 * All transport uses the shared axios instance (`@/api/interceptor`) with the
 * JWT interceptor + 401 refresh — no bare `axios.create` / raw fetch.
 *
 * Backend contracts: src/ate_cloud/api/v1/knowledge_reads.py,
 * src/ate_cloud/schemas/knowledge.py.
 */
import http from './interceptor'

const api = http

// ── Requirements / cases paged lists (task 26) ───────────────────────────────

/** A persisted test requirement (mirrors backend TestRequirementResponse). */
export interface TestRequirement {
  id: string
  product_code: string
  requirement_code: string
  title: string
  description: string | null
  source: 'dsl' | 'atml' | 'manual'
  atml_ref: string | null
  created_at: string
  updated_at: string
}

/** A test case joined to its requirement for the traceability matrix. */
export interface TestCase {
  id: string
  requirement_id: string | null
  case_code: string
  title: string
  sequence_id: string | null
  step_id: string
  atml_ref: string | null
  status: string
  created_at: string
  updated_at: string
  /** Denormalized from the linked requirement (null for an orphan case). */
  product_code: string | null
  /** Denormalized from the linked requirement (null for an orphan case). */
  requirement_code: string | null
}

/** Paged list envelope ({items,total}) shared by the requirements/cases lists. */
export interface Paged<T> {
  items: T[]
  total: number
}

/** Query params for GET /knowledge/requirements. */
export interface RequirementListParams {
  product_code?: string
  source?: string
  skip?: number
  limit?: number
}

/** Query params for GET /knowledge/cases. */
export interface CaseListParams {
  requirement_id?: string
  product_code?: string
  skip?: number
  limit?: number
}

/** Fetch a paged list of test requirements. */
export async function fetchRequirements(
  params: RequirementListParams = {},
): Promise<Paged<TestRequirement>> {
  const query: Record<string, string | number> = {}
  if (params.product_code) query.product_code = params.product_code
  if (params.source) query.source = params.source
  if (params.skip !== undefined) query.skip = params.skip
  if (params.limit !== undefined) query.limit = params.limit
  const response = await api.get<Paged<TestRequirement>>('/knowledge/requirements', {
    params: query,
  })
  return response.data
}

/** Fetch a paged list of test cases (joined to their requirement). */
export async function fetchCases(params: CaseListParams = {}): Promise<Paged<TestCase>> {
  const query: Record<string, string | number> = {}
  if (params.requirement_id) query.requirement_id = params.requirement_id
  if (params.product_code) query.product_code = params.product_code
  if (params.skip !== undefined) query.skip = params.skip
  if (params.limit !== undefined) query.limit = params.limit
  const response = await api.get<Paged<TestCase>>('/knowledge/cases', { params: query })
  return response.data
}

// ── Traceability tree (task 26) ──────────────────────────────────────────────

/** One case row inside the traceability tree (case → DSL step mapping). */
export interface TraceabilityCase {
  id: string
  case_code: string
  title: string
  sequence_id: string | null
  step_id: string
  atml_ref: string | null
  status: string
}

/** A requirement with its verifying cases (requirement → cases → steps). */
export interface TraceabilityRequirement {
  id: string
  requirement_code: string
  title: string
  source: 'dsl' | 'atml' | 'manual'
  cases: TraceabilityCase[]
}

/** requirement → cases → DSL-step tree returned by GET /knowledge/traceability. */
export interface TraceabilityTree {
  product_code: string | null
  requirements: TraceabilityRequirement[]
  /** Cases ingested without a linked requirement (traceability gaps). */
  unlinked_cases: TraceabilityCase[]
}

/**
 * Fetch the requirement → cases → DSL-step traceability tree.
 *
 * Cases without a requirement land in `unlinked_cases` so matrix gaps (cases
 * ingested before their requirement) stay visible.
 */
export async function fetchTraceability(
  productCode?: string,
): Promise<TraceabilityTree> {
  const params: Record<string, string> = {}
  if (productCode) params.product_code = productCode
  const response = await api.get<TraceabilityTree>('/knowledge/traceability', { params })
  return response.data
}
