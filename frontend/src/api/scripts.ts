import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 10000,
  headers: {
    'Content-Type': 'application/json',
  },
})

export interface Script {
  id: string
  name: string
  description?: string
  version: string
  script_path: string
  category?: string
  tags?: string[]
  params?: ScriptParam[]
  author?: string
  created_at?: string
  updated_at?: string
}

export interface ScriptParam {
  name: string
  type: 'string' | 'number' | 'boolean' | 'object' | 'array'
  description?: string
  default?: unknown
  required?: boolean
}

export interface ScriptListResponse {
  items: Script[]
  total: number
}

export interface ScriptCategory {
  name: string
  count: number
}

export interface ScriptContentResponse {
  content: string
  version: string
  last_modified: string | null
}

export interface ScriptContentUpdate {
  content: string
  commit_message?: string
}

export interface ScriptVersionInfo {
  hash: string
  message: string
  author: string
  timestamp: string
}

export interface ScriptVersionListResponse {
  versions: ScriptVersionInfo[]
}

/**
 * Fetch all available scripts
 */
export async function fetchScripts(): Promise<Script[]> {
  const response = await api.get<ScriptListResponse>('/scripts')
  return response.data.items
}

/**
 * Fetch a single script by ID
 */
export async function fetchScriptById(id: string): Promise<Script> {
  const response = await api.get<Script>(`/scripts/${id}`)
  return response.data
}

/**
 * Search scripts by name or description
 */
export async function searchScripts(query: string): Promise<Script[]> {
  const response = await api.get<ScriptListResponse>('/scripts', {
    params: { q: query },
  })
  return response.data.items
}

/**
 * Get script categories
 */
export async function fetchScriptCategories(): Promise<ScriptCategory[]> {
  const response = await api.get<ScriptCategory[]>('/scripts/categories')
  return response.data
}

/**
 * Fetch script content
 */
export async function fetchScriptContent(id: string): Promise<ScriptContentResponse> {
  const response = await api.get<ScriptContentResponse>(`/scripts/${id}/content`)
  return response.data
}

/**
 * Update script content
 */
export async function updateScriptContent(id: string, data: ScriptContentUpdate): Promise<ScriptContentResponse> {
  const response = await api.put<ScriptContentResponse>(`/scripts/${id}/content`, data)
  return response.data
}

/**
 * Fetch script version history
 */
export async function fetchScriptVersions(id: string): Promise<ScriptVersionListResponse> {
  const response = await api.get<ScriptVersionListResponse>(`/scripts/${id}/versions`)
  return response.data
}

/**
 * Fetch script content at a specific version
 */
export async function fetchScriptVersionContent(id: string, commitHash: string): Promise<ScriptContentResponse> {
  const response = await api.get<ScriptContentResponse>(`/scripts/${id}/versions/${commitHash}`)
  return response.data
}
