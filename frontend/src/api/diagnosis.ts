/**
 * Diagnosis API module (task 22).
 *
 * Thin typed client over the backend AI-diagnosis endpoint:
 *
 * - `POST /api/v1/diagnose` — hybrid retrieval (Qdrant + ontology KG) plus LLM
 *   analysis; every diagnosis is persisted server-side and linked to the run /
 *   session when supplied.
 *
 * All transport goes through the shared axios instance (`@/api/interceptor`)
 * which carries the JWT interceptor and the 401 refresh flow — there is no
 * bare `axios.create` / raw fetch here.
 *
 * Backend contract: src/ate_cloud/api/v1/diagnose.py
 *   DiagnoseRequest  { product_type, failed_test, error_code?, log_snippet?,
 *                      run_id?, session_id? }
 *   DiagnoseResponse { diagnosis_id, root_cause, confidence,
 *                      evidence_citations[], repair_steps[], retrieved_cases[] }
 */
import http from './interceptor'

const api = http

/** Request body for POST /diagnose (mirrors backend DiagnoseRequest). */
export interface DiagnoseRequest {
  /** Product type identifier. */
  product_type: string
  /** Name/description of the failed test. */
  failed_test: string
  /** Error code if available. */
  error_code?: string
  /** Log fragment from the failed execution. */
  log_snippet?: string
  /** Execution run id to link the persisted diagnosis to. */
  run_id?: string
  /** Edge/NATS session reference. */
  session_id?: string
}

/** Response body for POST /diagnose (mirrors backend DiagnoseResponse). */
export interface DiagnoseResponse {
  /** Unique diagnosis id, used for later feedback. */
  diagnosis_id: string
  /** Primary root-cause explanation. */
  root_cause: string
  /** Confidence score in [0, 1]. */
  confidence: number
  /** Citations referencing retrieved cases. */
  evidence_citations: string[]
  /** Actionable repair steps. */
  repair_steps: string[]
  /** Raw retrieved failure cases (for transparency). */
  retrieved_cases: Array<Record<string, unknown>>
}

/**
 * Diagnose a test failure. Sends the run/symptom payload to POST /diagnose and
 * returns the persisted diagnosis. Rejects with the shared http client's error
 * (503 LLM circuit-breaker open, 502 diagnosis failure, etc.).
 */
export async function diagnoseFault(payload: DiagnoseRequest): Promise<DiagnoseResponse> {
  const response = await api.post<DiagnoseResponse>('/diagnose', payload)
  return response.data
}
