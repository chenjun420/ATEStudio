import http from './interceptor'

const api = http

// ─── 工装拓扑数据模型（设计文档 §8.3.2）─────────────────────────────────

/**
 * 仪器通道信号类型。
 */
export type ChannelType =
  | 'voltage'
  | 'current'
  | 'resistance'
  | 'digital_io'
  | 'rf'
  | 'thermal'

/**
 * 通道方向。
 */
export type ChannelDirection = 'input' | 'output' | 'bidirectional'

export interface Channel {
  id: string
  name: string
  type: ChannelType
  direction: ChannelDirection
  specs?: Record<string, unknown>
  status?: string
}

/**
 * 仪器类型（§8.3.2 Instrument.type）。
 */
export type InstrumentType =
  | 'psu'
  | 'dmm'
  | 'eload'
  | 'oscilloscope'
  | 'gpib_gateway'
  | 'tcp_device'
  | 'custom'

export interface CommunicationConfig {
  type: 'gpib' | 'tcp' | 'serial' | 'usb' | 'custom'
  address?: string | null
  port?: number | null
  config?: Record<string, unknown>
}

/**
 * 仪器仪表。
 */
export interface Instrument {
  id: string
  name: string
  type: InstrumentType
  model?: string
  manufacturer?: string
  communication?: CommunicationConfig
  channels: Channel[]
  status?: string
  position?: { x: number; y: number }
  simulation_profile?: string | null
}

/**
 * 继电器。
 */
export interface Relay {
  id: string
  type?: 'spst' | 'spdt' | 'dpdt' | 'matrix'
  control_signal?: string | null
  contacts?: Record<string, unknown>
  state?: 'open' | 'closed'
}

/**
 * 执行器。
 */
export interface Actuator {
  id: string
  type?: 'cylinder' | 'motor' | 'valve'
  controlMethod?: 'gpio' | 'modbus' | 'tcp'
  state?: string
}

/**
 * 传感器。
 */
export interface Sensor {
  id: string
  type?: 'position' | 'temperature' | 'pressure' | 'proximity' | 'optical'
  unit?: string
  value?: number | null
  range?: { min: number; max: number }
}

/**
 * 夹具端子。
 */
export interface Terminal {
  id: string
  name?: string
  signal_type?: string
  position?: { x: number; y: number }
}

/**
 * 夹具。
 */
export interface Fixture {
  id: string
  name: string
  version?: string
  terminals: Terminal[]
  relays?: Relay[]
  sensors?: Sensor[]
  actuators?: Actuator[]
  status?: string
  dut_slot_count?: number
  position?: { x: number; y: number }
}

/**
 * DUT 测试点。
 */
export interface TestPoint {
  id: string
  net?: string
  type?: 'voltage' | 'current' | 'resistance' | 'frequency' | 'digital'
  expected_range?: { min: number; max: number }
  measured_value?: number | null
  status?: string
}

/**
 * 被测产品。
 */
export interface DUT {
  id: string
  product_model?: string
  serial_number?: string | null
  test_points: TestPoint[]
  power_pins?: Array<Record<string, unknown>>
  uutIndex?: number
  slot_index?: number
  status?: string
  measurements?: Record<string, unknown>
  position?: { x: number; y: number }
}

/**
 * 链路端点实体类型。
 */
export type LinkEndpointType =
  | 'instrument_channel'
  | 'fixture_terminal'
  | 'dut_testpoint'
  | 'relay_contact'

export interface LinkEndpoint {
  entity_type: LinkEndpointType
  entity_id: string
  port_id: string
}

/**
 * 接线链路。
 */
export interface Link {
  id: string
  from: LinkEndpoint
  to: LinkEndpoint
  signal_type: 'power' | 'signal' | 'ground' | 'rf' | 'thermal' | 'air'
  wire_gauge?: string | null
  max_current?: number | null
  routeId?: string | null
  status?: 'idle' | 'active' | 'fault' | 'warning'
  fault_info?: FaultInfo | null
}

/**
 * 信号路径（矩阵开关路由）。
 */
export interface Route {
  id: string
  name: string
  links?: string[]
  relays?: string[]
  active?: boolean
  associated_step?: string | null
}

/**
 * 故障信息。
 */
