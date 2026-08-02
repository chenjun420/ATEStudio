/**
 * Integration tests for LoopContainerNode with SubGraphContainer parent-child API.
 *
 * Three tests verify the integration between LoopContainerNode.vue's collapse/
 * expand and auto-expand logic and the X6 parent-child cell API that
 * SubGraphContainer.vue relies on (getChildren, setParent, bidirectional sync).
 *
 * NOTE: @antv/x6 is ESM-only and cannot be loaded in vitest's jsdom environment
 * (see learnings.md Todo 13: "lib/index.js uses exports (CJS) but is treated
 * as ESM, causing ReferenceError: exports is not defined"). These tests use a
 * minimal mock Graph that faithfully implements the parent-child API.
 *
 * The mock uses Vue's reactive() for node data, simulating the x6-vue-shape
 * reactivity bridge: when setData modifies the data, the component's nodeData
 * computed re-evaluates. This allows testing toggleCollapse's full cycle
 * (collapse → expand) without manually calling wrapper.setProps().
 *
 * Object identity is preserved: setParent stores the raw node reference,
 * getParent returns it, and toRaw(props.node) unwraps Vue's proxy to the same
 * raw object — so the identity check in onChildPositionChange works correctly.
 */

import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { reactive } from 'vue'
import LoopContainerNode from '../components/nodes/LoopContainerNode.vue'
import type { Node } from '@antv/x6'
import type { LoopContainerData, ScriptStepData } from '@/models/nodes/types'

// ─── Minimal X6 Graph/Node mock ─────────────────────────────────────────────
//
// Faithfully implements the parent-child relationship API that SubGraphContainer
// and LoopContainerNode rely on. Mirrors X6's Cell semantics:
//   - setParent(parent): sets parent ref, removes from old parent's children.
//     Does NOT add to new parent's children — use addChild for that.
//   - addChild(child): adds to children array AND calls child.setParent(this).
//   - getChildren(): returns the children array (empty if none added).
//
// The reactive() wrapper on _data simulates x6-vue-shape's reactivity bridge.
// When the component calls node.setData({ collapsed: true }), Object.assign
// modifies the reactive data in place, triggering Vue's set trap and causing
// the nodeData/isCollapsed computeds to re-evaluate.

interface MockNodeConfig {
  id?: string
  x?: number
  y?: number
  width?: number
  height?: number
  shape?: string
  data?: unknown
}

let _idCounter = 0

class MockNode {
  readonly id: string
  readonly model: { graph: MockGraph }
  _parent: MockNode | null = null
  _children: MockNode[] = []
  private _data: Record<string, unknown>
  private _position: { x: number; y: number }
  private _size: { width: number; height: number }
  private _visible = true

  constructor(config: MockNodeConfig, graph: MockGraph) {
    _idCounter += 1
    this.id = config.id ?? `node-${_idCounter}`
    this.model = { graph }
    this._data = reactive(
      (config.data as Record<string, unknown> | undefined) ?? {},
    )
    this._position = { x: config.x ?? 0, y: config.y ?? 0 }
    this._size = { width: config.width ?? 208, height: config.height ?? 120 }
  }

  getChildren(): MockNode[] {
    return this._children
  }

  setParent(parent: MockNode | null): void {
    if (this._parent !== null) {
      const idx = this._parent._children.indexOf(this)
      if (idx >= 0) this._parent._children.splice(idx, 1)
    }
    this._parent = parent
  }

  getParent(): MockNode | null {
    return this._parent
  }

  addChild(child: MockNode): void {
    child.setParent(this)
    if (!this._children.includes(child)) {
      this._children.push(child)
    }
  }

  getData(): Record<string, unknown> {
    return this._data
  }

  setData(data: Record<string, unknown>): void {
    Object.assign(this._data, data)
  }

  hide(): void {
    this._visible = false
  }

  show(): void {
    this._visible = true
  }

  isVisible(): boolean {
    return this._visible
  }

  resize(width: number, height: number): void {
    this._size = { width, height }
  }

  getPosition(): { x: number; y: number } {
    return { ...this._position }
  }

  getSize(): { width: number; height: number } {
    return { ...this._size }
  }

  isNode(): boolean {
    return true
  }
}

