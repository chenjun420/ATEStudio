import http from './interceptor'

const api = http

/** FMEA rating bounds — mirrors backend schemas/knowledge.py RATING_MIN/MAX. */
export const RATING_MIN = 1
export const RATING_MAX = 10

/** High-RPN row highlighting thresholds (client-side display only). */
export const RPN_DANGER_THRESHOLD = 100
export const RPN_WARNING_THRESHOLD = 60

/** A persisted FMEA entry. `rpn` is computed server-side (severity*occurrence*detection). */
export interface FmeaRecord {
  id: string
  component_code: string
  function_name: string | null
  fault_code: string | null
  failure_mode: string
  effects: string | null
  cause: string | null
  severity: number
  occurrence: number
  detection: number
  rpn: number
  recommended_action: string | null
  created_at: string
  updated_at: string
}

/**
 * Create payload for an FMEA entry.
 *
 * Deliberately has NO `rpn` field: the server derives it as S*O*D and
 * ignores any client-supplied value. Ratings are integers 1-10.
 */
export interface FmeaCreate {
  component_code: string
  function_name?: string | null
  fault_code?: string | null
  failure_mode: string
  effects?: string | null
  cause?: string | null
  severity: number
  occurrence: number
  detection: number
  recommended_action?: string | null
}

/**
 * Partial-update payload for an FMEA entry.
 *
 * Every field optional; supplied ratings still constrained to 1-10. `rpn` is
 * absent — always derived server-side.
 */
export interface FmeaUpdate {
  component_code?: string
  function_name?: string | null
  fault_code?: string | null
  failure_mode?: string
  effects?: string | null
  cause?: string | null
  severity?: number
  occurrence?: number
  detection?: number
  recommended_action?: string | null
}

/** Paginated list response ({items,total}) returned by GET /fmea. */
export interface FmeaListResponse {
  items: FmeaRecord[]
  total: number
}

/** Query parameters for the list endpoint. */
export interface FmeaListParams {
  component_code?: string
  fault_code?: string
  skip?: number
  limit?: number
}

/** Compute RPN from the three ratings (client-side display; server is authoritative). */
export function computeRpn(severity: number, occurrence: number, detection: number): number {
  return severity * occurrence * detection
}

/** True when a rating is an integer within the allowed 1-10 range. */
export function isValidRating(value: number | null | undefined): boolean {
  return (
    typeof value === 'number' &&
    Number.isInteger(value) &&
    value >= RATING_MIN &&
    value <= RATING_MAX
  )
}

/** Row risk band derived from RPN, for color-coded highlighting. */
export type RpnBand = 'danger' | 'warning' | 'normal'

export function rpnBand(rpn: number): RpnBand {
  if (rpn >= RPN_DANGER_THRESHOLD) return 'danger'
  if (rpn >= RPN_WARNING_THRESHOLD) return 'warning'
  return 'normal'
}

/** List FMEA entries with optional filters. */
export async function fetchFmeas(params: FmeaListParams = {}): Promise<FmeaListResponse> {
  const query: Record<string, string | number> = {}
  if (params.component_code) query.component_code = params.component_code
  if (params.fault_code) query.fault_code = params.fault_code
  if (params.skip !== undefined) query.skip = params.skip
  if (params.limit !== undefined) query.limit = params.limit
  const response = await api.get<FmeaListResponse>('/fmea', { params: query })
  return response.data
}

/** Get a single FMEA entry by id. */
export async function fetchFmea(id: string): Promise<FmeaRecord> {
  const response = await api.get<FmeaRecord>(`/fmea/${encodeURIComponent(id)}`)
  return response.data
}

/** Create an FMEA entry. The returned record carries the server-computed rpn. */
export async function createFmea(data: FmeaCreate): Promise<FmeaRecord> {
  const response = await api.post<FmeaRecord>('/fmea', data)
  return response.data
}

/** Partially update an FMEA entry; the server recomputes rpn. */
export async function updateFmea(id: string, data: FmeaUpdate): Promise<FmeaRecord> {
  const response = await api.put<FmeaRecord>(`/fmea/${encodeURIComponent(id)}`, data)
  return response.data
}

/** Delete an FMEA entry. */
export async function deleteFmea(id: string): Promise<void> {
  await api.delete(`/fmea/${encodeURIComponent(id)}`)
}
