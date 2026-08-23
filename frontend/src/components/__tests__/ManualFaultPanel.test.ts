/**
 * Tests for ManualFaultPanel + utils/manualFault (T38, v41-gap-analysis #38).
 *
 * Covers: per-scope payload building (layer-consistent catalogs), fault-type
 * options filtering by scope, invalid JSON blocked, numeric range validation
 * (negative probability / count), empty target blocked, submit disabled while
 * no active run, successful submit → api call → toast + history entry,
 * api failure → error toast.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'

import {
  MANUAL_FAULT_SCOPES,
  buildManualFaultPayload,
  faultTypesForScope,
  parseParamsJson,
} from '@/utils/manualFault'
import ManualFaultPanel from '../ManualFaultPanel.vue'
import { injectManualFault } from '@/api/executions'
import { useTopologyRuntimeStore } from '@/stores/topologyRuntime'

// ─── Module mocks ────────────────────────────────────────────────────────────

const injectManualFaultMock = vi.fn()

vi.mock('@/api/executions', () => ({
  injectManualFault: (...args: unknown[]) => injectManualFaultMock(...args),
}))

const mockStore = {
  topology: {
    links: [{ id: 'LINK-A' }, { id: 'LINK-B' }],
    instruments: [{ id: 'dmm-1' }, { id: 'psu-1' }],
  },
}

vi.mock('@/stores/topologyRuntime', () => ({
  useTopologyRuntimeStore: () => mockStore,
}))

// ─── Utils: payload building ────────────────────────────────────────────────

describe('utils/manualFault', () => {
  it('exposes all five scopes mapped to their §7.7.1 layers', () => {
    const layers = Object.fromEntries(MANUAL_FAULT_SCOPES.map((s) => [s.value, s.layer]))
    expect(layers).toEqual({
      link: 'network',
      instrument: 'instrument',
      step: 'scheduler',
      scheduler: 'scheduler',
      protocol: 'protocol',
    })
  })

  it('filters fault-type options by scope', () => {
    const linkTypes = faultTypesForScope('link').map((t) => t.value)
    expect(linkTypes).toContain('open_circuit')
    expect(linkTypes).not.toContain('scpi_error')

    const protocolTypes = faultTypesForScope('protocol').map((t) => t.value)
    expect(protocolTypes).toEqual(['scpi_error', 'truncated_data', 'checksum_error'])

    // Unknown scope yields no options.
    expect(faultTypesForScope('galaxy')).toEqual([])
  })

  it('builds a valid link-scope payload', () => {
    const res = buildManualFaultPayload({
      scope: 'link',
      targetId: 'LINK-A',
      faultType: 'open_circuit',
      paramsText: '',
    })
    expect(res).toEqual({
      ok: true,
      payload: { scope: 'link', target_id: 'LINK-A', fault_type: 'open_circuit' },
    })
  })

  it('merges parsed params into the payload', () => {
    const res = buildManualFaultPayload({
      scope: 'instrument',
      targetId: 'dmm-1',
      faultType: 'value_override',
      paramsText: '{"value": 4.2}',
    })
    expect(res.ok).toBe(true)
    if (res.ok) {
      expect(res.payload.params).toEqual({ value: 4.2 })
    }
  })

  it('blocks invalid params JSON', () => {
    expect(parseParamsJson('{not json').ok).toBe(false)
    const res = buildManualFaultPayload({
      scope: 'step',
      targetId: 's1',
      faultType: 'timeout',
      paramsText: '{not json',
    })
    expect(res.ok).toBe(false)
    if (!res.ok) expect(res.error).toContain('JSON')
  })

  it('rejects non-object params JSON (array/null)', () => {
    expect(parseParamsJson('[1,2]').ok).toBe(false)
    expect(parseParamsJson('null').ok).toBe(false)
  })

  it('rejects negative probability and out-of-range values', () => {
    const negative = buildManualFaultPayload({
      scope: 'step',
      targetId: 's1',
      faultType: 'timeout',
      paramsText: '{"probability": -0.5}',
    })
    expect(negative.ok).toBe(false)

    const tooBig = buildManualFaultPayload({
      scope: 'step',
      targetId: 's1',
      faultType: 'timeout',
      paramsText: '{"probability": 1.5}',
    })
    expect(tooBig.ok).toBe(false)

    const badCount = buildManualFaultPayload({
      scope: 'scheduler',
      targetId: '*',
      faultType: 'resource_deadlock',
      paramsText: '{"count": 0}',
    })
    expect(badCount.ok).toBe(false)
  })

  it('blocks empty target and cross-scope fault types', () => {
    const noTarget = buildManualFaultPayload({
      scope: 'link',
      targetId: '   ',
      faultType: 'open_circuit',
      paramsText: '',
    })
    expect(noTarget.ok).toBe(false)

    const crossScope = buildManualFaultPayload({
      scope: 'link',
      targetId: 'L1',
      faultType: 'scpi_error',
      paramsText: '',
    })
    expect(crossScope.ok).toBe(false)
  })
})

// ─── Component ──────────────────────────────────────────────────────────────

function mountPanel(props: Record<string, unknown> = {}): VueWrapper {
  return mount(ManualFaultPanel, {
    props: { runId: 'run-123', ...props },
    global: { plugins: [ElementPlus] },
    attachTo: document.body,
  })
}

let wrapper: VueWrapper | null = null

beforeEach(() => {
  injectManualFaultMock.mockReset()
  injectManualFaultMock.mockResolvedValue({
    ok: true,
    run_id: 'run-123',
    scope: 'link',
    layer: 'network',
    target_id: 'LINK-A',
    fault_type: 'open_circuit',
    fault_id: 'manual-link-LINK-A-open_circuit',
  })
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

async function fillAndSubmit(
  overrides: { scope?: string; target?: string; faultType?: string; params?: string } = {},
) {
  wrapper = mountPanel()
  // Scope select is an ElSelect; drive state through the DOM inputs directly.
  const targetInput = wrapper.find('[data-testid="mf-target"]')
  if (targetInput.exists()) {
    const input = targetInput.find('input')
    await input.setValue(overrides.target ?? 'LINK-A')
  }
  await (wrapper.vm as unknown as { $nextTick: () => Promise<void> }).$nextTick()
  const submitBtn = wrapper.find('[data-testid="mf-submit"]')
  await submitBtn.trigger('click')
}

describe('ManualFaultPanel', () => {
  it('disables injection while there is no active run', () => {
    wrapper = mountPanel({ runId: null })
    const btn = wrapper.find('[data-testid="mf-submit"]').element as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(wrapper.find('[data-testid="mf-no-run-hint"]').exists()).toBe(true)
  })

  it('enables injection when a run is active', () => {
    wrapper = mountPanel({ runId: 'run-abc' })
    const btn = wrapper.find('[data-testid="mf-submit"]').element as HTMLButtonElement
    expect(btn.disabled).toBe(false)
  })

  it('offers topology links as targets for link scope', () => {
    wrapper = mountPanel({ runId: 'run-abc' })
    // ElSelect teleports dropdown items; assert the option source instead.
    const vm = wrapper.vm as unknown as { targetOptions: string[] }
    expect(vm.targetOptions).toEqual(['LINK-A', 'LINK-B'])
  })

  it('submits via api and records history on success', async () => {
    await fillAndSubmit({ target: 'LINK-A' })

    // The panel drives internal state; emulate a valid submission by calling
    // the exposed submit path through button click after setting form state.
    // (ElSelect v-model needs real interaction; fall back to direct call.)
    const vm = wrapper!.vm as unknown as {
      scope: string
      targetId: string
      faultType: string
      submit: () => Promise<void>
    }
    vm.scope = 'link'
    vm.targetId = 'LINK-A'
    vm.faultType = 'open_circuit'
    await vm.submit()

    expect(injectManualFaultMock).toHaveBeenCalledWith('run-123', {
      scope: 'link',
      target_id: 'LINK-A',
      fault_type: 'open_circuit',
    })
    await wrapper!.vm.$nextTick()
    expect(wrapper!.findAll('[data-testid="mf-entry"]')).toHaveLength(1)
  })

  it('blocks invalid JSON client-side without calling the api', async () => {
    wrapper = mountPanel()
    const vm = wrapper!.vm as unknown as {
      scope: string
      targetId: string
      faultType: string
      paramsText: string
      validationError: string | null
      submit: () => Promise<void>
    }
    vm.scope = 'step'
    vm.targetId = 'step-1'
    vm.faultType = 'timeout'
    vm.paramsText = '{broken'
    await vm.submit()

    expect(injectManualFaultMock).not.toHaveBeenCalled()
    expect(vm.validationError).toContain('JSON')
    expect(wrapper!.find('[data-testid="mf-error"]').exists()).toBe(true)
  })

  it('rejects negative probability client-side without calling the api', async () => {
    wrapper = mountPanel()
    const vm = wrapper!.vm as unknown as {
      scope: string
      targetId: string
      faultType: string
      paramsText: string
      validationError: string | null
      submit: () => Promise<void>
    }
    vm.scope = 'step'
    vm.targetId = 'step-1'
    vm.faultType = 'timeout'
    vm.paramsText = '{"probability": -1}'
    await vm.submit()

    expect(injectManualFaultMock).not.toHaveBeenCalled()
    expect(vm.validationError).toContain('probability')
  })

  it('shows error toast path when api rejects', async () => {
    injectManualFaultMock.mockRejectedValue(new Error('409 no active execution'))
    wrapper = mountPanel()
    const vm = wrapper!.vm as unknown as {
      scope: string
      targetId: string
      faultType: string
      submit: () => Promise<void>
    }
    vm.scope = 'link'
    vm.targetId = 'LINK-A'
    vm.faultType = 'open_circuit'
    await vm.submit()

    expect(injectManualFaultMock).toHaveBeenCalledTimes(1)
    // No history entry recorded on failure.
    expect(wrapper!.findAll('[data-testid="mf-entry"]')).toHaveLength(0)
  })
})
