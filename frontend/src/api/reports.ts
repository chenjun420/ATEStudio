import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

/**
 * Download a test report in the specified format.
 * GET /api/v1/reports/{format}/{executionId}
 *
 * format: 'atml' | 'csv' | 'parquet'
 * Returns a Blob (file download).
 */
export async function downloadReport(
  executionId: string,
  format: 'atml' | 'csv' | 'parquet',
): Promise<Blob> {
  const response = await api.get(`/reports/${format}/${executionId}`, {
    responseType: 'blob',
  })
  return response.data
}

/**
 * Get ATML XML report as text.
 * GET /api/v1/reports/atml/{executionId}
 */
export async function getAtmlReport(executionId: string): Promise<string> {
  const blob = await downloadReport(executionId, 'atml')
  return blob.text()
}

/**
 * Trigger file download in browser.
 */
export function triggerFileDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
