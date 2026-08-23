/**
 * 路由高亮纯函数（T32，v41-gap-analysis #32，设计文档 §8.3 路由激活可视化）。
 *
 * 语义（§8.3）：
 *   - 选中一条路由后，其组成链路（Route.links）与需闭合继电器
 *     （Route.relays）以强调色描边；其余链路/继电器压暗。
 *   - 故障样式优先（fault wins）：故障链路既不强调也不压暗，
 *     运行时 fault 红样式原样保留。
 *   - 视图只读：本模块仅计算集合，绝不修改拓扑模型；
 *     X6 写入见 utils/routeHighlightAdapter.ts（thin adapter）。
 *
 * 所有函数均为纯函数（不修改入参、零 X6/DOM 依赖），
 * 模式对齐 utils/routes.ts / utils/topologySimulation.ts。
 */
import type { RouteLike } from './routes'

/** 与 api/fixtures.ts::Link 对齐的最小结构（仅高亮所需字段）。 */
export interface HighlightLinkInput {
  id: string
  status?: string
  fault_info?: unknown
}

/** 与 api/fixtures.ts::Relay 对齐的最小结构。 */
export interface HighlightRelayInput {
  id: string
}

export interface RouteHighlightInput {
  routes: readonly RouteLike[]
  links: readonly HighlightLinkInput[]
  relays: readonly HighlightRelayInput[]
  selectedRouteId: string | null | undefined
}

/** 高亮计算结果——三个互斥写入目标（adapter 据此写 attr）。 */
export interface RouteHighlightResult {
  /** 强调描边的链路 id（选中路由路径上的非故障链路）。 */
  accentedLinkIds: ReadonlySet<string>
  /** 强调的继电器触点 id（选中路由需闭合的继电器）。 */
  accentedRelayContacts: ReadonlySet<string>
  /** 压暗的元素 id（不在路径上的链路 + 继电器；不含故障链路）。 */
  dimmedNodeIds: ReadonlySet<string>
}

const EMPTY: ReadonlySet<string> = new Set()

function noHighlight(): RouteHighlightResult {
  return { accentedLinkIds: EMPTY, accentedRelayContacts: EMPTY, dimmedNodeIds: EMPTY }
}

/** 故障链路判定：status === 'fault' 或携带 fault_info → 样式归运行时故障层管。 */
export function isFaultedLink(link: HighlightLinkInput): boolean {
  return link.status === 'fault' || link.fault_info != null
}

/**
 * 计算高亮集合。无选中/选中不存在 → 全空（no-op）。
 * 仅画布上真实存在的链路/继电器参与集合；悬空引用被忽略。
 */
export function computeRouteHighlight(input: RouteHighlightInput): RouteHighlightResult {
  const { routes, links, relays, selectedRouteId } = input
  if (!selectedRouteId) return noHighlight()
  const route = routes.find((r) => r.id === selectedRouteId)
  if (!route) return noHighlight()

  const routeLinks = new Set(route.links ?? [])
  const routeRelays = new Set(route.relays ?? [])

  const accentedLinkIds = new Set<string>()
  const dimmedLinks = new Set<string>()
  for (const l of links) {
    if (isFaultedLink(l)) continue // fault wins：不强调、不压暗
    if (routeLinks.has(l.id)) accentedLinkIds.add(l.id)
    else dimmedLinks.add(l.id)
  }

  const accentedRelayContacts = new Set<string>()
  const dimmedRelays = new Set<string>()
  for (const r of relays) {
    if (routeRelays.has(r.id)) accentedRelayContacts.add(r.id)
    else dimmedRelays.add(r.id)
  }

  return {
    accentedLinkIds,
    accentedRelayContacts,
    dimmedNodeIds: new Set([...dimmedLinks, ...dimmedRelays]),
  }
}

/**
 * 从 routes 推导"当前选中"的路由 id：恰好一个 active 时返回它；
 * 零个或多个同时激活 → null（多激活语义有歧义，宁可不高亮也不误强调）。
 */
export function pickActiveRouteId(routes: readonly RouteLike[]): string | null {
  const active = routes.filter((r) => r.active === true)
  return active.length === 1 ? (active[0].id ?? null) : null
}
