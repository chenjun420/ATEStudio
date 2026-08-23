/**
 * T34（v41-gap-analysis #34）— per-fault-type visual vocabulary 纯函数测试。
 *
 * 全部 jsdom 安全：只断言纯数据与 attr 补丁，绝不触碰 CSS 动画/X6/DOM。
 * 验收映射（计划 §34）：
 *   - 每种类型 → 独立样式断言（T1/T2/T4/T5）
 *   - 未知类型回退默认红（T3/T8）
 *   - 多链路多故障互相独立（T7）；清除由 adapter 层负责（见 faultVisualsAdapter）
 */
import { describe, expect, it } from 'vitest'

import {
  applyFaultVisuals,
  styleAttrsFor,
  styleForFaultType,
  suspectLinksOf,
  FAULT_VISUAL_STYLES,
  GENERIC_FAULT_FALLBACK,
  type FaultVisualInput,
  type FaultVisualStyle,
} from '../faultVisuals'

/** shared/fixture_topology.py::FaultType 全部枚举值。 */
const BACKEND_FAULT_TYPES = [
  'open_circuit',
  'short_circuit',
  'over_voltage',
  'over_current',
  'communication',
  'measurement_out_of_range',
  'relay_fault',
] as const

/** 前端注入菜单的防御性补充（api/fixtures.ts::LinkFaultKind，对齐 T33 先例）。 */
const INJECTION_KINDS = ['contact_resistance', 'noise'] as const

/** 完整样式元组——distinctness 判定基准。 */
function tupleOf(s: FaultVisualStyle): unknown[] {
  return [s.stroke, s.strokeWidth, s.strokeDasharray, s.opacity, s.animation ?? '', s.marker ?? '']
}

/** 构造 cellKindOf：仅列出的 id 视为边。 */
const edgesOf =
  (...ids: string[]) =>
  (id: string): 'edge' | undefined =>
    ids.includes(id) ? 'edge' : undefined

describe('FAULT_VISUAL_STYLES 覆盖与区分度', () => {
  it('T1 后端 7 种 FaultType + 注入菜单 2 种防御类型全部有专属条目', () => {
    for (const t of [...BACKEND_FAULT_TYPES, ...INJECTION_KINDS]) {
      expect(FAULT_VISUAL_STYLES[t], `missing style for ${t}`).toBeTruthy()
      // 有专属条目 ≠ 兜底样式
      expect(tupleOf(FAULT_VISUAL_STYLES[t])).not.toEqual(tupleOf(GENERIC_FAULT_FALLBACK))
    }
  })

  it('T2 两两互异：任意两种类型不共享完整样式元组（含兜底共 10 项）', () => {
    const entries: Array<[string, FaultVisualStyle]> = [
      ...Object.entries(FAULT_VISUAL_STYLES),
      ['<fallback>', GENERIC_FAULT_FALLBACK],
    ]
    for (let i = 0; i < entries.length; i++) {
      for (let j = i + 1; j < entries.length; j++) {
        expect(tupleOf(entries[i][1]), `${entries[i][0]} vs ${entries[j][0]}`).not.toEqual(
          tupleOf(entries[j][1]),
        )
      }
    }
  })

  it('T3 未知/空类型回退通用红（#F56C6C 实线常规宽）', () => {
    for (const t of ['alien_type', '', 'OPEN_CIRCUIT']) {
      expect(styleForFaultType(t)).toEqual(GENERIC_FAULT_FALLBACK)
    }
    expect(GENERIC_FAULT_FALLBACK.stroke).toBe('#F56C6C')
    expect(GENERIC_FAULT_FALLBACK.strokeDasharray).toBe('')
  })

  it('T4 open_circuit=红色虚线（断路线），short_circuit=红色加粗实线——同色系不同形', () => {
    const open = styleForFaultType('open_circuit')
    const short = styleForFaultType('short_circuit')
    expect(open.stroke).toBe('#F56C6C')
    expect(open.strokeDasharray).not.toBe('')
    expect(short.stroke).toBe('#F56C6C')
    expect(short.strokeDasharray).toBe('')
    expect(short.strokeWidth).toBeGreaterThan(open.strokeWidth)
  })

  it('T5 动效标记：noise=flicker、measurement_out_of_range=pulse、over V/I=flash（jsdom 安全：仅枚举字段）', () => {
    expect(styleForFaultType('noise').animation).toBe('flicker')
    expect(styleForFaultType('measurement_out_of_range').animation).toBe('pulse')
    expect(styleForFaultType('over_voltage').animation).toBe('flash')
    expect(styleForFaultType('over_current').animation).toBe('flash')
    // 动效经类名 attr 下发，而非运行时注入 CSS —— jsdom 断言安全
    expect(styleAttrsFor('noise')['line/class']).toContain('fault-anim-flicker')
    expect(styleAttrsFor('measurement_out_of_range')['line/class']).toContain('fault-anim-pulse')
  })
})

