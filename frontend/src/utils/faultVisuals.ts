/**
 * 每类故障的画布视觉词汇（T34，v41-gap-analysis #34，设计文档 §8.3.7）。
 *
 * 语义（§8.3.7 + 计划 #34）：
 *   - 故障类型 → 独立样式元组 {stroke, strokeWidth, strokeDasharray, opacity,
 *     animation?, marker?}；任意两种类型（含兜底）不共享完整元组，
 *     操作员一眼可分辨故障种类。
 *   - 键覆盖 shared/fixture_topology.py::FaultType 全部 7 个枚举值，
 *     另含注入菜单防御类型 contact_resistance / noise（对齐 T33 先例）。
 *   - 未知类型 → GENERIC_FAULT_FALLBACK 通用红（#F56C6C，T30 视觉延续）。
 *   - 动效 jsdom 安全：animation/marker 仅作为枚举字段存在，经 adapter
 *     落成惰性 class attr（CSS 关键帧由视图 <style> 提供，测试只断言
 *     attr 值，绝不依赖动画真正运行）；无 rAF 循环、无运行时样式注入。
 *
 * 所有函数均为纯函数（不修改入参、零 X6/DOM 依赖），
 * 模式对齐 utils/routeHighlight.ts / utils/faultSuggestions.ts。
 */

/** 动效标记——adapter 映射为 `fault-anim-<name>` class attr。 */
export type FaultAnimation = 'flicker' | 'pulse' | 'flash'

/** 非线条标记——adapter 映射为 `fault-marker-<name>` class attr。 */
export type FaultMarker = 'cross' | 'badge' | 'ring'

export interface FaultVisualStyle {
  stroke: string
  strokeWidth: number
  strokeDasharray: string
  opacity: number
  animation?: FaultAnimation
  marker?: FaultMarker
}

/**
 * 类型未知时的通用兜底：故障红实线常规宽（T30 uniform-red 的延续，
 * 计划 QA failure 场景「unknown type falls back to default red」）。
 */
export const GENERIC_FAULT_FALLBACK: FaultVisualStyle = {
  stroke: '#F56C6C',
  strokeWidth: 2,
  strokeDasharray: '',
  opacity: 1,
}

/**
 * 故障类型 → 视觉样式映射（§8.3.7 词汇表）。
 * 色板取自既有设计语言：#F56C6C 危险红 / #E6A23C 警告橙黄 /
 * #909399 信息灰 / #9B59B6 rf 紫（LINK_STYLES 同源），不引入新色。
 */
export const FAULT_VISUAL_STYLES: Readonly<Record<string, FaultVisualStyle>> = {
  // 断路：红色虚线（线路"断开"隐喻）
  open_circuit: { stroke: '#F56C6C', strokeWidth: 2, strokeDasharray: '8 4', opacity: 1 },
  // 短路：红色加粗实线（过载冲击感）
  short_circuit: { stroke: '#F56C6C', strokeWidth: 4, strokeDasharray: '', opacity: 1 },
  // 接触电阻：警告橙虚线（劣化而非断绝）
  contact_resistance: { stroke: '#E6A23C', strokeWidth: 2, strokeDasharray: '4 3', opacity: 1 },
  // 噪声：橙色细碎虚线 + 闪烁动效（信号抖动隐喻）
  noise: {
    stroke: '#E6A23C',
    strokeWidth: 2,
    strokeDasharray: '2 4',
    opacity: 0.85,
    animation: 'flicker',
  },
  // 过压/过流：闪烁节点环（计划 §34 "flashing node ring"；色分警告/危险）
  over_voltage: {
    stroke: '#E6A23C',
    strokeWidth: 3,
    strokeDasharray: '',
    opacity: 1,
    animation: 'flash',
    marker: 'ring',
  },
  over_current: {
    stroke: '#F56C6C',
    strokeWidth: 3,
    strokeDasharray: '',
    opacity: 1,
    animation: 'flash',
    marker: 'ring',
  },
  // 通信故障：灰色虚线 + 仪器徽标（链路本身完好，问题在对话）
  communication: {
    stroke: '#909399',
    strokeWidth: 2,
    strokeDasharray: '6 4',
    opacity: 1,
    marker: 'badge',
  },
  // 量程超限：橙黄加粗 + 黄色脉冲动效（halo/pulse）
  measurement_out_of_range: {
    stroke: '#E6A23C',
    strokeWidth: 3,
    strokeDasharray: '',
    opacity: 0.9,
    animation: 'pulse',
  },
  // 继电器故障：rf 紫描边 + 十字标记（继电器触点失效）
  relay_fault: {
    stroke: '#9B59B6',
    strokeWidth: 3,
    strokeDasharray: '',
    opacity: 1,
    marker: 'cross',
  },
}

