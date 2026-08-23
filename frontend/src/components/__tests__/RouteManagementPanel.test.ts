/**
 * Tests for route management (T28, 设计文档 §8.3.2 Route 实体).
 *
 * Two layers:
 * - Pure helpers in `utils/routes.ts`: backend-valid route factory, unique-id
 *   allocation, immutable create/rename/delete/link-assign/relay-assign/
 *   activate transforms, associated_step normalization + step-reference check.
 * - `RouteManagementPanel.vue` component: renders the route list, create /
 *   rename / delete, link & relay assignment chips, activate toggle and
 *   associated_step binding — all changes emitted upward as a new `routes`
 *   array so the parent view persists them into topology_data.routes.
 *
 * Spec guards covered here:
 * - activating a route only flips its own `active` flag (no auto-activation
 *   of anything else, and mounting the panel never emits a change)
 * - deleting a route referenced by a step (associated_step set) requires an
 *   explicit confirm before the deletion is emitted.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'

import RouteManagementPanel from '../RouteManagementPanel.vue'
import {
  applyAssignLinks,
  applyAssignRelays,
  applyAssociatedStep,
  applyCreate,
  applyDelete,
  applyRename,
  applySetActive,
  createRoute,
  normalizeAssociatedStep,
  routeReferencedByStep,
  uniqueRouteId,
  type RouteLike,
} from '@/utils/routes'
import type { Link, Relay, Route } from '@/api/fixtures'

// ─── Factories ───────────────────────────────────────────────────────────────

function makeRoute(overrides: Partial<RouteLike> = {}): RouteLike {
  return {
    id: 'ROUTE_1',
    name: '路由 1',
    links: [],
    relays: [],
    active: false,
    associated_step: null,
    ...overrides,
  }
}

function makeLink(id: string): Link {
  return {
    id,
    from: { entity_type: 'instrument_channel', entity_id: 'PSU_1', port_id: 'CH1' },
    to: { entity_type: 'dut_testpoint', entity_id: 'DUT_1', port_id: 'TP1' },
    signal_type: 'power',
  }
}

function makeRelay(id: string): Relay {
  return { id, type: 'spst', control_signal: `CTRL_${id}`, state: 'open' }
}

/**
 * Mounts the panel with a faithful parent contract: every emitted
 * `routes-change` payload is written back into the `routes` prop, mirroring
 * FixtureDesigner's `onRoutesChange` assigning into reactive topology_data.
 */
function mountPanel(routes: Route[], links: Link[] = [], relays: Relay[] = []) {
  const wrapper = mount(RouteManagementPanel, {
    props: { routes, links, relays },
    global: { plugins: [ElementPlus] },
  })
  const writeBack = async () => {
    const events = wrapper.emitted('routes-change')
    if (events?.length) {
      await wrapper.setProps({
        routes: events[events.length - 1]![0] as unknown as Route[],
      })
    }
  }
  return { wrapper, writeBack }
}

function lastEmittedRoutes(wrapper: VueWrapper): RouteLike[] {
  const events = wrapper.emitted('routes-change')
  expect(events).toBeTruthy()
  // eslint-disable-next-line @typescript-eslint/no-non-null-assertion
  return events![events!.length - 1]![0] as unknown as RouteLike[]
}

// ─── Pure helpers: factory, ids, CRUD transforms ─────────────────────────────

describe('routes helpers: factory & CRUD', () => {
  it('createRoute produces backend-valid defaults (active=false, empty lists, null step)', () => {
    const r = createRoute(0)
    expect(r.id).toBe('ROUTE_1')
    expect(r.name).toBe('路由 1')
    expect(r.links).toEqual([])
    expect(r.relays).toEqual([])
    expect(r.active).toBe(false)
    expect(r.associated_step).toBeNull()
  })

  it('uniqueRouteId avoids collisions with existing routes', () => {
    const routes = [makeRoute({ id: 'ROUTE_1' }), makeRoute({ id: 'ROUTE_2' })]
    expect(uniqueRouteId(routes)).toBe('ROUTE_3')
    expect(uniqueRouteId([makeRoute({ id: 'ROUTE_3' })])).toBe('ROUTE_1')
  })

  it('applyCreate appends immutably; source array untouched', () => {
    const src = [makeRoute()]
    const next = applyCreate(src, makeRoute({ id: 'ROUTE_9', name: 'X' }))
    expect(next).toHaveLength(2)
    expect(next[1].id).toBe('ROUTE_9')
    expect(src).toHaveLength(1)
  })

  it('applyRename writes the new name immutably and keeps other fields', () => {
    const src = [makeRoute({ name: '旧名', links: ['L1'] })]
    const next = applyRename(src, 'ROUTE_1', 'VOUT 路径')
    expect(next[0].name).toBe('VOUT 路径')
    expect(next[0].links).toEqual(['L1'])
    expect(src[0].name).toBe('旧名')
  })

  it('applyDelete removes only the target route immutably', () => {
    const src = [makeRoute({ id: 'ROUTE_1' }), makeRoute({ id: 'ROUTE_2' })]
    const next = applyDelete(src, 'ROUTE_1')
    expect(next.map((r) => r.id)).toEqual(['ROUTE_2'])
    expect(src).toHaveLength(2)
  })
})

