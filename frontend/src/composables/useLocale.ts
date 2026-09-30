/**
 * Switching the interface language, and remembering the choice.
 *
 * The choice is written to two places on purpose:
 *
 *  * `localStorage` — so the next page load is already in the right language.
 *    Without this the switcher would only last until reload, which reads as
 *    "the toggle doesn't work".
 *  * the user's server-side preferences — so the language follows the person
 *    across browsers. `useAuth.applyPreferences` already applies
 *    `prefs.language` on login, so writing it is all that is needed; nothing
 *    new had to be added to the read path.
 *
 * The server write is deliberately best-effort. A language is not worth an
 * error toast, and failing it must not leave the switcher looking broken: the
 * local write has already happened, so the visible language is correct either
 * way.
 */

import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import i18n, {
  DEFAULT_LOCALE,
  LOCALE_STORAGE_KEY,
  SUPPORTED_LOCALES,
  type Locale,
} from '@/i18n'
import { updatePreferences } from '@/api/auth'

const current = ref<Locale>(i18n.global.locale.value as Locale)

function persist(loc: Locale): void {
  try {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, loc)
  } catch {
    // Storage unavailable — the language still applies for this session.
  }
}

export function useLocale() {
  const { t } = useI18n()

  /** Apply a locale to the running interface. Synchronous, always succeeds. */
  function apply(loc: Locale): void {
    i18n.global.locale.value = loc
    current.value = loc
    persist(loc)
  }

  /**
   * Switch language, then try to remember it server-side.
   *
   * The interface is already in the new language when this is called, so the
   * user sees the result immediately rather than after a round trip.
   */
  async function setLocale(loc: Locale): Promise<void> {
    if (!SUPPORTED_LOCALES.includes(loc) || loc === current.value) return
    apply(loc)
    try {
      await updatePreferences({ language: loc })
    } catch {
      // Not worth surfacing: localStorage already holds the choice, and the
      // next login simply re-applies the previous server-side value.
    }
  }

  /**
   * Adopt a locale pushed in from elsewhere (e.g. `applyPreferences` on login)
   * without writing it back — that path already has the value server-side.
   */
  function sync(loc: Locale): void {
    apply(loc)
  }

  watch(
    () => i18n.global.locale.value,
    (loc) => {
      current.value = loc as Locale
    },
  )

  return {
    t,
    locale: current,
    locales: SUPPORTED_LOCALES,
    defaultLocale: DEFAULT_LOCALE,
    setLocale,
    sync,
  }
}
