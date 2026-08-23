import http from './interceptor'
import type { OfflineStatusSnapshot, ReconcileReport } from '@/utils/offlineStatus'

const api = http

/**
 * GET /api/v1/offline/status - offline badge snapshot (T24 contract).
 */
export async function fetchOfflineStatus(): Promise<OfflineStatusSnapshot> {
  const response = await api.get<OfflineStatusSnapshot>('/offline/status')
  return response.data
}

/**
 * POST /api/v1/offline/reconcile - manual reconciliation trigger (202).
 */
export async function triggerReconcile(): Promise<ReconcileReport> {
  const response = await api.post<ReconcileReport>('/offline/reconcile')
  return response.data
}