describe('applyFaultVisuals 补丁变换', () => {
  it('T6 单故障+单链路 → 该边的扁平 line/* attr 补丁', () => {
    const faults: FaultVisualInput[] = [
      { type: 'open_circuit', location: { suspect_links: ['L1'] } },
    ]
    const patches = applyFaultVisuals(edgesOf('L1'), faults)
    expect(Object.keys(patches)).toEqual(['L1'])
    expect(patches.L1['line/stroke']).toBe('#F56C6C')
    expect(patches.L1['line/strokeWidth']).toBe(2)
    expect(patches.L1['line/strokeDasharray']).toBe('8 4')
    expect(patches.L1['line/opacity']).toBe(1)
  })

  it('T7 不同链路上的多条故障互相独立（各自类型的补丁互不串扰）', () => {
    const faults: FaultVisualInput[] = [
      { type: 'open_circuit', location: { suspect_links: ['L1'] } },
      { type: 'short_circuit', location: { suspect_links: ['L2'] } },
      { type: 'communication', location: { suspect_links: ['L3'] } },
    ]
    const patches = applyFaultVisuals(edgesOf('L1', 'L2', 'L3'), faults)
    expect(Object.keys(patches).sort()).toEqual(['L1', 'L2', 'L3'])
    expect(patches.L1['line/strokeDasharray']).toBe('8 4') // open: 红虚线
    expect(patches.L2['line/strokeWidth']).toBe(4) // short: 加粗实线
    expect(patches.L3['line/stroke']).toBe('#909399') // comm: 灰
    expect(patches.L3['line/class']).toContain('fault-marker-badge')
  })

  it('T8 未知类型故障 → 兜底红补丁（QA failure 场景）', () => {
    const patches = applyFaultVisuals(edgesOf('L9'), [
      { type: 'quantum_entanglement_fault', location: { suspect_links: ['L9'] } },
    ])
    expect(patches.L9['line/stroke']).toBe('#F56C6C')
    expect(patches.L9['line/strokeWidth']).toBe(GENERIC_FAULT_FALLBACK.strokeWidth)
    expect(patches.L9['line/class']).toBeUndefined()
  })

  it('T9 防御式解析：畸形 location 不炸不产出；合法部分照常解析', () => {
    const faults: FaultVisualInput[] = [
      { type: 'open_circuit', location: null },
      { type: 'open_circuit', location: 'not-an-object' },
      { type: 'open_circuit', location: { suspect_links: 'L1' } },
      { type: 'open_circuit', location: { suspect_links: [42, null, 'L1'] } },
      { type: 'short_circuit' },
    ]
    const patches = applyFaultVisuals(edgesOf('L1'), faults)
    expect(Object.keys(patches)).toEqual(['L1'])
    expect(patches.L1['line/stroke']).toBe('#F56C6C')
  })

  it('T10 画布上不存在的链路被静默跳过（对齐 routeHighlightAdapter 语义）', () => {
    const patches = applyFaultVisuals(edgesOf('L1'), [
      { type: 'open_circuit', location: { suspect_links: ['L1', 'GHOST'] } },
    ])
    expect(patches.GHOST).toBeUndefined()
    expect(patches.L1).toBeTruthy()
  })

  it('T11 节点目标 → body/* 补丁 + 标记类（relay_fault 十字 / communication 仪器徽标）', () => {
    const kinds = (id: string): 'node' | undefined => (id === 'R1' || id === 'INS1' ? 'node' : undefined)
    const patches = applyFaultVisuals(kinds, [
      { type: 'relay_fault', location: { suspect_links: ['R1'] } },
      { type: 'communication', location: { suspect_links: ['INS1'] } },
    ])
    expect(patches.R1['body/stroke']).toBe('#9B59B6')
    expect(patches.R1['body/class']).toContain('fault-marker-cross')
    expect(patches.R1['line/stroke']).toBeUndefined() // 节点不打 line/*
    expect(patches.INS1['body/class']).toContain('fault-marker-badge')
  })

  it('T12 纯函数：不改入参（faults 数组与元素保持深相等）；suspectLinksOf 只读', () => {
    const faults: FaultVisualInput[] = [
      { type: 'noise', location: { suspect_links: ['L1', 'L2'] } },
    ]
    const snapshot = JSON.parse(JSON.stringify(faults))
    applyFaultVisuals(edgesOf('L1', 'L2'), faults)
    expect(faults).toEqual(snapshot)
    expect(suspectLinksOf(faults[0])).toEqual(['L1', 'L2'])
    expect(suspectLinksOf({ type: 'x' })).toEqual([])
  })
})
