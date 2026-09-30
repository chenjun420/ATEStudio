import { describe, it, expect, beforeEach } from 'vitest'
import en from '@/i18n/locales/en'
import zhCN from '@/i18n/locales/zh-CN'
import { appLabel, menuLabel } from '@/composables/menuLabels'
import { DEFAULT_LOCALE, SUPPORTED_LOCALES } from '@/i18n'

/**
 * Locale parity and menu-label resolution.
 *
 * Two failures this file exists to prevent, both of which are invisible in the
 * UI until someone is looking for them:
 *
 *  1. **A key present in one locale and missing in the other.** vue-i18n does
 *     not warn at runtime for a missing message; it renders the key. So a key
 *     added to zh-CN and forgotten in en.ts shows up as the literal string
 *     `users.adminRole` on an English screen — a bug report that reads as a
 *     broken build rather than an untranslated string.
 *
 *  2. **A menu with no translation.** The sidebar labels come from the
 *     database, which stores one Chinese string with no language dimension, so
 *     a menu the map does not know silently stays Chinese in an English UI.
 *     That is why the fallback exists; these tests keep the map honest about
 *     what it covers rather than letting the fallback hide the gap forever.
 */

/** The seed codes, mirrored from src/ate_cloud/api/v1/apps.py. */
const SEEDED_MENU_CODES = [
  'stations', 'node-detail',
  'sequences', 'sequence-editor', 'flow-templates', 'scripts',
  'node-binding', 'fixture-designer',
  'dashboard', 'history', 'measurements', 'reports', 'tracing',
  'simulation-console', 'operator-panel',
  'settings', 'changeover', 'calibration', 'fmea', 'users', 'roles',
  // defined in AppLayout until the backend seed learns them
  'traceability',
]

const SEEDED_APP_CODES = ['node-mgmt', 'flow-mgmt', 'exec-monitor', 'system']

/** A `t` that returns the key when a translation is missing, like vue-i18n. */
function translator(dict: Record<string, unknown>): (key: string) => string {
  return (key: string) => {
    const value = key.split('.').reduce<unknown>(
      (acc, part) => (acc && typeof acc === 'object' ? (acc as Record<string, unknown>)[part] : undefined),
      dict,
    )
    return typeof value === 'string' ? value : key
  }
}

const tEn = translator(en as unknown as Record<string, unknown>)
const tZh = translator(zhCN as unknown as Record<string, unknown>)

function keysOf(dict: Record<string, unknown>, prefix = ''): string[] {
  return Object.entries(dict).flatMap(([k, v]) =>
    v && typeof v === 'object'
      ? keysOf(v as Record<string, unknown>, `${prefix}${k}.`)
      : [`${prefix}${k}`],
  )
}

describe('locale parity', () => {
  it('both locales define the same top-level groups', () => {
    expect(Object.keys(zhCN).sort()).toEqual(Object.keys(en).sort())
  })

  it('no key exists in one locale and not the other', () => {
    const enKeys = new Set(keysOf(en as unknown as Record<string, unknown>))
    const zhKeys = new Set(keysOf(zhCN as unknown as Record<string, unknown>))

    const missingInEn = [...zhKeys].filter((k) => !enKeys.has(k))
    const missingInZh = [...enKeys].filter((k) => !zhKeys.has(k))

    expect({ missingInEn, missingInZh }).toEqual({ missingInEn: [], missingInZh: [] })
  })

  it('every message is a non-empty string', () => {
    // An empty translation renders as nothing, which looks like a layout bug.
    const empty: string[] = []
    for (const [locale, dict] of [['en', en], ['zh-CN', zhCN]] as const) {
      for (const key of keysOf(dict as unknown as Record<string, unknown>)) {
        const value = key
          .split('.')
          .reduce<unknown>(
            (acc, part) => (acc && typeof acc === 'object' ? (acc as Record<string, unknown>)[part] : undefined),
            dict as unknown as Record<string, unknown>,
          )
        if (typeof value !== 'string' || value.trim() === '') empty.push(`${locale}:${key}`)
      }
    }
    expect(empty).toEqual([])
  })

  it('Chinese is the default and is actually shipped', () => {
    expect(DEFAULT_LOCALE).toBe('zh-CN')
    expect(SUPPORTED_LOCALES).toContain(DEFAULT_LOCALE)
  })
})

describe('menu labels resolve through i18n', () => {
  beforeEach(() => {
    // A t that never resolves, so the fallback path is what is under test.
  })

  it.each(SEEDED_MENU_CODES)('menu "%s" is translated in both locales', (code) => {
    const menu = { code, name: `服务器名 ${code}` }
    const inEn = menuLabel(menu, tEn)
    const inZh = menuLabel(menu, tZh)

    expect(inEn, `${code} 在英文下仍显示数据库名`).not.toBe(menu.name)
    expect(inZh, `${code} 在中文下仍显示数据库名`).not.toBe(menu.name)
    expect(inEn).not.toBe(inZh)
  })

  it.each(SEEDED_APP_CODES)('app "%s" is translated in both locales', (code) => {
    const app = { code, name: `服务器名 ${code}` }
    expect(appLabel(app, tEn)).not.toBe(app.name)
    expect(appLabel(app, tZh)).not.toBe(app.name)
  })

  it('falls back to the stored name for an unmapped code', () => {
    // The point of the fallback: a menu added to the seed without a key shows
    // its server-provided name, never a raw `menu.whatever` to an operator.
    const menu = { code: 'brand-new-menu', name: '新产品菜单' }
    expect(menuLabel(menu, tEn)).toBe('新产品菜单')
    expect(menuLabel(menu, tZh)).toBe('新产品菜单')
  })

  it('falls back when a key exists but has no translation', () => {
    // Mirrors vue-i18n: t() returns the key itself, which is the signal.
    const menu = { code: 'stations', name: '数据库里的名字' }
    const brokenT = (key: string) => key
    expect(menuLabel(menu, brokenT)).toBe('数据库里的名字')
  })

  it('user and role management are translatable — they are the new entries', () => {
    expect(menuLabel({ code: 'users', name: 'x' }, tEn)).toBe('User Management')
    expect(menuLabel({ code: 'users', name: 'x' }, tZh)).toBe('用户管理')
    expect(menuLabel({ code: 'roles', name: 'x' }, tEn)).toBe('Roles & Permissions')
    expect(menuLabel({ code: 'roles', name: 'x' }, tZh)).toBe('角色与权限')
  })
})
