/**
 * Tests for fault suspect cards (T33, v41-gap-analysis #33, 设计文档 §8.3.7).
 *
 * Two layers:
 * - Pure helpers in `utils/faultSuggestions.ts`: severity ranking + desc
 *   comparator, fault-type → fix-suggestion mapping (complete for every
 *   backend FaultType value), suspect-card builder that expands each fault's
 *   `location.suspect_links` into per-link cards (backend suggestion wins
 *   over the local map; unknown types get a generic fallback).
 * - `FaultSuspectPanel.vue` component: renders severity-sorted cards with a
 *   color-coded severity badge, link id, confidence (when present) and the
 *   fix suggestion; clicking a card emits `select-link` with the link id so
 *   the parent can highlight it on the canvas. Empty input renders the
 *   empty state and no cards.
 *
 * Spec guards covered here:
 * - Must NOT recompute localization in frontend — cards only decorate what
 *   the backend FaultLocalizer already produced.
 * - Cards must not block interaction — plain clickable divs, no modal.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'

import FaultSuspectPanel from '../FaultSuspectPanel.vue'
import {
  buildSuspectCards,
  compareBySeverityDesc,
  severityRank,
  suggestionForFaultType,
  GENERIC_SUGGESTION_FALLBACK,
  type SuspectFault,
} from '@/utils/faultSuggestions'

// ─── Factories ───────────────────────────────────────────────────────────────

function makeFault(overrides: Partial<SuspectFault> = {}): SuspectFault {
  return {
    type: 'open_circuit',
    severity: 'error',
    message: '测试点 TP1 测量值为零，疑似链路开路',
    suggestion: null,
    location: { suspect_links: ['L1'] },
    ...overrides,
  }
}

/** Backend shared/fixture_topology.py::FaultType enum values (source of truth). */
const BACKEND_FAULT_TYPES = [
  'open_circuit',
  'short_circuit',
  'over_voltage',
  'over_current',
  'communication',
  'measurement_out_of_range',
  'relay_fault',
] as const

// ─── Pure helpers ────────────────────────────────────────────────────────────

describe('severityRank / compareBySeverityDesc', () => {
  it('[T1] ranks critical > error > warning > unknown', () => {
    expect(severityRank('critical')).toBeGreaterThan(severityRank('error'))
    expect(severityRank('error')).toBeGreaterThan(severityRank('warning'))
    expect(severityRank('warning')).toBeGreaterThan(severityRank('bogus'))
    expect(severityRank('bogus')).toBe(0)
  })

  it('[T2] comparator orders high>medium>low regardless of input order', () => {
    const faults = [
      makeFault({ type: 'measurement_out_of_range', severity: 'warning' }),
      makeFault({ type: 'open_circuit', severity: 'critical' }),
      makeFault({ type: 'relay_fault', severity: 'error' }),
    ]
    const sorted = [...faults].sort(compareBySeverityDesc)
    expect(sorted.map((f) => f.severity)).toEqual(['critical', 'error', 'warning'])
  })
})

describe('suggestionForFaultType', () => {
  it('[T3] maps EVERY backend FaultType to a non-fallback suggestion (+ contact_resistance)', () => {
    for (const type of [...BACKEND_FAULT_TYPES, 'contact_resistance']) {
      const text = suggestionForFaultType(type)
      expect(text.length, `missing suggestion for ${type}`).toBeGreaterThan(0)
      expect(text, `${type} fell back to generic`).not.toBe(GENERIC_SUGGESTION_FALLBACK)
    }
    // relay_fault / communication explicitly required by plan §8.3.6 strategies
    expect(suggestionForFaultType('relay_fault')).toContain('继电器')
    expect(suggestionForFaultType('communication')).toContain('通信')
  })

  it('[T4] backend-produced suggestion wins; missing/null falls back to map then generic', () => {
    const cards = buildSuspectCards([
      makeFault({ suggestion: '后端建议：更换线缆' }),
      makeFault({ type: 'short_circuit', suggestion: null }),
      makeFault({ type: 'unknown_future_type', suggestion: undefined }),
    ])
    expect(cards[0].suggestion).toBe('后端建议：更换线缆')
    expect(cards[1].suggestion).toBe(suggestionForFaultType('short_circuit'))
    expect(cards[2].suggestion).toBe(GENERIC_SUGGESTION_FALLBACK)
  })
})

describe('buildSuspectCards', () => {
  it('[T5] expands each suspect_link into its own card, sorted by severity desc', () => {
    const cards = buildSuspectCards([
      makeFault({
        type: 'measurement_out_of_range',
        severity: 'warning',
        location: { suspect_links: ['L3', 'L4'] },
      }),
      makeFault({ type: 'open_circuit', severity: 'critical', location: { suspect_links: ['L1', 'L2'] } }),
    ])
    expect(cards.map((c) => c.linkId)).toEqual(['L1', 'L2', 'L3', 'L4'])
    expect(cards[0].severity).toBe('critical')
    expect(cards[0].key).not.toBe(cards[1].key) // unique keys for v-for
  })

  it('[T6] fault without suspect_links still yields one card (empty linkId, not clickable)', () => {
    const cards = buildSuspectCards([makeFault({ location: null })])
    expect(cards).toHaveLength(1)
    expect(cards[0].linkId).toBe('')
  })

  it('[T7] passes confidence through only when present and numeric', () => {
    const [withConf, withoutConf] = buildSuspectCards([
      makeFault({ confidence: 0.87 }),
      makeFault({}),
    ])
    expect(withConf.confidence).toBeCloseTo(0.87)
    expect(withoutConf.confidence).toBeUndefined()
  })
})

// ─── Component ───────────────────────────────────────────────────────────────

describe('FaultSuspectPanel.vue', () => {
  it('[T8] empty state: renders empty indicator, zero cards', () => {
    const wrapper = mount(FaultSuspectPanel, {
      props: { faults: [] },
      global: { plugins: [ElementPlus] },
    })
    expect(wrapper.findAll('.suspect-card')).toHaveLength(0)
    expect(wrapper.find('.el-empty').exists()).toBe(true)
  })

  it('[T9] renders severity-sorted cards with badge, link id and suggestion text', () => {
    const wrapper = mount(FaultSuspectPanel, {
      props: {
        faults: [
          makeFault({ type: 'measurement_out_of_range', severity: 'warning', location: { suspect_links: ['L9'] } }),
          makeFault({ type: 'open_circuit', severity: 'critical', location: { suspect_links: ['L1'] }, confidence: 0.9 }),
        ],
      },
      global: { plugins: [ElementPlus] },
    })
    const cards = wrapper.findAll('.suspect-card')
    expect(cards).toHaveLength(2)
    // critical first
    expect(cards[0].text()).toContain('critical')
    expect(cards[0].text()).toContain('L1')
    expect(cards[0].text()).toContain('open_circuit')
    expect(cards[0].text()).toContain('90%')
    expect(cards[1].text()).toContain('warning')
  })

  it('[T10] click card emits select-link with its link id; linkless card never emits', async () => {
    const wrapper = mount(FaultSuspectPanel, {
      props: {
        faults: [
          makeFault({ location: { suspect_links: ['L7'] } }),
          makeFault({ location: null }),
        ],
      },
      global: { plugins: [ElementPlus] },
    })
    const cards = wrapper.findAll('.suspect-card')
    await cards[0].trigger('click')
    await cards[1].trigger('click')
    expect(wrapper.emitted('select-link')).toEqual([['L7']])
  })
})
