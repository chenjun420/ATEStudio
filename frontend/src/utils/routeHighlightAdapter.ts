/**
 * T32 (v41-gap-analysis #32) — thin X6 adapter for the pure route highlight.
 *
 * Reads nothing from the topology model: the pure `computeRouteHighlight`
 * decides WHICH cells get accented/dimmed; this module only writes X6 attrs
 * (`line/stroke`, `line/strokeWidth`, `line/strokeDasharray`, `line/opacity`,
 * `body/opacity`). View-only by construction — no model mutation, and faulted
 * links never appear in the result sets so runtime fault styling wins.
 *
 * Coordinate/style note: base link styles (LINK_STYLES) moved here from
 * FixtureDesigner.vue so accent/dim/clear restore from ONE source of truth;
 * the view imports the same table for initial rendering. Relay contacts are
 * not canvas cells today — `getCellById` misses are silently skipped, so the
 * same code keeps working if relays ever become nodes.
 */
import type { Cell, Graph } from '@antv/x6'

import {
  computeRouteHighlight,
  pickActiveRouteId,
  type HighlightLinkInput,
  type HighlightRelayInput,
  type RouteHighlightResult,
} from './routeHighlight'
import type { RouteLike } from './routes'

/** 链路基础样式（按 signal_type）——自 FixtureDesigner.vue 迁出，单一事实来源。 */
export const LINK_STYLES: Record<string, { stroke: string; strokeWidth: number; dash?: string }> = {
  power: { stroke: '#E6A23C', strokeWidth: 3 },
  signal: { stroke: '#409EFF', strokeWidth: 2 },
  ground: { stroke: '#606266', strokeWidth: 2 },
  rf: { stroke: '#9B59B6', strokeWidth: 2, dash: '4 2' },
  thermal: { stroke: '#F56C6C', strokeWidth: 2 },
  air: { stroke: '#67C23A', strokeWidth: 2, dash: '6 3' },
}

/** 路径强调色（沿用 T28 激活路由的绿色视觉语言，与故障红 #F56C6C 区分）。 */
export const ACCENT_COLOR = '#67C23A'
/** 强调描边在基础宽度上的加粗量。 */
const ACCENT_WIDTH_BOOST = 2
/** 非路径元素压暗不透明度。 */
export const DIM_OPACITY = 0.25

function baseStyleOf(cell: Cell) {
  const d = (cell.getData() ?? {}) as { signalType?: string }
  return LINK_STYLES[d.signalType ?? 'signal'] ?? LINK_STYLES.signal
}

function restoreEdge(edge: Cell): void {
  const base = baseStyleOf(edge)
  edge.attr('line/stroke', base.stroke)
  edge.attr('line/strokeWidth', base.strokeWidth)
  edge.attr('line/strokeDasharray', base.dash ?? '')
  edge.attr('line/opacity', 1)
}

/**
 * 将高亮结果写入画布：路径边强调描边，其余链路压暗；
 * 继电器触点当前不是画布单元 → getCellById 未命中即静默跳过。
 */
export function applyRouteHighlight(g: Graph, hl: RouteHighlightResult): void {
  for (const id of hl.accentedLinkIds) {
    const cell = g.getCellById(id)
    if (!cell || !cell.isEdge()) continue
    const base = baseStyleOf(cell)
    cell.attr('line/stroke', ACCENT_COLOR)
    cell.attr('line/strokeWidth', base.strokeWidth + ACCENT_WIDTH_BOOST)
    cell.attr('line/strokeDasharray', '')
    cell.attr('line/opacity', 1)
  }
  for (const id of hl.dimmedNodeIds) {
    const cell = g.getCellById(id)
    if (!cell) continue
    if (cell.isEdge()) {
      restoreEdge(cell)
      cell.attr('line/opacity', DIM_OPACITY)
    } else if (cell.isNode()) {
      cell.attr('body/opacity', DIM_OPACITY)
    }
  }
}

/** 清除高亮：恢复给定链路/继电器 id 的基础样式与完全不透明。 */
export function clearRouteHighlight(g: Graph, ids: Iterable<string>): void {
  for (const id of ids) {
    const cell = g.getCellById(id)
    if (!cell) continue
    if (cell.isEdge()) restoreEdge(cell)
    else if (cell.isNode()) cell.attr('body/opacity', 1)
  }
}

/**
 * 便捷入口：由 routes 自行推导选中路由（唯一 active 者），有则应用高亮，
 * 无则整体清除。视图侧一行调用即可完成 selection→compute→apply/clear。
 */
export function syncRouteHighlight(
  g: Graph,
  input: { routes: readonly RouteLike[]; links: readonly HighlightLinkInput[]; relays: readonly HighlightRelayInput[] },
): void {
  const selectedRouteId = pickActiveRouteId(input.routes)
  if (!selectedRouteId) {
    clearRouteHighlight(g, [...input.links.map((l) => l.id), ...input.relays.map((r) => r.id)])
    return
  }
  applyRouteHighlight(
    g,
    computeRouteHighlight({ ...input, selectedRouteId }),
  )
}
