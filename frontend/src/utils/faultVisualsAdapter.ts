/**
 * T34（v41-gap-analysis #34）— thin X6 adapter for per-fault-type visuals.
 *
 * 纯层 utils/faultVisuals.ts 决定「哪种故障长什么样」；本模块只负责把
 * 补丁写进 X6（逐键 cell.attr(k, v)）与故障清除后的基础样式还原。
 * 还原复用 routeHighlightAdapter.clearRouteHighlight——单一恢复事实来源。
 *
 * jsdom 安全：动效仅以惰性 class attr 落盘（fault-anim-*），关键帧由视图
 * <style> 提供；此处无 rAF、无运行时样式注入，测试只断言 attr。
 */
import type { Graph } from '@antv/x6'

import { applyFaultVisuals, styleAttrsFor, type CellKind, type FaultVisualInput } from './faultVisuals'
import { clearRouteHighlight } from './routeHighlightAdapter'

/** 当前被故障样式着色的单元 id——清除时据此还原基础样式。 */
const painted = new Set<string>()

function cellKindOf(g: Graph, id: string): CellKind | undefined {
  const c = g.getCellById(id)
  if (!c) return undefined
  return c.isEdge() ? 'edge' : c.isNode() ? 'node' : undefined
}

/**
 * SSE 故障集 → 画布同步（幂等）：新补丁逐键写入；上一轮着色而本轮
 * 不再故障的单元恢复基础样式。faults 为空即全量还原。
 */
export function syncFaultVisuals(g: Graph, faults: readonly FaultVisualInput[]): void {
  const patches = applyFaultVisuals((id) => cellKindOf(g, id), faults)
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

/** 单链路按故障类型上色（注入即时确认 / 疑似卡片点击）。 */
export function paintFaultLink(g: Graph, linkId: string, faultType: string): void {
  const kind = cellKindOf(g, linkId)
  if (!kind) return
  const cell = g.getCellById(linkId)
  for (const [k, v] of Object.entries(styleAttrsFor(faultType, kind))) cell!.attr(k, v)
  painted.add(linkId)
}
