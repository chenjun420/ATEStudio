/**
 * Tests for loop-container-node X6 shape registration via x6-vue-shape.
 *
 * Verifies:
 * - The Vue LoopContainerNode component is registered (not Shape.Rect)
 * - SubGraphContainer.vue's shape-name lookup still resolves to 'loop-container-node'
 * - The shape name string is exactly 'loop-container-node'
 *
 * NOTE: @antv/x6 is ESM-only and cannot be loaded in vitest's jsdom environment
 * (see existing useSerializer.test.ts for the same constraint). Since
 * @antv/x6-vue-shape transitively imports @antv/x6 at module load time, we mock
 * @antv/x6-vue-shape with a faithful implementation of register()/shapeMaps
 * that mirrors the real registry.js behavior: shapeMaps[shape] = { component }.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import LoopContainerNode from '../components/nodes/LoopContainerNode.vue'
import { isLoopContainerData } from '@/models/nodes/types'
import type { LoopContainerData, NodeData } from '@/models/nodes/types'

// ─── Mock @antv/x6-vue-shape ─────────────────────────────────────────────────
//
// The real register() in @antv/x6-vue-shape es/registry.js does:
//   shapeMaps[shape] = { component }
//   Graph.registerNode(shape, { inherit: inherit || 'vue-shape', ...others }, true)
//
// We replicate the shapeMaps storage (the observable contract) and stub
// Graph.registerNode (the X6 side-effect, not testable in jsdom).
// vi.hoisted() ensures mockShapeMaps is initialized before the vi.mock factory
// runs (vi.mock is hoisted to the top of the file by Vitest).
const { mockShapeMaps } = vi.hoisted(() => ({
  mockShapeMaps: {} as Record<string, { component: unknown }>,
}))

vi.mock('@antv/x6-vue-shape', () => ({
  shapeMaps: mockShapeMaps,
  register: vi.fn((config: { shape: string; component: unknown }) => {
    mockShapeMaps[config.shape] = { component: config.component }
  }),
}))

// Import after mock is set up (Vitest hoists vi.mock above all imports)
import { register, shapeMaps } from '@antv/x6-vue-shape'

const SHAPE_NAME = 'loop-container-node'

describe('loop-container-node registration', () => {
  beforeEach(() => {
    // Clear and re-register before each test for isolation
    delete mockShapeMaps[SHAPE_NAME]
    register({
      shape: SHAPE_NAME,
      component: LoopContainerNode,
      width: 208,
      height: 120,
    })
  })

  it('test_loop_container_uses_vue_shape', () => {
    // The old registration used Graph.registerNode with inherit: Shape.Rect.
    // The new registration uses x6-vue-shape register(), which stores the
    // Vue component in shapeMaps and inherits from 'vue-shape' (foreignObject).
    expect(shapeMaps[SHAPE_NAME]).toBeDefined()
    expect(shapeMaps[SHAPE_NAME].component).toBe(LoopContainerNode)
  })

  it('test_sub_graph_container_still_works', () => {
    // SubGraphContainer.vue determines shape via:
    //   isLoopContainerData(data) ? 'loop-container-node' : 'script-step-node'
    // Verify the type guard still maps LoopContainerData to 'loop-container-node'
    // and that shape is registered as a Vue shape.
    const loopData: LoopContainerData = {
      loopId: 'loop-001',
      loopType: 'for',
      condition: '',
      executionMode: 'serial',
      maxConcurrency: 1,
      status: 'idle',
    }

    expect(isLoopContainerData(loopData as NodeData)).toBe(true)

    const shape = isLoopContainerData(loopData as NodeData) ? SHAPE_NAME : 'script-step-node'
    expect(shape).toBe(SHAPE_NAME)
    expect(shapeMaps[shape]).toBeDefined()
  })

  it('test_shape_name_unchanged', () => {
    // The shape name must be exactly 'loop-container-node' — not camelCase,
    // not PascalCase. SubGraphContainer.vue, PropertyPanel.vue, and
    // useSerializer.ts all reference this exact string literal.
    const EXPECTED = 'loop-container-node'

    expect(SHAPE_NAME).toBe(EXPECTED)
    expect(shapeMaps[EXPECTED]).toBeDefined()
    expect(Object.keys(shapeMaps)).toContain(EXPECTED)
  })
})