// ─── Pure helpers: assignment, activation, step binding ──────────────────────

describe('routes helpers: assignment & activation', () => {
  it('applyAssignLinks replaces the link list immutably', () => {
    const src = [makeRoute({ links: ['L1'] })]
    const next = applyAssignLinks(src, 'ROUTE_1', ['L2', 'L3'])
    expect(next[0].links).toEqual(['L2', 'L3'])
    expect(src[0].links).toEqual(['L1'])
  })

  it('applyAssignRelays replaces the relay list immutably', () => {
    const src = [makeRoute({ relays: ['RLY_1'] })]
    const next = applyAssignRelays(src, 'ROUTE_1', ['RLY_2'])
    expect(next[0].relays).toEqual(['RLY_2'])
    expect(src[0].relays).toEqual(['RLY_1'])
  })

  it('applySetActive toggles the active flag on/off without touching sibling routes', () => {
    const src = [makeRoute({ id: 'ROUTE_1' }), makeRoute({ id: 'ROUTE_2', active: true })]
    const on = applySetActive(src, 'ROUTE_1', true)
    expect(on[0].active).toBe(true)
    expect(on[1].active).toBe(true) // per-route flag semantics: siblings untouched
    expect(src[0].active).toBe(false) // source unchanged

    const off = applySetActive(on, 'ROUTE_1', false)
    expect(off[0].active).toBe(false)
  })

  it('normalizeAssociatedStep trims and maps blank to null; applyAssociatedStep binds immutably', () => {
    expect(normalizeAssociatedStep('  STEP_power_on  ')).toBe('STEP_power_on')
    expect(normalizeAssociatedStep('   ')).toBeNull()
    expect(normalizeAssociatedStep('')).toBeNull()

    const src = [makeRoute({ associated_step: 'OLD' })]
    const bound = applyAssociatedStep(src, 'ROUTE_1', 'STEP_a')
    expect(bound[0].associated_step).toBe('STEP_a')
    const cleared = applyAssociatedStep(src, 'ROUTE_1', '')
    expect(cleared[0].associated_step).toBeNull()
    expect(src[0].associated_step).toBe('OLD')
  })

  it('routeReferencedByStep is true exactly when associated_step is a non-empty string', () => {
    expect(routeReferencedByStep(makeRoute({ associated_step: 'S1' }))).toBe(true)
    expect(routeReferencedByStep(makeRoute({ associated_step: null }))).toBe(false)
    expect(routeReferencedByStep(makeRoute({ associated_step: undefined }))).toBe(false)
    expect(routeReferencedByStep(makeRoute({ associated_step: '' }))).toBe(false)
  })
})

// ─── Component: rendering & interaction ──────────────────────────────────────

