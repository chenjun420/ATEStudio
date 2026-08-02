import { ref, readonly } from 'vue'
import { fetchApps, fetchAppMenus, seedApps, type AppInfo, type AppWithMenus } from '@/api/apps'

// Shared reactive state
const _apps = ref<AppInfo[]>([])
const _currentAppMenus = ref<AppWithMenus | null>(null)
const _loading = ref(false)
const _error = ref<string | null>(null)

/** Cache of app menus by app ID to avoid redundant fetches */
const _menuCache = new Map<string, AppWithMenus>()

export function useApps() {
  async function loadApps(): Promise<void> {
    _loading.value = true
    _error.value = null
    try {
      _apps.value = await fetchApps()
    } catch (e: unknown) {
      _error.value = e instanceof Error ? e.message : 'Failed to load apps'
    } finally {
      _loading.value = false
    }
  }

  async function loadAppMenus(appId: string): Promise<void> {
    // Check cache first
    const cached = _menuCache.get(appId)
    if (cached) {
      _currentAppMenus.value = cached
      return
    }

    _loading.value = true
    _error.value = null
    try {
      const appWithMenus = await fetchAppMenus(appId)
      _menuCache.set(appId, appWithMenus)
      _currentAppMenus.value = appWithMenus
    } catch (e: unknown) {
      _error.value = e instanceof Error ? e.message : 'Failed to load app menus'
    } finally {
      _loading.value = false
    }
  }

  async function ensureSeed(): Promise<void> {
    try {
      await seedApps()
      await loadApps()
    } catch (e: unknown) {
      // Seed may fail if already seeded — that's fine
      console.warn('Seed failed (may already exist):', e)
      await loadApps()
    }
  }

  function clearMenuCache(appId?: string): void {
    if (appId) {
      _menuCache.delete(appId)
    } else {
      _menuCache.clear()
    }
  }

  return {
    apps: readonly(_apps),
    currentAppMenus: readonly(_currentAppMenus),
    loading: readonly(_loading),
    error: readonly(_error),
    loadApps,
    loadAppMenus,
    ensureSeed,
    clearMenuCache,
  }
}