class MockGraph {
  private _nodes = new Map<string, MockNode>()
  private _listeners: Array<{ event: string; callback: (args: unknown) => void }> = []

  addNode(config: MockNodeConfig): MockNode {
    const node = new MockNode(config, this)
    this._nodes.set(node.id, node)
    return node
  }

  getCellById(id: string): MockNode | undefined {
    return this._nodes.get(id)
  }

  getNodes(): MockNode[] {
    return Array.from(this._nodes.values())
  }

  on(event: string, callback: (args: unknown) => void): void {
    this._listeners.push({ event, callback })
  }

  off(event: string, callback: (args: unknown) => void): void {
    this._listeners = this._listeners.filter(
      (l) => l.event !== event || l.callback !== callback,
    )
  }

  /** Test helper: emit an event to all registered listeners. */
  emit(event: string, args: unknown): void {
    for (const listener of this._listeners) {
      if (listener.event === event) {
        listener.callback(args)
      }
    }
  }
}

// ─── Data factories ─────────────────────────────────────────────────────────

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

function createScriptData(overrides: Partial<ScriptStepData> = {}): ScriptStepData {
  return {
    stepId: 'step-001',
    scriptName: 'initialize',
    scriptVersion: '1.0.0',
    params: {},
    preconditions: [],
    resources: [],
    timeout: 30000,
    onFail: 'stop',
    exportOutputs: false,
    status: 'idle',
    ...overrides,
  }
}

// ─── Tests ──────────────────────────────────────────────────────────────────

