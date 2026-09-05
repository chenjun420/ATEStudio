<script setup lang="ts">
/**
 * Knowledge-graph browse view (task 25).
 *
 * Renders the knowledge graph returned by `GET /api/v1/knowledge/graph`
 * ({ nodes: [{id,label,type,name,properties}], edges: [{source,target,type}] }).
 *
 * Rendering is a dependency-free SVG layout (no React / no Semantica Explorer /
 * no added npm package): nodes are placed deterministically on a circle and
 * edges drawn as directed lines, colored by node type. A graph backend that is
 * absent/down makes the backend answer 503, which this view renders as a
 * friendly empty state (graceful degradation) rather than an error crash.
 *
 * API: /api/v1/knowledge/graph (see frontend/src/api/knowledge.ts).
 */
import { computed, onMounted, ref } from 'vue'
import {
  ElCard,
  ElButton,
  ElInput,
  ElInputNumber,
  ElTag,
  ElAlert,
  ElEmpty,
  ElSkeleton,
} from 'element-plus'
import { fetchKnowledgeGraph, type GraphBrowse, type GraphNode } from '@/api/knowledge'

// ─── State ───────────────────────────────────────────────────────────────────

const graph = ref<GraphBrowse>({ nodes: [], edges: [] })
const loading = ref(false)
/** True when the graph backend answered 503 (unavailable) — friendly empty state. */
const unavailable = ref(false)
const error = ref<string | null>(null)

// Filters (forwarded as ?limit= / ?label=).
const limit = ref(100)
const labelFilter = ref('')

// ─── Layout (deterministic circle, no external graph library) ────────────────

/** SVG viewBox dimensions — the graph scales responsively to its container. */
const VIEW_W = 900
const VIEW_H = 620
const CENTER_X = VIEW_W / 2
const CENTER_Y = VIEW_H / 2
const RING_RADIUS = Math.min(VIEW_W, VIEW_H) / 2 - 90
const NODE_RADIUS = 16

interface PositionedNode extends GraphNode {
  x: number
  y: number
}

/** Nodes placed evenly around a ring centered in the viewBox. */
const positionedNodes = computed<PositionedNode[]>(() => {
  const nodes = graph.value.nodes
  const count = nodes.length
  if (count === 0) return []
  return nodes.map((node, i) => {
    // Single node sits at the center; two+ spread around the ring.
    const angle = count === 1 ? -Math.PI / 2 : (i / count) * Math.PI * 2 - Math.PI / 2
    return {
      ...node,
      x: CENTER_X + RING_RADIUS * Math.cos(angle),
      y: CENTER_Y + RING_RADIUS * Math.sin(angle),
    }
  })
})

/** Fast id → positioned-node lookup for resolving edge endpoints. */
const nodeById = computed<Map<string, PositionedNode>>(() => {
  const map = new Map<string, PositionedNode>()
  for (const n of positionedNodes.value) map.set(n.id, n)
  return map
})

interface PositionedEdge {
  key: string
  x1: number
  y1: number
  x2: number
  y2: number
  type: string
}

/** Edges whose both endpoints exist in the current node set (others dropped). */
const positionedEdges = computed<PositionedEdge[]>(() => {
  const out: PositionedEdge[] = []
  graph.value.edges.forEach((edge, i) => {
    const src = nodeById.value.get(edge.source)
    const dst = nodeById.value.get(edge.target)
    if (!src || !dst) return
    out.push({
      key: `${edge.source}->${edge.target}-${i}`,
      x1: src.x,
      y1: src.y,
      x2: dst.x,
      y2: dst.y,
      type: edge.type,
    })
  })
  return out
})

/** Distinct node types (for the legend + stable color assignment). */
const nodeTypes = computed<string[]>(() => {
  const seen = new Set<string>()
  for (const n of graph.value.nodes) seen.add(n.type || n.label || 'node')
  return Array.from(seen)
})

/** A stable, accessible fill color per node type (cycled from a fixed palette). */
const TYPE_COLORS = ['#409eff', '#67c23a', '#e6a23c', '#f56c6c', '#909399', '#8b5cf6', '#06b6d4']

function typeColor(type: string): string {
  const idx = nodeTypes.value.indexOf(type)
  return TYPE_COLORS[(idx < 0 ? 0 : idx) % TYPE_COLORS.length]
}

function nodeColor(node: GraphNode): string {
  return typeColor(node.type || node.label || 'node')
}

/** Short display label for a node (name > label > id tail). */
function nodeLabel(node: GraphNode): string {
  if (node.name) return node.name
  if (node.label) return node.label
  return node.id.length > 12 ? node.id.slice(-8) : node.id
}

// ─── Data fetching ───────────────────────────────────────────────────────────

/** Extract a human-readable message from an axios error (FastAPI detail). */
function extractError(e: unknown, fallback: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } | undefined } | null)?.response
    ?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  if (e instanceof Error && e.message) return e.message
  return fallback
}

function httpStatus(e: unknown): number | undefined {
  return (e as { response?: { status?: number } } | null)?.response?.status
}

async function loadGraph(): Promise<void> {
  loading.value = true
  error.value = null
  unavailable.value = false
  try {
    const data = await fetchKnowledgeGraph({
      limit: limit.value,
      label: labelFilter.value.trim() || undefined,
    })
    graph.value = data
  } catch (e: unknown) {
    graph.value = { nodes: [], edges: [] }
    if (httpStatus(e) === 503) {
      // Graph backend absent/down — graceful empty state, not a hard error.
      unavailable.value = true
    } else {
      error.value = extractError(e, 'Failed to load knowledge graph.')
    }
  } finally {
    loading.value = false
  }
}