/** 取故障类型的视觉样式；未知类型返回通用兜底（永不返回 undefined）。 */
export function styleForFaultType(type: string): FaultVisualStyle {
  return FAULT_VISUAL_STYLES[type] ?? GENERIC_FAULT_FALLBACK
}

/** 故障输入（stores/topologyRuntime.ts::FaultEntry 的最小结构）。 */
export interface FaultVisualInput {
  type: string
  location?: unknown
}

/** 画布单元类别（纯层用自有判别，零 X6 类型依赖）。 */
export type CellKind = 'edge' | 'node'

/** 扁平 X6 attr 补丁，如 { 'line/stroke': '#F56C6C' }——adapter 逐键 cell.attr(k, v)。 */
export type FaultAttrPatch = Record<string, string | number>

/**
 * 样式 → 扁平 attr 补丁。边写 line/*（含 dasharray），节点写 body/*
 * （无虚线概念）；animation/marker 合成为惰性 class attr。
 */
export function styleAttrsFor(type: string, kind: CellKind = 'edge'): FaultAttrPatch {
  const s = styleForFaultType(type)
  const prefix = kind === 'node' ? 'body' : 'line'
  const attrs: FaultAttrPatch = {
    [`${prefix}/stroke`]: s.stroke,
    [`${prefix}/strokeWidth`]: s.strokeWidth,
    [`${prefix}/opacity`]: s.opacity,
  }
  if (kind === 'edge') attrs['line/strokeDasharray'] = s.strokeDasharray
  const classes: string[] = []
  if (s.animation) classes.push(`fault-anim-${s.animation}`)
  if (s.marker) classes.push(`fault-marker-${s.marker}`)
  if (classes.length > 0) attrs[`${prefix}/class`] = classes.join(' ')
  return attrs
}

/** 防御式解析 location.suspect_links（SSE 载荷形状不可信，对齐 T33）。 */
export function suspectLinksOf(fault: FaultVisualInput): string[] {
  if (fault == null || typeof fault !== 'object') return []
  const links = (fault.location as { suspect_links?: unknown } | null | undefined)?.suspect_links
  if (!Array.isArray(links)) return []
  return links.filter((l): l is string => typeof l === 'string')
}

/**
 * 由活动故障集计算每单元 attr 补丁。
 *
 * @param cellKindOf 单元判别回调（adapter 注入 g.getCellById 判别；
 *   画布上不存在的 id 返回 undefined → 静默跳过）。
 * @param faults SSE 到达的活动故障列表。
 * @returns cellId → 补丁。同一链路被多条故障命中时后写覆盖先写
 *   （确定性：取遍历序最后一条）。
 */
export function applyFaultVisuals(
  cellKindOf: (id: string) => CellKind | undefined,
  faults: readonly FaultVisualInput[],
): Record<string, FaultAttrPatch> {
  const patches: Record<string, FaultAttrPatch> = {}
  for (const fault of faults ?? []) {
    for (const id of suspectLinksOf(fault)) {
      const kind = cellKindOf(id)
      if (!kind) continue
      patches[id] = styleAttrsFor(fault.type, kind)
    }
  }
  return patches
}
