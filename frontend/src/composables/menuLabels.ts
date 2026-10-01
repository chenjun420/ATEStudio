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

/**
 * Menu `code` (as seeded in the backend) -> i18n key under `menu.`.
 *
 * Groups are listed alongside pages. AppLayout resolves group labels through the
 * same table, and the parity test walks it, so a group missing here is a group
 * whose label is pinned to one language.
 */
const MENU_KEY_BY_CODE: Record<string, string> = {
  // -- 产测开发 (test-dev) ------------------------------------------------
  traceability: 'menu.traceabilityMatrix',
  'condition-review': 'menu.conditionReview',
  sequences: 'menu.sequenceList',
  'sequence-editor': 'menu.flowEditor',
  'flow-templates': 'menu.flowNodeTemplates',
  scripts: 'menu.scriptManagement',
  'fixture-designer': 'menu.fixtureDesigner',
  'station-management': 'menu.stationManagement',
  // groups
  requirements: 'menu.groupRequirements',
  process: 'menu.groupProcess',
  'station-binding': 'menu.groupStationBinding',

  // -- 运行监控 (runtime) -------------------------------------------------
  dashboard: 'menu.dashboard',
  history: 'menu.executionHistory',
  measurements: 'menu.measurements',
  reports: 'menu.reports',
  tracing: 'menu.tracing',
  'simulation-console': 'menu.simulationConsole',
  stations: 'menu.stationList',
  workers: 'menu.workerRegistry',
  calibration: 'menu.calibration',
  changeover: 'menu.productChangeover',
  'fault-cases': 'menu.faultCaseLibrary',
  fmea: 'menu.fmea',
  // groups
  line: 'menu.groupLine',
  execution: 'menu.groupExecution',
  debug: 'menu.groupDebug',
  'station-ops': 'menu.groupStationOps',
  fault: 'menu.groupFault',

  // -- 系统 (system) ------------------------------------------------------
  settings: 'menu.settings',
  users: 'menu.userManagement',
  roles: 'menu.roleManagement',
}

/** Top-level app `code` -> i18n key. */
const APP_KEY_BY_CODE: Record<string, string> = {
  'test-dev': 'menu.testDevelopment',
  runtime: 'menu.runtimeMonitoring',
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
