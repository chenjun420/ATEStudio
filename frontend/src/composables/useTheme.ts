/**
 * useTheme — auto-detect system dark/light preference and toggle html.dark class.
 *
 * Element Plus dark mode requires `<html class="dark">` + the dark/css-vars.css
 * import. This composable watches `prefers-color-scheme` and applies the class
 * automatically. User preference is persisted to localStorage and overrides
 * system preference when set explicitly.
 */

import { ref, watch, onMounted, onUnmounted } from 'vue'

export type ThemeMode = 'light' | 'dark' | 'auto'

const STORAGE_KEY = 'ate-theme-mode'

const mode = ref<ThemeMode>('auto')
const isDark = ref(false)
const systemDark = ref(false)

let mediaQuery: MediaQueryList | null = null
let mediaHandler: ((e: MediaQueryListEvent) => void) | null = null

/** Read system preference via matchMedia */
function detectSystemDark(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

/** Apply the html.dark class based on resolved theme */
function applyTheme(): void {
  const resolved =
    mode.value === 'auto' ? (systemDark.value ? 'dark' : 'light') : mode.value
  isDark.value = resolved === 'dark'

  const html = document.documentElement
  if (isDark.value) {
    html.classList.add('dark')
  } else {
    html.classList.remove('dark')
  }
}

/** Initialize theme on app startup — call once from main.ts */
export function initTheme(): void {
  // Load saved preference
  const saved = localStorage.getItem(STORAGE_KEY) as ThemeMode | null
  mode.value = saved ?? 'auto'

  // Detect system preference
  systemDark.value = detectSystemDark()

  // Listen for system theme changes
  if (typeof window !== 'undefined' && window.matchMedia) {
    mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    mediaHandler = (e: MediaQueryListEvent) => {
      systemDark.value = e.matches
      applyTheme()
    }
    mediaQuery.addEventListener('change', mediaHandler)
  }

  applyTheme()
}

/** Composable for use in components */
export function useTheme() {
  onMounted(() => {
    // initTheme already called from main.ts, but ensure listeners if not
    if (!mediaQuery && typeof window !== 'undefined' && window.matchMedia) {
      systemDark.value = detectSystemDark()
      mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
      mediaHandler = (e: MediaQueryListEvent) => {
        systemDark.value = e.matches
        applyTheme()
      }
      mediaQuery.addEventListener('change', mediaHandler)
    }
  })

  onUnmounted(() => {
    // Don't remove listener on component unmount — theme is app-wide
    // Only remove on full page teardown
  })

  /** Set theme mode explicitly */
  function setMode(newMode: ThemeMode): void {
    mode.value = newMode
    localStorage.setItem(STORAGE_KEY, newMode)
    applyTheme()
  }

  /** Toggle between light and dark (skipping auto) */
  function toggle(): void {
    setMode(isDark.value ? 'light' : 'dark')
  }

  // Watch system changes to reapply when in auto mode
  watch(systemDark, () => {
    if (mode.value === 'auto') {
      applyTheme()
    }
  })

  return {
    mode,
    isDark: isDark,
    setMode,
    toggle,
  }
}
