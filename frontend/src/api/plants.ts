/**
 * 厂区 / 工位 / 工位故障案例 API.
 *
 * This is the first frontend client for these endpoints. They were built in P0
 * and verified end-to-end on the board, but nothing in `src/` called them — the
 * page that claimed to be the station list was reading `/workers`, a different
 * table entirely (see `WorkerRegistry.vue`). The two-level context selector
 * cannot be built without this file.
 *
 * Contracts: src/ate_cloud/api/v1/stations.py,
 *            src/ate_cloud/schemas/station.py.
 *
 * Note on naming: `api/stations.ts` already exists and is about *workers* —
 * it calls `/workers` and `/node-flow-bindings`. It is not a client for the
 * Station table and never was. This module is `/plants`, `/stations` and
 * `/fault-cases`.
 */
import http from './interceptor'

// ── 厂区 ──────────────────────────────────────────────────────────────────

export interface Plant {
  id: string
  code: string
  name: string
  parent_id: string | null
  is_active: boolean
  station_count: number
  created_at: string
  updated_at: string
}

export interface PlantListResponse {
  total: number
  items: Plant[]
}

export interface PlantCreate {
  code: string
  name: string
  parent_id?: string | null
}

export async function listPlants(): Promise<PlantListResponse> {
  const response = await http.get<PlantListResponse>('/plants')
  return response.data
}

export async function createPlant(data: PlantCreate): Promise<Plant> {
  const response = await http.post<Plant>('/plants', data)
  return response.data
}

/**
 * Delete a plant.
 *
 * The backend answers 409 when stations still reference it, rather than
 * cascading. That is deliberate — see the P0 notes on `stations.plant_id` being
 * NOT NULL — so the caller has to surface the conflict instead of the delete
 * appearing to work.
 */
export async function deletePlant(plantId: string): Promise<void> {
  await http.delete(`/plants/${plantId}`)
}

// ── 工位 ──────────────────────────────────────────────────────────────────

export interface Station {
  id: string
  plant_id: string
  plant_code: string | null
  plant_name: string | null
  code: string
  name: string
  attributes: string | null
  is_active: boolean
  fault_case_count: number
  created_at: string
  updated_at: string
}

export interface StationListResponse {
  total: number
  items: Station[]
}

export interface StationCreate {
  plant_id: string
  code: string
  name: string
  attributes?: string | null
}

export interface StationUpdate {
  name?: string
  attributes?: string | null
  is_active?: boolean
}

export async function listStations(
  plantId?: string,
  skip = 0,
  limit = 100,
  includeInactive = false,
): Promise<StationListResponse> {
  const response = await http.get<StationListResponse>('/stations', {
    params: { plant_id: plantId, skip, limit, include_inactive: includeInactive },
  })
  return response.data
}

export async function createStation(data: StationCreate): Promise<Station> {
  const response = await http.post<Station>('/stations', data)
  return response.data
}

export async function getStation(stationId: string): Promise<Station> {
  const response = await http.get<Station>(`/stations/${stationId}`)
  return response.data
}

/**
 * PATCH, not PUT — the backend uses PATCH and rejects unknown fields.
 *
 * `code` and `plant_id` are deliberately not updatable: both are part of the
 * station's identity, and the backend will not take them.
 */
export async function updateStation(
  stationId: string,
  data: StationUpdate,
): Promise<Station> {
  const response = await http.patch<Station>(`/stations/${stationId}`, data)
  return response.data
}

export async function deleteStation(stationId: string): Promise<void> {
  await http.delete(`/stations/${stationId}`)
}

// ── 工位故障案例 ──────────────────────────────────────────────────────────

export interface FaultCase {
  id: string
  station_id: string
  station_code: string | null
  station_name: string | null
  plant_id: string | null
  product_code: string | null
  symptom: string
  cause: string | null
  effect: string | null
  fix: string | null
  /**
   * Whether a person confirmed the fix worked.
   *
   * The UI must not present an unverified fix as the answer. This is the same
   * rule as "an unapproved condition must not become a judgement criterion",
   * and it is why the default is false server-side too.
   */
  fix_verified: boolean
  severity: number | null
  occurrence: number | null
  detection: number | null
  rpn: number | null
  source_diagnosis_id: string | null
  occurred_at: string
  created_at: string
  updated_at: string
}

export interface FaultCaseListResponse {
  total: number
  items: FaultCase[]
}

export interface FaultCaseCreate {
  station_id: string
  symptom: string
  cause?: string | null
  effect?: string | null
  fix?: string | null
  fix_verified?: boolean
  product_code?: string | null
  severity?: number | null
  occurrence?: number | null
  detection?: number | null
  rpn?: number | null
  occurred_at?: string | null
}

export type FaultCaseUpdate = Partial<Omit<FaultCaseCreate, 'station_id'>>

export interface FaultCaseQuery {
  station_id?: string
  plant_id?: string
  product_code?: string
  skip?: number
  limit?: number
}

export async function listFaultCases(
  query: FaultCaseQuery = {},
): Promise<FaultCaseListResponse> {
  const response = await http.get<FaultCaseListResponse>('/fault-cases', {
    params: query,
  })
  return response.data
}

export async function createFaultCase(data: FaultCaseCreate): Promise<FaultCase> {
  const response = await http.post<FaultCase>('/fault-cases', data)
  return response.data
}

export async function updateFaultCase(
  caseId: string,
  data: FaultCaseUpdate,
): Promise<FaultCase> {
  const response = await http.patch<FaultCase>(`/fault-cases/${caseId}`, data)
  return response.data
}

export async function deleteFaultCase(caseId: string): Promise<void> {
  await http.delete(`/fault-cases/${caseId}`)
}

export interface ReindexResponse {
  indexed: number
  skipped: number
  detail: string
}

/**
 * Rebuild the vector index over the fault cases.
 *
 * `skipped` is not a failure count, it is a count of cases that could not be
 * indexed — which on a deployment without embedding credentials is *every* case.
 * The caller has to show it, because a library that looks populated and
 * retrieves nothing is the failure this project keeps paying for.
 */
export async function reindexFaultCases(): Promise<ReindexResponse> {
  const response = await http.post<ReindexResponse>('/fault-cases/reindex')
  return response.data
}
