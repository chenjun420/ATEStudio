/**
 * T35（v41-gap-analysis #35）— thin X6 adapter for the history heatmap.
 *
 * 纯层 utils/faultHeatmap.ts 决定「哪条链路多热、什么颜色」；本模块只负责
 * 把补丁写进 X6（逐键 cell.attr(k, v)）与 toggle off 后的基础样式还原。
 * 还原复用 routeHighlightAdapter.clearRouteHighlight——单一恢复事实来源
 * （与 faultVisualsAdapter 同一模式）。
 *
 * 与实时故障样式（T34）的关系：热力层只在 toggle 时写入一次；SSE fault
 * 重绘（syncFaultVisuals）随后到达时按 attr 覆盖——故障红优先，符合 T32
 * 「fault wins visually」约定。本模块不订阅任何响应式源。
 */
import type { Graph } from '@antv/x6'

import { applyHeatmap, type CellKind, type FaultStatsPayload } from './faultHeatmap'
import { clearRouteHighlight } from './routeHighlightAdapter'

/** 当前被热力着色的单元 id——清除时据此还原基础样式。 */
const painted = new Set<string>()

function cellKindOf(g: Graph, id: string): CellKind | undefined {
  const c = g.getCellById(id)
  if (!c) return undefined
  return c.isEdge() ? 'edge' : c.isNode() ? 'node' : undefined
}

/**
 * fault-stats → 画布同步（幂等）：新补丁逐键写入；上一轮着色而本轮
 * 不再命中的单元恢复基础样式。stats 为空/零历史即全量还原。
 */
export function syncHeatmap(g: Graph, stats: FaultStatsPayload | null | undefined): void {
  const patches = applyHeatmap((id) => cellKindOf(g, id), stats)
  for (const id of painted) {
    if (!patches[id]) clearRouteHighlight(g, [id])
  }
  painted.clear()
  for (const [id, patch] of Object.entries(patches)) {
    const cell = g.getCellById(id)
    if (!cell) continue
    for (const k of Object.keys(patch)) cell.attr(k, patch[k])
    painted.add(id)
  }
}

/** 关闭热力图：还原全部已着色单元的基础样式（幂等）。 */
export function clearHeatmap(g: Graph): void {
  if (painted.size > 0) clearRouteHighlight(g, [...painted])
  painted.clear()
}
