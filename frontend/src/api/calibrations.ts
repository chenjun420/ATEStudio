import http from './interceptor'

const api = http

/** Calibration status values returned by the backend. */
export type CalibrationStatus = 'VALID' | 'EXPIRING' | 'EXPIRED' | 'UNKNOWN'

/** A single calibration record. */
export interface CalibrationRecord {
  id: string
  instrument_id: string
  last_calibration: string
  interval_days: number
  next_due: string
  status: 'VALID' | 'EXPIRING' | 'EXPIRED'
  notes: string | null
  created_at: string
  updated_at: string
}

/** Payload for recording a new calibration result. */
export interface CalibrationCreate {
  instrument_id: string
  last_calibration?: string
  interval_days: number
  notes?: string
}

/** Payload for partially updating an existing record. */
export interface CalibrationUpdate {
  instrument_id?: string
  last_calibration?: string
  interval_days?: number
  notes?: string
}

/** Response from the status-check endpoint. */
export interface CalibrationStatusResponse {
  instrument_id: string
  status: CalibrationStatus
  next_due: string | null
  days_until_due: number | null
  record: CalibrationRecord | null
}

/** Paginated-style list response. */
export interface CalibrationListResponse {
  items: CalibrationRecord[]
  total: number
}

/** Response from the check-expiry refresh endpoint. */
export interface CheckExpiryResponse {
  updated: number
}

/** List calibration records with optional filters. */
export async function fetchCalibrations(
  instrumentId?: string,
  statusFilter?: 'VALID' | 'EXPIRING' | 'EXPIRED',
): Promise<CalibrationRecord[]> {
  const params: Record<string, string> = {}
  if (instrumentId) params.instrument_id = instrumentId
  if (statusFilter) params.status = statusFilter
  const response = await api.get<CalibrationListResponse>('/calibrations', { params })
  return response.data.items
}

/** Get the latest calibration record for a single instrument. */
export async function fetchCalibration(instrumentId: string): Promise<CalibrationRecord> {
  const response = await api.get<CalibrationRecord>(`/calibrations/${encodeURIComponent(instrumentId)}`)
  return response.data
}

/** Check the calibration status for a single instrument. */
export async function fetchCalibrationStatus(
  instrumentId: string,
): Promise<CalibrationStatusResponse> {
  const response = await api.get<CalibrationStatusResponse>('/calibrations/status', {
    params: { instrument_id: instrumentId },
  })
  return response.data
}

/** Record a new calibration result (create or update latest for the instrument). */
export async function recordCalibration(data: CalibrationCreate): Promise<CalibrationRecord> {
  const response = await api.post<CalibrationRecord>('/calibrations', data)
  return response.data
}

/** Partially update a calibration record. */
export async function updateCalibration(
  instrumentId: string,
  data: CalibrationUpdate,
): Promise<CalibrationRecord> {
  const response = await api.put<CalibrationRecord>(
    `/calibrations/${encodeURIComponent(instrumentId)}`,
    data,
  )
  return response.data
}

/** Delete all calibration records for an instrument. */
export async function deleteCalibration(instrumentId: string): Promise<void> {
  await api.delete(`/calibrations/${encodeURIComponent(instrumentId)}`)
}

/** Refresh the status column for all calibration records. */
export async function checkExpiry(): Promise<CheckExpiryResponse> {
  const response = await api.post<CheckExpiryResponse>('/calibrations/check-expiry')
  return response.data
}
