import http from './interceptor'

const api = http

/**
 * SPC capability indices and process statistics for one stream.
 */
export interface SPCStatistics {
  product_type: string
  measurement_name: string
  sample_count: number
  mean: number | null
  std_dev_within: number | null
  std_dev_overall: number | null
  cp: number | null
  cpk: number | null
  ppk: number | null
  usl: number | null
  lsl: number | null
  last_updated: string | null
}

/**
 * One subgroup's X-bar and R statistics.
 */
export interface SPCSubgroupStat {
  index: number
  mean: number
  range: number
  sample_count: number
}

/**
 * X-bar / R control chart data with control limits.
 */
export interface SPCChart {
  product_type: string
  measurement_name: string
  center_line: number | null
  ucl: number | null
  lcl: number | null
  r_center: number | null
  r_ucl: number | null
  r_lcl: number | null
  subgroup_size: number
  subgroups: SPCSubgroupStat[]
}

/**
 * An SPC alert raised by anomaly detection.
 */
export interface SPCAlert {
  product_type: string
  measurement_name: string
  rule: string
  severity: string
  message: string
  value: number | null
  timestamp: string
  sample_count: number
}

/**
 * GET /api/v1/spc/{product_type}/{measurement_name} — SPC statistics.
 */
export async function getSPCStatistics(
  productType: string,
  measurementName: string,
  limit = 100,
): Promise<SPCStatistics> {
  const response = await api.get<SPCStatistics>(
    `/spc/${encodeURIComponent(productType)}/${encodeURIComponent(measurementName)}`,
    { params: { limit } },
  )
  return response.data
}

/**
 * GET /api/v1/spc/{product_type}/{measurement_name}/chart — X-bar / R chart data.
 */
export async function getSPCChart(
  productType: string,
  measurementName: string,
  limit = 100,
): Promise<SPCChart> {
  const response = await api.get<SPCChart>(
    `/spc/${encodeURIComponent(productType)}/${encodeURIComponent(measurementName)}/chart`,
    { params: { limit } },
  )
  return response.data
}

/**
 * GET /api/v1/spc/alerts — recent SPC alerts.
 */
export async function getSPCAlerts(limit = 50): Promise<SPCAlert[]> {
  const response = await api.get<SPCAlert[]>('/spc/alerts', { params: { limit } })
  return response.data
}