export interface FaultInfo {
  type: string
  severity: 'warning' | 'error' | 'critical'
  message: string
  detected_at?: number
  detected_by?: string
  suggestion?: string | null
}

/**
 * 工装拓扑（§8.3.2 FixtureTopology）。
 */
export interface FixtureTopologyData {
  name?: string
  version?: string
  product_model?: string
  instruments: Instrument[]
  fixtures: Fixture[]
  duts: DUT[]
  links: Link[]
  routes: Route[]
}

// ─── API 响应类型 ───────────────────────────────────────────────────────────

/**
 * 工装配置资源（API 响应）。
 */
export interface FixtureTopologyResponse {
  id: string
  name: string
  version: string
  description: string | null
  product_model: string | null
  topology_data: FixtureTopologyData
  created_by: string | null
  tags: string[]
  created_at: string
  updated_at: string
}

/**
 * 列表响应（GET /api/v1/fixtures）。
 */
export interface FixtureTopologyListResponse {
  items: FixtureTopologyResponse[]
  total: number
}

/**
 * 校验问题条目。
 */
export interface ValidationIssue {
  code?: string
  level?: 'error' | 'warning'
  message: string
  entity_id?: string | null
  entity_type?: string | null
}

/**
 * 校验结果（POST /api/v1/fixtures/{id}/validate）。
 */
export interface ValidationResult {
  valid: boolean
  errors: ValidationIssue[]
  warnings: ValidationIssue[]
  summary?: string
}

/**
 * 版本历史条目。
 */
export interface FixtureVersionResponse {
  id: string
  topology_id: string
  version: string
  change_log: string | null
  topology_data: FixtureTopologyData
  created_at: string
}

/**
 * 导出响应（POST /api/v1/fixtures/{id}/export）。
 */
export interface FixtureExportResponse {
  format: 'json' | 'yaml'
  content: string
}

/**
 * 设备模板。
 */
export interface FixtureDeviceTemplate {
  id: string
  category: 'instrument' | 'fixture' | 'dut'
  type: string
  model: string
  manufacturer: string | null
  spec_data: Record<string, unknown>
  icon: string | null
  created_at: string
}

/**
 * 创建设备模板请求。
 */
export interface FixtureDeviceTemplateCreate {
  category: 'instrument' | 'fixture' | 'dut'
  type: string
  model: string
  manufacturer?: string | null
  spec_data: Record<string, unknown>
  icon?: string | null
}

// ─── API 函数 ───────────────────────────────────────────────────────────────

/**
 * GET /api/v1/fixtures - 工装拓扑列表。
 */
export async function listFixtureTopologies(
  params: { skip?: number; limit?: number; product_model?: string } = {},
): Promise<FixtureTopologyListResponse> {
  const response = await api.get<FixtureTopologyListResponse>('/fixtures', {
    params: { skip: 0, limit: 100, ...params },
  })
  return response.data
}

/**
 * GET /api/v1/fixtures/{id} - 工装拓扑详情。
 */
export async function getFixtureTopology(id: string): Promise<FixtureTopologyResponse> {
  const response = await api.get<FixtureTopologyResponse>(`/fixtures/${id}`)
  return response.data
}

/**
 * POST /api/v1/fixtures - 创建工装拓扑。
 */
export async function createFixtureTopology(
  data: {
    name: string
    version?: string
    description?: string | null
    product_model?: string | null
    topology_data: FixtureTopologyData
    created_by?: string | null
    tags?: string[]
  },
): Promise<FixtureTopologyResponse> {
  const response = await api.post<FixtureTopologyResponse>('/fixtures', data)
  return response.data
}

/**
 * PUT /api/v1/fixtures/{id} - 更新工装拓扑（未指定 version 时自动递增）。
 */
export async function updateFixtureTopology(
  id: string,
  data: {
    name?: string
    version?: string
    description?: string | null
    product_model?: string | null
    topology_data?: FixtureTopologyData
    tags?: string[]
  },
): Promise<FixtureTopologyResponse> {
  const response = await api.put<FixtureTopologyResponse>(`/fixtures/${id}`, data)
  return response.data
}

/**
 * DELETE /api/v1/fixtures/{id} - 删除工装拓扑。
 */
export async function deleteFixtureTopology(id: string): Promise<void> {
  await api.delete(`/fixtures/${id}`)
}

