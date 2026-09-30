/**
 * Menu label localisation.
 *
 * Why this exists
 * ---------------
 * The sidebar rendered `menu.name` straight from the database, and
 * `app_menus.name` is a single Chinese string with no language dimension. So the
 * sidebar was Chinese in every locale while the shell around it followed
 * `$t()` — the two halves of the same screen disagreed, and no amount of
 * switching language would change the menu text.
 *
 * The fix maps a menu's stable `code` to an i18n key and resolves the label at
 * render time, falling back to the database name. The fallback is what keeps
 * this safe: a menu added to the seed without a matching key still renders its
 * server-provided name instead of showing a raw `menu.foo` string to an
 * operator. A visible key would look like a bug in the product; a stale label
 * looks like a missing translation, which is what it is.
 *
 * Keyed on `code` rather than `route_path` because `code` is the seed's own
 * identifier and does not change when a route is reorganised. `route_path` is
 * the more obvious choice and the wrong one — it moves.
 */

import type { AppMenuItem } from '@/api/apps'

/** Menu `code` (as seeded in the backend) → i18n key under `menu.`. */
const MENU_KEY_BY_CODE: Record<string, string> = {
  // node-mgmt
  stations: 'menu.stationList',
  'node-detail': 'menu.nodeDetail',
  // flow-mgmt
  sequences: 'menu.sequenceList',
  'sequence-editor': 'menu.flowEditor',
  'flow-templates': 'menu.nodeTemplates',
  scripts: 'menu.scriptManagement',
  'node-binding': 'menu.nodeFlowBinding',
  'fixture-designer': 'menu.fixtureDesigner',
  // exec-monitor
  dashboard: 'menu.dashboard',
  history: 'menu.executionHistory',
  measurements: 'menu.measurements',
  reports: 'menu.reports',
  tracing: 'menu.tracing',
  'simulation-console': 'menu.simulationConsole',
  'operator-panel': 'menu.operatorPanel',
  // system
  settings: 'menu.settings',
  changeover: 'menu.productChangeover',
  calibration: 'menu.calibration',
  fmea: 'menu.fmea',
  users: 'menu.userManagement',
  roles: 'menu.roleManagement',
  // defined in AppLayout until the backend seed learns them
  traceability: 'menu.traceabilityMatrix',
}

/** Top-level app `code` → i18n key. */
const APP_KEY_BY_CODE: Record<string, string> = {
  'node-mgmt': 'menu.nodeManagement',
  'flow-mgmt': 'menu.flowManagement',
  'exec-monitor': 'menu.executionMonitoring',
  system: 'menu.systemManagement',
}

type Translate = (key: string) => string

/**
 * Resolve a menu label, preferring the translation over the stored name.
 *
 * `t` returns the key itself when a translation is missing, which is exactly
 * the signal to fall back — so a new menu can never render as `menu.newThing`.
 */
export function menuLabel(menu: { code: string; name: string }, t: Translate): string {
  const key = MENU_KEY_BY_CODE[menu.code]
  if (!key) return menu.name
  const translated = t(key)
  return translated && translated !== key ? translated : menu.name
}

/** Same, for a top-level app. */
export function appLabel(app: { code: string; name: string }, t: Translate): string {
  const key = APP_KEY_BY_CODE[app.code]
  if (!key) return app.name
  const translated = t(key)
  return translated && translated !== key ? translated : app.name
}

/** Exposed for the parity test: which codes have no translation yet. */
export function untranslatedMenuCodes(): string[] {
  return Object.keys(MENU_KEY_BY_CODE)
}

/** Exposed for the parity test. */
export function untranslatedAppCodes(): string[] {
  return Object.keys(APP_KEY_BY_CODE)
}

export type { AppMenuItem }
