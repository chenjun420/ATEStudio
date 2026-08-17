import { ref, computed, readonly } from 'vue'
import { useRouter } from 'vue-router'
import {
  login as apiLogin,
  getCurrentUser,
  getPreferences,
  updatePreferences,
  type UserResponse,
  type UserPreferences,
} from '@/api/auth'
import { ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, clearAuthStorage, redirectToLogin } from '@/api/interceptor'
import { useTheme, type ThemeMode } from '@/composables/useTheme'
import i18n from '@/i18n'

const _token = ref<string | null>(localStorage.getItem(ACCESS_TOKEN_KEY))
const _refreshToken = ref<string | null>(localStorage.getItem(REFRESH_TOKEN_KEY))
const _user = ref<UserResponse | null>(null)
const _preferences = ref<UserPreferences | null>(null)
const _initialized = ref(false)

export function useAuth() {
  const router = useRouter()

  const isAuthenticated = computed(() => _token.value !== null && _user.value !== null)
  const isAdmin = computed(() => _user.value?.role === 'admin')

  function hasScope(scope: string): boolean {
    if (!_user.value) return false
    if (_user.value.role === 'admin') return true
    return _user.value.scopes.includes(scope)
  }

  async function login(username: string, password: string): Promise<void> {
    const tokenResp = await apiLogin(username, password)
    _token.value = tokenResp.access_token
    _refreshToken.value = tokenResp.refresh_token
    localStorage.setItem(ACCESS_TOKEN_KEY, tokenResp.access_token)
    localStorage.setItem(REFRESH_TOKEN_KEY, tokenResp.refresh_token)
    await fetchUser()
    await loadPreferences()
  }

  function logout(): void {
    _token.value = null
    _refreshToken.value = null
    _user.value = null
    _preferences.value = null
    clearAuthStorage()
    router.push('/login')
  }

  async function fetchUser(): Promise<void> {
    _user.value = await getCurrentUser()
  }

  function applyPreferences(prefs: UserPreferences): void {
    _preferences.value = prefs
    if (prefs.theme_mode) {
      const { setMode } = useTheme()
      setMode(prefs.theme_mode as ThemeMode)
    }
    if (prefs.language) {
      i18n.global.locale.value = prefs.language as 'en' | 'zh-CN'
    }
  }

  async function loadPreferences(): Promise<void> {
    try {
      const prefs = await getPreferences()
      applyPreferences(prefs)
    } catch {
      // Preferences may not exist yet — use defaults
    }
  }

  async function savePreferences(theme_mode: string, language: string): Promise<void> {
    const prefs = await updatePreferences({ theme_mode, language })
    applyPreferences(prefs)
  }

  async function init(): Promise<void> {
    if (_initialized.value) return
    _initialized.value = true

    if (_token.value) {
      try {
        await fetchUser()
        await loadPreferences()
      } catch {
        logout()
      }
    }
  }

  return {
    token: readonly(_token),
    refreshToken: readonly(_refreshToken),
    user: readonly(_user),
    preferences: readonly(_preferences),
    isAuthenticated,
    isAdmin,
    hasScope,
    login,
    logout,
    fetchUser,
    loadPreferences,
    savePreferences,
    init,
    redirectToLogin,
  }
}