describe('LoopContainerNode integration with SubGraphContainer API', () => {
  it('test_sub_graph_sync', () => {
    // Verify the parent-child API contract that SubGraphContainer.vue relies on:
    //   containerNode.getChildren() returns child nodes
    //   childNode.getParent() returns the container node
    //
    // SubGraphContainer.populateSubGraph() calls containerNode.getChildren()
    // and iterates the result. syncBackToMainGraph() calls newNode.setParent()
    // and containerNode.addChild() to establish the relationship.
    const graph = new MockGraph()

    const loopData = createLoopData()
    const loopContainer = graph.addNode({
      id: 'loop-1',
      shape: 'loop-container-node',
      x: 0,
      y: 0,
      width: 208,
      height: 120,
      data: loopData,
    })

    const childData = createScriptData({ stepId: 'step-child-1', scriptName: 'measure' })
    const child = graph.addNode({
      id: 'step-1',
      shape: 'script-step-node',
      x: 20,
      y: 20,
      width: 160,
      height: 64,
      data: childData,
    })

    // Establish parent-child relationship — mirrors SubGraphContainer.syncBackToMainGraph()
    // which calls both newNode.setParent(containerNode) and containerNode.addChild(newNode).
    child.setParent(loopContainer)
    loopContainer.addChild(child)

    // Verify bidirectional sync: parent knows its children, child knows its parent
    const children = loopContainer.getChildren()
    expect(children).toBeDefined()
    expect(children.length).toBe(1)
    expect(children[0]).toBe(child)

    const parent = child.getParent()
    expect(parent).toBe(loopContainer)

    // Verify graph retrieves both nodes by ID — SubGraphContainer uses
    // mainGraph.getCellById(props.containerNodeId) to find the container.
    expect(graph.getCellById('loop-1')).toBe(loopContainer)
    expect(graph.getCellById('step-1')).toBe(child)

    // Verify data round-trips — SubGraphContainer reads childNode.getData()
    // to determine shape (loop-container-node vs script-step-node) and label.
    const retrievedData = graph.getCellById('step-1')!.getData()
    expect(retrievedData).toMatchObject({
      stepId: 'step-child-1',
      scriptName: 'measure',
    })

    // Adding a second child updates the parent's children array
    const child2 = graph.addNode({
      id: 'step-2',
      shape: 'script-step-node',
      x: 20,
      y: 100,
      data: createScriptData({ stepId: 'step-child-2', scriptName: 'validate' }),
    })
    child2.setParent(loopContainer)
    loopContainer.addChild(child2)

    expect(loopContainer.getChildren().length).toBe(2)
    expect(loopContainer.getChildren()).toContain(child)
    expect(loopContainer.getChildren()).toContain(child2)
  })

  it('test_collapse_expand_integration', async () => {
    // Mount LoopContainerNode with a mock graph containing 2 child cells.
    // Click the collapse toggle — children should hide (isVisible() === false).
    // Click again — children should show (isVisible() === true).
    //
    // The reactive() data bridge ensures the isCollapsed computed updates
    // after setData, so the second click correctly toggles to expand.
    const graph = new MockGraph()

    const loopData = createLoopData()
    const loopContainer = graph.addNode({
      id: 'loop-1',
      x: 0,
      y: 0,
      width: 208,
      height: 120,
      data: loopData,
    })

    const child1 = graph.addNode({
      id: 'child-1',
      x: 20,
      y: 20,
      width: 160,
      height: 64,
      data: createScriptData({ stepId: 'c1', scriptName: 'step1' }),
    })
    const child2 = graph.addNode({
      id: 'child-2',
      x: 20,
      y: 100,
      width: 160,
      height: 64,
      data: createScriptData({ stepId: 'c2', scriptName: 'step2' }),
    })

    child1.setParent(loopContainer)
    loopContainer.addChild(child1)
    child2.setParent(loopContainer)
    loopContainer.addChild(child2)

    // Both children visible initially
    expect(child1.isVisible()).toBe(true)
    expect(child2.isVisible()).toBe(true)

    // Mount LoopContainerNode without `data` prop — the component falls back
    // to node.getData() (reactive mock data), simulating real x6-vue-shape
    // rendering where data is not passed as a prop.
    const wrapper = mount(LoopContainerNode, {
      props: {
        node: loopContainer as unknown as Node,
      },
    })

    // Click collapse toggle — children should hide
    await wrapper.find('[data-testid="collapse-toggle"]').trigger('click')

    expect(child1.isVisible()).toBe(false)
    expect(child2.isVisible()).toBe(false)

    // Click expand toggle — children should show
    // The reactive data bridge ensures isCollapsed updates after setData,
    // so toggleCollapse correctly computes next = !true = false → show().
    await wrapper.find('[data-testid="collapse-toggle"]').trigger('click')

    expect(child1.isVisible()).toBe(true)
    expect(child2.isVisible()).toBe(true)

    wrapper.unmount()
  })

  it('test_child_addition_auto_expand', async () => {
    // Mount LoopContainerNode at default size 208x120. Add a child at
    // position (300, 200) — outside parent bounds. Trigger node:change:position.
    // Parent should resize to contain the child (width > 208 or height > 120).
    //
    // The handler's identity check (changedNode.getParent() !== toRaw(props.node))
    // works because setParent stores the raw MockNode reference, and toRaw
    // unwraps Vue's proxy to the same raw object.
    const graph = new MockGraph()

    const loopData = createLoopData()
    const loopContainer = graph.addNode({
      id: 'loop-1',
      x: 0,
      y: 0,
      width: 208,
      height: 120,
      data: loopData,
    })

    // Mount the component — onMounted registers node:change:position handler
    // via props.node.model.graph.on(...).
    const wrapper = mount(LoopContainerNode, {
      props: {
        node: loopContainer as unknown as Node,
      },
    })

    // Initial size is 208x120 (minimum)
    const initialSize = loopContainer.getSize()
    expect(initialSize.width).toBe(208)
    expect(initialSize.height).toBe(120)

    // Add a child at (300, 200) with size 300x200 — well outside parent bounds
    // and large enough to force expansion beyond 208x120 in both dimensions.
    const child = graph.addNode({
      id: 'child-far',
      x: 300,
      y: 200,
      width: 300,
      height: 200,
      data: createScriptData({ stepId: 'far', scriptName: 'remote' }),
    })
    child.setParent(loopContainer)
    loopContainer.addChild(child)

    // Trigger node:change:position — the handler recomputes the bounding box
    // of all children and resizes the parent.
    // Bounding box: minX=300, minY=200, maxX=600, maxY=400
    // Computed size: 300x200 → resize(max(208,300), max(120,200)) = resize(300, 200)
    graph.emit('node:change:position', { node: child })

    const newSize = loopContainer.getSize()
    expect(newSize.width).toBeGreaterThan(208)
    expect(newSize.height).toBeGreaterThan(120)
    expect(newSize.width).toBe(300)
    expect(newSize.height).toBe(200)

    wrapper.unmount()
  })
})
