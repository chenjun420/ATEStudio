<script setup lang="ts">
/**
 * LoopContainerNode - Container node for loop constructs in X6 graph
 *
 * Renders loop type (for/while/foreach), condition expression, and
 * execution mode badge (serial/parallel). Visually distinct from
 * ScriptStepNode via indigo/purple color scheme to signal container
 * semantics rather than a single executable step.
 *
 * A chevron toggle in the header collapses/expands child cells via
 * cell.hide()/cell.show() — children are NOT removed from the graph,
 * only their visibility is toggled. The collapsed flag persists in
 * the node's data so it survives serialization.
 *
 * When execution is active (status != idle), an iteration progress
 * bar shows current/total iterations. For parallel mode, concurrent
 * execution slot indicators reflect maxConcurrency capacity.
 */

import { computed, onMounted, onUnmounted, toRaw } from 'vue'
import type { Node } from '@antv/x6'
import type { LoopContainerData } from '@/models/nodes/types'

/** Extended data shape — collapse state and runtime iteration progress
 *  live alongside LoopContainerData fields. */
type LoopContainerNodeData = LoopContainerData & {
  collapsed?: boolean
  currentIteration?: number
}

interface Props {
  /** Loop data. When rendered by x6-vue-shape, data is not passed as a prop;
   *  the component falls back to node.getData() to retrieve it. */
  data?: LoopContainerData
  node: Node
}

const props = defineProps<Props>()

// When rendered by x6-vue-shape, `data` is not passed as a prop.
// Fall back to node.getData() to retrieve loop data from the X6 node.
const nodeData = computed<LoopContainerNodeData | undefined>(
  () => props.data ?? (props.node?.getData() as LoopContainerNodeData | undefined),
)

// Runtime-safe access — x6 may pass undefined data before node init completes.
const loopType = computed(() => nodeData.value?.loopType ?? 'for')
const condition = computed(() => nodeData.value?.condition ?? '')
const executionMode = computed(() => nodeData.value?.executionMode ?? 'serial')
const status = computed(() => nodeData.value?.status ?? 'idle')
const count = computed(() => nodeData.value?.count ?? 0)
const currentIteration = computed(() => nodeData.value?.currentIteration ?? 0)
const maxConcurrency = computed(() => nodeData.value?.maxConcurrency ?? 1)

const hasCondition = computed(() => condition.value.trim().length > 0)
const isParallel = computed(() => executionMode.value === 'parallel')

// Collapse state — defaults to expanded (false) when not yet stored on the node.
const isCollapsed = computed(() => nodeData.value?.collapsed ?? false)

// Status dot color — mirrors ScriptStepNode semantic mapping.
const statusDotClass = computed(() => {
  switch (status.value) {
    case 'running':
      return 'tw-bg-info'
    case 'passed':
      return 'tw-bg-success'
    case 'failed':
      return 'tw-bg-error'
    case 'error':
      return 'tw-bg-warning'
    default:
      return 'tw-bg-neutral-400'
  }
})

/**
 * Status → border color mapping.
 * Mirrors GraphContainer.vue's STATUS_VISUAL_MAP (line 54) — duplicated
 * locally as Tailwind classes to avoid coupling to GraphContainer's
 * X6-attrs-oriented { stroke, fill, strokeWidth } shape.
 */
const STATUS_BORDER_MAP: Record<string, string> = {
  idle: 'tw-border-neutral-300',
  running: 'tw-border-info',
  passed: 'tw-border-success',
  failed: 'tw-border-error',
  error: 'tw-border-warning',
  skipped: 'tw-border-neutral-400',
}

const statusBorderClass = computed(
  () => STATUS_BORDER_MAP[status.value] ?? STATUS_BORDER_MAP.idle,
)

// Iteration progress — visible when execution is active (status != idle) and
// a total iteration count is configured. currentIteration defaults to 0 when
// not yet stored (e.g., loop just started but no iteration completed).
const hasIterationProgress = computed(
  () => status.value !== 'idle' && count.value > 0,
)
const iterationPercent = computed(() => {
  if (count.value === 0) return 0
  return Math.min(100, Math.round((currentIteration.value / count.value) * 100))
})

// Parallel execution slots — one indicator per maxConcurrency slot.
// Active slots reflect iterations currently in flight (up to capacity).
const parallelSlots = computed<boolean[]>(() => {
  const active = Math.min(currentIteration.value, maxConcurrency.value)
  return Array.from({ length: maxConcurrency.value }, (_, i) => i < active)
})

/**
 * Toggle collapse/expand of all child cells.
 *
 * Iterates node.getChildren() and calls cell.hide() when collapsing or
 * cell.show() when expanding. Children remain in the graph — only their
 * visibility changes. The new collapsed flag is persisted via node.setData()
 * so it propagates through the nodeData computed and survives serialization.
 */
function toggleCollapse(): void {
  const next = !isCollapsed.value
  const children = props.node.getChildren()
  if (children) {
    for (const child of children) {
      if (next) {
        child.hide()
      } else {
        child.show()
      }
    }
  }
  props.node.setData({ ...nodeData.value, collapsed: next })
}

/** Minimum container dimensions — never shrink below this when recalculating bounds. */
const MIN_WIDTH = 208
const MIN_HEIGHT = 120

/**
 * Recalculate parent bounds when a child node's position changes.
 *
 * Triggered by graph `node:change:position` events. When a child of this
 * loop container moves, recomputes the bounding box of all children and
 * resizes the parent to contain them. No-op when collapsed (children are
 * hidden, positions stale) or when the changed node is not a child of
 * this container.
 *
 * The parent is never shrunk below MIN_WIDTH x MIN_HEIGHT (208x120).
 * Children are NOT moved — only the parent's size changes.
 *
 * Uses `toRaw(props.node)` for the parent identity check because Vue
 * wraps object props in a reactive proxy — the raw X6 Cell returned by
 * `getParent()` is never `===` to the proxied `props.node`.
 */
