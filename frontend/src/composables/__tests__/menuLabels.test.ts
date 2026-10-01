import { describe, it, expect, beforeEach } from 'vitest'
import en from '@/i18n/locales/en'
import zhCN from '@/i18n/locales/zh-CN'
import { appLabel, menuLabel, untranslatedAppCodes, untranslatedMenuCodes } from '@/composables/menuLabels'
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

/**
 * The seed codes, mirrored from src/ate_cloud/api/v1/apps.py.
 *
 * Kept as a hand-written list rather than imported from the backend, because
 * this is a frontend test with no Node-side view of Python. It is a mirror, so
 * it can drift — which is why every entry is asserted to resolve below: a code
 * added to the seed and forgotten here fails nothing, but a code added to the
 * map and forgotten here would render the database name, and the fallback test
 * below is the one that would catch it.
 */
const SEEDED_MENU_CODES = [
  // 产测开发
  'traceability', 'condition-review',
  'sequences', 'sequence-editor', 'flow-templates', 'scripts',
  'fixture-designer', 'station-management',
  // groups
  'requirements', 'process', 'station-binding',
  // 运行监控
  'dashboard', 'history', 'measurements', 'reports', 'tracing',
  'simulation-console', 'stations', 'workers', 'calibration', 'changeover',
  'fault-cases', 'fmea',
  // groups
  'line', 'execution', 'debug', 'station-ops', 'fault',
  // 系统
  'settings', 'users', 'roles',
]

const SEEDED_APP_CODES = ['test-dev', 'runtime', 'system']

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

  it('the map covers exactly the seeded codes — no gaps, no leftovers', () => {
    // The drift guard for the mirror above. A code in the seed and not in the
    // map renders the database's Chinese name in an English UI; a code in the
    // map and not in the seed is dead weight that suggests a rename was only
    // half done — which is exactly what happened when 节点管理 became 产测开发
    // and left `node-mgmt`, `flow-mgmt` and `exec-monitor` behind.
    expect([...untranslatedMenuCodes()].sort()).toEqual([...SEEDED_MENU_CODES].sort())
    expect([...untranslatedAppCodes()].sort()).toEqual([...SEEDED_APP_CODES].sort())
  })

  it('no menu code or app code still uses the retired 节点 vocabulary', () => {
    // 节点 meant two things. Only one was renamed. A flow node is a step in the
    // sequence graph and keeps the word (流程节点模板); a station is a physical
    // position and became 工位. So `flow-templates` is expected to keep "flow"
    // while nothing may be called node-binding / node-detail / node-mgmt.
    const retired = ['node-mgmt', 'flow-mgmt', 'exec-monitor', 'node-binding', 'node-detail']
    for (const code of retired) {
      expect(untranslatedMenuCodes()).not.toContain(code)
      expect(untranslatedAppCodes()).not.toContain(code)
    }
  })
})