/**
 * POST /api/v1/fixtures/{id}/validate - 8 类接线校验（§8.3.5）。
 */
export async function validateFixtureTopology(
  id: string,
  strictness: 'error' | 'warning' = 'error',
): Promise<ValidationResult> {
  const response = await api.post<ValidationResult>(
    `/fixtures/${id}/validate`,
    {},
    { params: { strictness } },
  )
  return response.data
}

/**
 * POST /api/v1/fixtures/{id}/duplicate - 复制工装拓扑。
 */
export async function duplicateFixtureTopology(id: string): Promise<FixtureTopologyResponse> {
  const response = await api.post<FixtureTopologyResponse>(`/fixtures/${id}/duplicate`)
  return response.data
}

/**
 * GET /api/v1/fixtures/{id}/versions - 版本历史。
 */
export async function listFixtureVersions(id: string): Promise<FixtureVersionResponse[]> {
  const response = await api.get<FixtureVersionResponse[]>(`/fixtures/${id}/versions`)
  return response.data
}

/**
 * POST /api/v1/fixtures/{id}/export - 导出 JSON/YAML。
 */
export async function exportFixtureTopology(
  id: string,
  format: 'json' | 'yaml' = 'json',
  version?: string,
): Promise<FixtureExportResponse> {
  const response = await api.post<FixtureExportResponse>(
    `/fixtures/${id}/export`,
    {},
    { params: { format, version } },
  )
  return response.data
}

/**
 * GET /api/v1/fixtures/templates - 设备模板列表。
 */
export async function listDeviceTemplates(
  category?: 'instrument' | 'fixture' | 'dut',
): Promise<FixtureDeviceTemplate[]> {
  const response = await api.get<FixtureDeviceTemplate[]>('/fixtures/templates', {
    params: category ? { category } : {},
  })
  return response.data
}

/**
 * POST /api/v1/fixtures/templates - 创建设备模板。
 */
export async function createDeviceTemplate(
  data: FixtureDeviceTemplateCreate,
): Promise<FixtureDeviceTemplate> {
  const response = await api.post<FixtureDeviceTemplate>('/fixtures/templates', data)
  return response.data
}

/**
 * 下载导出文件（浏览器触发）。
 */
export function downloadFixtureExport(content: string, format: 'json' | 'yaml', name: string): void {
  const mime = format === 'yaml' ? 'application/x-yaml' : 'application/json'
  const blob = new Blob([content], { type: `${mime};charset=utf-8` })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${name}.${format}`
  anchor.click()
  URL.revokeObjectURL(url)
}

// ─── 链路故障注入（T30，设计文档 §8.3）──────────────────────────────────────

/**
 * 链路故障类型（doc §8.3 规定集合，不得扩展）。
 */
export type LinkFaultKind = 'open_circuit' | 'short_circuit' | 'contact_resistance' | 'noise'

/**
 * POST /executions/{run_id}/fault-injection - 链路故障注入。
 *
 * 将右键菜单选择的故障类型转发给云端虚拟驱动（§8.3：禁止纯客户端模拟）。
 */
export async function injectLinkFault(
  runId: string,
  linkId: string,
  faultType: LinkFaultKind,
): Promise<void> {
  await api.post(`/executions/${runId}/fault-injection`, {
    link_id: linkId,
    fault_type: faultType,
  })
}

// ─── 历史故障热力图（T35，设计文档 §8.3）────────────────────────────────────

/**
 * 单链路历史故障统计条目。
 */
export interface FaultStatEntry {
  count: number
  last_seen?: string | null
}

/**
 * GET /fixtures/{id}/fault-stats 响应。
 */
export interface FixtureFaultStatsResponse {
  links: Record<string, FaultStatEntry>
  generated_at: string
}

/**
 * GET /fixtures/{id}/fault-stats - 按链路聚合历史故障频次（热力图数据源）。
 *
 * 懒加载约定：仅在用户打开 热力图 开关时调用，不参与初始加载（§8.3）。
 */
export async function getFaultStats(id: string): Promise<FixtureFaultStatsResponse> {
  const response = await api.get<FixtureFaultStatsResponse>(`/fixtures/${id}/fault-stats`)
  return response.data
}
