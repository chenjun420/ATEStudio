/**
 * Tests for LoopContainerNode component.
 *
 * Verifies:
 * - Loop type label renders from data.loopType
 * - Condition text renders from data.condition
 * - Execution mode badge renders from data.executionMode (serial/parallel, NOT "sequential")
 * - Collapse toggle button renders in the header
 * - Collapsing hides all child cells via cell.hide()
 * - Expanding shows all child cells via cell.show()
 * - Iteration progress display (X/Y + bar) when status != idle and count > 0
 * - Parallel execution slot indicators when executionMode is 'parallel'
 * - Node border color reflects execution status
 * - Parent bounds auto-expand when a child node moves (graph node:change:position)
 * - Parent never shrinks below default 208x120 minimum
 * - Only children of THIS loop container trigger resize (not foreign nodes)
 */

import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import LoopContainerNode from '../LoopContainerNode.vue'
import type { LoopContainerData } from '@/models/nodes/types'
import type { Node } from '@antv/x6'

/** Extended data shape matching the component's internal type. */
type LoopContainerNodeData = LoopContainerData & {
  collapsed?: boolean
  currentIteration?: number
}

/** Factory for LoopContainerData with sensible defaults. */
function createLoopData(overrides: Partial<LoopContainerData> = {}): LoopContainerData {
  return {
    loopId: 'loop-001',
    loopType: 'for',
    condition: '',
    executionMode: 'serial',
    maxConcurrency: 1,
    status: 'idle',
    ...overrides,
  }
}

/** Minimal stub — the scaffold does not invoke Node methods. */
const stubNode = {} as Node

/** Mock cell with hide/show spies. */
interface MockCell {
  hide: ReturnType<typeof vi.fn>
  show: ReturnType<typeof vi.fn>
}

/** Build a mock Node with getChildren/setData/getData spies. */
function createMockNode(children: MockCell[] = [], data?: LoopContainerNodeData): Node {
  return {
    getChildren: vi.fn(() => children) as unknown as Node['getChildren'],
    setData: vi.fn() as unknown as Node['setData'],
    getData: vi.fn(() => data) as unknown as Node['getData'],
  } as Node
}

/** Mock graph with on/off spies for event listener testing. */
interface MockGraph {
  on: ReturnType<typeof vi.fn>
  off: ReturnType<typeof vi.fn>
}

/** Mock child with position/size/parent for bounds calculation testing. */
interface MockChildForBounds {
  getPosition: ReturnType<typeof vi.fn>
  getSize: ReturnType<typeof vi.fn>
  getParent: ReturnType<typeof vi.fn>
  isNode: ReturnType<typeof vi.fn>
  hide: ReturnType<typeof vi.fn>
  show: ReturnType<typeof vi.fn>
}

/** Build a mock Node with graph event support for bounds-calc tests. */
function createMockNodeWithGraph(
  children: MockChildForBounds[] = [],
  data?: LoopContainerNodeData,
): { node: Node; graph: MockGraph } {
  const graph: MockGraph = { on: vi.fn(), off: vi.fn() }
  const node = {
    getChildren: vi.fn(() => children) as unknown as Node['getChildren'],
    setData: vi.fn() as unknown as Node['setData'],
    getData: vi.fn(() => data) as unknown as Node['getData'],
    resize: vi.fn() as unknown as Node['resize'],
    model: { graph },
  } as unknown as Node
  return { node, graph }
}

/** Build a mock child node with position, size, and parent for bounds tests. */
function createMockChild(
  x: number,
  y: number,
  width: number,
  height: number,
): MockChildForBounds {
  return {
    getPosition: vi.fn(() => ({ x, y })),
    getSize: vi.fn(() => ({ width, height })),
    getParent: vi.fn(() => null),
    isNode: vi.fn(() => true),
    hide: vi.fn(),
    show: vi.fn(),
  }
}