describe('RouteManagementPanel component', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('renders the route list with names/counts and never auto-activates or emits on mount', () => {
    const routes = [
      makeRoute({ id: 'ROUTE_1', name: 'A', links: ['L1'], relays: ['RLY_1'] }),
      makeRoute({ id: 'ROUTE_2', name: 'B', active: true }),
    ]
    const { wrapper } = mountPanel(routes as unknown as Route[])
    const items = wrapper.findAll('.route-list > .route-item')
    expect(items).toHaveLength(2)
    expect(items[0].text()).toContain('A')
    // counts reflect assigned links/relays (1 link, 1 relay on ROUTE_1)
    expect(items[0].find('.route-counts').text()).toContain('1链路')
    expect(items[0].find('.route-counts').text()).toContain('1继电器')
    // active badge only on ROUTE_2 — no auto-activation happened for ROUTE_1
    expect(items[0].find('.route-active-badge').exists()).toBe(false)
    expect(items[1].find('.route-active-badge').exists()).toBe(true)
    // mounting must not emit any change (spec: no auto-activate on load)
    expect(wrapper.emitted('routes-change')).toBeFalsy()
  })

  it('adds a route with a unique id via the create row and selects it', async () => {
    const { wrapper, writeBack } = mountPanel([makeRoute()] as unknown as Route[])
    await wrapper.find('.route-new-name input').setValue('旁路')
    await wrapper.find('.route-add').trigger('click')
    await writeBack()
    const routes = lastEmittedRoutes(wrapper)
    expect(routes).toHaveLength(2)
    expect(routes[1].id).toBe('ROUTE_2')
    expect(routes[1].name).toBe('旁路')
    expect(routes[1].active).toBe(false)
    // new route becomes the selected one → rename draft synced
    expect((wrapper.find('.route-rename-input input').element as HTMLInputElement).value).toBe('旁路')
  })

  it('renames the selected route through the rename input change event', async () => {
    const { wrapper } = mountPanel([makeRoute()] as unknown as Route[])
    await wrapper.find('.route-rename-input input').setValue('VOUT 路径')
    await wrapper.find('.route-rename-input input').trigger('change')
    const routes = lastEmittedRoutes(wrapper)
    expect(routes[0].name).toBe('VOUT 路径')
  })

  it('toggles link membership via assignment chips (assign then unassign)', async () => {
    const { wrapper, writeBack } = mountPanel(
      [makeRoute()] as unknown as Route[],
      [makeLink('L1'), makeLink('L2'), makeLink('L3')],
    )
    const chip = (id: string) => wrapper.find(`.route-link-option[data-link="${id}"]`)
    await chip('L1').trigger('click')
    await writeBack()
    await chip('L3').trigger('click')
    await writeBack()
    let routes = lastEmittedRoutes(wrapper)
    expect(routes[0].links).toEqual(['L1', 'L3'])
    expect(chip('L1').classes()).toContain('selected')

    await chip('L1').trigger('click')
    await writeBack()
    routes = lastEmittedRoutes(wrapper)
    expect(routes[0].links).toEqual(['L3'])
  })

  it('toggles relay membership via assignment chips', async () => {
    const { wrapper, writeBack } = mountPanel(
      [makeRoute()] as unknown as Route[],
      [],
      [makeRelay('RLY_1'), makeRelay('RLY_2')],
    )
    await wrapper.find('.route-relay-option[data-relay="RLY_2"]').trigger('click')
    await writeBack()
    const routes = lastEmittedRoutes(wrapper)
    expect(routes[0].relays).toEqual(['RLY_2'])
  })

  it('activate button toggles the active flag in the emitted payload (on then off)', async () => {
    const { wrapper, writeBack } = mountPanel([makeRoute()] as unknown as Route[])
    const btn = wrapper.find('.route-activate')
    expect(btn.text()).toContain('激活')
    await btn.trigger('click')
    await writeBack()
    expect(lastEmittedRoutes(wrapper)[0].active).toBe(true)
    expect(wrapper.find('.route-activate').text()).toContain('停用')
    await wrapper.find('.route-activate').trigger('click')
    await writeBack()
    expect(lastEmittedRoutes(wrapper)[0].active).toBe(false)
  })

  it('deletes an unreferenced route immediately without any confirm dialog', async () => {
    const confirmSpy = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm')
    const { wrapper } = mountPanel([
      makeRoute({ id: 'ROUTE_1' }),
      makeRoute({ id: 'ROUTE_2' }),
    ] as unknown as Route[])
    await wrapper.find('.route-delete').trigger('click')
    await flushPromises()
    const routes = lastEmittedRoutes(wrapper)
    expect(routes.map((r) => r.id)).toEqual(['ROUTE_2'])
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it('deleting a step-referenced route requires confirm: cancel blocks, confirm deletes', async () => {
    const routesProp = [
      makeRoute({ id: 'ROUTE_1', associated_step: 'STEP_9' }),
      makeRoute({ id: 'ROUTE_2' }),
    ] as unknown as Route[]

    // cancel path: confirm rejected → no emission, error note shown
    vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')
    let panel = mountPanel(routesProp)
    await panel.wrapper.find('.route-delete').trigger('click')
    await flushPromises()
    expect(panel.wrapper.emitted('routes-change')?.length ?? 0).toBe(0)
    expect(panel.wrapper.find('.route-error').text()).toContain('取消')

    // confirm path: resolved → deletion emitted
    vi.restoreAllMocks()
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm')
    panel = mountPanel(routesProp)
    await panel.wrapper.find('.route-delete').trigger('click')
    await flushPromises()
    expect(lastEmittedRoutes(panel.wrapper).map((r) => r.id)).toEqual(['ROUTE_2'])
  })

  it('binds associated_step via the step input; clearing emits null', async () => {
    const { wrapper } = mountPanel([makeRoute()] as unknown as Route[])
    const input = wrapper.find('.route-step-input input')
    await input.setValue('STEP_power_on')
    await input.trigger('change')
    expect(lastEmittedRoutes(wrapper)[0].associated_step).toBe('STEP_power_on')

    await input.setValue('   ')
    await input.trigger('change')
    expect(lastEmittedRoutes(wrapper)[0].associated_step).toBeNull()
  })
})
