/**
 * Tests for KnowledgeGraph.vue (task 25).
 *
 * Verifies:
 * - Nodes/edges from a mocked GET /knowledge/graph response render in the SVG
 *   (one circle per node, node labels, a legend per node type, edge lines).
 * - A graph-backend 503 renders the friendly "unavailable" empty state (no
 *   crash, no SVG) — graceful degradation.
 * - A valid but empty response renders the "no nodes" empty state.
 * - The reload button re-calls the API.
 *
 * The @/api/knowledge module is mocked; Element Plus is installed globally.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import ElementPlus from 'element-plus'
import KnowledgeGraph from '../KnowledgeGraph.vue'

const { fetchGraphMock } = vi.hoisted(() => ({ fetchGraphMock: vi.fn() }))

vi.mock('@/api/knowledge', () => ({
  fetchKnowledgeGraph: fetchGraphMock,
}))

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver

function makeGraph() {
  return {
    nodes: [
      { id: 'n1', label: 'Component', type: 'Component', name: 'PSU-12V', properties: {} },
      { id: 'n2', label: 'Fault', type: 'Fault', name: 'voltage_drift', properties: {} },
      { id: 'n3', label: 'Test', type: 'Test', name: '', properties: {} },
    ],
    edges: [
      { source: 'n1', target: 'n2', type: 'hasFault' },
      { source: 'n3', target: 'n2', type: 'verifies' },
    ],
  }
}

async function mountView() {
  const wrapper = mount(KnowledgeGraph, {
    global: { plugins: [ElementPlus] },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

describe('KnowledgeGraph', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders nodes and edges from the mocked graph response', async () => {
    fetchGraphMock.mockResolvedValue(makeGraph())
    const wrapper = await mountView()

    // Header counts.
    expect(wrapper.find('[data-testid="count-nodes"]').text()).toContain('3')
    expect(wrapper.find('[data-testid="count-edges"]').text()).toContain('2')

    // SVG graph is present with one circle per node.
    expect(wrapper.find('[data-testid="graph-svg"]').exists()).toBe(true)
    const circles = wrapper.findAll('[data-testid="graph-nodes"] circle')
    expect(circles).toHaveLength(3)

    // Edge lines render (one per edge with both endpoints present).
    const edges = wrapper.findAll('[data-testid="graph-edges"] line')
    expect(edges).toHaveLength(2)

    // Node labels render (name preferred; falls back to label/id).
    const svgText = wrapper.find('[data-testid="graph-svg"]').text()
    expect(svgText).toContain('PSU-12V')
    expect(svgText).toContain('voltage_drift')

    // Legend lists the distinct node types.
    const legend = wrapper.find('[data-testid="graph-legend"]')
    expect(legend.text()).toContain('Component')
    expect(legend.text()).toContain('Fault')
    expect(legend.text()).toContain('Test')
  })

  it('renders the unavailable empty state when the graph backend returns 503', async () => {
    fetchGraphMock.mockRejectedValue({ response: { status: 503 } })
    const wrapper = await mountView()

    const empty = wrapper.find('[data-testid="graph-unavailable"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toContain('unavailable')
    // No graph SVG is rendered in the degraded state.
    expect(wrapper.find('[data-testid="graph-svg"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="error-alert"]').exists()).toBe(false)
  })

  it('renders the empty state when the response has no nodes', async () => {
    fetchGraphMock.mockResolvedValue({ nodes: [], edges: [] })
    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="graph-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="graph-svg"]').exists()).toBe(false)
  })

  it('renders an error banner for a non-503 failure', async () => {
    fetchGraphMock.mockRejectedValue(new Error('Network down'))
    const wrapper = await mountView()

    const banner = wrapper.find('[data-testid="error-alert"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Network down')
  })

  it('reload button re-fetches the graph', async () => {
    fetchGraphMock.mockResolvedValue(makeGraph())
    const wrapper = await mountView()
    expect(fetchGraphMock).toHaveBeenCalledTimes(1)

    await wrapper.find('[data-testid="btn-reload"]').trigger('click')
    await flushPromises()
    expect(fetchGraphMock).toHaveBeenCalledTimes(2)
  })
})