/** Extract the node:change:position handler registered via graph.on. */
function getPositionHandler(graph: MockGraph): (args: { node: Node }) => void {
  const calls = graph.on.mock.calls.filter(
    ([event]) => event === 'node:change:position',
  )
  expect(calls.length).toBeGreaterThan(0)
  return calls[0]![1] as (args: { node: Node }) => void
}

describe('LoopContainerNode', () => {
  it('test_renders_loop_type_label', () => {
    const wrapper = mount(LoopContainerNode, {
      props: {
        data: createLoopData({ loopType: 'for' }),
        node: stubNode,
      },
    })
    expect(wrapper.text()).toContain('for')
  })

  it('test_renders_condition', () => {
    const wrapper = mount(LoopContainerNode, {
      props: {
        data: createLoopData({ condition: 'count > 5' }),
        node: stubNode,
      },
    })
    expect(wrapper.text()).toContain('count > 5')
  })

  it('test_renders_execution_mode_badge', () => {
    const wrapper = mount(LoopContainerNode, {
      props: {
        data: createLoopData({ executionMode: 'serial' }),
        node: stubNode,
      },
    })
    expect(wrapper.text()).toContain('serial')
  })

  it('test_toggle_button_renders', () => {
    const wrapper = mount(LoopContainerNode, {
      props: {
        data: createLoopData(),
        node: stubNode,
      },
    })
    const toggle = wrapper.find('[data-testid="collapse-toggle"]')
    expect(toggle.exists()).toBe(true)
    // Default state is expanded — aria-expanded should be true
    expect(toggle.attributes('aria-expanded')).toBe('true')
  })

  it('test_collapse_hides_children', async () => {
    const child1: MockCell = { hide: vi.fn(), show: vi.fn() }
    const child2: MockCell = { hide: vi.fn(), show: vi.fn() }
    const mockNode = createMockNode([child1, child2])

    const wrapper = mount(LoopContainerNode, {
      props: {
        data: createLoopData(),
        node: mockNode,
      },
    })

    // Initially expanded — click to collapse
    await wrapper.find('[data-testid="collapse-toggle"]').trigger('click')

    expect(child1.hide).toHaveBeenCalledTimes(1)
    expect(child2.hide).toHaveBeenCalledTimes(1)
    // collapsed=true persisted in node data
    expect(mockNode.setData).toHaveBeenCalledWith(
      expect.objectContaining({ collapsed: true }),
    )
  })

  it('test_expand_shows_children', async () => {
    const child1: MockCell = { hide: vi.fn(), show: vi.fn() }
    const child2: MockCell = { hide: vi.fn(), show: vi.fn() }
    // Start collapsed
    const data = createLoopData() as LoopContainerNodeData
    data.collapsed = true
    const mockNode = createMockNode([child1, child2], data)

    const wrapper = mount(LoopContainerNode, {
      props: {
        data,
        node: mockNode,
      },
    })

    // aria-expanded should be false when collapsed
    const toggle = wrapper.find('[data-testid="collapse-toggle"]')
    expect(toggle.attributes('aria-expanded')).toBe('false')

    // Click to expand
    await toggle.trigger('click')

    expect(child1.show).toHaveBeenCalledTimes(1)
    expect(child2.show).toHaveBeenCalledTimes(1)
    // collapsed=false persisted in node data
    expect(mockNode.setData).toHaveBeenCalledWith(
      expect.objectContaining({ collapsed: false }),
    )
  })

  it('test_iteration_progress_display', () => {
    const data = createLoopData({ status: 'running', count: 10 }) as LoopContainerNodeData
    data.currentIteration = 3

    const wrapper = mount(LoopContainerNode, {
      props: {
        data,
        node: stubNode,
      },
    })

    const progress = wrapper.find('[data-testid="iteration-progress"]')
    expect(progress.exists()).toBe(true)
    // "Iteration 3/10" text appears
    expect(progress.text()).toContain('Iteration 3/10')
    // 30% computed from 3/10
    expect(progress.text()).toContain('30%')

    // Progress bar exists with correct width
    const bar = wrapper.find('[data-testid="iteration-progress-bar"]')
    expect(bar.exists()).toBe(true)
    expect(bar.attributes('style')).toContain('30%')
  })

  it('test_parallel_slots_display', () => {
    const wrapper = mount(LoopContainerNode, {
      props: {
        data: createLoopData({ executionMode: 'parallel', maxConcurrency: 4 }),
        node: stubNode,
      },
    })

    const slots = wrapper.find('[data-testid="parallel-slots"]')
    expect(slots.exists()).toBe(true)

    // 4 slot indicators render (one per maxConcurrency)
    const slotDots = slots.findAll('[data-slot-index]')
    expect(slotDots.length).toBe(4)
  })

  it('test_status_border_color', () => {
    // running → info border
    const runningWrapper = mount(LoopContainerNode, {
      props: {
        data: createLoopData({ status: 'running' }),
        node: stubNode,
      },
    })
    expect(runningWrapper.find('.loop-container-node').classes()).toContain('tw-border-info')

    // failed → error border
    const failedWrapper = mount(LoopContainerNode, {
      props: {
        data: createLoopData({ status: 'failed' }),
        node: stubNode,
      },
    })
    expect(failedWrapper.find('.loop-container-node').classes()).toContain('tw-border-error')

    // idle → neutral border (default)
    const idleWrapper = mount(LoopContainerNode, {
      props: {
        data: createLoopData({ status: 'idle' }),
        node: stubNode,
      },
    })
    expect(idleWrapper.find('.loop-container-node').classes()).toContain('tw-border-neutral-300')
  })

  it('test_parent_expands_when_child_moves', () => {
    // Two children: (10,10) 100x50 and (200,100) 100x50.
    // Bounding box: minX=10, minY=10, maxX=300, maxY=150 → 290x140.
    // Both above minimum → resize(290, 140).
    const child1 = createMockChild(10, 10, 100, 50)
    const child2 = createMockChild(200, 100, 100, 50)
    const { node: mockNode, graph } = createMockNodeWithGraph([child1, child2])

    // Wire parent relationship after both node and children exist.
    child1.getParent.mockReturnValue(mockNode)
    child2.getParent.mockReturnValue(mockNode)

    mount(LoopContainerNode, {
      props: {
        data: createLoopData(),
        node: mockNode,
      },
    })

    const handler = getPositionHandler(graph)
    handler({ node: child1 as unknown as Node })

    expect(mockNode.resize).toHaveBeenCalledWith(290, 140)
  })

  it('test_parent_does_not_shrink_below_default', () => {
    // One child at (0,0) 50x30 — bounding box 50x30, below 208x120.
    // resize clamped to minimum (208, 120).
    const child = createMockChild(0, 0, 50, 30)
    const { node: mockNode, graph } = createMockNodeWithGraph([child])

    child.getParent.mockReturnValue(mockNode)

    mount(LoopContainerNode, {
      props: {
        data: createLoopData(),
        node: mockNode,
      },
    })

    const handler = getPositionHandler(graph)
    handler({ node: child as unknown as Node })

    expect(mockNode.resize).toHaveBeenCalledWith(208, 120)
  })

  it('test_only_listens_to_own_children', () => {
    // Child whose parent is a DIFFERENT node — must not trigger resize.
    const otherParent = {} as Node
    const child = createMockChild(500, 500, 100, 100)
    const { node: mockNode, graph } = createMockNodeWithGraph([child])

    child.getParent.mockReturnValue(otherParent)

    mount(LoopContainerNode, {
      props: {
        data: createLoopData(),
        node: mockNode,
      },
    })

    const handler = getPositionHandler(graph)
    handler({ node: child as unknown as Node })

    expect(mockNode.resize).not.toHaveBeenCalled()
  })
})
