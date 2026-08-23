/**
 * 信号路由编辑纯函数（T28，设计文档 §8.3.2 Route 实体）。
 *
 * 数据形状与 src/shared/fixture_topology.py::Route 对齐：
 *   { id, name, links: list[str], relays: list[str], active: bool,
 *     associated_step: str | None }
 *
 * 语义（§8.3 路由 = 矩阵开关的一条信号路径）：
 *   - links  组成该路径的链路 ID 列表（激活时在画布上高亮）
 *   - relays 该路径需要闭合的继电器 ID 列表
 *   - active 激活标志——按路由独立翻转（不自动激活其他路由，
 *     加载拓扑时也绝不自动置位）
 *   - associated_step 绑定的测试步骤 ID；非空即视为"被步骤引用"，
 *     删除前必须经用户确认
 *
 * 所有变换均为纯函数（返回新数组，不修改入参），便于测试与
 * Vue 响应式回写（父视图将结果整体赋回 topology_data.routes）。
 */

/**
 * 与后端 Route 模型对齐的最小结构（frontend/src/api/fixtures.ts::Route 的
 * 结构超集，使 Route[] 可直接作为 RouteLike[] 使用）。
 */
export interface RouteLike {
  id: string
  name?: string
  links?: string[]
  relays?: string[]
  active?: boolean
  associated_step?: string | null
}

/** 创建后端合法的路由实体（active 默认 false——绝不自动激活）。 */
export function createRoute(index: number, name = ''): RouteLike {
  return {
    id: `ROUTE_${index + 1}`,
    name: name || `路由 ${index + 1}`,
    links: [],
    relays: [],
    active: false,
    associated_step: null,
  }
}

/** 在现有 id 集合之外分配 ROUTE_{n}：优先复用最小空位。 */
export function uniqueRouteId(routes: readonly RouteLike[]): string {
  const ids = new Set(routes.map((r) => r.id))
  let i = 1
  while (ids.has(`ROUTE_${i}`)) i++
  return `ROUTE_${i}`
}

function indexOfId(routes: readonly RouteLike[], id: string): number {
  const idx = routes.findIndex((r) => r.id === id)
  return idx >= 0 ? idx : -1
}

/** 追加路由（不可变）。目标不存在时原样返回新数组副本。 */
export function applyCreate(routes: readonly RouteLike[], route: RouteLike): RouteLike[] {
  return [...routes, { ...route }]
}

/** 重命名（不可变）。 */
export function applyRename(routes: readonly RouteLike[], id: string, name: string): RouteLike[] {
  const idx = indexOfId(routes, id)
  if (idx < 0) return [...routes]
  const next = [...routes]
  next[idx] = { ...next[idx], name }
  return next
}

/** 删除路由（不可变）。 */
export function applyDelete(routes: readonly RouteLike[], id: string): RouteLike[] {
  return routes.filter((r) => r.id !== id)
}

/** 整体替换组成链路列表（不可变）。 */
export function applyAssignLinks(
  routes: readonly RouteLike[],
  id: string,
  linkIds: readonly string[],
): RouteLike[] {
  const idx = indexOfId(routes, id)
  if (idx < 0) return [...routes]
  const next = [...routes]
  next[idx] = { ...next[idx], links: [...linkIds] }
  return next
}

/** 整体替换闭合继电器列表（不可变）。 */
export function applyAssignRelays(
  routes: readonly RouteLike[],
  id: string,
  relayIds: readonly string[],
): RouteLike[] {
  const idx = indexOfId(routes, id)
  if (idx < 0) return [...routes]
  const next = [...routes]
  next[idx] = { ...next[idx], relays: [...relayIds] }
  return next
}

/**
 * 设置激活标志（不可变）。仅翻转目标路由自身的 active 位；
 * 兄弟路由不受影响，也不存在任何隐式联动。
 */
export function applySetActive(
  routes: readonly RouteLike[],
  id: string,
  active: boolean,
): RouteLike[] {
  const idx = indexOfId(routes, id)
  if (idx < 0) return [...routes]
  const next = [...routes]
  next[idx] = { ...next[idx], active }
  return next
}

/** 规范化步骤绑定输入：去空白；空白串 → null（解除绑定）。 */
export function normalizeAssociatedStep(value: string): string | null {
  const trimmed = value.trim()
  return trimmed.length > 0 ? trimmed : null
}

/** 写入 associated_step 绑定（不可变）。 */
export function applyAssociatedStep(
  routes: readonly RouteLike[],
  id: string,
  value: string,
): RouteLike[] {
  const idx = indexOfId(routes, id)
  if (idx < 0) return [...routes]
  const next = [...routes]
  next[idx] = { ...next[idx], associated_step: normalizeAssociatedStep(value) }
  return next
}

/** 路由是否被测试步骤引用（associated_step 非空字符串）→ 删除需确认。 */
export function routeReferencedByStep(route: RouteLike): boolean {
  return typeof route.associated_step === 'string' && route.associated_step.length > 0
}
