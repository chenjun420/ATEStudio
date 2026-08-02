import http from './interceptor'

export interface Permission {
  id: string
  code: string
  module: string
  description: string | null
  created_at: string
}

export interface PermissionListResponse {
  items: Permission[]
  total: number
}

export interface Role {
  id: string
  name: string
  description: string | null
  is_system: boolean
  is_active: boolean
  permissions: string[]
  created_at: string
}

export interface RoleCreate {
  name: string
  description?: string
  permissions?: string[]
}

export interface RoleUpdate {
  name?: string
  description?: string
  permissions?: string[]
  is_active?: boolean
}

export interface RoleListResponse {
  items: Role[]
  total: number
}

export async function listPermissions(): Promise<PermissionListResponse> {
  const response = await http.get<PermissionListResponse>('/rbac/permissions')
  return response.data
}

export async function listRoles(): Promise<RoleListResponse> {
  const response = await http.get<RoleListResponse>('/rbac/roles')
  return response.data
}

export async function getRole(id: string): Promise<Role> {
  const response = await http.get<Role>(`/rbac/roles/${id}`)
  return response.data
}

export async function createRole(data: RoleCreate): Promise<Role> {
  const response = await http.post<Role>('/rbac/roles', data)
  return response.data
}

export async function updateRole(id: string, data: RoleUpdate): Promise<Role> {
  const response = await http.put<Role>(`/rbac/roles/${id}`, data)
  return response.data
}

export async function deleteRole(id: string): Promise<void> {
  await http.delete(`/rbac/roles/${id}`)
}

export async function seedRBAC(): Promise<void> {
  await http.post('/rbac/roles/seed')
}
