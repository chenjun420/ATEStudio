import http from './interceptor'

const api = http

/**
 * 调试断点（§8.4 仿真调试控制台断点）。
 */
export interface DebugBreakpoint {
  id: string
  session_id: string | null
  step_id: string | null
  node_id: string | null
  line_number: number | null
  condition: string | null
  enabled: boolean
  node_data: Record<string, unknown> | null
  created_at?: string
}

/**
 * 创建断点请求。
 */
export interface DebugBreakpointCreate {
  session_id?: string | null
  step_id?: string | null
  node_id?: string | null
  line_number?: number | null
  condition?: string | null
  enabled?: boolean
  node_data?: Record<string, unknown> | null
}

/**
 * POST /api/v1/debug/breakpoints - 创建断点（需 ATE_DEV_MODE=true）。
 */
export async function createBreakpoint(data: DebugBreakpointCreate): Promise<DebugBreakpoint> {
  const response = await api.post<DebugBreakpoint>('/debug/breakpoints', data)
  return response.data
}

/**
 * GET /api/v1/debug/breakpoints - 断点列表（可按 session_id 过滤）。
 */
export async function listBreakpoints(sessionId?: string): Promise<{ items: DebugBreakpoint[]; total: number }> {
  const response = await api.get<{ items: DebugBreakpoint[]; total: number }>('/debug/breakpoints', {
    params: sessionId ? { session_id: sessionId } : {},
  })
  return response.data
}

/**
 * DELETE /api/v1/debug/breakpoints/{id} - 删除断点。
 */
export async function deleteBreakpoint(id: string): Promise<void> {
  await api.delete(`/debug/breakpoints/${id}`)
}
