import http from './interceptor'

export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export interface UserResponse {
  id: string
  username: string
  role: string
  scopes: string[]
  is_active: boolean
}

export interface UserPreferences {
  theme_mode: string
  language: string
}

export interface UpdatePreferencesRequest {
  theme_mode?: string
  language?: string
}

export async function login(username: string, password: string): Promise<TokenResponse> {
  const response = await http.post<TokenResponse>('/auth/login', { username, password })
  return response.data
}

export interface RegisterRequest {
  username: string
  password: string
}

export async function register(username: string, password: string): Promise<TokenResponse> {
  const response = await http.post<TokenResponse>('/auth/register', { username, password })
  return response.data
}

export async function refreshToken(refresh_token: string): Promise<TokenResponse> {
  const response = await http.post<TokenResponse>('/auth/refresh', { refresh_token })
  return response.data
}

export async function getCurrentUser(): Promise<UserResponse> {
  const response = await http.get<UserResponse>('/auth/me')
  return response.data
}

export async function getPreferences(): Promise<UserPreferences> {
  const response = await http.get<UserPreferences>('/users/me/preferences')
  return response.data
}

export async function updatePreferences(data: UpdatePreferencesRequest): Promise<UserPreferences> {
  const response = await http.put<UserPreferences>('/users/me/preferences', data)
  return response.data
}
