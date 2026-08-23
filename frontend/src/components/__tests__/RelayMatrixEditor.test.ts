/**
 * Tests for relay matrix editing (T27, 设计文档 §8.3.2/§8.3.3).
 *
 * Two layers:
 * - Pure helpers in `utils/relayContacts.ts`: contact layouts per relay type
 *   (spst/spdt/dpdt/matrix), immutable bind/unbind/toggle/resize transforms,
 *   and terminal-occupancy validation (illegal contact links are blocked).
 * - `RelayMatrixEditor.vue` component: renders the relay grid per type,
 *   click-to-toggle contacts, terminal binding via picker, matrix resize —
 *   all changes emitted upward as a new `relays` array so the parent view
 *   persists them into topology_data.fixtures[].relays.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ElementPlus from 'element-plus'

import RelayMatrixEditor from '../RelayMatrixEditor.vue'
import {
  contactNamesForType,
  createRelay,
  checkBind,
  applyBind,
  applyToggleState,
  resizeMatrixContacts,
  type RelayLike,
} from '@/utils/relayContacts'
import type { Fixture, Relay } from '@/api/fixtures'

// ─── Factories ───────────────────────────────────────────────────────────────

function makeRelay(overrides: Partial<RelayLike> = {}): RelayLike {
  return {
    id: 'RLY_1',
    type: 'spst',
    control_signal: 'CTRL_1',
    contacts: { common: null, no: null },
    state: 'open',
    ...overrides,
  }
}

function makeFixture(relays: Relay[] = []): Fixture {
  return {
    id: 'FIX_1',
    name: 'FIX_1',
    terminals: [
      { id: 'T1', name: 'T1' },
      { id: 'T2', name: 'T2' },
      { id: 'T3', name: 'T3' },
      { id: 'T4', name: 'T4' },
    ],
    relays,
  }
}

function mountEditor(fixture: Fixture) {
  return mount(RelayMatrixEditor, {
    props: { fixture },
    global: { plugins: [ElementPlus] },
  })
}

function lastEmittedRelays(wrapper: ReturnType<typeof mountEditor>): RelayLike[] {
  const events = wrapper.emitted('relays-change')
  expect(events).toBeTruthy()
  // eslint-disable-next-line @typescript-eslint/no-non-null-assertion
  return events![events!.length - 1]![0] as unknown as RelayLike[]
}

// ─── Pure helpers: layouts & factories ──────────────────────────────────────

describe('relayContacts helpers', () => {
  it('contactNamesForType returns the §8.3.2 layout for each relay type', () => {
    expect(contactNamesForType('spst')).toEqual(['common', 'no'])
    expect(contactNamesForType('spdt')).toEqual(['common', 'no', 'nc'])
    expect(contactNamesForType('dpdt')).toEqual(['1c', '1no', '1nc', '2c', '2no', '2nc'])
    // matrix derives r{i}c{j} names from its contacts keys (row-major order)
    const matrixContacts = { r1c2: null, r1c1: 'T1', r2c1: null, r2c2: null }
    expect(contactNamesForType('matrix', matrixContacts)).toEqual(['r1c1', 'r1c2', 'r2c1', 'r2c2'])
  })

  it('createRelay produces backend-valid defaults for every type', () => {
    const spst = createRelay(0, 'spst')
    expect(spst.id).toBe('RLY_1')
    expect(spst.control_signal).toBeTruthy() // backend requires min_length=1
    expect(spst.state).toBe('open')
    expect(spst.contacts).toEqual({ common: null, no: null })

    const dpdt = createRelay(1, 'dpdt')
    expect(dpdt.id).toBe('RLY_2')
    expect(Object.keys(dpdt.contacts ?? {})).toHaveLength(6)

    const matrix = createRelay(2, 'matrix')
    expect(matrix.contacts).toEqual({ r1c1: null, r1c2: null, r2c1: null, r2c2: null })
  })
})

// ─── Pure helpers: validation & transforms ──────────────────────────────────

describe('relay contact binding validation', () => {
  const relays = [
    makeRelay({ id: 'RLY_1', contacts: { common: 'T1', no: null } }),
    makeRelay({ id: 'RLY_2', contacts: { common: null, no: null } }),
  ]

  it('checkBind blocks linking a contact to an occupied terminal with a message', () => {
    const res = checkBind(relays, 'RLY_2', 'common', 'T1')
    expect(res.ok).toBe(false)
    if (!res.ok) {
      expect(res.error).toContain('T1')
      expect(res.error).toContain('RLY_1')
    }
  })

  it('checkBind allows free terminals; applyBind writes the binding immutably', () => {
    expect(checkBind(relays, 'RLY_2', 'common', 'T3').ok).toBe(true)
    const res = applyBind(relays, 'RLY_2', 'common', 'T3')
    expect(res.ok).toBe(true)
    if (res.ok) {
      expect(res.relays[1].contacts?.common).toBe('T3')
      // original untouched (pure transform)
      expect(relays[1].contacts?.common).toBeNull()
      expect(res.relays[0].contacts?.common).toBe('T1')
    }
  })

  it('applyToggleState flips open/closed and persists in the returned array', () => {
    const closed = applyToggleState(relays, 'RLY_1')
    expect(closed[0].state).toBe('closed')
    const reopened = applyToggleState(closed, 'RLY_1')
    expect(reopened[0].state).toBe('open')
    expect(relays[0].state).toBe('open') // source unchanged
  })

  it('resizeMatrixContacts preserves in-bounds bindings and fills new cells with null', () => {
    const contacts = { r1c1: 'T1', r2c2: 'T4' }
    const grown = resizeMatrixContacts(contacts, 3, 2)
    expect(grown.r1c1).toBe('T1')
    expect(grown.r2c2).toBe('T4')
    expect(grown.r3c1).toBeNull()
    expect(grown.r3c2).toBeNull()
    expect(Object.keys(grown)).toHaveLength(6)

    const shrunk = resizeMatrixContacts(grown, 2, 2)
    expect(shrunk.r1c1).toBe('T1')
    expect(shrunk.r3c1).toBeUndefined()
    expect(Object.keys(shrunk)).toHaveLength(4)
  })
})

// ─── Component: rendering & interaction ─────────────────────────────────────

describe('RelayMatrixEditor component', () => {
  it('renders contact cells according to each relay type', async () => {
    const fixture = makeFixture([
      makeRelay({ id: 'RLY_1', type: 'spst', contacts: { common: null, no: null } }),
      makeRelay({
        id: 'RLY_2',
        type: 'dpdt',
        control_signal: 'CTRL_2',
        contacts: { '1c': null, '1no': null, '1nc': null, '2c': null, '2no': null, '2nc': null },
      }),
      makeRelay({
        id: 'RLY_3',
        type: 'matrix',
        control_signal: 'CTRL_3',
        contacts: { r1c1: null, r1c2: null, r2c1: null, r2c2: null },
      }),
    ])
    const wrapper = mountEditor(fixture)
    // relay list shows all three with their types
    const items = wrapper.findAll('.relay-list > li')
    expect(items).toHaveLength(3)
    expect(items[0].text()).toContain('spst')
    expect(items[1].text()).toContain('dpdt')
    expect(items[2].text()).toContain('matrix')

    // default selection = first relay → spst cells
    let cells = wrapper.findAll('.contact-cell').map((c) => c.text())
    expect(cells.some((t) => t.includes('common'))).toBe(true)
    expect(cells.some((t) => t.includes('no'))).toBe(true)

    // switch to dpdt relay → six pole cells
    await items[1].trigger('click')
    cells = wrapper.findAll('.contact-cell').map((c) => c.text())
    expect(cells.filter((t) => t.includes('1c') || t.includes('2c'))).toHaveLength(2)

    // switch to matrix relay → crosspoint grid cells
    await items[2].trigger('click')
    const matrixCells = wrapper.findAll('.matrix-cell')
    expect(matrixCells).toHaveLength(4)
  })

  it('adds a relay of the chosen type and emits relays-change', async () => {
    const wrapper = mountEditor(makeFixture([makeRelay({})]))
    await wrapper.find('.relay-add[data-type="dpdt"]').trigger('click')
    const relays = lastEmittedRelays(wrapper)
    expect(relays).toHaveLength(2)
    expect(relays[1].type).toBe('dpdt')
    expect(relays[1].id).toBe('RLY_2')
    expect(relays[1].control_signal).toBeTruthy()
  })

  it('toggling a relay state emits relays-change with the flipped state', async () => {
    const wrapper = mountEditor(makeFixture([makeRelay({ id: 'RLY_1', state: 'open' })]))
    await wrapper.find('.relay-list .relay-state-toggle').trigger('click')
    const relays = lastEmittedRelays(wrapper)
    expect(relays[0].state).toBe('closed')
  })

  it('bind flow: arm an unbound contact then pick a free terminal to bind', async () => {
    const wrapper = mountEditor(
      makeFixture([makeRelay({ id: 'RLY_1', contacts: { common: null, no: null } })]),
    )
    // arm the "no" contact → terminal picker appears
    const cells = wrapper.findAll('.contact-cell')
    const noCell = cells.find((c) => c.text().includes('no'))
    expect(noCell).toBeTruthy()
    await noCell!.trigger('click')
    expect(wrapper.find('.terminal-picker').exists()).toBe(true)

    // pick T2 → binding emitted
    await wrapper.find('.terminal-option[data-terminal="T2"]').trigger('click')
    const relays = lastEmittedRelays(wrapper)
    expect(relays[0].contacts?.no).toBe('T2')
  })

  it('blocks illegal link: occupied terminal is not offered and direct bind shows error without emitting', async () => {
    const wrapper = mountEditor(
      makeFixture([makeRelay({ id: 'RLY_1', contacts: { common: 'T1', no: null } })]),
    )
    // T1 is already bound to RLY_1.common → picker must not offer it
    await wrapper.findAll('.contact-cell')[1].trigger('click') // arm "no"
    const offered = wrapper.findAll('.terminal-option').map((b) => b.text())
    expect(offered).not.toContain('T1')
    expect(offered).toContain('T2')

    // defensive guard: requesting the occupied terminal directly is blocked
    const before = wrapper.emitted('relays-change')?.length ?? 0
    const vm = wrapper.vm as unknown as { requestBind: (r: string, c: string, t: string) => boolean }
    expect(vm.requestBind('RLY_1', 'no', 'T1')).toBe(false)
    await nextTick()
    expect(wrapper.emitted('relays-change')?.length ?? 0).toBe(before)
    expect(wrapper.find('.relay-error').text()).toContain('T1')
  })

  it('matrix resize adds rows while preserving existing bindings', async () => {
    const wrapper = mountEditor(
      makeFixture([
        makeRelay({
          id: 'RLY_1',
          type: 'matrix',
          control_signal: 'CTRL_1',
          contacts: { r1c1: 'T1', r1c2: null, r2c1: null, r2c2: 'T4' },
        }),
      ]),
    )
    await wrapper.find('.mx-row-add').trigger('click')
    const relays = lastEmittedRelays(wrapper)
    expect(relays[0].contacts?.r1c1).toBe('T1')
    expect(relays[0].contacts?.r2c2).toBe('T4')
    expect(relays[0].contacts?.r3c1).toBeNull()
    expect(relays[0].contacts?.r3c2).toBeNull()
  })
})
