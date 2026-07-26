import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 10000,
  headers: {
    'Content-Type': 'application/json',
  },
})

export interface NodeTemplate {
  id: string
  name: string
  type: string
  appearance?: Record<string, unknown>
  default_data?: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface NodeTemplateCreate {
  name: string
  type: string
  appearance?: Record<string, unknown>
  default_data?: Record<string, unknown>
}

export interface NodeTemplateUpdate {
  name?: string
  type?: string
  appearance?: Record<string, unknown>
  default_data?: Record<string, unknown>
}

export interface NodeTemplateListResponse {
  items: NodeTemplate[]
  total: number
}

/**
 * Fetch all node templates
 */
export async function listNodeTemplates(): Promise<NodeTemplate[]> {
  const response = await api.get<NodeTemplateListResponse>('/node-templates')
  return response.data.items
}

/**
 * Fetch a single node template by ID
 */
export async function getNodeTemplate(id: string): Promise<NodeTemplate> {
  const response = await api.get<NodeTemplate>(`/node-templates/${id}`)
  return response.data
}

/**
 * Create a new node template
 */
export async function createNodeTemplate(
  data: NodeTemplateCreate
): Promise<NodeTemplate> {
  const response = await api.post<NodeTemplate>('/node-templates', data)
  return response.data
}

/**
 * Update an existing node template
 */
export async function updateNodeTemplate(
  id: string,
  data: NodeTemplateUpdate
): Promise<NodeTemplate> {
  const response = await api.put<NodeTemplate>(`/node-templates/${id}`, data)
  return response.data
}

/**
 * Delete a node template
 */
export async function deleteNodeTemplate(id: string): Promise<void> {
  await api.delete(`/node-templates/${id}`)
}
