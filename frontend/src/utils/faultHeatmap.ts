/**
 * 历史故障热力图纯层（T35，v41-gap-analysis #35，设计文档 §8.3）。
 *
 * 语义：
 *   - 后端 GET /fixtures/{id}/fault-stats 返回 {links:{link_id:{count,last_seen}},
 *     generated_at}；本模块把 count 映射为 0-3 热度档位，并产出 X6 attr 补丁。
 *   - 分桶采用固定阈值（确定性优先，避免小样本四分位数抖动）：
 *       0 → 0（无着色，均匀底色）；1–2 → 1；3–5 → 2；≥6 → 3。
 *   - 色阶沿用既有设计语言（routeHighlight/faultVisuals 同源色板）：
 *       绿 #67C23A → 黄橙 #E6A23C → 红 #F56C6C，透明度随档位递增。
 *   - 故障实时样式（T34）优先于热力着色：视图侧 SSE fault 重绘会覆盖
 *     line/stroke；热力层只在 toggle 时写入，不参与运行时循环。
 *
 * 所有函数均为纯函数（不修改入参、零 X6/DOM 依赖），
 * 模式对齐 utils/routeHighlight.ts / utils/faultVisuals.ts。
 */

/** 单链路历史故障统计条目（后端 fault-stats 载荷）。 */
export interface FaultStatEntry {
  count: number
  last_seen?: string | null
}

/** GET /fixtures/{id}/fault-stats 响应。 */
export interface FaultStatsPayload {
  links: Record<string, FaultStatEntry>
  generated_at: string
}

/** 热度档位：0=无历史（不着色），1/2/3 严重度递增。 */
export type HeatLevel = 0 | 1 | 2 | 3

/** 档位阈值上界（含）：count ≤ 阈值归入该档。 */
const LEVEL_THRESHOLDS = [0, 2, 5] as const

/**
 * count → 热度档位。
 * 固定阈值分桶：0→0；1–2→1；3–5→2；≥6→3；
 * 负数/NaN/非有限值防御性归 0（载荷形状不可信，对齐 T33 先例）。
 */
export function heatLevelOf(count: number): HeatLevel {
  if (!Number.isFinite(count) || count <= 0) return 0
  if (count <= LEVEL_THRESHOLDS[1]) return 1
  if (count <= LEVEL_THRESHOLDS[2]) return 2
  return 3
}

/** 档位 → 热力色（绿→黄橙→红；#67C23A/#E6A23C/#F56C6C 与既有色板同源）。 */
export const HEAT_COLORS: Readonly<Record<Exclude<HeatLevel, 0>, string>> = {
  1: '#67C23A',
  2: '#E6A23C',
  3: '#F56C6C',
}

/** 档位 → 不透明度（随严重度递增，单调不减）。 */
export const HEAT_OPACITIES: Readonly<Record<Exclude<HeatLevel, 0>, number>> = {
  1: 0.5,
  2: 0.75,
  3: 1,
}

/** 图例条目（视图侧渲染色阶图例；minCount 为进入该档的最小次数）。 */
export interface HeatLegendEntry {
  level: Exclude<HeatLevel, 0>
  color: string
  opacity: number
  label: string
  minCount: number
}

/** 色阶图例数据（纯数据，jsdom 安全渲染）。 */
export const HEAT_LEGEND: readonly HeatLegendEntry[] = [
  { level: 1, color: HEAT_COLORS[1], opacity: HEAT_OPACITIES[1], label: '低（1–2 次）', minCount: 1 },
  { level: 2, color: HEAT_COLORS[2], opacity: HEAT_OPACITIES[2], label: '中（3–5 次）', minCount: 3 },
  { level: 3, color: HEAT_COLORS[3], opacity: HEAT_OPACITIES[3], label: '高（≥6 次）', minCount: 6 },
]

/** 画布单元类别（纯层用自有判别，零 X6 类型依赖）。 */
export type CellKind = 'edge' | 'node'

/** 扁平 X6 attr 补丁——adapter 逐键 cell.attr(k, v)。 */
export type HeatAttrPatch = Record<string, string | number>

/**
 * 档位 → 扁平 attr 补丁。边写 line/*，节点写 body/*；
 * level 0 返回空补丁（调用方跳过，保持均匀底色）。
 */
export function heatAttrsFor(level: HeatLevel, kind: CellKind = 'edge'): HeatAttrPatch {
  if (level === 0) return {}
  const prefix = kind === 'node' ? 'body' : 'line'
  return {
    [`${prefix}/stroke`]: HEAT_COLORS[level],
    [`${prefix}/opacity`]: HEAT_OPACITIES[level],
  }
}

/**
 * 由 fault-stats 计算每单元热力补丁。
 *
 * @param cellKindOf 单元判别回调（adapter 注入 g.getCellById 判别；
 *   画布上不存在的 id 返回 undefined → 静默跳过）。
 * @param stats 后端聚合结果；links 为空（零历史）→ 返回空表（no-op）。
 * @returns cellId → 补丁；level 0 的链路不产生条目。
 */
export function applyHeatmap(
  cellKindOf: (id: string) => CellKind | undefined,
  stats: FaultStatsPayload | null | undefined,
): Record<string, HeatAttrPatch> {
  const patches: Record<string, HeatAttrPatch> = {}
  const links = stats?.links ?? {}
  for (const [id, entry] of Object.entries(links)) {
    const kind = cellKindOf(id)
    if (!kind) continue
    const patch = heatAttrsFor(heatLevelOf(entry?.count ?? 0), kind)
    if (Object.keys(patch).length === 0) continue
    patches[id] = patch
  }
  return patches
}
