import axios from 'axios'
import type { YamlSequence } from '@/types/dsl'
import type { NodeGroup } from '@/models/nodes/types'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 10000,
  headers: {
    'Content-Type': 'application/json',
  },
})

/**
 * Sequence entity returned from the backend
 */
export interface Sequence {
  id: string
  name: string
  description?: string
  version: string
  yaml_content: string
  tags?: string[]
  author?: string
  created_at?: string
  updated_at?: string
  groups?: NodeGroup[]
}

/**
 * Payload for creating a new sequence
 */
export interface SequenceCreate {
  name: string
  description?: string
  version: string
  yaml_content: string
  tags?: string[]
}

/**
 * Payload for updating an existing sequence
 */
export interface SequenceUpdate {
  name?: string
  description?: string
  version?: string
  yaml_content?: string
  tags?: string[]
}

/**
 * Response type for sequence list endpoint
 */
export interface SequenceListResponse {
  items: Sequence[]
  total: number
}

/**
 * Parsed sequence with graph data for frontend use
 */
export interface SequenceDetail extends Sequence {
  parsed?: YamlSequence
}

/**
 * Fetch all available sequences
 */
export async function fetchSequences(): Promise<Sequence[]> {
  const response = await api.get<SequenceListResponse>('/sequences')
  return response.data.items
}

/**
 * Fetch a single sequence by ID
 */
export async function fetchSequenceById(id: string): Promise<SequenceDetail> {
  const response = await api.get<Sequence>(`/sequences/${id}`)
  return response.data as SequenceDetail
}

/**
 * Create a new sequence
 */
export async function createSequence(data: SequenceCreate): Promise<Sequence> {
  const response = await api.post<Sequence>('/sequences', data)
  return response.data
}

/**
 * Update an existing sequence
 */
export async function updateSequence(id: string, data: SequenceUpdate): Promise<Sequence> {
  const response = await api.put<Sequence>(`/sequences/${id}`, data)
  return response.data
}

/**
 * Delete a sequence by ID
 */
export async function deleteSequence(id: string): Promise<void> {
  await api.delete(`/sequences/${id}`)
}