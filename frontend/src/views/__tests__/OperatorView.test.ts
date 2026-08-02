/**
 * Tests for OperatorView.vue view component.
 *
 * Verifies:
 * - Renders the operator-view root container
 * - Passes station_id from route params to OperatorInteractionPanel
 * - Read-only mode: no edit buttons (no sequence editor controls)
 * - Renders OperatorInteractionPanel as child component
 *
 * The OperatorInteractionPanel is mocked to isolate the view test.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref, h } from 'vue'
import ElementPlus from 'element-plus'
import { createRouter, createMemoryHistory, type Router } from 'vue-router'
import OperatorView from '../OperatorView.vue'

// ─── Mock OperatorInteractionPanel ───────────────────────────────────────────

/** Props captured from the panel mock for assertion. */
let capturedStationId = ref('')

vi.mock('@/components/OperatorInteractionPanel.vue', () => ({
  default: {
    name: 'OperatorInteractionPanel',
    props: ['stationId'],
    setup(props: { stationId: string }) {
      capturedStationId.value = props.stationId
      return () =>
        h('div', { 'data-testid': 'mock-panel' }, `Station: ${props.stationId}`)
    },
  },
}))

// ─── Helpers ─────────────────────────────────────────────────────────────────

function createRouterWithParam(stationId: string): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/operator/:station_id',
        name: 'OperatorView',
        component: OperatorView,
        props: true,
      },
      { path: '/', redirect: '/operator/test' },
    ],
  })
}

async function mountView(stationId: string) {
  const router = createRouterWithParam(stationId)
  await router.push(`/operator/${stationId}`)
  await router.isReady()

  return mount(OperatorView, {
    global: {
      plugins: [router, ElementPlus],
    },
  })
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('OperatorView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    capturedStationId.value = ''
  })

  it('test_renders_operator_view_container', async () => {
    const wrapper = await mountView('w001')
    const root = wrapper.find('[data-testid="operator-view"]')
    expect(root.exists()).toBe(true)
  })

  it('test_passes_station_id_from_route_params', async () => {
    await mountView('w001')
    expect(capturedStationId.value).toBe('w001')
  })

  it('test_passes_different_station_id', async () => {
    await mountView('STN-42')
    expect(capturedStationId.value).toBe('STN-42')
  })

  it('test_renders_operator_interaction_panel', async () => {
    const wrapper = await mountView('w001')
    const panel = wrapper.find('[data-testid="mock-panel"]')
    expect(panel.exists()).toBe(true)
  })

  it('test_read_only_mode_no_edit_buttons', async () => {
    const wrapper = await mountView('w001')
    // The view should NOT contain any "Edit", "Save", "Delete" buttons
    // that would indicate editing capability.
    const text = wrapper.text()
    expect(text).not.toContain('Edit Sequence')
    expect(text).not.toContain('Save')
    expect(text).not.toContain('Delete Step')
  })

  it('test_empty_station_id_handled_gracefully', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        {
          path: '/operator/:station_id',
          name: 'OperatorView',
          component: OperatorView,
          props: true,
        },
        {
          path: '/operator',
          redirect: '/operator/unknown',
        },
      ],
    })
    await router.push('/operator/unknown')
    await router.isReady()

    const wrapper = mount(OperatorView, {
      global: {
        plugins: [router, ElementPlus],
      },
    })
    // Should still render the view without crashing
    const root = wrapper.find('[data-testid="operator-view"]')
    expect(root.exists()).toBe(true)
  })
})