function onChildPositionChange({ node: changedNode }: { node: Node }): void {
  // Skip when collapsed — children are hidden, their positions are stale.
  if (isCollapsed.value) return

  // Only react to children of THIS loop container. toRaw() unwraps Vue's
  // reactive proxy on props.node so the reference matches the raw Cell
  // returned by getParent().
  if (changedNode.getParent() !== toRaw(props.node)) return

  const children = props.node.getChildren()
  if (!children || children.length === 0) return

  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity

  for (const child of children) {
    if (!child.isNode()) continue
    const pos = child.getPosition()
    const size = child.getSize()
    minX = Math.min(minX, pos.x)
    minY = Math.min(minY, pos.y)
    maxX = Math.max(maxX, pos.x + size.width)
    maxY = Math.max(maxY, pos.y + size.height)
  }

  // No valid child nodes found — keep current size.
  if (minX === Infinity) return

  const computedWidth = maxX - minX
  const computedHeight = maxY - minY

  props.node.resize(
    Math.max(MIN_WIDTH, computedWidth),
    Math.max(MIN_HEIGHT, computedHeight),
  )
}

onMounted(() => {
  const graph = props.node.model?.graph
  if (!graph) return
  graph.on('node:change:position', onChildPositionChange)
})

onUnmounted(() => {
  const graph = props.node.model?.graph
  if (!graph) return
  graph.off('node:change:position', onChildPositionChange)
})
</script>

<template>
  <div
    class="loop-container-node tw-flex tw-flex-col tw-w-52 tw-rounded-xl tw-border-2 tw-bg-white tw-shadow-sm tw-overflow-hidden tw-font-sans"
    :class="statusBorderClass"
  >
    <!-- Header: collapse toggle + loop type label + status dot -->
    <div
      class="tw-flex tw-items-center tw-justify-between tw-px-3 tw-py-2 tw-bg-primary-50 tw-border-b tw-border-primary-200"
    >
      <div class="tw-flex tw-items-center tw-gap-2">
        <!-- Collapse/expand chevron — toggles child cell visibility -->
        <button
          type="button"
          data-testid="collapse-toggle"
          class="tw-flex tw-items-center tw-justify-center tw-w-4 tw-h-4 tw-text-primary-600 hover:tw-text-primary-800 tw-cursor-pointer tw-transition-colors tw-bg-transparent tw-border-none tw-p-0"
          :aria-label="isCollapsed ? 'Expand loop children' : 'Collapse loop children'"
          :aria-expanded="!isCollapsed"
          @click="toggleCollapse"
        >
          <svg
            class="tw-w-3 tw-h-3 tw-transition-transform"
            :class="isCollapsed ? '' : 'tw-rotate-90'"
            viewBox="0 0 12 12"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d="M4 2 L8 6 L4 10" />
          </svg>
        </button>
        <span
          class="tw-inline-block tw-w-2 tw-h-2 tw-rounded-full"
          :class="statusDotClass"
        ></span>
        <span class="tw-text-sm tw-font-semibold tw-text-primary-700">{{ loopType }}</span>
      </div>
      <!-- Execution mode badge: serial | parallel -->
      <span
        class="tw-text-xs tw-font-medium tw-px-2 tw-py-0.5 tw-rounded-full"
        :class="
          isParallel
            ? 'tw-bg-accent-blue tw-text-white'
            : 'tw-bg-neutral-200 tw-text-neutral-700'
        "
      >{{ executionMode }}</span>
    </div>

    <!-- Body: condition expression + iteration controls -->
    <div class="tw-px-3 tw-py-2">
      <div
        v-if="hasCondition"
        class="tw-text-xs tw-text-neutral-600 tw-font-mono tw-break-all"
      >{{ condition }}</div>
      <div v-else class="tw-text-xs tw-text-neutral-400 tw-italic">No condition set</div>

      <!-- Iteration progress — visible when execution is active and count is set -->
      <div
        v-if="hasIterationProgress"
        class="tw-mt-2 tw-space-y-1"
        data-testid="iteration-progress"
      >
        <div class="tw-flex tw-items-center tw-justify-between tw-text-xs tw-text-neutral-600">
          <span>Iteration {{ currentIteration }}/{{ count }}</span>
          <span>{{ iterationPercent }}%</span>
        </div>
        <div class="tw-w-full tw-h-1.5 tw-bg-neutral-200 tw-rounded-full tw-overflow-hidden">
          <div
            class="tw-h-full tw-bg-primary-500 tw-rounded-full tw-transition-all"
            :style="{ width: `${iterationPercent}%` }"
            data-testid="iteration-progress-bar"
          ></div>
        </div>
      </div>

      <!-- Parallel execution slots — visible when executionMode is 'parallel' -->
      <div
        v-if="isParallel"
        class="tw-mt-2 tw-flex tw-items-center tw-gap-1"
        data-testid="parallel-slots"
      >
        <span class="tw-text-xs tw-text-neutral-500 tw-mr-1">Slots:</span>
        <span
          v-for="(active, i) in parallelSlots"
          :key="i"
          class="tw-inline-block tw-w-2 tw-h-2 tw-rounded-full"
          :class="active ? 'tw-bg-primary-500' : 'tw-bg-neutral-300'"
          :data-slot-index="i"
          :data-slot-active="active ? 'true' : 'false'"
        ></span>
      </div>
    </div>
  </div>
</template>
