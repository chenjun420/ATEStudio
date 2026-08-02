import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 10000,
  headers: {
    'Content-Type': 'application/json',
  },
})

/** Menu item with optional children for tree rendering */
export interface AppMenuItem {
  id: string
  app_id: string
  parent_id: string | null
  code: string
  name: string
  route_path: string
  route_name: string | null
  icon: string | null
  sort_order: number
  is_active: boolean
  children?: AppMenuItem[]
}

/** App definition from backend */
export interface AppInfo {
  id: string
  code: string
  name: string
  description: string | null
  icon: string | null
  sort_order: number
  is_active: boolean
}

/** App with menu tree */
export interface AppWithMenus extends AppInfo {
  menus: AppMenuItem[]
}

/** List response */
export interface AppListResponse {
  items: AppInfo[]
  total: number
}

/** Fetch all active apps */
export async function fetchApps(): Promise<AppInfo[]> {
  const response = await api.get<AppListResponse>('/apps')
  return response.data.items
}

/** Fetch a single app with its menu tree */
export async function fetchAppMenus(appId: string): Promise<AppWithMenus> {
  const response = await api.get<AppWithMenus>(`/apps/${appId}`)
  return response.data
}

/** Seed default apps and menus (idempotent) */
export async function seedApps(): Promise<{ created_apps: number; created_menus: number; status: string }> {
  const response = await api.post('/apps/seed')
  return response.data
}