function applyFilters(): void {
  void loadGraph()
}

function resetFilters(): void {
  labelFilter.value = ''
  limit.value = 100
  void loadGraph()
}

onMounted(() => {
  void loadGraph()
})
</script>

<template>
  <div class="kg-panel">
    <!-- ─── Header ─── -->
    <header class="kg-header">
      <div class="kg-header-left">
        <h1 class="kg-title">Knowledge Graph</h1>
        <ElTag type="info" size="small" data-testid="count-nodes">
          Nodes: {{ graph.nodes.length }}
        </ElTag>
        <ElTag type="info" size="small" data-testid="count-edges">
          Edges: {{ graph.edges.length }}
        </ElTag>
      </div>
      <div class="kg-header-right">
        <ElButton size="small" @click="loadGraph" data-testid="btn-reload">Reload</ElButton>
      </div>
    </header>

    <!-- ─── Filters ─── -->
    <ElCard class="kg-filter-card" shadow="never">
      <div class="kg-filter-row">
        <ElInput
          v-model="labelFilter"
          placeholder="Filter by node label/type"
          clearable
          size="small"
          class="kg-filter-input"
          data-testid="filter-label"
          @keyup.enter="applyFilters"
        />
        <ElInputNumber
          v-model="limit"
          :min="1"
          :max="500"
          :step="50"
          :precision="0"
          controls-position="right"
          size="small"
          data-testid="filter-limit"
        />
        <ElButton size="small" type="primary" @click="applyFilters" data-testid="btn-filter">
          Search
        </ElButton>
        <ElButton size="small" @click="resetFilters" data-testid="btn-filter-reset">
          Reset
        </ElButton>
      </div>
    </ElCard>

    <!-- ─── Error banner (non-503 failures) ─── -->
    <ElAlert
      v-if="error"
      data-testid="error-alert"
      title="Failed to load knowledge graph"
      :description="error"
      type="error"
      :closable="false"
      show-icon
    />

    <!-- ─── Graph canvas ─── -->
    <ElCard class="kg-canvas-card" shadow="never" data-testid="canvas-card">
      <ElSkeleton v-if="loading" :rows="6" animated data-testid="graph-skeleton" />

      <!-- Graph backend unavailable (503) — friendly empty state. -->
      <ElEmpty
        v-else-if="unavailable"
        data-testid="graph-unavailable"
        description="Knowledge graph is unavailable. The graph backend may be offline or not configured."
      />

      <!-- Valid response but no nodes. -->
      <ElEmpty
        v-else-if="graph.nodes.length === 0"
        data-testid="graph-empty"
        description="No graph nodes match the current filters."
      />

      <!-- ─── SVG graph ─── -->
      <div v-else class="kg-svg-wrap" data-testid="graph-svg-wrap">
        <!-- Legend -->
        <div class="kg-legend" data-testid="graph-legend">
          <span
            v-for="t in nodeTypes"
            :key="t"
            class="kg-legend-item"
            :data-testid="`legend-${t}`"
          >
            <span class="kg-legend-dot" :style="{ backgroundColor: typeColor(t) }" />
            {{ t }}
          </span>
        </div>

        <svg
          :viewBox="`0 0 ${VIEW_W} ${VIEW_H}`"
          class="kg-svg"
          role="img"
          aria-label="Knowledge graph"
          data-testid="graph-svg"
        >
          <defs>
            <marker
              id="kg-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#909399" />
            </marker>
          </defs>

          <!-- Edges -->
          <g class="kg-edges" data-testid="graph-edges">
            <line
              v-for="e in positionedEdges"
              :key="e.key"
              :x1="e.x1"
              :y1="e.y1"
              :x2="e.x2"
              :y2="e.y2"
              class="kg-edge"
              marker-end="url(#kg-arrow)"
            >
              <title>{{ e.type }}</title>
            </line>
          </g>

          <!-- Nodes -->
          <g class="kg-nodes" data-testid="graph-nodes">
            <g
              v-for="n in positionedNodes"
              :key="n.id"
              class="kg-node"
              :data-testid="`node-${n.id}`"
            >
              <circle :cx="n.x" :cy="n.y" :r="NODE_RADIUS" :fill="nodeColor(n)">
                <title>{{ n.id }} — {{ n.label }}</title>
              </circle>
              <text :x="n.x" :y="n.y + NODE_RADIUS + 14" class="kg-node-label" text-anchor="middle">
                {{ nodeLabel(n) }}
              </text>
            </g>
          </g>
        </svg>
      </div>
    </ElCard>
  </div>
</template>

<style scoped>
.kg-panel {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-md) var(--spacing-lg);
  min-height: 100vh;
  background-color: var(--color-bg-secondary);
}

.kg-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.kg-header-left {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.kg-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin: 0;
}

.kg-filter-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
}

.kg-filter-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.kg-filter-input {
  width: 240px;
}

.kg-canvas-card {
  background-color: var(--color-bg-primary);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-xl);
}

.kg-svg-wrap {
  position: relative;
  width: 100%;
}

.kg-svg {
  width: 100%;
  height: auto;
  display: block;
}

.kg-edge {
  stroke: #909399;
  stroke-width: 1.5;
  opacity: 0.55;
}

.kg-node-label {
  font-size: 12px;
  fill: var(--color-text-secondary, #606266);
  pointer-events: none;
}

.kg-legend {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
  margin-bottom: var(--spacing-sm);
}

.kg-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 0.75rem;
  color: var(--color-text-secondary, #606266);
}

.kg-legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}

@media (max-width: 768px) {
  .kg-panel {
    padding: var(--spacing-sm);
  }

  .kg-filter-input {
    width: 100%;
  }
}
</style>
