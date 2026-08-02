import http from './interceptor'
import type { UserResponse } from './auth'

export interface UserCreate {
  username: string
  password: string
  role: string
  scopes?: string[]
  is_active?: boolean
}

export interface UserUpdate {
  password?: string
  role?: string
  scopes?: string[]
  is_active?: boolean
}

export interface UserListResponse {
  items: UserResponse[]
  total: number
}

export async function listUsers(): Promise<UserListResponse> {
  const response = await http.get<UserListResponse>('/users')
  return response.data
}

export async function createUser(data: UserCreate): Promise<UserResponse> {
  const response = await http.post<UserResponse>('/users', data)
  return response.data
}

export async function updateUser(id: string, data: UserUpdate): Promise<UserResponse> {
  const response = await http.put<UserResponse>(`/users/${id}`, data)
  return response.data
}

export async function deleteUser(id: string): Promise<void> {
  await http.delete(`/users/${id}`)
}

export interface PasswordChangeRequest {
  old_password: string
  new_password: string
}

export async function changePassword(old_password: string, new_password: string): Promise<void> {
  await http.put('/users/me/password', { old_password, new_password })
}

export async function deactivateAccount(): Promise<void> {
  await http.post('/users/me/deactivate')
}
