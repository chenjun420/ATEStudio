import { createI18n } from 'vue-i18n'
import en from './locales/en'
import zhCN from './locales/zh-CN'

/** Locales the product actually ships. */
export type Locale = 'zh-CN' | 'en'

export const SUPPORTED_LOCALES: Locale[] = ['zh-CN', 'en']

/** Key under which the choice is remembered on this browser. */
export const LOCALE_STORAGE_KEY = 'ate.locale'

/**
 * Why Chinese is the default
 * --------------------------
 * The content pages are Chinese: 25 components carry roughly 4200 strings of
 * domain prose that are not routed through `$t()`, and the sidebar labels come
 * from `app_menus.name`, which is Chinese-only. Defaulting to `en` therefore
 * produced a screen whose shell said "Login" while its body said 判据 — two
 * languages on one page, which reads as a bug rather than as localisation.
 *
 * The shell is now translated in both directions and switchable, so English is
 * one click away for the parts that support it. Defaulting to the language the
 * bulk of the product is actually written in is the honest choice; defaulting
 * to the one that covers 7% of it is not.
 */
export const DEFAULT_LOCALE: Locale = 'zh-CN'

/**
 * Read a persisted choice.
 *
 * Guarded against storage being unavailable (private mode, disabled cookies) —
 * a language preference is never worth failing app boot over.
 */
export function storedLocale(): Locale | null {
  try {
    const raw = window.localStorage.getItem(LOCALE_STORAGE_KEY)
    return SUPPORTED_LOCALES.includes(raw as Locale) ? (raw as Locale) : null
  } catch {
    return null
  }
}

const i18n = createI18n({
  legacy: false,
  locale: storedLocale() ?? DEFAULT_LOCALE,
  // Falls through to English rather than to the key: a missing translation
  // should read as an untranslated string in another language, not as
  // `menu.foo` leaking into the interface.
  fallbackLocale: 'en',
  missingWarn: import.meta.env.DEV,
  fallbackWarn: import.meta.env.DEV,
  messages: {
    en,
    'zh-CN': zhCN,
  },
})

export default i18n
