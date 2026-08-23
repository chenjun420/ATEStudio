/**
 * 故障修复建议纯函数（T33，v41-gap-analysis #33，设计文档 §8.3.7 故障定位视图）。
 *
 * 语义（§8.3.7 + 计划 #33）：
 *   - 前端绝不重算定位：卡片只装饰后端 FaultLocalizer 已产出的结果
 *     （runtime/fault_localizer.py::FaultLocation.as_dict，经 SSE fault 事件
 *     → stores/topologyRuntime.ts::faults 到达）。
 *   - 修复建议优先级：后端 suggestion（已产出）→ 本地故障类型映射表
 *     （§8.3.6 策略）→ 通用兜底文案。
 *   - 每条故障的 location.suspect_links 展开为逐链路卡片，
 *     按严重度降序（critical > error > warning > 未知）排列。
 *
 * 所有函数均为纯函数（不修改入参、零 X6/DOM 依赖），
 * 模式对齐 utils/routeHighlight.ts / utils/routes.ts。
 */

/** 与 shared/fixture_topology.py::Severity 对齐的严重度取值。 */
export type FaultSeverity = 'critical' | 'error' | 'warning'

/**
 * 疑似故障输入（stores/topologyRuntime.ts::FaultEntry 的最小结构）。
 * location 为 unknown：SSE 载荷形状由后端决定，防御式解析（见 parseSuspectLinks）。
 */
export interface SuspectFault {
  type: string
  severity: FaultSeverity | string
  message: string
  suggestion?: string | null
  confidence?: number
  location?: unknown
}

/** 单张疑似故障卡片（组件渲染单元）。 */
export interface SuspectCard {
  /** v-for key：故障序号 + 链路 id，保证唯一。 */
  key: string
  /** 疑似链路 id；无定位信息时为空串（卡片不可点击）。 */
  linkId: string
  faultType: string
  severity: string
  message: string
  suggestion: string
  confidence?: number
}

/** 严重度 → 排序权重（高>中>低；未知类型最低，不炸排序）。 */
export function severityRank(severity: string): number {
  switch (severity) {
    case 'critical':
      return 3
    case 'error':
      return 2
    case 'warning':
      return 1
    default:
      return 0
  }
}

/** 严重度降序比较器（计划验收：severity order high>medium>low）。 */
export function compareBySeverityDesc(
  a: Pick<SuspectCard, 'severity'>,
  b: Pick<SuspectCard, 'severity'>,
): number {
  return severityRank(b.severity) - severityRank(a.severity)
}

/**
 * 故障类型 → 修复建议映射（§8.3.6/§8.3.7 定位策略）。
 * 键覆盖 shared/fixture_topology.py::FaultType 全部枚举值；
 * contact_resistance 为计划要求的防御性补充（后端当前未枚举）。
 */
export const FAULT_TYPE_SUGGESTIONS: Readonly<Record<string, string>> = {
  open_circuit: '检查连接器与触点是否松动氧化，重新插拔接线并确认继电器闭合',
  short_circuit: '检查线缆绝缘是否破损，确认相邻走线无搭接短路',
  contact_resistance: '清洁并重新插拔触点，测量接触电阻确认氧化程度',
  relay_fault: '反复通断继电器排除卡滞，检查线圈驱动与控制信号',
  measurement_out_of_range: '核对接线极性与仪器量程设置，校准后复测',
  over_voltage: '检查电源设定值与 DUT 过压保护，降低输出后复测',
  over_current: '检查负载是否短路或过载，确认限流设定后复测',
  communication: '检查通信线缆与地址配置，必要时重启仪器',
}

/** 类型未知时的通用兜底建议。 */
export const GENERIC_SUGGESTION_FALLBACK =
  '按定位策略逐段排查路径链路与继电器闭合状态，必要时重新校准仪器'

/** 取故障类型的修复建议；未知类型返回通用兜底（永不返回空串）。 */
export function suggestionForFaultType(type: string): string {
  return FAULT_TYPE_SUGGESTIONS[type] ?? GENERIC_SUGGESTION_FALLBACK
}

/** 防御式解析 location.suspect_links（SSE 载荷形状不可信）。 */
function parseSuspectLinks(location: unknown): string[] {
  if (location == null || typeof location !== 'object') return []
  const links = (location as { suspect_links?: unknown }).suspect_links
  if (!Array.isArray(links)) return []
  return links.filter((l): l is string => typeof l === 'string')
}

function makeCard(fault: SuspectFault, linkId: string, key: string): SuspectCard {
  const own = typeof fault.suggestion === 'string' ? fault.suggestion.trim() : ''
  const card: SuspectCard = {
    key,
    linkId,
    faultType: fault.type,
    severity: fault.severity,
    message: fault.message,
    // 后端建议优先（不重算定位）；缺失时本地映射兜底
    suggestion: own || suggestionForFaultType(fault.type),
  }
  if (typeof fault.confidence === 'number' && Number.isFinite(fault.confidence)) {
    card.confidence = fault.confidence
  }
  return card
}

/**
 * 由 store 的 faults 构建疑似卡片列表：
 * 每条故障 × 每个 suspect_link 展开为一张卡（无定位信息则单卡、linkId 空），
 * 按严重度降序排序。纯函数——不改入参。
 */
export function buildSuspectCards(faults: readonly SuspectFault[]): SuspectCard[] {
  const cards: SuspectCard[] = []
  faults.forEach((fault, fi) => {
    const links = parseSuspectLinks(fault.location)
    if (links.length === 0) {
      cards.push(makeCard(fault, '', String(fi)))
      return
    }
    for (const linkId of links) {
      cards.push(makeCard(fault, linkId, `${fi}:${linkId}`))
    }
  })
  return cards.sort(compareBySeverityDesc)
}
